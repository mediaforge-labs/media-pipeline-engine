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
import threading
import time
import unicodedata
import urllib.parse
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree as ET

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

VIDEO_EXTENSIONS={'.mp4','.mov','.mkv','.webm','.m4v','.avi'}
INDEX_STEMS={'repositorio gta','catalogo geral','transcricao visual gta vi','media catalog'}
EXCLUDED_PARTS={'output','outputs','youtube','renders','render','tmp','temp','.git','fontes_oficiais'}
DEFAULT_MAX_ASSET_BYTES=250*1024*1024
ACTIVE_RELATIVE_ROOT=pathlib.PurePosixPath('media/gta_vi')
ACTIVE_EDIT_SUFFIX='-mudo.mp4'
MIN_UNIQUE_VISUAL_BASELINE=126
PROJECT_REF='rhddgfvtrkmusbvphnlg'
REGION='us-west-2'
BUCKET='mediaforge-assets'
ENDPOINT=f'https://{PROJECT_REF}.storage.supabase.co/storage/v1/s3'
CONTROL_PREFIX='gateway/dell-main'
GATEWAY_VERSION='3.0-active-gta-vi'


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def norm(value:str)->str:
    value=unicodedata.normalize('NFKD',value)
    value=''.join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r'\s+',' ',value.lower().replace('_',' ').replace('-',' ')).strip()


def s3_client():
    access=os.environ.get('SUPABASE_S3_ACCESS_KEY_ID','').strip()
    secret=os.environ.get('SUPABASE_S3_SECRET_ACCESS_KEY','').strip()
    if not access or not secret:
        raise RuntimeError('SUPABASE_S3_ACCESS_KEY_ID / SUPABASE_S3_SECRET_ACCESS_KEY ausentes')
    return boto3.client(
        's3', endpoint_url=ENDPOINT, region_name=REGION,
        aws_access_key_id=access, aws_secret_access_key=secret,
        config=Config(signature_version='s3v4', request_checksum_calculation='when_required',
                      response_checksum_validation='when_required', retries={'max_attempts':8,'mode':'standard'},
                      connect_timeout=20, read_timeout=120,
                      s3={'addressing_style':'path','payload_signing_enabled':True}))


def put_json(s3,key,payload):
    raw=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    s3.put_object(Bucket=BUCKET,Key=key,Body=raw,ContentLength=len(raw),ContentType='application/json',CacheControl='no-store')


def get_json(s3,key):
    try:
        obj=s3.get_object(Bucket=BUCKET,Key=key)
        return json.loads(obj['Body'].read().decode('utf-8'))
    except ClientError as exc:
        code=str((exc.response.get('Error') or {}).get('Code') or '')
        status=(exc.response.get('ResponseMetadata') or {}).get('HTTPStatusCode')
        if code in {'404','NoSuchKey','NotFound'} or status==404:
            return None
        raise


def read_text(path:pathlib.Path)->str:
    s=path.suffix.lower()
    try:
        if s in {'.txt','.md','.csv','.tsv','.json','.jsonl','.html','.htm','.xml'}:
            return path.read_text(encoding='utf-8',errors='ignore')
        if s=='.docx':
            with zipfile.ZipFile(path) as zf:
                root=ET.fromstring(zf.read('word/document.xml'))
            return '\n'.join((n.text or '') for n in root.iter() if n.tag.endswith('}t'))
        if s=='.xlsx':
            with zipfile.ZipFile(path) as zf:
                shared=[]
                if 'xl/sharedStrings.xml' in zf.namelist():
                    root=ET.fromstring(zf.read('xl/sharedStrings.xml'))
                    for si in root.iter():
                        if si.tag.endswith('}si'):
                            shared.append(' '.join((n.text or '') for n in si.iter() if n.tag.endswith('}t')))
                out=[]
                for name in sorted(n for n in zf.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')):
                    root=ET.fromstring(zf.read(name))
                    for cell in root.iter():
                        if not cell.tag.endswith('}c'): continue
                        ctype=cell.attrib.get('t'); v=next((n for n in cell if n.tag.endswith('}v')),None)
                        if v is None or v.text is None: continue
                        if ctype=='s':
                            try: out.append(shared[int(v.text)])
                            except Exception: pass
                        else: out.append(v.text)
                return '\n'.join(out)
        if s=='.pdf':
            try:
                from pypdf import PdfReader
                return '\n'.join((p.extract_text() or '') for p in PdfReader(str(path)).pages)
            except Exception: return ''
    except Exception: return ''
    return ''


def duration_seconds(path:pathlib.Path):
    try:
        p=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],capture_output=True,text=True,check=True,timeout=30)
        return round(float(p.stdout.strip()),3)
    except Exception: return None


