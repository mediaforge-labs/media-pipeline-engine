from __future__ import annotations

import base64
import os
import pathlib
import time
from urllib.parse import urljoin, urlparse

import requests

TUS_VERSION = "1.0.0"
CHUNK_SIZE = 6 * 1024 * 1024  # Supabase currently requires 6 MB TUS chunks.
RETRY_DELAYS = (0, 3, 5, 10, 20)


def _api_key() -> str:
    key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("SUPABASE_SECRET_KEY is not configured")
    return key


def _auth_headers() -> dict[str, str]:
    key = _api_key()
    headers = {"apikey": key}
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _endpoint() -> str:
    raw = os.environ.get("SUPABASE_URL", "").strip()
    if not raw:
        raise RuntimeError("SUPABASE_URL is not configured")
    host = urlparse(raw).hostname or ""
    project_ref = host.split(".", 1)[0]
    if not project_ref:
        raise RuntimeError(f"Could not resolve Supabase project ref from {raw}")
    return f"https://{project_ref}.storage.supabase.co/storage/v1/upload/resumable"


def _metadata(values: dict[str, str]) -> str:
    encoded: list[str] = []
    for name, value in values.items():
        token = base64.b64encode(value.encode("utf-8")).decode("ascii")
        encoded.append(f"{name} {token}")
    return ",".join(encoded)


def _head_offset(session: requests.Session, upload_url: str) -> int:
    headers = {**_auth_headers(), "Tus-Resumable": TUS_VERSION}
    response = session.head(upload_url, headers=headers, timeout=(30, 120))
    response.raise_for_status()
    value = response.headers.get("Upload-Offset")
    if value is None:
        raise RuntimeError("TUS HEAD response did not include Upload-Offset")
    return int(value)


def upload_file(
    local_path: pathlib.Path,
    *,
    bucket: str,
    object_path: str,
    content_type: str = "application/octet-stream",
    cache_control: str = "3600",
    upsert: bool = True,
) -> str:
    local_path = pathlib.Path(local_path)
    if not local_path.is_file() or local_path.stat().st_size <= 0:
        raise RuntimeError(f"TUS upload source missing or empty: {local_path}")

    total = local_path.stat().st_size
    endpoint = _endpoint()
    session = requests.Session()
    create_headers = {
        **_auth_headers(),
        "Tus-Resumable": TUS_VERSION,
        "Upload-Length": str(total),
        "Upload-Metadata": _metadata(
            {
                "bucketName": bucket,
                "objectName": object_path,
                "contentType": content_type,
                "cacheControl": cache_control,
            }
        ),
        "Content-Length": "0",
    }
    if upsert:
        create_headers["x-upsert"] = "true"

    response = session.post(endpoint, headers=create_headers, timeout=(30, 120))
    if response.status_code not in {201, 204}:
        raise RuntimeError(
            f"TUS create failed HTTP {response.status_code}: {response.text[:800]}"
        )
    location = response.headers.get("Location")
    if not location:
        raise RuntimeError("TUS create response did not include Location")
    upload_url = urljoin(endpoint, location)

    offset = _head_offset(session, upload_url)
    with local_path.open("rb") as fh:
        while offset < total:
            fh.seek(offset)
            chunk = fh.read(min(CHUNK_SIZE, total - offset))
            if not chunk:
                raise RuntimeError(f"Unexpected EOF during TUS upload at offset {offset}")

            sent = False
            last_error = ""
            for delay in RETRY_DELAYS:
                if delay:
                    time.sleep(delay)
                patch_headers = {
                    **_auth_headers(),
                    "Tus-Resumable": TUS_VERSION,
                    "Upload-Offset": str(offset),
                    "Content-Type": "application/offset+octet-stream",
                    "Content-Length": str(len(chunk)),
                }
                if upsert:
                    patch_headers["x-upsert"] = "true"
                try:
                    part = session.patch(
                        upload_url,
                        headers=patch_headers,
                        data=chunk,
                        timeout=(30, 300),
                    )
                    if part.status_code == 204:
                        new_offset = int(part.headers.get("Upload-Offset", offset + len(chunk)))
                        if new_offset <= offset:
                            raise RuntimeError(
                                f"TUS offset did not advance: {offset} -> {new_offset}"
                            )
                        offset = new_offset
                        sent = True
                        pct = (offset / total) * 100
                        print(f"TUS progress {offset}/{total} ({pct:.1f}%)", flush=True)
                        break
                    last_error = f"HTTP {part.status_code}: {part.text[:500]}"
                except Exception as exc:
                    last_error = str(exc)

                try:
                    server_offset = _head_offset(session, upload_url)
                    if server_offset > offset:
                        offset = server_offset
                        sent = True
                        break
                except Exception as head_exc:
                    last_error += f"; HEAD recovery failed: {head_exc}"

            if not sent:
                raise RuntimeError(f"TUS chunk failed at offset {offset}: {last_error}")

    final_offset = _head_offset(session, upload_url)
    if final_offset != total:
        raise RuntimeError(f"TUS final offset mismatch: {final_offset} != {total}")

    print(
        f"TUS_UPLOAD_OK bucket={bucket} object={object_path} bytes={total}",
        flush=True,
    )
    return f"supabase://{bucket}/{object_path}"
