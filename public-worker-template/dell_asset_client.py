#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import requests

STOP = {
    'gta','vi','video','videos','game','jogo','rockstar','games','para','com','uma','que',
    'the','and','this','that','from','into','sobre','por','dos','das','nos','nas','seu','sua',
}

CONCEPTS = [
    ['sport','esporte','basquete','academia','musculacao','musculação','corrida','luta','bilhar','golfe','paraqued','caiaque','mergulho','jet','moto'],
    ['noite','boate','neon','night'],
    ['praia','pantano','pântano','natureza','beach'],
    ['lucia','lúcia'],
    ['jason'],
    ['carro','carros','veiculo','veículo','dirigir','corrida'],
    ['arma','armas','tiro','tiroteio','combate'],
    ['policia','polícia','perseguicao','perseguição'],
    ['vice city','cidade','rua','avenida'],
    ['barco','lancha','jetski','jet ski'],
]


def control(action: str, body: dict | None = None):
    base = os.environ['SUPABASE_URL'].rstrip('/')
    key = os.environ['SUPABASE_SECRET_KEY']
    url = f'{base}/functions/v1/mediaforge-dell-control'
    headers = {'apikey': key, 'Content-Type': 'application/json', 'User-Agent': 'MediaForge-GitHub/4.2'}
    if body is None:
        response = requests.get(url, headers=headers, params={'action': action}, timeout=60)
    else:
        response = requests.post(url, headers=headers, json={'action': action, **body}, timeout=60)
    if response.status_code >= 400:
        raise RuntimeError(f'Dell control failed ({response.status_code}): {response.text[:500]}')
    return response.json()


def toks(value: str) -> set[str]:
    return {x for x in re.findall(r'[a-z0-9à-ÿ]{3,}', value.lower()) if x not in STOP}


def asset_text(asset: dict) -> str:
    return ' '.join(
        [
            str(asset.get('file_name') or ''),
            str(asset.get('relative_path') or ''),
            str(asset.get('description') or ''),
            str(asset.get('context') or ''),
            ' '.join(str(x) for x in (asset.get('tags') or [])),
        ]
    ).lower()


def asset_size(asset: dict) -> int:
    return max(0, int(asset.get('size_bytes') or 0))


def score(asset: dict, query: str) -> float:
    hay = asset_text(asset)
    q = toks(query)
    h = toks(hay)
    value = float(len(q & h) * 10)
    ql = query.lower()
    for terms in CONCEPTS:
        if any(term in ql for term in terms) and any(term in hay for term in terms):
            value += 9.0
    value += int(hashlib.sha256((query + str(asset['asset_id'])).encode()).hexdigest()[:4], 16) / 65535
    size_mb = asset_size(asset) / 1024 / 1024
    value -= min(size_mb, 250.0) * 0.004
    return value


def split_sentences(text: str) -> list[str]:
    text = re.sub(r'\s+', ' ', text or '').strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r'(?<=[.!?])\s+', text) if part.strip()]


def segment_script(text: str, target_words: int = 12, max_words: int = 18) -> list[str]:
    units: list[str] = []
    for sentence in split_sentences(text):
        words = sentence.split()
        if len(words) <= max_words:
            units.append(sentence)
            continue
        for start in range(0, len(words), target_words):
            chunk = ' '.join(words[start:start + target_words]).strip()
            if chunk:
                units.append(chunk)

    segments: list[str] = []
    current: list[str] = []
    count = 0
    for unit in units:
        n = len(unit.split())
        if current and count + n > max_words:
            segments.append(' '.join(current))
            current = []
            count = 0
        current.append(unit)
        count += n
        if count >= target_words:
            segments.append(' '.join(current))
            current = []
            count = 0
    if current:
        segments.append(' '.join(current))
    return segments


