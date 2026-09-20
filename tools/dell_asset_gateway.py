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
INDEX_STEMS={'repositorio gta','catalogo geral','transcricao visual gta vi'}
EXCLUDED_PARTS={'output','outputs','youtube','renders','render','tmp','temp','.git'}
DEFAULT_MAX_ASSET_BYTES=250*1024*1024
PROJECT_REF='rhddgfvtrkmusbvphnlg'
REGION='us-west-2'
BUCKET='mediaforge-assets'
ENDPOINT=f'https://{PROJECT_REF}.storage.supabase.co/storage/v1/s3'
CONTROL_PREFIX='gateway/dell-main'


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


def context_for(video:pathlib.Path,texts:list[str])->str:
    out=[]
    for text in texts:
        low=text.lower()
        for needle in (video.name.lower(),video.stem.lower()):
            pos=low.find(needle)
            if pos>=0: out.append(text[max(0,pos-600):min(len(text),pos+len(needle)+1600)])
        if len(out)>=6: break
    return '\n'.join(out)


def scan_catalog(root:pathlib.Path,max_bytes:int):
    index_files=sorted([p for p in root.rglob('*') if p.is_file() and any(stem==norm(p.stem) or stem in norm(p.stem) for stem in INDEX_STEMS)],key=lambda p:p.name.lower())
    texts=[read_text(p) for p in index_files]
    candidates=[]
    for p in root.rglob('*'):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTENSIONS: continue
        rel=p.relative_to(root)
        if any(norm(part) in EXCLUDED_PARTS for part in rel.parts[:-1]): continue
        size=p.stat().st_size
        if size<=0 or size>max_bytes: continue
        candidates.append(p)
    seen={}; assets=[]
    for p in sorted(candidates,key=lambda x:x.as_posix().lower()):
        qh=quick_hash(p)
        if qh in seen: continue
        seen[qh]=p
        rel=p.relative_to(root).as_posix(); ctx=context_for(p,texts)
        aid=hashlib.sha256(rel.encode('utf-8')).hexdigest()[:24]
        tags=sorted(set(re.findall(r'[a-z0-9]{3,}',norm(f'{p.stem} {p.parent.name} {ctx[:8000]}'))))[:160]
        assets.append({'asset_id':aid,'relative_path':rel,'file_name':p.name,'size_bytes':p.stat().st_size,'duration_seconds':duration_seconds(p),'quick_hash':qh,'approved':True,'description':p.stem,'context':ctx[:16000],'tags':tags})
    return assets,index_files


class State:
    def __init__(self,args,assets):
        self.args=args; self.assets={x['asset_id']:x for x in assets}; self.s3=s3_client()


class Handler(BaseHTTPRequestHandler):
    server_version='MediaForgeDellGateway/2.0'
    def log_message(self,fmt,*args): print(f'[gateway] {self.address_string()} - {fmt%args}')
    def _json(self,status,payload):
        raw=json.dumps(payload).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        state=self.server.state
        if self.path=='/health': return self._json(200,{'ok':True,'gateway_id':state.args.gateway_id})
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
        try: path.relative_to(root)
        except Exception: return self._json(403,{'error':'unsafe_path'})
        if not path.is_file(): return self._json(404,{'error':'file_offline'})
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
            put_json(state.s3,f'{CONTROL_PREFIX}/status.json',{'gateway_id':state.args.gateway_id,'public_url':endpoint or None,'status':'online' if endpoint else 'degraded','heartbeat_at':now_iso(),'catalog_assets':len(state.assets)})
        except Exception as e: print('[heartbeat]',e)
        time.sleep(20)


def sync_catalog(args):
    root=pathlib.Path(args.root); assets,index_files=scan_catalog(root,args.max_asset_bytes)
    payload={'gateway_id':args.gateway_id,'generated_at':now_iso(),'assets':assets,'indexes':[p.name for p in index_files],'max_asset_bytes':args.max_asset_bytes}
    pathlib.Path(args.catalog_file).parent.mkdir(parents=True,exist_ok=True)
    pathlib.Path(args.catalog_file).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    s3=s3_client(); put_json(s3,f'{CONTROL_PREFIX}/catalog.json',payload)
    put_json(s3,f'{CONTROL_PREFIX}/status.json',{'gateway_id':args.gateway_id,'public_url':None,'status':'degraded','heartbeat_at':now_iso(),'catalog_assets':len(assets)})
    print(json.dumps({'status':'catalog_synced','assets':len(assets),'indexes':[p.name for p in index_files],'max_asset_mb':round(args.max_asset_bytes/1024/1024,1)},ensure_ascii=False))


def main():
    cfg=pathlib.Path(os.environ.get('LOCALAPPDATA','.'),'MediaForge')
    ap=argparse.ArgumentParser(); ap.add_argument('--root',default=r'C:\LeonidanosVideoPipeline'); ap.add_argument('--gateway-id',default='dell-main'); ap.add_argument('--host',default='127.0.0.1'); ap.add_argument('--port',type=int,default=8765); ap.add_argument('--endpoint-file',default=str(cfg/'gateway-url.txt')); ap.add_argument('--catalog-file',default=str(cfg/'catalog.json')); ap.add_argument('--sync-catalog',action='store_true'); ap.add_argument('--max-asset-bytes',type=int,default=DEFAULT_MAX_ASSET_BYTES)
    args=ap.parse_args()
    if not pathlib.Path(args.root).is_dir(): raise SystemExit(f'Root not found: {args.root}')
    if args.sync_catalog: sync_catalog(args); return
    p=pathlib.Path(args.catalog_file)
    if not p.exists(): raise SystemExit('Catalogo local ausente. Rode setup novamente.')
    assets=(json.loads(p.read_text(encoding='utf-8')).get('assets') or [])
    state=State(args,assets); threading.Thread(target=heartbeat,args=(state,),daemon=True).start(); server=ThreadingHTTPServer((args.host,args.port),Handler); server.state=state; print(f'Gateway listening on http://{args.host}:{args.port}'); server.serve_forever()

if __name__=='__main__': main()
