# storage.py
import os
import uuid
import time
import mimetypes
from typing import Optional
from google.cloud import storage as gcs

# Optional: restrict to formats you actually handle (Pillow often can't process SVG/animated)
ALLOWED_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",   # fine if you don't re-process frames; stored as-is
    "image/svg+xml"  # only if you store as-is (no Pillow operations)
}

def _safe_ext(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    # Default to .bin if unknown
    return ext if ext else ".bin"

def upload_image(file_storage, folder: str = "misc") -> str:
    """
    Uploads a Werkzeug FileStorage to a GCS bucket and returns a browser-loadable URL.
    Requires env var UPLOAD_BUCKET. Uses default credentials on App Engine.

    Returns: public HTTPS URL (storage.googleapis.com/<bucket>/<object>)
    Raises: ValueError/RuntimeError on misconfig/unsupported type
    """
    if not file_storage or not getattr(file_storage, "filename", ""):
        raise ValueError("No file provided.")

    bucket_name = os.environ.get("UPLOAD_BUCKET")
    if not bucket_name:
        raise RuntimeError("UPLOAD_BUCKET env var is not set.")

    # Determine content-type safely
    ctype = file_storage.mimetype or mimetypes.guess_type(file_storage.filename)[0] or "application/octet-stream"
    # If you want to enforce a strict allowlist, uncomment:
    # if ctype not in ALLOWED_CONTENT_TYPES:
    #     raise ValueError(f"Unsupported image type: {ctype}")

    # Build unique object name
    ext = _safe_ext(file_storage.filename)
    obj_name = f"{folder}/{int(time.time())}-{uuid.uuid4().hex}{ext}"

    client = gcs.Client()                # Uses App Engine service account
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(obj_name)

    # Upload stream directly; DO NOT try to Pillow-open SVG/animated types here
    # (If you add processing later, branch by ctype and only process PNG/JPEG/WEBP frames you support.)
    blob.upload_from_file(file_storage.stream, content_type=ctype)

    # If bucket is public (recommended for simplicity), this URL will load in browsers:
    return f"https://storage.googleapis.com/{bucket_name}/{obj_name}"

    # If the bucket is private, return a signed URL instead:
    # from datetime import timedelta
    # return blob.generate_signed_url(expiration=timedelta(days=7), method="GET")
