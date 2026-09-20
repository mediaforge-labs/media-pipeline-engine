#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, pathlib, requests, subprocess, tempfile

DEFAULT_QUEUE_ID='530e6f9a-7c9d-4cb8-b0a6-69e4ac8fc00c'

def headers():
    key=os.environ['SUPABASE_SECRET_KEY']; h={'apikey':key,'Content-Type':'application/json','User-Agent':'MediaForge-Canary/1.0'}
    if not key.startswith('sb_secret_'): h['Authorization']=f'Bearer {key}'
    return h

def rows(table,params):
    url=f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/{table}"; r=requests.get(url,headers=headers(),params=params,timeout=60); r.raise_for_status(); return r.json()

def pronounce_pt(text:str)->str:
    replacements=[('crossplay','cross-plêi'),('Crossplay','cross-plêi'),('Jason','jeison'),('JASON','jeison'),('minigame','mini-gueimer'),('mini-game','mini-gueimer'),('Mini-game','mini-gueimer')]
    for a,b in replacements:text=text.replace(a,b)
    return text

def audio_preflight()->None:
    """Fail fast on the FFmpeg audio chain before expensive TTS/render work."""
    with tempfile.TemporaryDirectory(prefix='mediaforge-audio-preflight-') as tmp:
        root=pathlib.Path(tmp)
        narration=root/'narration.wav'; music=root/'music.wav'; mixed=root/'program.wav'
        commands=[
            ['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','sine=frequency=440:sample_rate=24000:duration=0.25','-ac','1','-c:a','pcm_s16le',str(narration)],
            ['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','sine=frequency=220:sample_rate=24000:duration=0.25','-ac','1','-c:a','pcm_s16le',str(music)],
            ['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(narration),'-i',str(music),'-filter_complex','[0:a]volume=1.0[n];[1:a]volume=0.08[m];[n][m]amix=inputs=2:duration=first:dropout_transition=0[a]','-map','[a]','-ar','24000','-ac','1','-c:a','pcm_s16le',str(mixed)],
            ['ffprobe','-hide_banner','-v','error','-select_streams','a:0','-show_entries','stream=codec_name,sample_rate,channels,duration','-of','json',str(mixed)],
        ]
        for cmd in commands:
            subprocess.run(cmd,check=True)
        if not mixed.is_file() or mixed.stat().st_size < 1000:
            raise SystemExit('Audio preflight failed: program.wav missing or too small')
        print(json.dumps({'status':'audio_preflight_ok','music_for_canary':False,'bytes':mixed.stat().st_size},ensure_ascii=False))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--queue-id',default=DEFAULT_QUEUE_ID);ap.add_argument('--output',required=True);ap.add_argument('--script-out',required=True);a=ap.parse_args()
    audio_preflight()
    q=rows('youtube_queue',{'select':'*','id':f'eq.{a.queue_id}','limit':'1'}); v=rows('youtube_video_variants',{'select':'*','queue_id':f'eq.{a.queue_id}','locale':'eq.pt-BR','limit':'1'})
    if not q: raise SystemExit('Queue not found')
    queue=q[0]; variant=v[0] if v else {}; title=(variant.get('youtube_title') or queue.get('youtube_title') or 'Leonidanos').strip(); script=(variant.get('tts_text') or variant.get('script') or queue.get('tts_text') or queue.get('script') or '').strip()
    if len(script.split())<20: raise SystemExit('Script too short')
    tts=pronounce_pt(script); pathlib.Path(a.script_out).write_text(tts,encoding='utf-8')
    job={'version':1,'mode':'production','jobs':{'pt-1':{'id':f'dell-canary-{a.queue_id}','locale':'pt-BR','title':title,'shorts_requested':5,'metadata':{'music_enabled':False,'video_library_enabled':True,'video_library_max_assets':18,'queue_id':a.queue_id,'canary':True,'audio_preflight':True},'script':tts,'media':[]}}}
    pathlib.Path(a.output).write_text(json.dumps(job,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps({'status':'ready','queue_id':a.queue_id,'title':title,'words':len(tts.split()),'music_enabled':False},ensure_ascii=False))
if __name__=='__main__':main()
