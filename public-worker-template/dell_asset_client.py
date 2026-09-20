#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, os, pathlib, re, shutil, urllib.request, uuid
from datetime import datetime, timedelta, timezone
import boto3
from botocore.client import Config

PROJECT_REF='rhddgfvtrkmusbvphnlg'; REGION='us-west-2'; BUCKET='mediaforge-assets'; ENDPOINT=f'https://{PROJECT_REF}.storage.supabase.co/storage/v1/s3'; CONTROL_PREFIX='gateway/dell-main'
STOP={'gta','vi','video','videos','game','jogo','rockstar','games','para','com','uma','que','the','and','this','that','from','into','sobre'}

def s3():
    return boto3.client('s3',endpoint_url=ENDPOINT,region_name=REGION,aws_access_key_id=os.environ['SUPABASE_S3_ACCESS_KEY_ID'],aws_secret_access_key=os.environ['SUPABASE_S3_SECRET_ACCESS_KEY'],config=Config(signature_version='s3v4',request_checksum_calculation='when_required',response_checksum_validation='when_required',s3={'addressing_style':'path','payload_signing_enabled':True}))

def get_json(c,key): return json.loads(c.get_object(Bucket=BUCKET,Key=key)['Body'].read().decode())
def put_json(c,key,p):
    raw=json.dumps(p,separators=(',',':')).encode(); c.put_object(Bucket=BUCKET,Key=key,Body=raw,ContentLength=len(raw),ContentType='application/json',CacheControl='no-store')
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
    c=s3(); script=pathlib.Path(a.script_file).read_text(encoding='utf-8'); query=a.title+'\n'+script
    status=get_json(c,f'{CONTROL_PREFIX}/status.json'); catalog=get_json(c,f'{CONTROL_PREFIX}/catalog.json')
    if status.get('status')!='online' or not status.get('public_url'): raise SystemExit('Dell asset gateway is offline')
    beat=datetime.fromisoformat(str(status.get('heartbeat_at')).replace('Z','+00:00'))
    if datetime.now(timezone.utc)-beat>timedelta(minutes=2): raise SystemExit('Dell asset gateway heartbeat is stale')
    assets=[x for x in catalog.get('assets',[]) if x.get('approved',True)]; ranked=sorted(((score(x,query),x) for x in assets),key=lambda x:x[0],reverse=True); chosen=[x for _,x in ranked[:a.max_assets]]
    if not chosen: raise SystemExit('No approved Dell assets available')
    rid=uuid.uuid4().hex; token=uuid.uuid4().hex+uuid.uuid4().hex; expires=(datetime.now(timezone.utc)+timedelta(minutes=45)).isoformat(); request_key=f'gateway/requests/{rid}.json'
    put_json(c,request_key,{'request_id':rid,'request_token':token,'gateway_id':a.gateway_id,'job_key':a.job_key,'asset_ids':[x['asset_id'] for x in chosen],'expires_at':expires})
    dest=pathlib.Path(a.dest); shutil.rmtree(dest,ignore_errors=True); dest.mkdir(parents=True,exist_ok=True); local=[]
    try:
        for i,item in enumerate(chosen,1):
            suffix=pathlib.Path(item['file_name']).suffix or '.mp4'; out=dest/f'{i:02d}-{item["asset_id"]}{suffix}'; url=f"{status['public_url'].rstrip('/')}/asset/{rid}/{item['asset_id']}"; rq=urllib.request.Request(url,headers={'X-MediaForge-Request-Token':token,'User-Agent':'MediaForge-GitHub/2.0'})
            with urllib.request.urlopen(rq,timeout=1800) as src,out.open('wb') as fh:
                while True:
                    chunk=src.read(8*1024*1024)
                    if not chunk: break
                    fh.write(chunk)
            if out.stat().st_size!=int(item['size_bytes']): raise RuntimeError(f'Size mismatch {item["asset_id"]}')
            local.append({**item,'local_name':out.name}); print(json.dumps({'downloaded':i,'of':len(chosen),'asset_id':item['asset_id'],'bytes':out.stat().st_size}))
        (dest/'Catalogo geral.json').write_text(json.dumps({'assets':local},ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        try: c.delete_object(Bucket=BUCKET,Key=request_key)
        except Exception: pass
    print(json.dumps({'status':'ready','root':str(dest.resolve()),'selected':len(chosen),'request_id':rid}))
if __name__=='__main__': main()
