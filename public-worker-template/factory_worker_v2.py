#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone

import boto3
import requests
from botocore.client import Config
from botocore.exceptions import ClientError

SUPABASE_BUCKET = "mediaforge-assets"
SUPABASE_PROJECT_REF = "rhddgfvtrkmusbvphnlg"
SUPABASE_S3_REGION = "us-west-2"
SUPABASE_S3_ENDPOINT = f"https://{SUPABASE_PROJECT_REF}.storage.supabase.co/storage/v1/s3"
RENDER_VERSION = "mediaforge-github-v9"
PART_SIZE = 64 * 1024 * 1024
SINGLE_PUT_LIMIT = 64 * 1024 * 1024
MAX_FACTORY_ATTEMPTS = 3


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def api_headers() -> dict[str, str]:
    key = required("SUPABASE_SECRET_KEY")
    headers = {"apikey": key, "User-Agent": "MediaForge/1.4"}
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def rest_url(path: str) -> str:
    return f"{required('SUPABASE_URL').rstrip('/')}/rest/v1/{path.lstrip('/')}"


def request_json(method: str, url: str, *, params=None, body=None, extra_headers=None, timeout=60):
    headers = api_headers()
    headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    response = requests.request(method, url, headers=headers, params=params, json=body, timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"Supabase request failed ({response.status_code}): {response.text[:500]}")
    if not response.content:
        return None
    return response.json()


def s3_client():
    """Legacy compatibility only; production v4 writes through Storage REST."""
    return boto3.client(
        "s3",
        endpoint_url=SUPABASE_S3_ENDPOINT,
        region_name=SUPABASE_S3_REGION,
        aws_access_key_id=required("SUPABASE_S3_ACCESS_KEY_ID"),
        aws_secret_access_key=required("SUPABASE_S3_SECRET_ACCESS_KEY"),
        config=Config(
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 8, "mode": "standard"},
            connect_timeout=30,
            read_timeout=300,
            s3={"addressing_style": "path", "payload_signing_enabled": True},
        ),
    )


def lease_job(locale: str, owner: str) -> dict | None:
    payload = request_json(
        "POST",
        rest_url("rpc/lease_mediaforge_job"),
        body={"p_locale": locale, "p_owner": owner, "p_lease_minutes": 180},
        extra_headers={"Prefer": "return=representation"},
    ) or []
    return payload[0] if payload else None


def fetch_one(table: str, **filters) -> dict | None:
    params = {"select": "*", "limit": "1"}
    for key, value in filters.items():
        params[key] = f"eq.{value}"
    rows = request_json("GET", rest_url(table), params=params) or []
    return rows[0] if rows else None


def patch_rows(table: str, body: dict, **filters) -> list[dict]:
    params = {key: f"eq.{value}" for key, value in filters.items()}
    rows = request_json(
        "PATCH",
        rest_url(table),
        params=params,
        body=body,
        extra_headers={"Prefer": "return=representation"},
    )
    return rows or []


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_job_manifest(job: dict, queue: dict, variant: dict, lane: str, locale: str) -> dict:
    script = (variant.get("tts_text") or variant.get("script") or queue.get("tts_text") or queue.get("script") or "").strip()
    title = (variant.get("youtube_title") or queue.get("youtube_title") or "Leonidanos").strip()
    if len(script.split()) < 20:
        raise RuntimeError(f"Factory job has no usable TTS script for {locale}")
    metadata = dict(job.get("metadata") or {})
    metadata.setdefault("music_enabled", True)
    metadata.setdefault("video_library_enabled", True)
    metadata.setdefault("video_library_max_assets", 18)
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


def object_size(client, key: str) -> int | None:
    try:
        result = client.head_object(Bucket=SUPABASE_BUCKET, Key=key)
        return int(result.get("ContentLength", -1))
    except ClientError as exc:
        error = exc.response.get("Error") or {}
        status = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if str(error.get("Code")) in {"404", "NoSuchKey", "NotFound"} or status == 404:
            return None
        raise


def direct_put(client, local_path: pathlib.Path, key: str, content_type: str) -> None:
    size = local_path.stat().st_size
    with local_path.open("rb") as fh:
        client.put_object(
            Bucket=SUPABASE_BUCKET,
            Key=key,
            Body=fh,
            ContentLength=size,
            ContentType=content_type,
            CacheControl="3600",
        )


