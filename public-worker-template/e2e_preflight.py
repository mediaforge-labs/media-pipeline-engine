#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import factory_worker_v2 as base
import factory_worker_v3 as v3
import factory_worker_v4 as v4
import supabase_chunked

EXPECTED_RENDER = "mediaforge-github-v10-strict-watermarked-shorts-v2"


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)


def make_video(path: pathlib.Path, size: str, duration: float = 1.2) -> None:
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=black:s={size}:d={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", str(path),
    ])


def probe(path: pathlib.Path) -> dict:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,width,height:format=duration", "-of", "json", str(path),
    ], text=True)
    data = json.loads(out)
    streams = data.get("streams") or []
    if not any(x.get("codec_type") == "video" for x in streams):
        raise RuntimeError(f"No video stream after watermark: {path}")
    if not any(x.get("codec_type") == "audio" for x in streams):
        raise RuntimeError(f"No audio stream after watermark: {path}")
    if float((data.get("format") or {}).get("duration") or 0) <= 0:
        raise RuntimeError(f"Invalid duration after watermark: {path}")
    return data


def assert_runtime_patches() -> None:
    if v3.RENDER_VERSION != EXPECTED_RENDER:
        raise RuntimeError(f"Wrong production render version: {v3.RENDER_VERSION}")
    if v3.pre_upload_transform is not v4.apply_output_watermarks:
        raise RuntimeError("v4 watermark transform is not installed in v3 production path")
    if base.storage_upload is not v4.storage_upload:
        raise RuntimeError("v4 upload guard is not installed")
    if not os.environ.get("SUPABASE_SECRET_KEY"):
        raise RuntimeError("SUPABASE_SECRET_KEY unavailable in preflight")


def test_watermark() -> None:
    with tempfile.TemporaryDirectory(prefix="mediaforge-preflight-watermark-") as tmp:
        out = pathlib.Path(tmp)
        (out / "video").mkdir()
        (out / "shorts").mkdir()
        make_video(out / "video" / "long-form.mp4", "640x360")
        for i in range(1, 6):
            make_video(out / "shorts" / f"short-{i:02d}.mp4", "360x640")
        v4.apply_output_watermarks(out)
        targets = [out / "video" / "long-form.mp4", *sorted((out / "shorts").glob("short-*.mp4"))]
        if len(targets) != 6:
            raise RuntimeError(f"Expected six publishable videos, got {len(targets)}")
        for path in targets:
            probe(path)
            marker = path.with_suffix(path.suffix + ".leonidanos-watermarked")
            if not marker.is_file():
                raise RuntimeError(f"Missing watermark attestation: {path}")


def test_supabase_rest_storage() -> None:
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    prefix = f"preflight/{run_id}"
    with tempfile.TemporaryDirectory(prefix="mediaforge-preflight-storage-") as tmp:
        root = pathlib.Path(tmp)
        small = root / "small.json"
        small.write_text(json.dumps({"preflight": True, "run_id": run_id}) + "\n", encoding="utf-8")
        uri = supabase_chunked.upload_direct_file(
            small, bucket=base.SUPABASE_BUCKET, storage_path=f"{prefix}/small.json", content_type="application/json"
        )
        if not uri.startswith("supabase://"):
            raise RuntimeError(f"Unexpected direct storage URI: {uri}")

        # Exercise the exact chunk/manifest/checksum implementation without a 40 MiB test file.
        old_chunk = supabase_chunked.CHUNK_SIZE
        supabase_chunked.CHUNK_SIZE = 1024 * 1024
        try:
            big = root / "chunk-test.bin"
            payload = (b"Leonidanos-MediaForge-preflight\n" * 100000)[: 2_500_000]
            big.write_bytes(payload)
            chunk_uri = supabase_chunked.upload_file(
                big, bucket=base.SUPABASE_BUCKET, storage_path=f"{prefix}/chunk-test.bin", content_type="application/octet-stream"
            )
        finally:
            supabase_chunked.CHUNK_SIZE = old_chunk
        if not chunk_uri.startswith("supabase-chunked://"):
            raise RuntimeError(f"Unexpected chunked storage URI: {chunk_uri}")


def test_database_read_path() -> None:
    rows = base.request_json(
        "GET", base.rest_url("youtube_factory_jobs"),
        params={"select": "id,status,locale", "limit": "1"}, timeout=30,
    )
    if rows is None or not isinstance(rows, list):
        raise RuntimeError("Supabase REST read path returned an invalid payload")


def main() -> None:
    assert_runtime_patches()
    test_database_read_path()
    test_watermark()
    test_supabase_rest_storage()
    print(json.dumps({
        "status": "passed",
        "render_version": v3.RENDER_VERSION,
        "watermark": "six_outputs_attested",
        "storage": "direct_and_chunked_rest_verified",
        "database": "read_verified",
        "production_queue_mutated": False,
        "youtube_mutated": False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
