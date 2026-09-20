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
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree as ET

VIDEO_EXTENSIONS={'.mp4','.mov','.mkv','.webm','.m4v','.avi'}
INDEX_STEMS={'repositorio gta','catalogo geral','transcricao visual gta vi'}
EXCLUDED_PARTS={'output','outputs','youtube','renders','render','tmp','temp','.git'}
DEFAULT_MAX_ASSET_BYTES=1024*1024*1024

def norm(value:str)->str:
    value=unicodedata.normalize('NFKD',value)
    value=''.join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r'\s+',' ',value.lower().replace('_',' ').replace('-',' ')).strip()

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
    size=path.stat().st_size
    h=hashlib.sha256(); h.update(str(size).encode())
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
            if pos>=0:
                out.append(text[max(0,pos-600):min(len(text),pos+len(needle)+1600)])
        if len(out)>=6: break
    return '\n'.join(out)

def postgrest(base,key,method,path,body=None,params=None,prefer=None):
    url=f"{base.rstrip('/')}/rest/v1/{path}"
    if params: url += '?' + urllib.parse.urlencode(params,safe='(),.*:{}[]')
    headers={'apikey':key,'Content-Type':'application/json','User-Agent':'MediaForge-Dell-Gateway/1.0'}
    if not key.startswith('sb_secret_'): headers['Authorization']=f'Bearer {key}'
    if prefer: headers['Prefer']=prefer
    data=None if body is None else json.dumps(body,ensure_ascii=False).encode()
    req=urllib.request.Request(url,data=data,headers=headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=60) as r:
            raw=r.read(); return json.loads(raw.decode()) if raw else None
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'Supabase {method} {path} failed {exc.code}: {exc.read().decode(errors="ignore")[:500]}')

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
        assets.append({'asset_id':aid,'relative_path':rel,'file_name':p.name,'size_bytes':p.stat().st_size,'duration_seconds':duration_seconds(p),'quick_hash':qh,'source_kind':'dell','approved':True,'description':p.stem,'context':ctx[:16000],'tags':tags,'metadata':{'index_files':[x.name for x in index_files]}})
    return assets,index_files

class State:
    def __init__(self,args): self.args=args

class Handler(BaseHTTPRequestHandler):
    server_version='MediaForgeDellGateway/1.0'
    def log_message(self,fmt,*args): print(f'[gateway] {self.address_string()} - {fmt%args}')
    def _json(self,status,payload):
        raw=json.dumps(payload).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        state=self.server.state
        if self.path=='/health': return self._json(200,{'ok':True,'gateway_id':state.args.gateway_id})
        m=re.fullmatch(r'/asset/([0-9a-fA-F-]{36})/([a-f0-9]{24})',urllib.parse.urlparse(self.path).path)
        if not m: return self._json(404,{'error':'not_found'})
        request_id,asset_id=m.groups(); token=self.headers.get('X-MediaForge-Request-Token','')
        if not token: return self._json(401,{'error':'missing_token'})
        base=state.args.supabase_url; key=state.args.supabase_key
        rows=postgrest(base,key,'GET','mediaforge_asset_requests',params={'select':'request_id,request_token,asset_ids,status,expires_at,gateway_id','request_id':f'eq.{request_id}','request_token':f'eq.{token}','gateway_id':f'eq.{state.args.gateway_id}','limit':'1'}) or []
        if not rows: return self._json(403,{'error':'invalid_request'})
        reqrow=rows[0]
        try: exp=datetime.fromisoformat(reqrow['expires_at'].replace('Z','+00:00'))
        except Exception: return self._json(403,{'error':'bad_expiry'})
        if exp<=datetime.now(timezone.utc) or reqrow.get('status') not in {'pending','active'}: return self._json(403,{'error':'expired_or_closed'})
        if asset_id not in (reqrow.get('asset_ids') or []): return self._json(403,{'error':'asset_not_allowed'})
        assets=postgrest(base,key,'GET','mediaforge_assets',params={'select':'relative_path,file_name,size_bytes,approved','asset_id':f'eq.{asset_id}','approved':'eq.true','limit':'1'}) or []
        if not assets: return self._json(404,{'error':'asset_missing'})
        root=pathlib.Path(state.args.root).resolve(); path=(root/assets[0]['relative_path']).resolve()
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
        try: postgrest(base,key,'PATCH','mediaforge_asset_requests',body={'status':'active','updated_at':datetime.now(timezone.utc).isoformat()},params={'request_id':f'eq.{request_id}'})
        except Exception: pass