def quick_hash(path:pathlib.Path)->str:
    size=path.stat().st_size; h=hashlib.sha256(); h.update(str(size).encode())
    with path.open('rb') as f:
        h.update(f.read(min(1024*1024,size)))
        if size>1024*1024:
            f.seek(max(0,size-1024*1024)); h.update(f.read(1024*1024))
    return h.hexdigest()


def context_for_names(names:list[str],texts:list[str])->str:
    out=[]
    needles=[]
    for value in names:
        p=pathlib.PurePosixPath(value)
        needles.extend([p.name.lower(), p.stem.lower()])
    for text in texts:
        low=text.lower()
        for needle in needles:
            if not needle:
                continue
            pos=low.find(needle)
            if pos>=0:
                out.append(text[max(0,pos-600):min(len(text),pos+len(needle)+1600)])
            if len(out)>=12:
                break
        if len(out)>=12:
            break
    return '\n'.join(out)


def _is_active_edit_file(path:pathlib.Path,active_root:pathlib.Path,max_bytes:int)->bool:
    if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
        return False
    if not path.name.lower().endswith(ACTIVE_EDIT_SUFFIX):
        return False
    rel=path.relative_to(active_root)
    if any(norm(part) in EXCLUDED_PARTS for part in rel.parts[:-1]):
        return False
    try:
        size=path.stat().st_size
    except OSError:
        return False
    return 0 < size <= max_bytes


def scan_catalog(root:pathlib.Path,max_bytes:int):
    active_root=root / 'media' / 'gta_vi'
    if not active_root.is_dir():
        raise RuntimeError(f'Colecao ativa ausente: {active_root}')

    index_files=sorted(
        [p for p in root.rglob('*') if p.is_file() and any(stem==norm(p.stem) or stem in norm(p.stem) for stem in INDEX_STEMS)],
        key=lambda p:p.as_posix().lower(),
    )
    texts=[read_text(p) for p in index_files]

    candidates=sorted(
        [p for p in active_root.rglob('*') if _is_active_edit_file(p,active_root,max_bytes)],
        key=lambda p:p.as_posix().lower(),
    )

    # The activity tree can contain the same visual clip under several semantic folders.
    # Group exact visual duplicates by quick hash and merge all aliases/categories into the
    # metadata, while serving only one canonical file. This preserves semantic richness and
    # guarantees that one visual source cannot be selected twice in the same production.
    groups:dict[str,list[pathlib.Path]]={}
    for p in candidates:
        groups.setdefault(quick_hash(p),[]).append(p)

    assets=[]
    for qh,paths in sorted(groups.items(),key=lambda pair: pair[1][0].as_posix().lower()):
        aliases=sorted(paths,key=lambda p:p.as_posix().lower())
        canonical=aliases[0]
        rel=canonical.relative_to(root).as_posix()
        collection_path=canonical.relative_to(active_root).as_posix()
        alias_rel=[p.relative_to(root).as_posix() for p in aliases]
        alias_collection=[p.relative_to(active_root).as_posix() for p in aliases]
        activity_labels=sorted({p.parent.name for p in aliases if p.parent != active_root})
        ctx=context_for_names(alias_collection,texts)
        aid=hashlib.sha256(qh.encode('utf-8')).hexdigest()[:24]
        tag_source=' '.join([canonical.stem,*activity_labels,*alias_collection,ctx[:12000]])
        tags=sorted(set(re.findall(r'[a-z0-9]{3,}',norm(tag_source))))[:240]
        assets.append({
            'asset_id':aid,
            'relative_path':rel,
            'file_name':canonical.name,
            'size_bytes':canonical.stat().st_size,
            'duration_seconds':duration_seconds(canonical),
            'quick_hash':qh,
            'approved':True,
            'description':canonical.stem,
            'context':ctx[:24000],
            'tags':tags,
            'collection':'media/gta_vi',
            'collection_path':collection_path,
            'kind':'active_edit_asset',
            'audio':'mudo',
            'alias_relative_paths':alias_rel,
            'alias_collection_paths':alias_collection,
            'activity_labels':activity_labels,
            'visual_duplicate_count':len(aliases),
        })

    stats={
        'active_root':active_root.as_posix(),
        'raw_active_edit_files':len(candidates),
        'unique_visual_assets':len(assets),
        'deduplicated_aliases':max(0,len(candidates)-len(assets)),
        'eligible_suffix':ACTIVE_EDIT_SUFFIX,
        'min_unique_visual_baseline':MIN_UNIQUE_VISUAL_BASELINE,
    }
    return assets,index_files,stats


