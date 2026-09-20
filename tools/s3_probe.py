#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import uuid

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

PROJECT_REF = "rhddgfvtrkmusbvphnlg"
REGION = "us-west-2"
BUCKET = "mediaforge-assets"
ENDPOINT = f"https://{PROJECT_REF}.storage.supabase.co/storage/v1/s3"


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Variavel ausente: {name}")
    return value


def client():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        region_name=REGION,
        aws_access_key_id=required("SUPABASE_S3_ACCESS_KEY_ID"),
        aws_secret_access_key=required("SUPABASE_S3_SECRET_ACCESS_KEY"),
        config=Config(
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=20,
            read_timeout=60,
            s3={
                "addressing_style": "path",
                "payload_signing_enabled": True,
            },
        ),
    )


def describe_error(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        response = exc.response or {}
        err = response.get("Error") or {}
        meta = response.get("ResponseMetadata") or {}
        return json.dumps(
            {
                "type": "ClientError",
                "code": err.get("Code"),
                "message": err.get("Message"),
                "http_status": meta.get("HTTPStatusCode"),
                "request_id": meta.get("RequestId"),
                "endpoint": ENDPOINT,
                "region": REGION,
                "bucket": BUCKET,
            },
            ensure_ascii=False,
        )
    return f"{type(exc).__name__}: {exc}"


def main() -> int:
    s3 = client()
    key = f"video-library/_health/s3-probe-{uuid.uuid4().hex}.txt"
    payload = b"mediaforge-s3-probe-v2\n" * 64

    try:
        s3.head_bucket(Bucket=BUCKET)
        s3.put_object(
            Bucket=BUCKET,
            Key=key,
            Body=payload,
            ContentLength=len(payload),
            ContentType="text/plain",
            CacheControl="no-store",
        )
        head = s3.head_object(Bucket=BUCKET, Key=key)
        remote_size = int(head.get("ContentLength", -1))
        if remote_size != len(payload):
            raise RuntimeError(
                f"Probe enviado, mas tamanho divergente: local={len(payload)} remoto={remote_size}"
            )
        s3.delete_object(Bucket=BUCKET, Key=key)
        print(
            json.dumps(
                {
                    "ok": True,
                    "endpoint": ENDPOINT,
                    "region": REGION,
                    "bucket": BUCKET,
                    "put_object": "ok",
                    "head_object": "ok",
                    "delete_object": "ok",
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (ClientError, BotoCoreError, RuntimeError) as exc:
        try:
            s3.delete_object(Bucket=BUCKET, Key=key)
        except Exception:
            pass
        print("S3 PREFLIGHT ERROR: " + describe_error(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
