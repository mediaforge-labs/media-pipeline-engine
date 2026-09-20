#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import pathlib
import re
import subprocess
import unicodedata
import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
BUCKET = "mediaforge-assets"
PREFIX = "video-library"
PROJECT_REF = "rhddgfvtrkmusbvphnlg"
REGION = "us-west-2"
S3_ENDPOINT = f"https://{PROJECT_REF}.storage.supabase.co/storage/v1/s3"
PART_SIZE = 64 * 1024 * 1024
SINGLE_PUT_LIMIT = 64 * 1024 * 1024

INDEX_RULES = (
    ("repositorio gta", ("repositorio", "gta")),
    ("catalogo geral", ("catalogo", "geral")),
    ("transcricao visual gta vi", ("transcricao", "visual", "gta")),
)


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", value.lower().replace("_", " ").replace("-", " ")).strip()


def is_index_candidate(path: pathlib.Path) -> bool:
    candidate = norm(path.stem)
    words = set(candidate.split())
    return any(all(token in words for token in tokens) for _, tokens in INDEX_RULES)


def read_text(path: pathlib.Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix in {".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".html", ".htm", ".xml"}:
            return path.read_text(encoding="utf-8", errors="ignore")
        if suffix == ".docx":
            with zipfile.ZipFile(path) as zf:
                root = ET.fromstring(zf.read("word/document.xml"))
            return "\n".join((n.text or "") for n in root.iter() if n.tag.endswith("}t"))
        if suffix == ".xlsx":
            with zipfile.ZipFile(path) as zf:
                shared = []
                if "xl/sharedStrings.xml" in zf.namelist():
                    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                    for si in root.iter():
                        if si.tag.endswith("}si"):
                            shared.append(" ".join((n.text or "") for n in si.iter() if n.tag.endswith("}t")))
                values = []
                sheets = sorted(
                    n for n in zf.namelist()
                    if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
                )
                for name in sheets:
                    root = ET.fromstring(zf.read(name))
                    for cell in root.iter():
                        if not cell.tag.endswith("}c"):
                            continue
                        ctype = cell.attrib.get("t")
                        val = next((n for n in cell if n.tag.endswith("}v")), None)
                        if val is None or val.text is None:
                            continue
                        if ctype == "s":
                            try:
                                values.append(shared[int(val.text)])
                            except Exception:
                                pass
                        else:
                            values.append(val.text)
                return "\n".join(values)
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return ""
            reader = PdfReader(str(path))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        print(f"WARNING: nao foi possivel ler indice {path.name}: {exc}")
    return ""


def context_for(video: pathlib.Path, texts: list[str]) -> str:
    contexts = []
    needles = (video.name.lower(), video.stem.lower())
    for text in texts:
        low = text.lower()
        for needle in needles:
            start = 0
            while True:
                pos = low.find(needle, start)
                if pos < 0:
                    break
                contexts.append(text[max(0, pos - 500):min(len(text), pos + len(needle) + 1200)])
                start = pos + max(1, len(needle))
                if len(contexts) >= 8:
                    break
            if len(contexts) >= 8:
                break
    return "\n".join(contexts)


def duration_seconds(path: pathlib.Path) -> float | None:
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return round(float(proc.stdout.strip()), 3)
    except Exception:
        return None


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Variavel ausente: {name}")
    return value


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        region_name=REGION,
        aws_access_key_id=required("SUPABASE_S3_ACCESS_KEY_ID"),
        aws_secret_access_key=required("SUPABASE_S3_SECRET_ACCESS_KEY"),
        config=Config(
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 8, "mode": "standard"},
            connect_timeout=30,
            read_timeout=300,
            s3={
                "addressing_style": "path",
                "payload_signing_enabled": True,
            },
        ),
    )