class State:
    def __init__(self,args,assets,catalog_meta=None):
        self.args=args; self.assets={x['asset_id']:x for x in assets}; self.catalog_meta=catalog_meta or {}; self.s3=s3_client()


class Handler(BaseHTTPRequestHandler):
    server_version='MediaForgeDellGateway/3.0'
    def log_message(self,fmt,*args): print(f'[gateway] {self.address_string()} - {fmt%args}')
    def _json(self,status,payload):
        raw=json.dumps(payload).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        state=self.server.state
        if self.path=='/health':
            return self._json(200,{
                'ok':True,
                'gateway_id':state.args.gateway_id,
                'version':GATEWAY_VERSION,
                'catalog_assets':len(state.assets),
                'active_collection':'media/gta_vi',
                'eligible_suffix':ACTIVE_EDIT_SUFFIX,
                'catalog_generated_at':state.catalog_meta.get('generated_at'),
            })
        m=re.fullmatch(r'/asset/([0-9a-f]{32})/([a-f0-9]{24})',urllib.parse.urlparse(self.path).path)
        if not m: return self._json(404,{'error':'not_found'})
        request_id,asset_id=m.groups(); token=self.headers.get('X-MediaForge-Request-Token','')
        if not token: return self._json(401,{'error':'missing_token'})
        reqrow=get_json(state.s3,f'gateway/requests/{request_id}.json')
        if not reqrow or reqrow.get('request_token')!=token or reqrow.get('gateway_id')!=state.args.gateway_id:
            return self._json(403,{'error':'invalid_request'})
        try: exp=datetime.fromisoformat(str(reqrow.get('expires_at') or '').replace('Z','+00:00'))
        except Exception: return self._json(403,{'error':'bad_expiry'})
        if exp<=datetime.now(timezone.utc): return self._json(403,{'error':'expired'})
        if asset_id not in (reqrow.get('asset_ids') or []): return self._json(403,{'error':'asset_not_allowed'})
        item=state.assets.get(asset_id)
        if not item: return self._json(404,{'error':'asset_missing'})
        root=pathlib.Path(state.args.root).resolve(); path=(root/item['relative_path']).resolve()
        try: path.relative_to(root / 'media' / 'gta_vi')
        except Exception: return self._json(403,{'error':'unsafe_or_inactive_path'})
        if not path.is_file() or not path.name.lower().endswith(ACTIVE_EDIT_SUFFIX): return self._json(404,{'error':'file_offline'})
        size=path.stat().st_size; ctype=mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        self.send_response(200); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(size)); self.send_header('Content-Disposition',f'attachment; filename="{path.name}"'); self.end_headers()
        with path.open('rb') as f:
            while True:
                chunk=f.read(8*1024*1024)
                if not chunk: break
                self.wfile.write(chunk)


def heartbeat(state:State):
    while True:
        try:
            endpoint=''; ep=pathlib.Path(state.args.endpoint_file)
            if ep.exists(): endpoint=ep.read_text(encoding='utf-8',errors='ignore').strip()
            put_json(state.s3,f'{CONTROL_PREFIX}/status.json',{
                'gateway_id':state.args.gateway_id,
                'public_url':endpoint or None,
                'status':'online' if endpoint else 'degraded',
                'heartbeat_at':now_iso(),
                'catalog_assets':len(state.assets),
                'gateway_version':GATEWAY_VERSION,
                'active_collection':'media/gta_vi',
                'eligible_suffix':ACTIVE_EDIT_SUFFIX,
            })
        except Exception as e: print('[heartbeat]',e)
        time.sleep(20)


