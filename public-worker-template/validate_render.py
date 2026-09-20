#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import json
import pathlib
from typing import Any

VIDEO_EXTENSIONS = ('.mp4', '.mov', '.mkv', '.webm', '.m4v', '.avi')
IDENTITY_KEYS = (
    'media_relative_path', 'asset_id', 'source_asset_id', 'video_asset_id',
    'relative_path', 'media_path', 'video_path', 'source_path', 'local_name',
)
SCENE_MARKERS = {
    'scene_index', 'start_seconds', 'end_seconds', 'duration_seconds',
    'narration_text', 'semantic_query', 'source_in_seconds',
}


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def identity_from_dict(node: dict[str, Any]) -> str | None:
    for key in IDENTITY_KEYS:
        value = node.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            text = str(value).strip()
            if key.endswith('path') or key == 'local_name':
                if text.lower().endswith(VIDEO_EXTENSIONS):
                    return text.replace('\\', '/').lower()
            else:
                return text.lower()
    for key, value in node.items():
        if not isinstance(value, str):
            continue
        low_key = key.lower()
        if any(term in low_key for term in ('video', 'media', 'asset', 'source')) and value.lower().endswith(VIDEO_EXTENSIONS):
            return value.replace('\\', '/').lower()
    return None


def collect_scene_usage(node: Any, output: list[str]) -> None:
    if isinstance(node, dict):
        keys = set(node)
        identity = identity_from_dict(node)
        if identity and len(keys & SCENE_MARKERS) >= 1:
            output.append(identity)
        for value in node.values():
            collect_scene_usage(value, output)
    elif isinstance(node, list):
        for value in node:
            collect_scene_usage(value, output)


def validate_selection(selection_path: pathlib.Path) -> dict[str, Any]:
    selection = load(selection_path)
    selected = int(selection.get('selected_unique_assets') or 0)
    required = int(selection.get('required_unique_assets') or 0)
    mapping = selection.get('segment_asset_map') or []
    ids = [str(row.get('asset_id')) for row in mapping if row.get('asset_id')]
    if selection.get('reuse_allowed') is not False:
        raise SystemExit('selection manifest does not enforce reuse_allowed=false')
    if selected < required or required < 1:
        raise SystemExit(f'unique media pool is insufficient: selected={selected}, required={required}')
    duplicates = [item for item, count in collections.Counter(ids).items() if count > 1]
    if duplicates:
        raise SystemExit(f'semantic selector reused asset IDs before render: {duplicates[:8]}')
    return {'selected_unique_assets': selected, 'required_unique_assets': required, 'mapped_segments': len(ids)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=pathlib.Path)
    parser.add_argument('--selection', type=pathlib.Path)
    parser.add_argument('--require-five-shorts', action='store_true')
    args = parser.parse_args()

    root = args.root
    manifest_path = root / 'manifest.json'
    media_manifest_path = root / 'media' / 'manifest.json'
    longform = root / 'video' / 'long-form.mp4'
    narration = root / 'audio' / 'narration.wav'
    captions = root / 'captions' / 'long-form.srt'

    for path in (manifest_path, media_manifest_path, longform, narration, captions):
        if not path.is_file() or path.stat().st_size <= 0:
            raise SystemExit(f'required render output missing: {path}')
    if longform.stat().st_size < 100000:
        raise SystemExit('long-form output is unexpectedly small')

    manifest = load(manifest_path)
    metrics = manifest.get('metrics') or {}
    if int(metrics.get('video_assets') or 0) < 1:
        raise SystemExit('render manifest reports no video assets')

    shorts = sorted((root / 'shorts').glob('short-*.mp4'))
    if args.require_five_shorts and len(shorts) < 5:
        raise SystemExit(f'expected at least 5 Shorts, got {len(shorts)}')

    selection_result = None
    if args.selection:
        selection_result = validate_selection(args.selection)

    media_manifest = load(media_manifest_path)
    usage: list[str] = []
    collect_scene_usage(media_manifest, usage)
    if not usage:
        # Do not call a render green if the core did not expose enough information to
        # prove that the no-reuse invariant was respected.
        raise SystemExit('could not verify per-scene media usage from media/manifest.json')

    counts = collections.Counter(usage)
    repeated = {item: count for item, count in counts.items() if count > 1}
    if repeated:
        sample = list(repeated.items())[:10]
        raise SystemExit(f'RENDER INVALID: source video reused across scenes: {sample}')

    print(json.dumps({
        'status': 'validated',
        'long_form_bytes': longform.stat().st_size,
        'shorts': len(shorts),
        'scene_media_uses': len(usage),
        'unique_scene_media': len(counts),
        'duplicates': 0,
        'selection': selection_result,
        'music_tracks': metrics.get('music_tracks'),
        'narration_seconds': metrics.get('narration_seconds'),
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
