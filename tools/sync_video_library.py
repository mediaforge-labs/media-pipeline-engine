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
from boto3.s3.transfer import TransferConfig
from botocore.client import Config
from botocore.exceptions import ClientError

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.avi'}
INDEX_STEMS = {'repositorio gta', 'catalogo geral', 'transcricao visual gta vi'}
BUCKET = 'mediaforge-assets'
PREFIX = 'video-library'
PROJECT_REF = 'rhddgfvtrkmusbvphnlg'
REGION = 'us-west-2'
S3_ENDPOINT = f'https://{PROJECT_REF}.storage.supabase.co/storage/v1/s3'

# Multipart uploads are substantially more reliable for the large curated video
# library than the legacy REST object endpoint. Boto3 will transparently split
# large files into parts and retry failed requests.
TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=64 * 1024 * 1024,
    multipart_chunksize=64 * 1024 * 1024,
    max_concurrency=4,
    use_threads=True,
)


def norm(value: str) -> str:
    value = unicodedata.normalize('NFKD', value)
    value = ''.join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r'\s+', ' ', value.lower().replace('_', ' ').replace('-', ' ')).strip()


def read_text(path: pathlib.Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix in {'.txt', '.md', '.csv', '.tsv', '.json', '.jsonl', '.html', '.htm', '.xml'}:
            return path.read_text(encoding='utf-8', errors='ignore')
        if suffix == '.docx':
            with zipfile.ZipFile(path) as zf:
                root = ET.fromstring(zf.read('word/document.xml'))
            return '\n'.join((n.text or '') for n in root.iter() if n.tag.endswith('}t'))
        if suffix == '.xlsx':
            with zipfile.ZipFile(path) as zf:
                shared = []
                if 'xl/sharedStrings.xml' in zf.namelist():
                    root = ET.fromstring(zf.read('xl/sharedStrings.xml'))
                    for si in root.iter():
                        if si.tag.endswith('}si'):
                            shared.append(' '.join((n.text or '') for n in si.iter() if n.tag.endswith('}t')))
                values = []
                for name in sorted(n for n in zf.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')):
                    root = ET.fromstring(zf.read(name))
                    for cell in root.iter():
                        if not cell.tag.endswith('}c'):
                            continue
                        ctype = cell.attrib.get('t')
                        v = next((n for n in cell if n.tag.endswith('}v')), None)
                        if v is None or v.text is None:
                            continue
                        if ctype == 's':
                            try:
                                values.append(shared[int(v.text)])
                            except Exception:
                                pass
                        else:
                            values.append(v.text)
                return '\n'.join(values)
    except Exception:
        return ''
    return ''


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
    return '\n'.join(contexts)


def duration_seconds(path: pathlib.Path) -> float | None:
    try:
        proc = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        return round(float(proc.stdout.strip()), 3)
    except Exception:
        return None


def s3_client():
    access_key = os.environ.get('SUPABASE_S3_ACCESS_KEY_ID', '').strip()
    secret_key = os.environ.get('SUPABASE_S3_SECRET_ACCESS_KEY', '').strip()
    if not access_key or not secret_key:
        raise SystemExit('Set SUPABASE_S3_ACCESS_KEY_ID and SUPABASE_S3_SECRET_ACCESS_KEY')
    return boto3.client(
        's3',
        endpoint_url=S3_ENDPOINT,
        region_name=REGION,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}),
    )


def object_size(client, storage_path: str) -> int | None:
    try:
        response = client.head_object(Bucket=BUCKET, Key=storage_path)
        return int(response.get('ContentLength', -1))
    except ClientError as exc:
        code = str(exc.response.get('Error', {}).get('Code', ''))
        status = int(exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0) or 0)
        if code in {'404', 'NoSuchKey', 'NotFound'} or status == 404:
            return None
        raise


