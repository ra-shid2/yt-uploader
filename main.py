import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import boto3
import requests
from boto3.s3.transfer import TransferConfig
from dotenv import load_dotenv
from tqdm import tqdm
from yt_dlp import YoutubeDL

load_dotenv()

# ---- config (.env) ----
API_URL = os.environ["API_URL"]
API_TOKEN = os.environ.get("API_TOKEN")                  # optional Bearer token
S3_BUCKET = os.environ["S3_BUCKET"]
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL") or None   # e.g. https://sgp1.digitaloceanspaces.com
S3_PUBLIC_BASE_URL = os.environ.get("S3_PUBLIC_BASE_URL") or None  # CDN / public domain
S3_PREFIX = os.environ.get("S3_PREFIX", "app/production").strip("/")

# Subfolders under S3_PREFIX
FOLDER_VIDEO = "videos"
FOLDER_THUMB = "thumbnails"
FOLDER_AUDIO = "audio"

# ---- JSON field names your API expects ----
FIELD_VIDEO = "video_url"
FIELD_THUMB = "thumbnail_url"
FIELD_AUDIO = "audio_url"

DOWNLOAD_DIR = "downloads"
STATE_FILE = "state.json"
LEGACY_DONE_FILE = "uploaded.txt"

MM_TZ = timezone(timedelta(hours=6, minutes=30))  # +06:30


class DiskFull(Exception):
    pass


class BotCheck(Exception):
    """YouTube is asking us to sign in / prove we're not a bot. Retrying makes it worse."""


def is_bot_check(e: Exception) -> bool:
    s = str(e)
    return "not a bot" in s or "Sign in to confirm" in s


# ======================= state (crash-safe) =======================
# status: pending -> ready (downloaded+converted) -> stored (in S3) -> uploaded (API has URLs)
#         failed = last attempt errored (retried on next run)

def load_state() -> dict:
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    if os.path.exists(LEGACY_DONE_FILE):
        with open(LEGACY_DONE_FILE) as f:
            for line in f:
                vid = line.strip()
                if vid:
                    state.setdefault(vid, {"url": "", "status": "uploaded"})
    return state


def save_state(state: dict) -> None:
    """Atomic write: a crash mid-write can never corrupt state.json."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


# ======================= helpers =======================

def slugify(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:80] or "video"


def read_urls_file(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]


def normalize(u: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", u):
        return f"https://www.youtube.com/watch?v={u}"
    return u


def ytdlp_common(args) -> dict:
    """Options shared by every yt-dlp call: cookies + polite sleeping."""
    opts = {
        "sleep_interval": 5,
        "max_sleep_interval": 15,
        "sleep_interval_requests": 1,
    }
    if args.cookies_from_browser:
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    elif args.cookies:
        opts["cookiefile"] = args.cookies
    return opts


def expand_urls(urls: list[str], args) -> list[tuple[str, str]]:
    """Expand playlists into individual videos -> [(id, url)], de-duplicated."""
    seen, out = set(), []
    opts = {**ytdlp_common(args), "extract_flat": "in_playlist",
            "quiet": True, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        for u in urls:
            try:
                info = ydl.extract_info(normalize(u), download=False)
            except Exception as e:
                if is_bot_check(e):
                    raise BotCheck(str(e)) from e
                print(f"[skip] could not read {u}: {e}")
                continue
            entries = info.get("entries") if info.get("_type") == "playlist" else [info]
            for e in entries or []:
                if not e or not e.get("id") or e["id"] in seen:
                    continue
                seen.add(e["id"])
                out.append((e["id"], e.get("webpage_url") or e.get("url")
                            or f"https://www.youtube.com/watch?v={e['id']}"))
    return out


def with_retries(fn, tries: int, what: str):
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except (KeyboardInterrupt, DiskFull, BotCheck):
            raise
        except Exception as e:
            if is_bot_check(e):
                raise BotCheck(str(e)) from e
            if attempt == tries:
                raise
            wait = min(60, 5 * 2 ** (attempt - 1))
            print(f"[retry] {what} failed ({e}); attempt {attempt}/{tries}, waiting {wait}s")
            time.sleep(wait)


def check_disk(min_free_gb: float) -> None:
    free = shutil.disk_usage(".").free / 1e9
    if free < min_free_gb:
        raise DiskFull(f"only {free:.1f} GB free (need {min_free_gb} GB)")


def cleanup_video_files(vid: str) -> None:
    """Remove everything downloads/<id>* (video, thumb, audio, .part, fragments)."""
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{vid}*")):
        try:
            os.remove(f)
        except OSError:
            pass


# ======================= download =======================

def build_format(max_height: int | None) -> str:
    """Prefer H.264 + AAC (iOS-safe), fall back to anything within the height cap."""
    h = f"[height<={max_height}]" if max_height else ""
    return (
        f"bv*[vcodec^=avc1]{h}+ba[acodec^=mp4a]/"
        f"b[vcodec^=avc1]{h}[acodec^=mp4a]/"
        f"bv*{h}+ba/b{h}"
    )


def download(url: str, max_height: int | None, args) -> tuple[str, str, dict]:
    opts = {
        **ytdlp_common(args),
        "format": build_format(max_height),
        "merge_output_format": "mp4",
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        "writethumbnail": True,
        "noplaylist": True,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}
        ],
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    base = os.path.join(DOWNLOAD_DIR, info["id"])
    video = info.get("requested_downloads", [{}])[0].get("filepath") or base + ".mp4"
    return video, base + ".jpg", info


# ======================= iOS-safe conversion =======================

def probe(path: str, stream: str) -> dict:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", stream,
        "-show_entries", "stream=codec_name,pix_fmt", "-of", "json", path,
    ])
    streams = json.loads(out).get("streams", [])
    return streams[0] if streams else {}


def ensure_ios_compatible(path: str) -> str:
    """MP4 + H.264 (yuv420p) + AAC + faststart. Re-encodes only what's needed."""
    v, a = probe(path, "v:0"), probe(path, "a:0")
    video_ok = v.get("codec_name") == "h264" and v.get("pix_fmt") == "yuv420p"
    audio_ok = a.get("codec_name") == "aac" or not a

    final = os.path.splitext(path)[0] + ".mp4"
    tmp = os.path.splitext(path)[0] + ".ios.mp4"

    v_args = ["-c:v", "copy"] if video_ok else [
        "-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
        "-pix_fmt", "yuv420p", "-crf", "23", "-preset", "medium"]
    a_args = ["-c:a", "copy"] if audio_ok else ["-c:a", "aac", "-b:a", "128k"]
    if not video_ok or not audio_ok:
        print("Re-encoding for iOS compatibility...")

    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, *v_args, *a_args,
                    "-movflags", "+faststart", tmp], check=True)

    if os.path.abspath(path) != os.path.abspath(final):
        os.remove(path)
    os.replace(tmp, final)
    return final