def multipart_put(client, local_path: pathlib.Path, key: str, content_type: str) -> None:
    created = client.create_multipart_upload(
        Bucket=SUPABASE_BUCKET,
        Key=key,
        ContentType=content_type,
        CacheControl="3600",
    )
    upload_id = created["UploadId"]
    parts: list[dict] = []
    try:
        with local_path.open("rb") as fh:
            part_number = 1
            while True:
                chunk = fh.read(PART_SIZE)
                if not chunk:
                    break
                result = client.upload_part(
                    Bucket=SUPABASE_BUCKET,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                    ContentLength=len(chunk),
                )
                parts.append({"ETag": result["ETag"], "PartNumber": part_number})
                part_number += 1
        client.complete_multipart_upload(
            Bucket=SUPABASE_BUCKET,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        try:
            client.abort_multipart_upload(Bucket=SUPABASE_BUCKET, Key=key, UploadId=upload_id)
        except Exception:
            pass
        raise


def storage_upload(client, local_path: pathlib.Path, storage_path: str) -> str:
    """Legacy S3 uploader retained for older/manual workflows only."""
    if not local_path.is_file() or local_path.stat().st_size <= 0:
        raise RuntimeError(f"Output file missing: {local_path}")
    size = local_path.stat().st_size
    content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    if size <= SINGLE_PUT_LIMIT:
        direct_put(client, local_path, storage_path, content_type)
    else:
        multipart_put(client, local_path, storage_path, content_type)
    remote_size = object_size(client, storage_path)
    if remote_size != size:
        raise RuntimeError(f"Storage verification failed for {storage_path}: local={size}, remote={remote_size}")
    return f"supabase://{SUPABASE_BUCKET}/{storage_path}"


def is_transient_failure(error: str) -> bool:
    text = (error or "").lower()
    patterns = (
        r"connection reset",
        r"connection aborted",
        r"remote end closed",
        r"temporary failure",
        r"timed? ?out",
        r"timeout",
        r"too many requests",
        r"rate limit",
        r"http[^\n]*(408|429|500|502|503|504)",
        r"service unavailable",
        r"bad gateway",
        r"gateway timeout",
        r"connectionerror",
        r"urlerror",
        r"dell_asset_client_v5\.py.*returned non-zero exit status",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def mark_failed(
    job: dict | None,
    variant: dict | None,
    owner: str,
    error: str,
    queue: dict | None = None,
) -> None:
    """Persist a deterministic failure or requeue a transient one safely.

    A GitHub Actions rerun alone cannot heal a job after the database row has been
    changed to `failed`, because the lease RPC only leases pending/expired-running
    rows. Transient failures are therefore returned to `pending` here (max 3 leases),
    with the lease cleared atomically. Deterministic failures remain failed.
    """
    message = (error or "unknown factory failure")[:1800]
    retryable = bool(job) and is_transient_failure(message) and int(job.get("attempt") or 0) < MAX_FACTORY_ATTEMPTS
    now = utcnow()

    if job:
        metadata = dict(job.get("metadata") or {})
        metadata["last_error"] = message
        metadata["last_failure_at"] = now
        metadata["last_failure_retryable"] = retryable
        payload = {
            "status": "pending" if retryable else "failed",
            "fallback_reason": message,
            "last_progress_at": now,
            "completed_at": None if retryable else now,
            "lease_owner": None,
            "lease_expires_at": None,
            "selected_backend": None if retryable else job.get("selected_backend"),
            "metadata": metadata,
            "updated_at": now,
        }
        try:
            rows = patch_rows(
                "youtube_factory_jobs",
                payload,
                id=job["id"],
                lease_owner=owner,
            )
            if not rows:
                print(
                    f"WARNING: factory failure checkpoint was not written for job {job['id']}; lease owner changed",
                    file=sys.stderr,
                )
        except Exception as checkpoint_exc:
            print(f"WARNING: could not persist factory failure state: {checkpoint_exc}", file=sys.stderr)

    if variant:
        try:
            variant_payload: dict[str, object] = {"last_error": message, "updated_at": now}
            if not retryable and not variant.get("youtube_video_id") and str(variant.get("status") or "") not in {"uploaded", "uploading"}:
                variant_payload["status"] = "failed"
            patch_rows("youtube_video_variants", variant_payload, id=variant["id"])
        except Exception as checkpoint_exc:
            print(f"WARNING: could not persist variant failure state: {checkpoint_exc}", file=sys.stderr)

    if queue and job and str(job.get("locale") or "") == "pt-BR" and not queue.get("youtube_video_id"):
        try:
            # A PT render owns the coarse queue render state. Returning it to failed on
            # a terminal failure prevents rows from getting stranded forever in rendering.
            queue_status = str(queue.get("status") or "") if retryable else "failed"
            patch_rows(
                "youtube_queue",
                {"status": queue_status, "last_error": message, "updated_at": now},
                id=queue["id"],
            )
        except Exception as checkpoint_exc:
            print(f"WARNING: could not persist queue failure state: {checkpoint_exc}", file=sys.stderr)


def run_factory(args: argparse.Namespace) -> int:
    owner = f"github:{os.environ.get('GITHUB_RUN_ID', 'local')}:{args.lane}"
    job = None
    variant = None
    queue = None
    try:
        job = lease_job(args.locale, owner)
        if not job:
            print(json.dumps({"status": "idle", "lane": args.lane, "locale": args.locale}))
            return 0
        queue = fetch_one("youtube_queue", id=job["queue_id"])
        if not queue:
            raise RuntimeError(f"Queue row not found: {job['queue_id']}")
        variant_id = (job.get("metadata") or {}).get("variant_id")
        if variant_id:
            variant = fetch_one("youtube_video_variants", id=variant_id)
        if not variant:
            variant = fetch_one("youtube_video_variants", queue_id=job["queue_id"], locale=args.locale)
        if not variant:
            raise RuntimeError(f"Video variant not found for queue={job['queue_id']} locale={args.locale}")

        queue_before = str(queue.get("status") or "")
        patch_rows("youtube_queue", {"status": "rendering", "updated_at": utcnow()}, id=job["queue_id"])
        runtime = pathlib.Path(args.runtime_dir)
        runtime.mkdir(parents=True, exist_ok=True)
        job_path = runtime / f"factory-job-{args.lane}.json"
        job_path.write_text(
            json.dumps(build_job_manifest(job, queue, variant, args.lane, args.locale), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["MEDIAFORGE_TTS_PROVIDER"] = "chatterbox"
        subprocess.run(
            [
                sys.executable, args.worker,
                "--bundle", args.bundle,
                "--job", str(job_path),
                "--lane", args.lane,
                "--locale", args.locale,
                "--output-dir", args.output_dir,
            ],
            check=True,
            env=env,
        )

        out = pathlib.Path(args.output_dir)
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        run_key = os.environ.get("GITHUB_RUN_ID", datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
        prefix = f"renders/{job['queue_id']}/{args.locale}/{run_key}"
        storage = s3_client()

        video_uri = storage_upload(storage, out / "video" / "long-form.mp4", f"{prefix}/long-form.mp4")
        audio_uri = storage_upload(storage, out / "audio" / "narration.wav", f"{prefix}/narration.wav")
        manifest_uri = storage_upload(storage, out / "manifest.json", f"{prefix}/manifest.json")
        storage_upload(storage, out / "captions" / "long-form.srt", f"{prefix}/long-form.srt")
        short_uris = []
        for short in sorted((out / "shorts").glob("short-*.mp4")):
            short_uris.append(storage_upload(storage, short, f"{prefix}/shorts/{short.name}"))

        current = str(variant.get("status") or "")
        if current == "thumbnail_ready":
            next_status = "upload_ready"
        elif current in {"uploaded", "uploading"}:
            next_status = current
        else:
            next_status = "video_ready"

        duration = float((manifest.get("metrics") or {}).get("narration_seconds") or 0)
        patch_rows(
            "youtube_video_variants",
            {
                "status": next_status,
                "audio_url": audio_uri,
                "video_url": video_uri,
                "video_duration_seconds": duration,
                "render_version": RENDER_VERSION,
                "last_error": None,
                "updated_at": utcnow(),
            },
            id=variant["id"],
        )
        if args.locale == "pt-BR":
            patch_rows(
                "youtube_queue",
                {
                    "status": queue_before if queue_before != "rendering" else "voice_ready",
                    "audio_url": audio_uri,
                    "video_url": video_uri,
                    "last_error": None,
                    "updated_at": utcnow(),
                },
                id=job["queue_id"],
            )

        metadata = dict(job.get("metadata") or {})
        metadata["result"] = {
            "video_url": video_uri,
            "audio_url": audio_uri,
            "manifest_url": manifest_uri,
            "shorts": short_uris,
            "render_version": RENDER_VERSION,
            "metrics": manifest.get("metrics") or {},
        }
        rows = patch_rows(
            "youtube_factory_jobs",
            {
                "status": "completed",
                "last_progress_at": utcnow(),
                "completed_at": utcnow(),
                "lease_owner": None,
                "lease_expires_at": None,
                "metadata": metadata,
                "updated_at": utcnow(),
            },
            id=job["id"],
            lease_owner=owner,
        )
        if not rows:
            raise RuntimeError("Factory output completed but job lease checkpoint could not be finalized")
        print(json.dumps({"status": "completed", "job_id": job["id"], "lane": args.lane, "video_url": video_uri}))
        return 0
    except Exception as exc:
        mark_failed(job, variant, owner, str(exc), queue=queue)
        raise


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", required=True)
    ap.add_argument("--locale", required=True)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--worker", default="public-worker-template/worker.py")
    ap.add_argument("--runtime-dir", default=".runtime")
    ap.add_argument("--output-dir", required=True)
    args = ap.parse_args()
    raise SystemExit(run_factory(args))


if __name__ == "__main__":
    main()
