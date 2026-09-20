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
import urllib.parse
import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import requests

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.webm', '.m4v', '.avi'}
INDEX_STEMS = {'repositorio gta', 'catalogo geral', 'transcricao visual gta vi'}
BUCKET = 'mediaforge-assets'
PREFIX = 'video-library'


def norm(value: str) -> str:
    value = unicodedata.normalize('NFKD', value)
    value = ''.join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r'\s+', ' ', value.lower().replace('_', ' ').replace('-', ' ')).strip()


def read_text(path: pathlib.Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix in {'.txt','.md','.csv','.tsv','.json','.jsonl','.html','.htm','.xml'}:
            return path.read_text(encoding='utf-8', errors='ignore')
        if suffix == '.docx':
            with zipfile.ZipFile(path) as zf:
                root = ET.fromstring(zf.read('word/document.xml'))
            return '\n'.join((n.text or '') for n in root.iter() if n.tag.endswith('}t'))
        if suffix == '.xlsx':
            with zipfile.ZipFile(path) as zf:
                shared=[]
                if 'xl/sharedStrings.xml' in zf.namelist():
                    root=ET.fromstring(zf.read('xl/sharedStrings.xml'))
                    for si in root.iter():
                        if si.tag.endswith('}si'):
                            shared.append(' '.join((n.text or '') for n in si.iter() if n.tag.endswith('}t')))
                values=[]
                for name in sorted(n for n in zf.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')):
                    root=ET.fromstring(zf.read(name))
                    for cell in root.iter():
                        if not cell.tag.endswith('}c'): continue
                        ctype=cell.attrib.get('t')
                        v=next((n for n in cell if n.tag.endswith('}v')),None)
                        if v is None or v.text is None: continue
                        if ctype=='s':
                            try: values.append(shared[int(v.text)])
                            except Exception: pass
                        else: values.append(v.text)
                return '\n'.join(values)
    except Exception:
        return ''
    return ''


def context_for(video: pathlib.Path, texts: list[str]) -> str:
    contexts=[]
    needles=(video.name.lower(), video.stem.lower())
    for text in texts:
        low=text.lower()
        for needle in needles:
            start=0
            while True:
                pos=low.find(needle,start)
                if pos<0: break
                contexts.append(text[max(0,pos-500):min(len(text),pos+len(needle)+1200)])
                start=pos+max(1,len(needle))
                if len(contexts)>=8: break
            if len(contexts)>=8: break
    return '\n'.join(contexts)


def duration_seconds(path: pathlib.Path) -> float | None:
    try:
        proc=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],capture_output=True,text=True,check=True)
        return round(float(proc.stdout.strip()),3)
    except Exception:
        return None


def headers() -> dict[str,str]:
    key=(os.environ.get('SUPABASE_SECRET_KEY') or os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or '').strip()
    if not key: raise SystemExit('Set SUPABASE_SECRET_KEY or SUPABASE_SERVICE_ROLE_KEY')
    out={'apikey':key,'User-Agent':'MediaForge-Library-Sync/1.0'}
    if not key.startswith('sb_secret_'): out['Authorization']=f'Bearer {key}'
    return out


def upload(base: str, local: pathlib.Path, storage_path: str, content_type: str | None=None) -> None:
    url=f"{base.rstrip('/')}/storage/v1/object/{BUCKET}/{urllib.parse.quote(storage_path,safe='/')}"
    h=headers(); h['x-upsert']='true'; h['Content-Type']=content_type or mimetypes.guess_type(local.name)[0] or 'application/octet-stream'
    with local.open('rb') as f:
        r=requests.post(url,headers=h,data=f,timeout=(30,7200))
    if r.status_code>=400:
        raise RuntimeError(f'Upload failed {storage_path}: {r.status_code} {r.text[:300]}')


def upload_bytes(base: str, data: bytes, storage_path: str, content_type='application/json') -> None:
    url=f"{base.rstrip('/')}/storage/v1/object/{BUCKET}/{urllib.parse.quote(storage_path,safe='/')}"
    h=headers(); h['x-upsert']='true'; h['Content-Type']=content_type
    r=requests.post(url,headers=h,data=data,timeout=(30,300))
    if r.status_code>=400:
        raise RuntimeError(f'Upload failed {storage_path}: {r.status_code} {r.text[:300]}')


def safe_rel(path: pathlib.Path, root: pathlib.Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def main() -> None:
    ap=argparse.ArgumentParser()
    ap.add_argument('--root', default=os.environ.get('LEONIDANOS_MEDIA_ROOT') or r'C:\LeonidanosVideoPipeline')
    ap.add_argument('--dry-run', action='store_true')
    args=ap.parse_args()
    root=pathlib.Path(args.root)
    if not root.is_dir(): raise SystemExit(f'Library root not found: {root}')
    base=os.environ.get('SUPABASE_URL','').strip()
    if not base: raise SystemExit('Set SUPABASE_URL')

    index_files=[p for p in root.rglob('*') if p.is_file() and norm(p.stem) in INDEX_STEMS]
    index_files=sorted(index_files,key=lambda p:p.name.lower())
    index_texts=[read_text(p) for p in index_files]
    videos=sorted([p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS], key=lambda p:p.as_posix().lower())
    print(f'Found {len(videos)} videos and {len(index_files)} index files')

    assets=[]
    for i,video in enumerate(videos,1):
        rel=safe_rel(video,root)
        digest=hashlib.sha256(rel.encode('utf-8')).hexdigest()[:16]
        storage_path=f'{PREFIX}/videos/{digest}-{video.name}'
        ctx=context_for(video,index_texts)
        item={
            'name':video.name,
            'relative_path':rel,
            'storage_path':storage_path,
            'size_bytes':video.stat().st_size,
            'duration_seconds':duration_seconds(video),
            'context':ctx,
            'tags':sorted(set(re.findall(r'[A-Za-zÀ-ÿ0-9]{4,}', norm(f'{video.stem} {ctx[:5000]}'))))[:120],
            'approved':True,
        }
        assets.append(item)
        if not args.dry_run:
            print(f'[{i}/{len(videos)}] upload {rel}')
            upload(base,video,storage_path)

    index_entries=[]
    for path,text in zip(index_files,index_texts):
        rel=safe_rel(path,root)
        digest=hashlib.sha256(rel.encode('utf-8')).hexdigest()[:16]
        storage_path=f'{PREFIX}/indexes/{digest}-{path.name}'
        index_entries.append({'name':path.name,'relative_path':rel,'storage_path':storage_path,'chars':len(text)})
        if not args.dry_run:
            upload(base,path,storage_path)

    catalog={
        'version':1,
        'generated_at':datetime.now(timezone.utc).isoformat(),
        'source_root_label':'C:\\LeonidanosVideoPipeline',
        'owner_approved':True,
        'index_files':index_entries,
        'assets':assets,
    }
    payload=(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    if args.dry_run:
        out=pathlib.Path('mediaforge-video-library-catalog.json')
        out.write_bytes(payload)
        print(f'Dry run catalog: {out.resolve()}')
    else:
        upload_bytes(base,payload,f'{PREFIX}/catalog.json')
        print(f'Uploaded catalog with {len(assets)} approved videos')

if __name__=='__main__': main()