def sync_catalog(args):
    root=pathlib.Path(args.root); assets,index_files,stats=scan_catalog(root,args.max_asset_bytes)
    minimum=max(1,int(os.environ.get('MEDIAFORGE_MIN_UNIQUE_GTA_VI_ASSETS',str(MIN_UNIQUE_VISUAL_BASELINE))))
    if len(assets)<minimum:
        raise SystemExit(
            f'Colecao ativa GTA VI abaixo do minimo de conteudo visual unico: '
            f'unique={len(assets)}, minimum={minimum}, raw_edit_files={stats["raw_active_edit_files"]}. '
            f'Use somente {root / "media" / "gta_vi"} e confirme os arquivos {ACTIVE_EDIT_SUFFIX}.'
        )
    payload={
        'gateway_id':args.gateway_id,
        'gateway_version':GATEWAY_VERSION,
        'generated_at':now_iso(),
        'scope':'media/gta_vi-active-edit-only',
        'assets':assets,
        'indexes':[p.name for p in index_files],
        'max_asset_bytes':args.max_asset_bytes,
        **stats,
    }
    pathlib.Path(args.catalog_file).parent.mkdir(parents=True,exist_ok=True)
    pathlib.Path(args.catalog_file).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    s3=s3_client(); put_json(s3,f'{CONTROL_PREFIX}/catalog.json',payload)
    put_json(s3,f'{CONTROL_PREFIX}/status.json',{
        'gateway_id':args.gateway_id,
        'gateway_version':GATEWAY_VERSION,
        'active_collection':'media/gta_vi',
        'eligible_suffix':ACTIVE_EDIT_SUFFIX,
        'public_url':None,
        'status':'degraded',
        'heartbeat_at':now_iso(),
        'catalog_assets':len(assets),
    })
    print(json.dumps({
        'status':'catalog_synced',
        'scope':payload['scope'],
        'unique_visual_assets':len(assets),
        'raw_active_edit_files':stats['raw_active_edit_files'],
        'deduplicated_aliases':stats['deduplicated_aliases'],
        'indexes':[p.name for p in index_files],
        'max_asset_mb':round(args.max_asset_bytes/1024/1024,1),
    },ensure_ascii=False))


def main():
    cfg=pathlib.Path(os.environ.get('LOCALAPPDATA','.'),'MediaForge')
    ap=argparse.ArgumentParser(); ap.add_argument('--root',default=r'C:\LeonidanosVideoPipeline'); ap.add_argument('--gateway-id',default='dell-main'); ap.add_argument('--host',default='127.0.0.1'); ap.add_argument('--port',type=int,default=8765); ap.add_argument('--endpoint-file',default=str(cfg/'gateway-url.txt')); ap.add_argument('--catalog-file',default=str(cfg/'catalog.json')); ap.add_argument('--sync-catalog',action='store_true'); ap.add_argument('--max-asset-bytes',type=int,default=DEFAULT_MAX_ASSET_BYTES)
    args=ap.parse_args()
    if not pathlib.Path(args.root).is_dir(): raise SystemExit(f'Root not found: {args.root}')
    if args.sync_catalog: sync_catalog(args); return
    p=pathlib.Path(args.catalog_file)
    if not p.exists(): raise SystemExit('Catalogo local ausente. Rode setup novamente.')
    catalog_meta=json.loads(p.read_text(encoding='utf-8')); assets=(catalog_meta.get('assets') or [])
    if catalog_meta.get('scope')!='media/gta_vi-active-edit-only':
        raise SystemExit('Catalogo local antigo/incompativel. Rode setup novamente para reindexar media/gta_vi.')
    state=State(args,assets,catalog_meta); threading.Thread(target=heartbeat,args=(state,),daemon=True).start(); server=ThreadingHTTPServer((args.host,args.port),Handler); server.state=state; print(f'Gateway listening on http://{args.host}:{args.port}'); server.serve_forever()

if __name__=='__main__': main()
