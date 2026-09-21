#!/usr/bin/env python3
from __future__ import annotations

import mimetypes
import pathlib

import factory_worker_v2 as base
import factory_worker_v3 as impl
import supabase_chunked

CHUNKED_UPLOAD_THRESHOLD = 40 * 1024 * 1024


def resilient_storage_upload(client, local_path: pathlib.Path, storage_path: str) -> str:
    """Use Supabase Storage REST for every production object.

    The S3 compatibility endpoint repeatedly produced empty PutObject failures in
    production. The REST endpoint is already the proven path used by thumbnails and
    chunk manifests, so production now uses it for both direct and chunked objects.
    The client argument is retained only for call-site compatibility and is ignored.
    """
    del client
    local_path = pathlib.Path(local_path)
    if not local_path.is_file() or local_path.stat().st_size <= 0:
        raise RuntimeError(f"Output file missing: {local_path}")

    size = local_path.stat().st_size
    content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    if size <= CHUNKED_UPLOAD_THRESHOLD:
        return supabase_chunked.upload_direct_file(
            local_path,
            bucket=base.SUPABASE_BUCKET,
            storage_path=storage_path,
            content_type=content_type,
        )

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