def heartbeat(state:State):
    while True:
        try:
            endpoint=''; ep=pathlib.Path(state.args.endpoint_file)
            if ep.exists(): endpoint=ep.read_text(encoding='utf-8',errors='ignore').strip()
            body={'gateway_id':state.args.gateway_id,'public_url':endpoint or None,'status':'online' if endpoint else 'degraded','root_label':r'C:\LeonidanosVideoPipeline','heartbeat_at':datetime.now(timezone.utc).isoformat(),'updated_at':datetime.now(timezone.utc).isoformat(),'metadata':{'pid':os.getpid(),'catalog_mode':'metadata-only'}}
            postgrest(state.args.supabase_url,state.args.supabase_key,'POST','mediaforge_asset_gateways',body=body,prefer='resolution=merge-duplicates,return=minimal')
        except Exception as e: print('[heartbeat]',e)
        time.sleep(20)

def sync_catalog(args):
    root=pathlib.Path(args.root); assets,index_files=scan_catalog(root,args.max_asset_bytes); now=datetime.now(timezone.utc).isoformat()
    for i in range(0,len(assets),100):
        batch=[]
        for item in assets[i:i+100]: item['last_seen_at']=now; item['updated_at']=now; batch.append(item)
        postgrest(args.supabase_url,args.supabase_key,'POST','mediaforge_assets',body=batch,prefer='resolution=merge-duplicates,return=minimal')
    postgrest(args.supabase_url,args.supabase_key,'POST','mediaforge_asset_gateways',body={'gateway_id':args.gateway_id,'status':'degraded','root_label':r'C:\LeonidanosVideoPipeline','heartbeat_at':now,'metadata':{'catalog_assets':len(assets),'index_files':[p.name for p in index_files]},'updated_at':now},prefer='resolution=merge-duplicates,return=minimal')
    print(json.dumps({'status':'catalog_synced','assets':len(assets),'indexes':[p.name for p in index_files]},ensure_ascii=False))

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--root',default=r'C:\LeonidanosVideoPipeline'); ap.add_argument('--gateway-id',default='dell-main'); ap.add_argument('--host',default='127.0.0.1'); ap.add_argument('--port',type=int,default=8765); ap.add_argument('--endpoint-file',default=str(pathlib.Path(os.environ.get('LOCALAPPDATA','.'),'MediaForge','gateway-url.txt'))); ap.add_argument('--sync-catalog',action='store_true'); ap.add_argument('--max-asset-bytes',type=int,default=DEFAULT_MAX_ASSET_BYTES)
    args=ap.parse_args(); args.supabase_url=os.environ.get('SUPABASE_URL','https://rhddgfvtrkmusbvphnlg.supabase.co').strip(); args.supabase_key=os.environ.get('SUPABASE_SECRET_KEY','').strip()
    if not args.supabase_key: raise SystemExit('SUPABASE_SECRET_KEY missing')
    if not pathlib.Path(args.root).is_dir(): raise SystemExit(f'Root not found: {args.root}')
    if args.sync_catalog: sync_catalog(args); return
    state=State(args); threading.Thread(target=heartbeat,args=(state,),daemon=True).start(); server=ThreadingHTTPServer((args.host,args.port),Handler); server.state=state; print(f'Gateway listening on http://{args.host}:{args.port}'); server.serve_forever()
if __name__=='__main__': main()
