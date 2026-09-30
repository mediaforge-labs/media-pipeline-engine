#!/usr/bin/env python3
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "peter-lofi.json"


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def require_secret(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required secret: {name}")
    return value


def build_target(base_url, stream_key):
    return base_url.rstrip("/") + "/" + stream_key.lstrip("/")


def build_command(source, target, cfg):
    v = cfg["video"]
    a = cfg["audio"]

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",
        "-re",
        "-stream_loop", "-1",
        "-i", source,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-vf", f"scale={v['width']}:{v['height']}:force_original_aspect_ratio=decrease,pad={v['width']}:{v['height']}:(ow-iw)/2:(oh-ih)/2,fps={v['fps']}",
        "-c:v", v["codec"],
        "-preset", "veryfast",
        "-pix_fmt", v["pixel_format"],
        "-r", str(v["fps"]),
        "-b:v", f"{v['bitrate_kbps']}k",
        "-minrate", f"{v['minrate_kbps']}k",
        "-maxrate", f"{v['maxrate_kbps']}k",
        "-bufsize", f"{v['buffer_kbps']}k",
        "-g", str(v["gop_frames"]),
        "-keyint_min", str(v["gop_frames"]),
        "-sc_threshold", "0",
        "-x264-params", "nal-hrd=cbr:force-cfr=1",
        "-c:a", a["codec"],
        "-b:a", f"{a['bitrate_kbps']}k",
        "-ar", str(a["sample_rate_hz"]),
        "-ac", str(a["channels"]),
        "-f", "flv",
        target,
    ]


def safe_command_preview(cmd):
    safe = list(cmd)
    if safe:
        safe[-1] = "<KICK_RTMPS_TARGET_REDACTED>"
    return " ".join(shlex.quote(x) for x in safe)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: stream_peter_lofi.py <media-file> [--dry-run]")

    source = sys.argv[1]
    dry_run = "--dry-run" in sys.argv[2:]

    if not Path(source).exists():
        raise SystemExit(f"Media source not found: {source}")

    cfg = load_config()
    url_env = cfg["transport"]["stream_url_env"]
    key_env = cfg["transport"]["stream_key_env"]

    base_url = require_secret(url_env)
    stream_key = require_secret(key_env)
    target = build_target(base_url, stream_key)

    cmd = build_command(source, target, cfg)

    print("Peter Lofi / Kick encoder profile")
    print(f"Resolution: {cfg['video']['width']}x{cfg['video']['height']}")
    print(f"FPS: {cfg['video']['fps']}")
    print(f"Rate control: {cfg['video']['rate_control']}")
    print(f"Video bitrate: {cfg['video']['bitrate_kbps']} kbps")
    print(f"Keyframe interval: {cfg['video']['keyframe_interval_seconds']} s")
    print(f"{url_env}: OK")
    print(f"{key_env}: OK")
    print("Command:", safe_command_preview(cmd))

    if dry_run:
        print("Dry run only; stream was not started.")
        return

    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
