#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, os, pathlib, re, shutil, urllib.request, uuid
from datetime import datetime, timedelta, timezone
import requests

STOP={'gta','vi','video','videos','game','jogo','rockstar','games','para','com','uma','que','the','and','this','that','from','into','sobre'}

def control(action:str, body:dict|None=None):
    base=os.environ['SUPABASE_URL'].rstrip('/')
    key=os.environ['SUPABASE_SECRET_KEY']
    url=f'{base}/functions/v1/mediaforge-dell-control'
    headers={'apikey':key,'Content-Type':'application/json','User-Agent':'MediaForge-GitHub/3.0'}
    if body is None:
        r=requests.get(url,headers=headers,params={'action':action},timeout=60)
    else:
        r=requests.post(url,headers=headers,json={'action':action,**body},timeout=60)
    if r.status_code>=400:
        raise RuntimeError(f'Dell control failed ({r.status_code}): {r.text[:500]}')
    return r.json()

def toks(s): return {x for x in re.findall(r'[a-z0-9]{3,}',s.lower()) if x not in STOP}
def score(asset,query):
    hay=' '.join([asset.get('file_name',''),asset.get('relative_path',''),asset.get('description',''),asset.get('context',''),' '.join(asset.get('tags') or [])]).lower(); q=toks(query); h=toks(hay); val=len(q&h)*10
    concepts=[['sport','esporte','basquete','academia','corrida','luta','bilhar','golfe','paraqued','caiaque','mergulho','jet','moto'],['noite','boate','neon','night'],['praia','pantano','natureza','beach'],['lucia'],['jason']]
    ql=query.lower()
    for terms in concepts:
        if any(t in ql for t in terms) and any(t in hay for t in terms): val+=8
    val+=int(hashlib.sha256((query+asset['asset_id']).encode()).hexdigest()[:4],16)/65535
    return val

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--title',required=True); ap.add_argument('--script-file',required=True); ap.add_argument('--dest',required=True); ap.add_argument('--max-assets',type=int,default=18); ap.add_argument('--gateway-id',default='dell-main'); ap.add_argument('--job-key',default='manual'); a=ap.parse_args()
    script=pathlib.Path(a.script_file).read_text(encoding='utf-8'); query=a.title+'\n'+script
    status=control('status'); catalog=control('catalog')
    if status.get('status')!='online' or not status.get('public_url'): raise SystemExit('Dell asset gateway is offline')
    beat=datetime.fromisoformat(str(status.get('heartbeat_at')).replace('Z','+00:00'))
    if datetime.now(timezone.utc)-beat>timedelta(minutes=2): raise SystemExit('Dell asset gateway heartbeat is stale')
    assets=[x for x in catalog.get('assets',[]) if x.get('approved',True)]; ranked=sorted(((score(x,query),x) for x in assets),key=lambda x:x[0],reverse=True); chosen=[x for _,x in ranked[:a.max_assets]]
    if not chosen: raise SystemExit('No approved Dell assets available')
    rid=uuid.uuid4().hex; token=uuid.uuid4().hex+uuid.uuid4().hex; expires=(datetime.now(timezone.utc)+timedelta(minutes=45)).isoformat()
    control('create_request',{'request_id':rid,'request_token':token,'job_key':a.job_key,'asset_ids':[x['asset_id'] for x in chosen],'expires_at':expires})
    dest=pathlib.Path(a.dest); shutil.rmtree(dest,ignore_errors=True); dest.mkdir(parents=True,exist_ok=True); local=[]
    try:
        for i,item in enumerate(chosen,1):
            suffix=pathlib.Path(item['file_name']).suffix or '.mp4'; out=dest/f'{i:02d}-{item["asset_id"]}{suffix}'; url=f"{status['public_url'].rstrip('/')}/asset/{rid}/{item['asset_id']}"; rq=urllib.request.Request(url,headers={'X-MediaForge-Request-Token':token,'User-Agent':'MediaForge-GitHub/3.0'})
            with urllib.request.urlopen(rq,timeout=1800) as src,out.open('wb') as fh:
                while True:
                    chunk=src.read(8*1024*1024)
                    if not chunk: break
                    fh.write(chunk)
            if out.stat().st_size!=int(item['size_bytes']): raise RuntimeError(f'Size mismatch {item["asset_id"]}')
            local.append({**item,'local_name':out.name}); print(json.dumps({'downloaded':i,'of':len(chosen),'asset_id':item['asset_id'],'bytes':out.stat().st_size}))
        (dest/'Catalogo geral.json').write_text(json.dumps({'assets':local},ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        try: control('delete_request',{'request_id':rid})
        except Exception: pass
    print(json.dumps({'status':'ready','root':str(dest.resolve()),'selected':len(chosen),'request_id':rid}))
if __name__=='__main__': main()
