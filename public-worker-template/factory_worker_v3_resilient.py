#!/usr/bin/env python3
from __future__ import annotations

import mimetypes
import pathlib

import factory_worker_v2 as base
import factory_worker_v3 as impl
import supabase_chunked

_ORIGINAL_STORAGE_UPLOAD = base.storage_upload
CHUNKED_UPLOAD_THRESHOLD = 40 * 1024 * 1024


def resilient_storage_upload(client, local_path: pathlib.Path, storage_path: str) -> str:
    local_path = pathlib.Path(local_path)
    if not local_path.is_file() or local_path.stat().st_size <= 0:
        raise RuntimeError(f"Output file missing: {local_path}")

    size = local_path.stat().st_size
    if size <= CHUNKED_UPLOAD_THRESHOLD:
        return _ORIGINAL_STORAGE_UPLOAD(client, local_path, storage_path)

    content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    return supabase_chunked.upload_file(
        local_path,
        bucket=base.SUPABASE_BUCKET,
        storage_path=storage_path,
        content_type=content_type,
    )


base.storage_upload = resilient_storage_upload
impl.base.storage_upload = resilient_storage_upload


if __name__ == "__main__":
    impl.main()
