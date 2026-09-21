from __future__ import annotations

import hashlib
import json
import os
import pathlib
from urllib.parse import quote

import requests

CHUNK_SIZE = 40 * 1024 * 1024


def _config() -> tuple[str, str]:
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not base_url or not key:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SECRET_KEY are required")
    return base_url, key


def _headers(content_type: str) -> dict[str, str]:
    _, key = _config()
    headers = {
        "apikey": key,
        "Content-Type": content_type,
        "Cache-Control": "3600",
        "x-upsert": "true",
    }
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _upload_bytes(bucket: str, object_path: str, data: bytes, content_type: str) -> None:
    base_url, _ = _config()
    url = f"{base_url}/storage/v1/object/{bucket}/{quote(object_path, safe='/')}"
    response = requests.post(url, headers=_headers(content_type), data=data, timeout=(30, 600))
    if response.status_code >= 400:
        raise RuntimeError(
            f"Storage chunk upload failed HTTP {response.status_code} for {object_path}: "
            f"{response.text[:500]}"
        )


def upload_file(
    local_path: pathlib.Path,
    *,
    bucket: str,
    storage_path: str,
    content_type: str = "video/mp4",
) -> str:
    local_path = pathlib.Path(local_path)
    if not local_path.is_file() or local_path.stat().st_size <= 0:
        raise RuntimeError(f"Chunked upload source missing or empty: {local_path}")

    total_size = local_path.stat().st_size
    prefix = f"{storage_path}.chunks"
    total_hash = hashlib.sha256()
    chunks: list[dict[str, object]] = []

    with local_path.open("rb") as fh:
        index = 0
        uploaded = 0
        while True:
            data = fh.read(CHUNK_SIZE)
            if not data:
                break
            total_hash.update(data)
            digest = hashlib.sha256(data).hexdigest()
            object_path = f"{prefix}/part-{index:05d}.bin"
            _upload_bytes(bucket, object_path, data, "application/octet-stream")
            chunks.append({"index": index, "object_path": object_path, "size": len(data), "sha256": digest})
            uploaded += len(data)
            print(
                f"CHUNK_UPLOAD {index + 1} bytes={uploaded}/{total_size} "
                f"({uploaded / total_size * 100:.1f}%)",
                flush=True,
            )
            index += 1

    if uploaded != total_size or not chunks:
        raise RuntimeError(f"Chunk split size mismatch: {uploaded} != {total_size}")

    manifest = {
        "version": 1,
        "storage_mode": "chunked",
        "bucket": bucket,
        "original_name": local_path.name,
        "content_type": content_type,
        "total_size": total_size,
        "sha256": total_hash.hexdigest(),
        "chunk_size": CHUNK_SIZE,
        "chunks": chunks,
    }
    manifest_path = f"{prefix}/manifest.json"
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    _upload_bytes(bucket, manifest_path, manifest_bytes, "application/json")
    print(
        f"CHUNKED_UPLOAD_OK bucket={bucket} manifest={manifest_path} "
        f"parts={len(chunks)} bytes={total_size}",
        flush=True,
    )
    return f"supabase-chunked://{bucket}/{manifest_path}"
