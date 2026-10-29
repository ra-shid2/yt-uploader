import argparse
import os
import re

import requests
from dotenv import load_dotenv
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
from tqdm import tqdm
from yt_dlp import YoutubeDL

load_dotenv()
API_URL = os.environ["API_URL"]


def slugify(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:80] or "video"


def download(url: str, max_height: int | None) -> tuple[str, str, dict]:
    fmt = "bv*+ba/b"
    if max_height:
        fmt = f"bv*[height<={max_height}]+ba/b[height<={max_height}]"
    opts = {
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": "downloads/%(id)s.%(ext)s",
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}
        ],
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    base = os.path.join("downloads", info["id"])
    return base + ".mp4", base + ".jpg", info


def upload(video_path, thumb_path, info, category, status, featured):
    with open(video_path, "rb") as v, open(thumb_path, "rb") as t:
        encoder = MultipartEncoder(
            fields={
                "title": info["title"],
                "slug": f"{slugify(info['title'])}-{info['id'].lower()}",
                "description": info.get("description") or "",
                "status": status,
                "category_name": category,
                "is_featured": "1" if featured else "0",
                "video_file": (os.path.basename(video_path), v, "video/mp4"),
                "thumbnail_file": (os.path.basename(thumb_path), t, "image/jpeg"),
            }
        )

        bar = tqdm(total=encoder.len, unit="B", unit_scale=True, desc="Uploading")
        last = 0

        def on_progress(monitor):
            nonlocal last
            bar.update(monitor.bytes_read - last)
            last = monitor.bytes_read

        monitor = MultipartEncoderMonitor(encoder, on_progress)

        try:
            return requests.post(
                API_URL,
                data=monitor,
                headers={
                    "Accept": "application/json",
                    "Content-Type": monitor.content_type,
                },
                timeout=600,
                allow_redirects=False,  # surface redirects instead of turning POST into GET
            )
        finally:
            bar.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("--category", default="Tutorials")
    p.add_argument("--status", default="draft")
    p.add_argument("--featured", action="store_true")
    p.add_argument("--max-height", type=int, default=720,
                   help="cap video resolution (default: 720, use 0 for no cap)")
    args = p.parse_args()

    video, thumb, info = download(args.url, args.max_height)
    print(f"Downloaded: {video} ({os.path.getsize(video) / 1024 / 1024:.1f} MiB)")

    resp = upload(video, thumb, info, args.category, args.status, args.featured)
    print(resp.status_code)
    if 300 <= resp.status_code < 400:
        print("Redirected to:", resp.headers.get("Location"), "- fix API_URL")
    print(resp.text[:1000])