def choose_unique_pool(title: str, script: str, assets: list[dict], max_assets: int, byte_budget: int):
    if not assets:
        raise SystemExit('No approved Dell assets available')

    segments = segment_script(script)
    if not segments:
        raise SystemExit('Narration could not be segmented for semantic media selection')

    words = max(1, len(script.split()))
    estimated_scene_count = max(len(segments), math.ceil(words / 12))
    required_unique = min(len(assets), estimated_scene_count)
    if required_unique > max_assets:
        raise SystemExit(
            f'Media preflight requires at least {required_unique} unique clips for this narration, '
            f'but --max-assets={max_assets}. Increase the pool before TTS/render.'
        )

    smallest_required = sorted(asset_size(item) for item in assets)[:required_unique]
    minimum_possible_bytes = sum(smallest_required)
    if len(smallest_required) < required_unique or minimum_possible_bytes > byte_budget:
        raise SystemExit(
            f'Media preflight cannot fit {required_unique} unique clips inside the runner byte budget. '
            f'Minimum possible set needs {minimum_possible_bytes / 1024 / 1024 / 1024:.3f} GiB, '
            f'budget is {byte_budget / 1024 / 1024 / 1024:.3f} GiB.'
        )

    target_pool = min(len(assets), max_assets, max(required_unique + 16, 64))
    used: set[str] = set()
    chosen: list[dict] = []
    selection_rows: list[dict] = []
    total_bytes = 0

    def unused_assets() -> list[dict]:
        return [item for item in assets if str(item.get('asset_id')) not in used]

    def candidates_that_preserve_capacity(remaining_slots_after_pick: int) -> list[dict]:
        pool = unused_assets()
        if remaining_slots_after_pick <= 0:
            return [item for item in pool if total_bytes + asset_size(item) <= byte_budget]

        by_size = sorted(pool, key=asset_size)
        if len(by_size) <= remaining_slots_after_pick:
            return []

        reserve_base = by_size[:remaining_slots_after_pick]
        reserve_ids = {str(item['asset_id']) for item in reserve_base}
        reserve_sum = sum(asset_size(item) for item in reserve_base)
        replacement = asset_size(by_size[remaining_slots_after_pick])

        feasible: list[dict] = []
        for item in pool:
            item_id = str(item['asset_id'])
            item_bytes = asset_size(item)
            reserve = reserve_sum
            if item_id in reserve_ids:
                reserve = reserve_sum - item_bytes + replacement
            if total_bytes + item_bytes + reserve <= byte_budget:
                feasible.append(item)
        return feasible

    # Map every real narration segment to one distinct GTA VI clip first.
    for segment_index, segment in enumerate(segments):
        remaining_required_after_pick = max(0, required_unique - len(chosen) - 1)
        candidates = candidates_that_preserve_capacity(remaining_required_after_pick)
        if not candidates:
            break
        query = title + '\n' + segment
        item = max(candidates, key=lambda candidate: score(candidate, query))
        aid = str(item['asset_id'])
        used.add(aid)
        total_bytes += asset_size(item)
        enriched = dict(item)
        original_context = str(enriched.get('context') or '').strip()
        enriched['context'] = (original_context + '\nNarration context: ' + segment).strip()
        enriched['mediaforge_segment_index'] = segment_index
        enriched['mediaforge_segment_text'] = segment
        enriched['mediaforge_selection_score'] = round(score(item, query), 4)
        enriched['mediaforge_max_uses'] = 1
        enriched['mediaforge_gta_vi_only'] = True
        chosen.append(enriched)
        selection_rows.append({'segment_index': segment_index, 'asset_id': aid, 'segment': segment, 'role': 'semantic-scene'})

    # IMPORTANT: required_unique can be slightly larger than len(segments) because the
    # duration-based safety estimate is conservative. The previous v4.1 selector aborted
    # here at 109/112 before it had a chance to add the three reserve clips. Complete the
    # mandatory unique set now, still preserving byte capacity and never reusing a source.
    whole_query = title + '\n' + script
    while len(chosen) < required_unique:
        remaining_required_after_pick = required_unique - len(chosen) - 1
        candidates = candidates_that_preserve_capacity(remaining_required_after_pick)
        if not candidates:
            break
        # For reserve clips, prefer relevance but give size a stronger tie-break so the
        # safety reserve does not consume unnecessary runner disk.
        def reserve_rank(item: dict):
            return (score(item, whole_query), -asset_size(item))
        item = max(candidates, key=reserve_rank)
        aid = str(item['asset_id'])
        used.add(aid)
        total_bytes += asset_size(item)
        enriched = dict(item)
        enriched['mediaforge_max_uses'] = 1
        enriched['mediaforge_gta_vi_only'] = True
        enriched['mediaforge_reserve_asset'] = True
        chosen.append(enriched)
        selection_rows.append({'segment_index': None, 'asset_id': aid, 'segment': None, 'role': 'unique-reserve'})

    if len(chosen) < required_unique:
        raise SystemExit(
            f'Media preflight selected only {len(chosen)} unique clips although {required_unique} are required. '
            f'No reuse was allowed and the remaining candidates could not fit the runner budget.'
        )

    # Optional buffer for scene-planner alternatives. This buffer is best-effort; the
    # mandatory no-reuse capacity above has already been proven and selected.
    remaining = unused_assets()
    remaining.sort(key=lambda item: (score(item, whole_query), -asset_size(item)), reverse=True)
    for item in remaining:
        if len(chosen) >= target_pool:
            break
        item_bytes = asset_size(item)
        if total_bytes + item_bytes > byte_budget:
            continue
        aid = str(item['asset_id'])
        used.add(aid)
        total_bytes += item_bytes
        enriched = dict(item)
        enriched['mediaforge_max_uses'] = 1
        enriched['mediaforge_gta_vi_only'] = True
        enriched['mediaforge_buffer_asset'] = True
        chosen.append(enriched)

    if len({str(item['asset_id']) for item in chosen}) != len(chosen):
        raise SystemExit('Internal media selector error: duplicate asset IDs in chosen pool')

    return chosen, selection_rows, segments, required_unique, total_bytes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--title', required=True)
    parser.add_argument('--script-file', required=True)
    parser.add_argument('--dest', required=True)
    parser.add_argument('--max-assets', type=int, default=140)
    parser.add_argument('--max-total-bytes', type=int, default=8 * 1024 * 1024 * 1024)
    parser.add_argument('--gateway-id', default='dell-main')
    parser.add_argument('--job-key', default='manual')
    args = parser.parse_args()

    script = pathlib.Path(args.script_file).read_text(encoding='utf-8')
    status = control('status')
    catalog = control('catalog')
    if status.get('status') != 'online' or not status.get('public_url'):
        raise SystemExit('Dell asset gateway is offline')
    beat = datetime.fromisoformat(str(status.get('heartbeat_at')).replace('Z', '+00:00'))
    if datetime.now(timezone.utc) - beat > timedelta(minutes=2):
        raise SystemExit('Dell asset gateway heartbeat is stale')

    assets = [item for item in catalog.get('assets', []) if item.get('approved', True)]
    chosen, selection_rows, segments, required_unique, selected_bytes = choose_unique_pool(
        args.title, script, assets, args.max_assets, args.max_total_bytes
    )

    request_id = uuid.uuid4().hex
    token = uuid.uuid4().hex + uuid.uuid4().hex
    expires = (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat()
    control(
        'create_request',
        {
            'request_id': request_id,
            'request_token': token,
            'job_key': args.job_key,
            'asset_ids': [item['asset_id'] for item in chosen],
            'expires_at': expires,
        },
    )

    dest = pathlib.Path(args.dest)
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    local: list[dict] = []
    try:
        for index, item in enumerate(chosen, 1):
            suffix = pathlib.Path(item['file_name']).suffix or '.mp4'
            out = dest / f'{index:03d}-{item["asset_id"]}{suffix}'
            url = f"{status['public_url'].rstrip('/')}/asset/{request_id}/{item['asset_id']}"
            request = urllib.request.Request(
                url,
                headers={'X-MediaForge-Request-Token': token, 'User-Agent': 'MediaForge-GitHub/4.2'},
            )
            with urllib.request.urlopen(request, timeout=1800) as source, out.open('wb') as target:
                while True:
                    chunk = source.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
            if out.stat().st_size != int(item['size_bytes']):
                raise RuntimeError(f'Size mismatch {item["asset_id"]}')
            local.append({**item, 'local_name': out.name})
            print(json.dumps({'downloaded': index, 'of': len(chosen), 'asset_id': item['asset_id'], 'bytes': out.stat().st_size}))

        (dest / 'Catalogo geral.json').write_text(
            json.dumps({'scope': 'gta-vi-owner-curated-only','reuse_policy': 'never-reuse-source-file','assets': local}, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        (dest / 'mediaforge-selection.json').write_text(
            json.dumps(
                {
                    'scope': 'gta-vi-owner-curated-only',
                    'segments': segments,
                    'segment_asset_map': selection_rows,
                    'required_unique_assets': required_unique,
                    'selected_unique_assets': len(chosen),
                    'selected_bytes': selected_bytes,
                    'reuse_allowed': False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding='utf-8',
        )
    finally:
        try:
            control('delete_request', {'request_id': request_id})
        except Exception:
            pass

    print(json.dumps({'status':'ready','root':str(dest.resolve()),'selected':len(chosen),'required_unique':required_unique,'segments':len(segments),'selected_gb':round(selected_bytes/1024/1024/1024,3),'reuse_allowed':False,'scope':'gta-vi-owner-curated-only','request_id':request_id}, ensure_ascii=False))


if __name__ == '__main__':
    main()