def upload(client, local: pathlib.Path, storage_path: str, content_type: str | None = None) -> str:
    local_size = local.stat().st_size
    remote_size = object_size(client, storage_path)
    if remote_size == local_size:
        return 'skipped'

    extra = {
        'ContentType': content_type or mimetypes.guess_type(local.name)[0] or 'application/octet-stream',
        'CacheControl': '3600',
    }
    client.upload_file(
        Filename=str(local),
        Bucket=BUCKET,
        Key=storage_path,
        ExtraArgs=extra,
        Config=TRANSFER_CONFIG,
    )
    uploaded_size = object_size(client, storage_path)
    if uploaded_size != local_size:
        raise RuntimeError(f'Upload verification failed for {storage_path}: local={local_size}, remote={uploaded_size}')
    return 'uploaded'


def upload_bytes(client, data: bytes, storage_path: str, content_type: str = 'application/json') -> None:
    client.put_object(
        Bucket=BUCKET,
        Key=storage_path,
        Body=data,
        ContentType=content_type,
        CacheControl='300',
    )


def safe_rel(path: pathlib.Path, root: pathlib.Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def is_index_candidate(path: pathlib.Path) -> bool:
    candidate = norm(path.stem)
    return any(stem == candidate or stem in candidate for stem in INDEX_STEMS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=os.environ.get('LEONIDANOS_MEDIA_ROOT') or r'C:\LeonidanosVideoPipeline')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    root = pathlib.Path(args.root)
    if not root.is_dir():
        raise SystemExit(f'Library root not found: {root}')

    index_files = sorted(
        [p for p in root.rglob('*') if p.is_file() and is_index_candidate(p)],
        key=lambda p: p.name.lower(),
    )
    index_texts = [read_text(p) for p in index_files]
    videos = sorted(
        [p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS],
        key=lambda p: p.as_posix().lower(),
    )

    print(f'Found {len(videos)} videos and {len(index_files)} index files')
    if index_files:
        print('Index files: ' + ' | '.join(safe_rel(p, root) for p in index_files))
    if len(index_files) < 3:
        print('WARNING: expected 3 named index files; video sync will continue and missing indexes can be added later.')

    client = None if args.dry_run else s3_client()
    assets = []
    uploaded = 0
    skipped = 0

    for i, video in enumerate(videos, 1):
        rel = safe_rel(video, root)
        digest = hashlib.sha256(rel.encode('utf-8')).hexdigest()[:16]
        storage_path = f'{PREFIX}/videos/{digest}-{video.name}'
        ctx = context_for(video, index_texts)
        item = {
            'name': video.name,
            'relative_path': rel,
            'storage_path': storage_path,
            'size_bytes': video.stat().st_size,
            'duration_seconds': duration_seconds(video),
            'context': ctx,
            'tags': sorted(set(re.findall(r'[A-Za-zÀ-ÿ0-9]{4,}', norm(f'{video.stem} {ctx[:5000]}'))))[:120],
            'approved': True,
        }
        assets.append(item)

        if not args.dry_run:
            result = upload(client, video, storage_path)
            if result == 'skipped':
                skipped += 1
                print(f'[{i}/{len(videos)}] skip existing {rel}')
            else:
                uploaded += 1
                print(f'[{i}/{len(videos)}] uploaded {rel}')

    index_entries = []
    for path, text in zip(index_files, index_texts):
        rel = safe_rel(path, root)
        digest = hashlib.sha256(rel.encode('utf-8')).hexdigest()[:16]
        storage_path = f'{PREFIX}/indexes/{digest}-{path.name}'
        index_entries.append({'name': path.name, 'relative_path': rel, 'storage_path': storage_path, 'chars': len(text)})
        if not args.dry_run:
            upload(client, path, storage_path)

    catalog = {
        'version': 3,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source_root_label': 'C:\\LeonidanosVideoPipeline',
        'owner_approved': True,
        'storage_backend': 'supabase-s3',
        'index_files': index_entries,
        'assets': assets,
    }
    payload = (json.dumps(catalog, ensure_ascii=False, indent=2) + '\n').encode('utf-8')

    if args.dry_run:
        out = pathlib.Path('mediaforge-video-library-catalog.json')
        out.write_bytes(payload)
        print(f'Dry run catalog: {out.resolve()}')
    else:
        upload_bytes(client, payload, f'{PREFIX}/catalog.json')
        print(f'Uploaded catalog with {len(assets)} approved videos')
        print(f'Sync summary: uploaded={uploaded}, skipped_existing={skipped}')


if __name__ == '__main__':
    main()
