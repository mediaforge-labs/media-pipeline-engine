#!/usr/bin/env python3
from __future__ import annotations

import mimetypes
import os
import pathlib
from urllib.parse import quote

import requests

import factory_worker_v2 as base
import factory_worker_v3 as impl

_ORIGINAL_STORAGE_UPLOAD = base.storage_upload
REST_UPLOAD_THRESHOLD = 64 * 1024 * 1024


def _api_headers(content_type: str, size: int) -> dict[str, str]:
    key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("SUPABASE_SECRET_KEY is not configured")
    headers = {
        "apikey": key,
        "Content-Type": content_type,
        "Content-Length": str(size),
        "Cache-Control": "3600",
        "x-upsert": "true",
    }
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def resilient_storage_upload(client, local_path: pathlib.Path, storage_path: str) -> str:
    if not local_path.is_file() or local_path.stat().st_size <= 0:
        raise RuntimeError(f"Output file missing: {local_path}")
    size = local_path.stat().st_size
    if size <= REST_UPLOAD_THRESHOLD:
        return _ORIGINAL_STORAGE_UPLOAD(client, local_path, storage_path)

    bucket = base.SUPABASE_BUCKET
    content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("SUPABASE_URL is not configured")
    url = f"{base_url}/storage/v1/object/{bucket}/{quote(storage_path, safe='/')}"
    headers = _api_headers(content_type, size)

    with local_path.open("rb") as fh:
        response = requests.post(url, headers=headers, data=fh, timeout=(30, 1800))
    if response.status_code >= 400:
        raise RuntimeError(
            f"Supabase Storage REST upload failed ({response.status_code}) for {storage_path}: "
            f"{response.text[:800]}"
        )

    print(
        f"Large render uploaded through Supabase Storage REST: "
        f"{storage_path} / {size} bytes / HTTP {response.status_code}"
    )
    return f"supabase://{bucket}/{storage_path}"


base.storage_upload = resilient_storage_upload
impl.base.storage_upload = resilient_storage_upload


if __name__ == "__main__":
    impl.main()