def safe_rel(path: pathlib.Path, root: pathlib.Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def object_size(client, key: str) -> int | None:
    try:
        result = client.head_object(Bucket=BUCKET, Key=key)
        return int(result.get("ContentLength", -1))
    except ClientError as exc:
        error = exc.response.get("Error") or {}
        status = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if str(error.get("Code")) in {"404", "NoSuchKey", "NotFound"} or status == 404:
            return None
        raise


def direct_put(client, local: pathlib.Path, key: str, content_type: str) -> None:
    size = local.stat().st_size
    with local.open("rb") as fh:
        client.put_object(
            Bucket=BUCKET,
            Key=key,
            Body=fh,
            ContentLength=size,
            ContentType=content_type,
            CacheControl="3600",
        )


def multipart_put(client, local: pathlib.Path, key: str, content_type: str) -> None:
    created = client.create_multipart_upload(
        Bucket=BUCKET,
        Key=key,
        ContentType=content_type,
        CacheControl="3600",
    )
    upload_id = created["UploadId"]
    parts: list[dict] = []
    try:
        with local.open("rb") as fh:
            part_number = 1
            while True:
                chunk = fh.read(PART_SIZE)
                if not chunk:
                    break
                result = client.upload_part(
                    Bucket=BUCKET,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                    ContentLength=len(chunk),
                )
                parts.append({"ETag": result["ETag"], "PartNumber": part_number})
                part_number += 1
        client.complete_multipart_upload(
            Bucket=BUCKET,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        try:
            client.abort_multipart_upload(
                Bucket=BUCKET,
                Key=key,
                UploadId=upload_id,
            )
        except Exception:
            pass
        raise


def upload_file(client, local: pathlib.Path, key: str) -> str:
    size = local.stat().st_size
    remote = object_size(client, key)
    if remote == size:
        return "skipped"

    content_type = mimetypes.guess_type(local.name)[0] or "application/octet-stream"
    if size <= SINGLE_PUT_LIMIT:
        direct_put(client, local, key, content_type)
    else:
        multipart_put(client, local, key, content_type)

    remote = object_size(client, key)
    if remote != size:
        raise RuntimeError(f"Upload verification failed: {key}; local={size}; remote={remote}")
    return "uploaded"


def upload_bytes(client, data: bytes, key: str, content_type: str = "application/json") -> None:
    client.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=data,
        ContentLength=len(data),
        ContentType=content_type,
        CacheControl="300",
    )


def describe_s3_error(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        response = exc.response or {}
        error = response.get("Error") or {}
        meta = response.get("ResponseMetadata") or {}
        return json.dumps(
            {
                "code": error.get("Code"),
                "message": error.get("Message"),
                "http_status": meta.get("HTTPStatusCode"),
                "request_id": meta.get("RequestId"),
            },
            ensure_ascii=False,
        )
    return f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=r"C:\LeonidanosVideoPipeline")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = pathlib.Path(args.root)
    if not root.is_dir():
        raise SystemExit(f"Library root not found: {root}")

    index_files = sorted(
        [p for p in root.rglob("*") if p.is_file() and is_index_candidate(p)],
        key=lambda p: p.as_posix().lower(),
    )
    index_texts = [read_text(p) for p in index_files]
    videos = sorted(
        [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS],
        key=lambda p: p.as_posix().lower(),
    )

    print(f"Found {len(videos)} videos and {len(index_files)} index files")
    if index_files:
        for path in index_files:
            print(f"INDEX: {safe_rel(path, root)}")
    if len(index_files) < 3:
        print("WARNING: menos de 3 indices detectados. O sync de videos continua.")

    client = None if args.dry_run else s3_client()
    assets = []
    uploaded = 0
    skipped = 0

    for i, video in enumerate(videos, 1):
        rel = safe_rel(video, root)
        digest = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:16]
        key = f"{PREFIX}/videos/{digest}-{video.name}"
        ctx = context_for(video, index_texts)
        assets.append(
            {
                "name": video.name,
                "relative_path": rel,
                "storage_path": key,
                "size_bytes": video.stat().st_size,
                "duration_seconds": duration_seconds(video),
                "context": ctx,
                "tags": sorted(
                    set(re.findall(r"[A-Za-zÀ-ÿ0-9]{4,}", norm(f"{video.stem} {ctx[:5000]}")))
                )[:120],
                "approved": True,
            }
        )

        if args.dry_run:
            continue

        try:
            result = upload_file(client, video, key)
        except (ClientError, BotoCoreError, RuntimeError) as exc:
            print(f"UPLOAD ERROR [{i}/{len(videos)}] {rel}: {describe_s3_error(exc)}")
            return 3

        if result == "skipped":
            skipped += 1
            print(f"[{i}/{len(videos)}] skip existing {rel}")
        else:
            uploaded += 1
            print(f"[{i}/{len(videos)}] uploaded {rel}")

    index_entries = []
    for path, text in zip(index_files, index_texts):
        rel = safe_rel(path, root)
        digest = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:16]
        key = f"{PREFIX}/indexes/{digest}-{path.name}"
        index_entries.append(
            {
                "name": path.name,
                "relative_path": rel,
                "storage_path": key,
                "chars": len(text),
            }
        )
        if not args.dry_run:
            try:
                upload_file(client, path, key)
            except (ClientError, BotoCoreError, RuntimeError) as exc:
                print(f"INDEX UPLOAD ERROR {rel}: {describe_s3_error(exc)}")
                return 4

    catalog = {
        "version": 4,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root_label": r"C:\LeonidanosVideoPipeline",
        "owner_approved": True,
        "storage_backend": "supabase-s3-direct",
        "index_files": index_entries,
        "assets": assets,
    }
    payload = (json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    if args.dry_run:
        out = pathlib.Path("mediaforge-video-library-catalog.json")
        out.write_bytes(payload)
        print(f"Dry run catalog: {out.resolve()}")
    else:
        try:
            upload_bytes(client, payload, f"{PREFIX}/catalog.json")
        except (ClientError, BotoCoreError, RuntimeError) as exc:
            print(f"CATALOG UPLOAD ERROR: {describe_s3_error(exc)}")
            return 5
        print(f"Uploaded catalog with {len(assets)} approved videos")
        print(f"Sync summary: uploaded={uploaded}, skipped_existing={skipped}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
