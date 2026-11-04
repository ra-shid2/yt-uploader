import os, boto3
from dotenv import load_dotenv
load_dotenv(override=True)

s3 = boto3.client("s3",
    region_name=os.environ["S3_REGION"],
    endpoint_url=os.environ["S3_ENDPOINT_URL"])

print("key in use:", os.environ["AWS_ACCESS_KEY_ID"])
s3.put_object(Bucket=os.environ["S3_BUCKET"], Key="test-uploads/test.txt",
              Body=b"hello", ContentType="text/plain")
print("upload OK (no ACL)")

s3.put_object(Bucket=os.environ["S3_BUCKET"], Key="test-uploads/test-public.txt",
              Body=b"hello", ContentType="text/plain", ACL="public-read")
print("upload OK (public-read)")
print(os.environ["S3_PUBLIC_BASE_URL"].rstrip("/") + "/test-uploads/test-public.txt")