def ensure_thumbnail(video: str, thumb: str) -> None:
    if os.path.exists(thumb):
        return
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", video,
                    "-frames:v", "1", "-q:v", "3", thumb], check=True)


def extract_audio(video: str, out: str) -> str | None:
    """Audio-only .mp3 (128k) for the API's audio_url."""
    if os.path.exists(out):
        return out
    if not probe(video, "a:0"):
        return None
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-vn",
                    "-c:a", "libmp3lame", "-b:a", "128k", out], check=True)
    return out


def video_duration(video: str) -> int:
    """Duration in whole seconds, read from the final file."""
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", video,
    ]).decode().strip()
    return int(round(float(out)))


def thumbnail_color(thumb: str) -> str:
    """Average color of the thumbnail as #rrggbb (ffmpeg scales it to 1x1)."""
    raw = subprocess.check_output([
        "ffmpeg", "-loglevel", "error", "-i", thumb,
        "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ])
    r, g, b = raw[0], raw[1], raw[2]
    return f"#{r:02x}{g:02x}{b:02x}"


# ======================= S3 =======================

def make_s3():
    s3 = boto3.client("s3", region_name=S3_REGION, endpoint_url=S3_ENDPOINT_URL)
    try:
        s3.head_bucket(Bucket=S3_BUCKET)   # fail fast on bad credentials / bucket name
    except Exception as e:
        print(f"[stop] cannot access S3 bucket '{S3_BUCKET}': {e}")
        sys.exit(4)
    return s3


def s3_key(folder: str, filename: str) -> str:
    """<S3_PREFIX>/<folder>/<filename>, e.g. app/production/videos/audio/abc.mp3"""
    return "/".join(p for p in (S3_PREFIX, folder, filename) if p)


def public_url(key: str) -> str:
    if S3_PUBLIC_BASE_URL:
        return f"{S3_PUBLIC_BASE_URL.rstrip('/')}/{quote(key)}"
    if S3_ENDPOINT_URL:
        return f"{S3_ENDPOINT_URL.rstrip('/')}/{S3_BUCKET}/{quote(key)}"
    return f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{quote(key)}"


def s3_upload(s3, path: str, key: str, content_type: str) -> str:
    size = os.path.getsize(path)
    cfg = TransferConfig(multipart_threshold=16 * 1024 * 1024,
                         multipart_chunksize=16 * 1024 * 1024, max_concurrency=4)
    with tqdm(total=size, unit="B", unit_scale=True, desc=f"S3 {os.path.basename(key)}") as bar:
        s3.upload_file(
            path, S3_BUCKET, key, Config=cfg, Callback=bar.update,
            ExtraArgs={"ContentType": content_type,
                       "CacheControl": "public, max-age=31536000",
                       "ACL": "public-read"},
        )
    return public_url(key)


# ======================= API (URLs only) =======================

def register_with_api(entry: dict, args) -> requests.Response:
    meta, urls = entry["meta"], entry["urls"]
    payload = {
        "title": meta["title"],
        "slug": f"{slugify(meta['title'])}-{meta['id'].lower()}",
        "description": meta.get("description") or "",
        FIELD_VIDEO: urls["video"],
        FIELD_THUMB: urls["thumbnail"],
        "is_featured": bool(args.featured),
        "category_name": args.category,
    }
    if urls.get("audio"):
        payload[FIELD_AUDIO] = urls["audio"]
    if meta.get("thumbnail_color"):
        payload["thumbnail_color"] = meta["thumbnail_color"]
    if meta.get("duration") is not None:
        payload["duration"] = meta["duration"]
    if meta.get("published_at"):
        payload["published_at"] = meta["published_at"]

    headers = {"Accept": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"

    def attempt():
        resp = requests.post(API_URL, json=payload, headers=headers,
                             timeout=60, allow_redirects=False)
        if resp.status_code >= 500 or resp.status_code in (408, 429):
            raise RuntimeError(f"server returned {resp.status_code}")
        return resp

    return with_retries(attempt, args.retries, "API call")


# ======================= per-video pipeline =======================

def process(vid: str, entry: dict, state: dict, s3, args) -> None:
    video = os.path.join(DOWNLOAD_DIR, f"{vid}.mp4")
    thumb = os.path.join(DOWNLOAD_DIR, f"{vid}.jpg")
    audio = os.path.join(DOWNLOAD_DIR, f"{vid}.mp3")

    if entry.get("status") != "stored":
        # Stage 1: download + convert (skipped if already done)
        already_ready = (entry.get("status") == "ready" and entry.get("meta")
                         and os.path.exists(video) and os.path.exists(thumb))
        if already_ready:
            print("Resuming: video already downloaded and converted.")
        else:
            check_disk(args.min_free_gb)
            downloaded, _, info = with_retries(
                lambda: download(entry["url"], args.max_height, args), args.retries, "download")
            video = ensure_ios_compatible(downloaded)
            ensure_thumbnail(video, thumb)
            entry["meta"] = {
                "id": info["id"],
                "title": info["title"],
                "description": info.get("description") or "",
                "duration": video_duration(video),
                "thumbnail_color": thumbnail_color(thumb),
                "published_at": datetime.now(MM_TZ).isoformat(timespec="seconds"),
            }
            entry["status"] = "ready"
            save_state(state)

        print(f"Ready: {video} ({os.path.getsize(video) / 1024 / 1024:.1f} MiB)")

        # Stage 2: upload all three assets to their own folders
        urls = {
            "video": with_retries(
                lambda: s3_upload(s3, video, s3_key(FOLDER_VIDEO, f"{vid}.mp4"), "video/mp4"),
                args.retries, "S3 video upload"),
            "thumbnail": with_retries(
                lambda: s3_upload(s3, thumb, s3_key(FOLDER_THUMB, f"{vid}.jpg"), "image/jpeg"),
                args.retries, "S3 thumbnail upload"),
        }
        a = extract_audio(video, audio)
        if a:
            urls["audio"] = with_retries(
                lambda: s3_upload(s3, a, s3_key(FOLDER_AUDIO, f"{vid}.mp3"), "audio/mpeg"),
                args.retries, "S3 audio upload")
        else:
            print("[warn] no audio stream; audio_url will be omitted")
        entry["urls"] = urls
        entry["status"] = "stored"
        save_state(state)
    else:
        print("Resuming: files already in S3, only the API call is left.")

    # Stage 3: send URLs (no files) to the API
    resp = register_with_api(entry, args)
    print(resp.status_code, resp.text[:300])
    if 300 <= resp.status_code < 400:
        raise RuntimeError(f"redirected to {resp.headers.get('Location')} - fix API_URL")
    if not 200 <= resp.status_code < 300:
        raise RuntimeError(f"API rejected: {resp.status_code} {resp.text[:200]}")

    entry["status"] = "uploaded"
    entry.pop("error", None)
    save_state(state)

    if not args.keep:
        cleanup_video_files(vid)  # S3 has the files now; free local disk immediately


# ======================= main =======================

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Resumable YouTube -> S3 -> API (URLs) uploader.")
    p.add_argument("urls", nargs="*", help="video URLs, video IDs, or playlist URLs")
    p.add_argument("-f", "--file", help="text file, one URL per line (# for comments)")
    p.add_argument("--category", default="Tutorials")
    p.add_argument("--featured", action="store_true")
    p.add_argument("--max-height", type=int, default=720,
                   help="cap video resolution (default: 720, use 0 for no cap)")
    p.add_argument("--retries", type=int, default=4, help="attempts per download/upload")
    p.add_argument("--sleep", type=float, default=30, help="seconds between videos (default: 30)")
    p.add_argument("--limit", type=int, default=0,
                   help="process at most N videos this run (0 = all). Try 30-50.")
    p.add_argument("--cookies-from-browser", dest="cookies_from_browser",
                   help="load YouTube cookies from a browser: chrome, firefox, safari, ...")
    p.add_argument("--cookies", help="path to a Netscape-format cookies.txt file")
    p.add_argument("--min-free-gb", type=float, default=2.0,
                   help="stop if free disk space drops below this")
    p.add_argument("--keep", action="store_true", help="don't delete local files after upload")
    p.add_argument("--force", action="store_true", help="redo videos already uploaded")
    args = p.parse_args()

    if args.max_height == 0:
        args.max_height = None

    state = load_state()

    urls = list(args.urls)
    if args.file:
        urls += read_urls_file(args.file)

    if urls:
        try:
            expanded = expand_urls(urls, args)
        except BotCheck:
            print("[stop] YouTube bot check while reading URLs. Use --cookies-from-browser, "
                  "wait a few hours, or change network, then rerun.")
            sys.exit(3)
        for vid, url in expanded:
            if vid not in state:
                state[vid] = {"url": url, "status": "pending"}
            elif args.force:
                state[vid].update(url=url, status="pending")
                state[vid].pop("urls", None)
                state[vid].pop("meta", None)
            elif not state[vid].get("url"):
                state[vid]["url"] = url
        save_state(state)

    todo = [(v, e) for v, e in state.items() if e.get("status") != "uploaded" and e.get("url")]
    if not todo:
        if not urls:
            p.error("nothing to do: give URLs / --file, or everything is already uploaded")
        print("Everything is already uploaded.")
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        sys.exit(0)

    total_left = len(todo)
    if args.limit and args.limit > 0:
        todo = todo[:args.limit]

    s3 = make_s3()

    resumed = sum(1 for _, e in todo if e.get("status") in ("ready", "stored"))
    print(f"{len(todo)} video(s) this run ({total_left} left in total, "
          f"{resumed} partially done, resuming).\n")

    ok, failed = 0, []
    try:
        for n, (vid, entry) in enumerate(todo, 1):
            print(f"\n===== [{n}/{len(todo)}] {vid} =====")
            try:
                process(vid, entry, state, s3, args)
                ok += 1
            except DiskFull as e:
                print(f"[stop] disk space too low: {e}. Free some space and rerun to resume.")
                save_state(state)
                sys.exit(2)
            except BotCheck:
                print("[stop] YouTube bot check. Fix cookies / wait a few hours / change "
                      "network, then rerun to resume. Remaining videos are untouched.")
                save_state(state)
                sys.exit(3)
            except Exception as e:
                print(f"[error] {vid}: {e}")
                if entry.get("status") not in ("ready", "stored"):
                    entry["status"] = "failed"
                entry["error"] = str(e)
                save_state(state)
                failed.append(vid)
            if n < len(todo):
                time.sleep(args.sleep)
    except KeyboardInterrupt:
        save_state(state)
        print("\nInterrupted. Progress saved - run the same command again to resume.")
        sys.exit(130)

    print(f"\nDone. {ok} uploaded, {len(failed)} failed.")
    if failed:
        print("Failed (will be retried on next run):")
        for v in failed:
            print(f"  {v}: {state[v].get('error')}")
        sys.exit(1)

    remaining = [v for v, e in state.items() if e.get("status") != "uploaded" and e.get("url")]
    if not remaining and not args.keep:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        print(f"Cleaned up {DOWNLOAD_DIR}/")
    elif remaining:
        print(f"{len(remaining)} video(s) still pending. Run again for the next batch.")