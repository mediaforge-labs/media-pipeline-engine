#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import factory_worker_v2 as base

RENDER_VERSION = "mediaforge-github-v10-strict"
# Long-form scripts can legitimately map to more than 140 semantic scenes. Keep a
# bounded headroom below the gateway's 200-item legacy ceiling while preserving the
# strict one-source-per-scene rule; the independent 8 GiB preflight remains the hard
# runner storage guard.
MAX_ASSETS = 180
MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024


def build_job_manifest(job: dict, queue: dict, variant: dict, lane: str, locale: str) -> dict:
    script = (
        variant.get("tts_text")
        or variant.get("script")
        or queue.get("tts_text")
        or queue.get("script")
        or ""
    ).strip()
    title = (variant.get("youtube_title") or queue.get("youtube_title") or "Leonidanos").strip()
    if len(script.split()) < 20:
        raise RuntimeError(f"Factory job has no usable TTS script for {locale}")

    metadata = dict(job.get("metadata") or {})
    # Stability first: production remains music-free until the audio chain is proven
    # independently. Music must never block publication.
    metadata["music_enabled"] = False
    metadata["video_library_enabled"] = True
    metadata["video_library_max_assets"] = MAX_ASSETS
    metadata["unique_media_only"] = True
    metadata["tts_chunk_order_strict"] = True
    metadata["queue_id"] = job.get("queue_id")
    metadata["variant_id"] = variant.get("id")

    return {
        "version": 1,
        "mode": "production",
        "jobs": {
            lane: {
                "id": job.get("job_key") or str(job["id"]),
                "locale": locale,
                "title": title,
                "shorts_requested": int(metadata.get("shorts_requested") or 5),
                "metadata": metadata,
                "script": script,
                "media": [],
            }
        },
    }


def validate_media_pool(asset_root: pathlib.Path) -> dict:
    selection_path = asset_root / "mediaforge-selection.json"
    catalog_path = asset_root / "Catalogo geral.json"
    if not selection_path.is_file() or not catalog_path.is_file():
        raise RuntimeError("Dell media preflight did not produce selection/catalog manifests")

    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    selected = int(
        selection.get("downloaded_unique_assets")
        or selection.get("selected_unique_assets")
        or 0
    )
    required = int(selection.get("required_unique_assets") or 0)
    rows = [
        row
        for row in (selection.get("segment_asset_map") or [])
        if row.get("segment_index") is not None
    ]
    ids = [str(row.get("asset_id")) for row in rows if row.get("asset_id")]
    segments = selection.get("segments") or []

    if selection.get("reuse_allowed") is not False:
        raise RuntimeError("Media pool does not enforce reuse_allowed=false")
    if selection.get("scope") != "gta-vi-owner-curated-only":
        raise RuntimeError(f"Media pool scope is not GTA VI owner curated: {selection.get('scope')}")
    if required < 1 or selected < required:
        raise RuntimeError(f"Insufficient unique media: selected={selected}, required={required}")
    if len(ids) != len(set(ids)):
        raise RuntimeError("Duplicate assets detected in semantic media map")
    if segments and len(ids) != len(segments):
        raise RuntimeError(
            f"Incomplete semantic mapping: mapped={len(ids)}, segments={len(segments)}"
        )

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    files = [asset_root / item["local_name"] for item in (catalog.get("assets") or [])]
    if len(files) != selected:
        raise RuntimeError(f"Catalog/file count mismatch: files={len(files)}, selected={selected}")

    for path in files:
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"Invalid downloaded media: {path}")
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    return {
        "selected": selected,
        "required": required,
        "mapped_segments": len(ids),
        "stale_catalog_assets_skipped": len(selection.get("unavailable_catalog_assets") or []),
        "selection_path": str(selection_path),
    }


def pre_upload_transform(out: pathlib.Path) -> None:
    """Hook overridden by v4 to apply the approved watermark before final validation."""
    del out


