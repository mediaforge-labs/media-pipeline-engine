#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, os, pathlib, re, shutil, urllib.parse, urllib.request, uuid
from datetime import datetime, timedelta, timezone

STOP={'gta','vi','video','videos','game','jogo','rockstar','games','para','com','uma','que','the','and','this','that','from','into','sobre'}

def req_json(method,path,body=None,params=None,prefer=None):
    base=os.environ['SUPABASE_URL'].rstrip('/'); key=os.environ['SUPABASE_SECRET_KEY']; url=f'{base}/rest/v1/{path}'
    if params:url+='?'+urllib.parse.urlencode(params,safe='(),.*:{}[]')
    h={'apikey':key,'Content-Type':'application/json','User-Agent':'MediaForge-Dell-Client/1.0'}
    if not key.startswith('sb_secret_'):h['Authorization']=f'Bearer {key}'
    if prefer:h['Prefer']=prefer
    data=None if body is None else json.dumps(body).encode(); r=urllib.request.Request(url,data=data,headers=h,method=method)
    with urllib.request.urlopen(r,timeout=60) as x:
        raw=x.read(); return json.loads(raw.decode()) if raw else None

def toks(s):return {x for x in re.findall(r'[a-z0-9]{3,}',s.lower()) if x not in STOP}
def score(asset,query):
    hay=' '.join([asset.get('file_name',''),asset.get('relative_path',''),asset.get('description',''),asset.get('context',''),' '.join(asset.get('tags') or [])]).lower(); q=toks(query); h=toks(hay); val=len(q & h)*10
    concepts={'esporte':['sport','esporte','basquete','academia','corrida','luta','bilhar','golfe','paraqued','caiaque','mergulho','jet','moto'], 'noite':['noite','boate','neon','night'], 'praia':['praia','pantano','natureza','beach'], 'lucia':['lucia'],'jason':['jason']}
    ql=query.lower()
    for terms in concepts.values():
        if any(t in ql for t in terms) and any(t in hay for t in terms):val+=8
    val += int(hashlib.sha256((query+asset['asset_id']).encode()).hexdigest()[:4],16)/65535
    return val

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--title',required=True);ap.add_argument('--script-file',required=True);ap.add_argument('--dest',required=True);ap.add_argument('--max-assets',type=int,default=18);ap.add_argument('--gateway-id',default='dell-main');ap.add_argument('--job-key',default='manual')
    a=ap.parse_args(); script=pathlib.Path(a.script_file).read_text(encoding='utf-8');query=a.title+'\n'+script
    gw=(req_json('GET','mediaforge_asset_gateways',params={'select':'gateway_id,public_url,status,heartbeat_at','gateway_id':f'eq.{a.gateway_id}','limit':'1'}) or [])
    if not gw or gw[0].get('status')!='online' or not gw[0].get('public_url'):raise SystemExit('Dell asset gateway is offline')
    try:
        beat=datetime.fromisoformat(str(gw[0].get('heartbeat_at') or '').replace('Z','+00:00'))
    except Exception:
        raise SystemExit('Dell asset gateway heartbeat is invalid')
    if datetime.now(timezone.utc)-beat>timedelta(minutes=2):
        raise SystemExit('Dell asset gateway heartbeat is stale')
    assets=req_json('GET','mediaforge_assets',params={'select':'asset_id,relative_path,file_name,size_bytes,duration_seconds,description,context,tags','approved':'eq.true','source_kind':'eq.dell','limit':'2000'}) or []
    ranked=sorted(((score(x,query),x) for x in assets),key=lambda x:x[0],reverse=True); chosen=[x for _,x in ranked[:a.max_assets]]
    if not chosen:raise SystemExit('No approved Dell assets available')
    token=uuid.uuid4().hex+uuid.uuid4().hex; expires=(datetime.now(timezone.utc)+timedelta(minutes=45)).isoformat(); body={'request_token':token,'gateway_id':a.gateway_id,'job_key':a.job_key,'asset_ids':[x['asset_id'] for x in chosen],'status':'pending','expires_at':expires,'metadata':{'title':a.title}}
    row=req_json('POST','mediaforge_asset_requests',body=body,prefer='return=representation') or []; rid=row[0]['request_id'] if row else None
    if not rid:raise SystemExit('Failed to create Dell asset request')
    dest=pathlib.Path(a.dest); shutil.rmtree(dest,ignore_errors=True);dest.mkdir(parents=True,exist_ok=True)
    catalog=[]
    try:
        for i,item in enumerate(chosen,1):
            suffix=pathlib.Path(item['file_name']).suffix or '.mp4'; out=dest/f'{i:02d}-{item["asset_id"]}{suffix}'
            url=f"{gw[0]['public_url'].rstrip('/')}/asset/{rid}/{item['asset_id']}"; rq=urllib.request.Request(url,headers={'X-MediaForge-Request-Token':token,'User-Agent':'MediaForge-GitHub/1.0'})
            with urllib.request.urlopen(rq,timeout=1800) as src,out.open('wb') as fh:
                while True:
                    chunk=src.read(8*1024*1024)
                    if not chunk:break
                    fh.write(chunk)
            if out.stat().st_size!=int(item['size_bytes']):raise RuntimeError(f'Size mismatch {item["asset_id"]}')
            catalog.append({**item,'local_name':out.name})
            print(json.dumps({'downloaded':i,'of':len(chosen),'asset_id':item['asset_id'],'bytes':out.stat().st_size}))
        (dest/'Catalogo geral.json').write_text(json.dumps({'assets':catalog},ensure_ascii=False,indent=2),encoding='utf-8')
        req_json('PATCH','mediaforge_asset_requests',body={'status':'completed','completed_at':datetime.now(timezone.utc).isoformat(),'updated_at':datetime.now(timezone.utc).isoformat()},params={'request_id':f'eq.{rid}'})
    except Exception:
        req_json('PATCH','mediaforge_asset_requests',body={'status':'failed','updated_at':datetime.now(timezone.utc).isoformat()},params={'request_id':f'eq.{rid}'})
        raise
    print(json.dumps({'status':'ready','root':str(dest.resolve()),'selected':len(chosen),'request_id':rid}))
if __name__=='__main__':main()
