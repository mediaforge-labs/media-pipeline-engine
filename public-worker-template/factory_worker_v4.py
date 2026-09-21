#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import subprocess

import requests

import factory_worker_v2 as base
import factory_worker_v3 as impl
import factory_worker_v3_resilient as resilient  # patches all production uploads to verified REST storage

APPROVED_WATERMARK_URL = (
    "https://rhddgfvtrkmusbvphnlg.supabase.co/storage/v1/object/public/"
    "blog-images/social-assets/leonidanos-logo.png"
)
WATERMARK_RENDER_VERSION = "mediaforge-github-v10-strict-watermarked-shorts-v2"

# Importing factory_worker_v3_resilient above installs the resilient uploader.
_ORIGINAL_STORAGE_UPLOAD = base.storage_upload
_ORIGINAL_PATCH_ROWS = base.patch_rows


def apply_approved_watermark(video_path: pathlib.Path) -> None:
    marker = video_path.with_suffix(video_path.suffix + ".leonidanos-watermarked")
    if marker.is_file():
        return

    logo_path = video_path.parent / "leonidanos-approved-watermark.png"
    if not logo_path.is_file():
        response = requests.get(APPROVED_WATERMARK_URL, timeout=60)
        response.raise_for_status()
        if not response.content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("Approved Leonidanos watermark is not a valid PNG")
        logo_path.write_bytes(response.content)

    output_path = video_path.with_name(video_path.stem + ".watermarked.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-i",
            str(logo_path),
            "-filter_complex",
            "[1:v]scale='min(220,iw)':-1,format=rgba,colorchannelmixer=aa=0.72[wm];"
            "[0:v][wm]overlay=W-w-36:H-h-32:format=auto[v]",
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "fast",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height:format=duration",
            "-of",
            "json",
            str(output_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError("Watermarked MediaForge output was not produced")
    output_path.replace(video_path)
    marker.write_text("approved-leonidanos-watermark-v2\n", encoding="utf-8")


def apply_output_watermarks(out: pathlib.Path) -> None:
    """Transform every publishable MP4 before the final render validator runs."""
    out = pathlib.Path(out)
    long_form = out / "video" / "long-form.mp4"
    shorts = sorted((out / "shorts").glob("short-*.mp4"))
    if not long_form.is_file():
        raise RuntimeError("Long-form output is missing before watermark transform")
    if len(shorts) != 5:
        raise RuntimeError(f"Watermark stage expected exactly 5 Shorts, got {len(shorts)}")

    targets = [long_form, *shorts]
    for path in targets:
        apply_approved_watermark(path)
        marker = path.with_suffix(path.suffix + ".leonidanos-watermarked")
        if not marker.is_file():
            raise RuntimeError(f"Watermark attestation marker missing after transform: {path}")

    print(f"Approved Leonidanos watermark applied and attested on {len(targets)} videos.")


def storage_upload(client, local_path: pathlib.Path, storage_path: str) -> str:
    # Defense in depth: pre_upload_transform already watermarks and validates these
    # exact files. Keep this idempotent guard so no future call-site can upload a raw
    # long-form/Short by bypassing that hook.
    local_path = pathlib.Path(local_path)
    is_long_form = local_path.name == "long-form.mp4" and storage_path.endswith("/long-form.mp4")
    is_short = local_path.suffix.lower() == ".mp4" and "/shorts/" in storage_path
    if is_long_form or is_short:
        apply_approved_watermark(local_path)
    return _ORIGINAL_STORAGE_UPLOAD(client, local_path, storage_path)


def patch_rows(table: str, body: dict, **filters):
    payload = dict(body)
    # A factory render normally writes video_ready. If a localized thumbnail already
    # exists, keep the state machine moving forward to upload_ready instead of
    # requiring a second manual transition.
    if table == "youtube_video_variants" and payload.get("status") == "video_ready" and filters.get("id"):
        current = base.fetch_one("youtube_video_variants", id=filters["id"])
        if current and current.get("thumbnail_url"):
            payload["status"] = "upload_ready"
    return _ORIGINAL_PATCH_ROWS(table, payload, **filters)


base.storage_upload = storage_upload
impl.base.storage_upload = storage_upload
base.patch_rows = patch_rows
impl.base.patch_rows = patch_rows
impl.pre_upload_transform = apply_output_watermarks
impl.RENDER_VERSION = WATERMARK_RENDER_VERSION


if __name__ == "__main__":
    impl.main()