def probe_duration(path: pathlib.Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    duration = float(result.stdout.strip())
    if duration <= 0:
        raise RuntimeError(f"Invalid final video duration: {duration}")
    return duration


def run_validator(args: argparse.Namespace, out: pathlib.Path, asset_root: pathlib.Path, env: dict[str, str]) -> None:
    subprocess.run(
        [
            sys.executable,
            args.validator,
            "--root",
            str(out),
            "--selection",
            str(asset_root / "mediaforge-selection.json"),
            "--require-five-shorts",
        ],
        check=True,
        env=env,
    )


def run_factory(args: argparse.Namespace) -> int:
    owner = f"github:{os.environ.get('GITHUB_RUN_ID', 'local')}:{args.lane}"
    job = None
    variant = None
    queue = None
    asset_root: pathlib.Path | None = None

    try:
        job = base.lease_job(args.locale, owner)
        if not job:
            print(json.dumps({"status": "idle", "lane": args.lane, "locale": args.locale}))
            return 0

        queue = base.fetch_one("youtube_queue", id=job["queue_id"])
        if not queue:
            raise RuntimeError(f"Queue row not found: {job['queue_id']}")

        variant_id = (job.get("metadata") or {}).get("variant_id")
        if variant_id:
            variant = base.fetch_one("youtube_video_variants", id=variant_id)
        if not variant:
            variant = base.fetch_one(
                "youtube_video_variants",
                queue_id=job["queue_id"],
                locale=args.locale,
            )
        if not variant:
            raise RuntimeError(
                f"Video variant not found for queue={job['queue_id']} locale={args.locale}"
            )
        if variant.get("youtube_privacy_status") != "private":
            raise RuntimeError("Safety block: production variant is not private")
        if variant.get("youtube_video_id"):
            raise RuntimeError(
                f"Safety block: variant already has YouTube checkpoint {variant['youtube_video_id']}; refusing rerender"
            )

        queue_before_status = str(queue.get("status") or "")
        if args.locale == "pt-BR":
            updated_queue = base.patch_rows(
                "youtube_queue",
                {"status": "rendering", "last_error": None, "updated_at": base.utcnow()},
                id=job["queue_id"],
            )
            if not updated_queue:
                raise RuntimeError("Could not checkpoint PT queue as rendering")

        runtime = pathlib.Path(args.runtime_dir)
        runtime.mkdir(parents=True, exist_ok=True)
        manifest = build_job_manifest(job, queue, variant, args.lane, args.locale)
        job_path = runtime / f"factory-job-{args.lane}.json"
        script_path = runtime / f"factory-script-{args.lane}.txt"
        job_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        lane_job = manifest["jobs"][args.lane]
        script_path.write_text(str(lane_job["script"]), encoding="utf-8")
        title = str(lane_job["title"])

        asset_root = runtime / f"factory-assets-{args.lane}"
        subprocess.run(
            [
                sys.executable,
                args.asset_client,
                "--title",
                title,
                "--script-file",
                str(script_path),
                "--dest",
                str(asset_root),
                "--max-assets",
                str(MAX_ASSETS),
                "--max-total-bytes",
                str(MAX_TOTAL_BYTES),
                "--job-key",
                f"factory-{os.environ.get('GITHUB_RUN_ID', 'local')}-{args.lane}",
            ],
            check=True,
        )

        media_result = validate_media_pool(asset_root)
        print(json.dumps({"status": "media_preflight_ok", **media_result}, ensure_ascii=False))

        env = os.environ.copy()
        env["MEDIAFORGE_TTS_PROVIDER"] = "chatterbox"
        env["MEDIAFORGE_TTS_MAX_WORKERS"] = "1"
        env["MEDIAFORGE_TTS_STRICT_CHUNK_ORDER"] = "1"
        env["MEDIAFORGE_STRICT_UNIQUE_MEDIA"] = "1"
        env["MEDIAFORGE_DEBUG_SUBPROCESS_STDERR"] = "1"
        env["MEDIAFORGE_VIDEO_LIBRARY_ROOT"] = str(asset_root.resolve())

        # Verify encrypted-core compatibility before expensive TTS/render.
        subprocess.run(
            [
                sys.executable,
                args.worker,
                "--bundle",
                args.bundle,
                "--job",
                str(job_path),
                "--lane",
                args.lane,
                "--locale",
                args.locale,
                "--output-dir",
                args.output_dir,
                "--verify-only",
            ],
            check=True,
            env=env,
        )

        subprocess.run(
            [
                sys.executable,
                args.worker,
                "--bundle",
                args.bundle,
                "--job",
                str(job_path),
                "--lane",
                args.lane,
                "--locale",
                args.locale,
                "--output-dir",
                args.output_dir,
            ],
            check=True,
            env=env,
        )

        out = pathlib.Path(args.output_dir)
        # First validate the renderer's raw output, then apply the v4 watermark hook and
        # validate the exact bytes that will be stored/uploaded. This closes the old gap
        # where watermark re-encoding happened after validation.
        run_validator(args, out, asset_root, env)
        pre_upload_transform(out)
        run_validator(args, out, asset_root, env)

        render_manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        final_duration = probe_duration(out / "video" / "long-form.mp4")
        run_key = os.environ.get(
            "GITHUB_RUN_ID",
            datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
        )
        prefix = f"renders/{job['queue_id']}/{args.locale}/{run_key}"
        storage = None  # v4/resilient storage uses verified Supabase REST, not S3.

        video_uri = base.storage_upload(storage, out / "video" / "long-form.mp4", f"{prefix}/long-form.mp4")
        audio_uri = base.storage_upload(storage, out / "audio" / "narration.wav", f"{prefix}/narration.wav")
        manifest_uri = base.storage_upload(storage, out / "manifest.json", f"{prefix}/manifest.json")
        selection_uri = base.storage_upload(
            storage,
            asset_root / "mediaforge-selection.json",
            f"{prefix}/mediaforge-selection.json",
        )
        base.storage_upload(storage, out / "captions" / "long-form.srt", f"{prefix}/long-form.srt")
        base.storage_upload(storage, out / "media" / "manifest.json", f"{prefix}/media-manifest.json")

        shorts = sorted((out / "shorts").glob("short-*.mp4"))
        if len(shorts) != 5:
            raise RuntimeError(f"Production contract requires exactly 5 Shorts, got {len(shorts)}")
        short_uris: list[str] = []
        for short in shorts:
            short_uris.append(
                base.storage_upload(storage, short, f"{prefix}/shorts/{short.name}")
            )

        current = str(variant.get("status") or "")
        if current == "thumbnail_ready":
            next_status = "upload_ready"
        elif current in {"uploaded", "uploading"}:
            next_status = current
        else:
            next_status = "video_ready"

        variant_rows = base.patch_rows(
            "youtube_video_variants",
            {
                "status": next_status,
                "audio_url": audio_uri,
                "video_url": video_uri,
                "video_duration_seconds": final_duration,
                "render_version": RENDER_VERSION,
                "last_error": None,
                "updated_at": base.utcnow(),
            },
            id=variant["id"],
        )
        if not variant_rows:
            raise RuntimeError("MediaForge output stored but variant checkpoint could not be written")

        if args.locale == "pt-BR":
            restore_status = queue_before_status
            if restore_status in {"", "rendering", "failed"}:
                restore_status = "voice_ready"
            queue_rows = base.patch_rows(
                "youtube_queue",
                {
                    "status": restore_status,
                    "audio_url": audio_uri,
                    "video_url": video_uri,
                    "last_error": None,
                    "updated_at": base.utcnow(),
                },
                id=job["queue_id"],
            )
            if not queue_rows:
                raise RuntimeError("PT MediaForge output stored but queue checkpoint could not be written")

        metadata = dict(job.get("metadata") or {})
        metadata["result"] = {
            "video_url": video_uri,
            "audio_url": audio_uri,
            "manifest_url": manifest_uri,
            "selection_url": selection_uri,
            "shorts": short_uris,
            "render_version": RENDER_VERSION,
            "metrics": render_manifest.get("metrics") or {},
            "final_video_duration_seconds": final_duration,
            "media_preflight": media_result,
            "music_enabled": False,
        }
        job_rows = base.patch_rows(
            "youtube_factory_jobs",
            {
                "status": "completed",
                "last_progress_at": base.utcnow(),
                "completed_at": base.utcnow(),
                "lease_owner": None,
                "lease_expires_at": None,
                "metadata": metadata,
                "updated_at": base.utcnow(),
            },
            id=job["id"],
            lease_owner=owner,
        )
        if not job_rows:
            raise RuntimeError("Factory render succeeded but lease checkpoint could not be finalized")

        print(
            json.dumps(
                {
                    "status": "completed",
                    "job_id": job["id"],
                    "lane": args.lane,
                    "locale": args.locale,
                    "video_url": video_uri,
                    "shorts": len(short_uris),
                    "music_enabled": False,
                    "render_version": RENDER_VERSION,
                    "final_video_duration_seconds": final_duration,
                },
                ensure_ascii=False,
            )
        )
        return 0

    except Exception as exc:
        base.mark_failed(job, variant, owner, str(exc), queue=queue)
        raise
    finally:
        if asset_root is not None:
            shutil.rmtree(asset_root, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", required=True)
    ap.add_argument("--locale", required=True)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--worker", default="public-worker-template/worker.py")
    ap.add_argument("--asset-client", default="public-worker-template/dell_asset_client_v5.py")
    ap.add_argument("--validator", default="public-worker-template/validate_render.py")
    ap.add_argument("--runtime-dir", default=".runtime")
    ap.add_argument("--output-dir", required=True)
    args = ap.parse_args()
    raise SystemExit(run_factory(args))


if __name__ == "__main__":
    main()
