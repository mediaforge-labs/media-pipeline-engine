#!/usr/bin/env python3
"""Public MediaForge worker bootstrap.

Decrypts the encrypted proprietary core into a temporary directory and invokes
its entrypoint without persisting plaintext core files in the repository.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import pathlib
import re
import subprocess
import tarfile
import tempfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"MFP1"
NONCE_SIZE = 12
KEY_ENV = "MEDIAFORGE_CORE_KEY_B64"


def load_key() -> bytes:
    raw = os.environ.get(KEY_ENV, "")
    if not raw:
        raise SystemExit(f"Missing required environment variable: {KEY_ENV}")
    key = base64.b64decode(raw, validate=True)
    if len(key) != 32:
        raise SystemExit(f"{KEY_ENV} must decode to exactly 32 bytes")
    return key


def decrypt_bundle(path: pathlib.Path, key: bytes) -> bytes:
    blob = path.read_bytes()
    if len(blob) <= len(MAGIC) + NONCE_SIZE or not blob.startswith(MAGIC):
        raise SystemExit("Invalid MediaForge encrypted bundle")
    nonce = blob[len(MAGIC):len(MAGIC) + NONCE_SIZE]
    ciphertext = blob[len(MAGIC) + NONCE_SIZE:]
    return AESGCM(key).decrypt(nonce, ciphertext, MAGIC)


def safe_extract_tar_gz(payload: bytes, destination: pathlib.Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        root = destination.resolve()
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if root != target and root not in target.parents:
                raise SystemExit(f"Unsafe archive member: {member.name}")
        archive.extractall(destination)


def apply_runtime_source_hardening(core_dir: pathlib.Path) -> dict[str, int]:
    """Patch only the ephemeral decrypted copy; plaintext is never committed.

    v7 inherited the old scene selector's reuse penalty. A penalty still permits a
    previously used clip to win. In strict mode it must be excluded completely.
    """
    stats = {"unique_media_patches": 0, "tts_sources_seen": 0}
    for path in core_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        original = text
        lower = text.lower()
        if "chunk" in lower and ("wav" in lower or "tts" in lower):
            stats["tts_sources_seen"] += 1

        # Common selector used by the migrated Leonidanos planner.
        if "use_counts" in text and "relative_path" in text:
            pattern = re.compile(
                r'(?P<indent>\s*)path\s*=\s*candidate\[(["\'])relative_path\2\]\s*\n(?!\s*if\s+use_counts)',
                re.MULTILINE,
            )

            def add_guard(match: re.Match[str]) -> str:
                indent = match.group("indent")
                quote = match.group(2)
                return (
                    f"{indent}path = candidate[{quote}relative_path{quote}]\n"
                    f"{indent}if use_counts.get(path, 0) > 0:\n"
                    f"{indent}    continue\n"
                )

            text, count = pattern.subn(add_guard, text)
            stats["unique_media_patches"] += count

            # If the selector contains the legacy soft penalty, remove it after the
            # hard exclusion above so reuse can never become a scoring decision.
            text = re.sub(r'^\s*score\s*-=?\s*use_counts\[path\]\s*\*\s*[0-9.]+\s*$', '', text, flags=re.MULTILINE)

        if text != original:
            path.write_text(text, encoding="utf-8")

    print(
        "MediaForge runtime hardening:",
        f"unique_media_patches={stats['unique_media_patches']}",
        f"tts_sources_seen={stats['tts_sources_seen']}",
    )
    return stats


def install_runtime_guards(core_dir: pathlib.Path, env: dict[str, str]) -> None:
    """Install deterministic filesystem ordering and optional subprocess diagnostics.

    Chunks named chunk-0001.wav, chunk-0002.wav, ... must always be consumed in
    numeric order. Path.glob/rglob, glob.glob and os.listdir do not promise that
    order, so the private runtime receives deterministic natural sorting.
    """
    guard_dir = core_dir / ".mediaforge-guards"
    guard_dir.mkdir(parents=True, exist_ok=True)
    diagnostics = os.environ.get("MEDIAFORGE_DEBUG_SUBPROCESS_STDERR", "").strip().lower() in {"1", "true", "yes", "on"}
    guard_code = f'''import glob as _glob
import os as _os
import pathlib as _pathlib
import re as _re
import subprocess as _subprocess
import sys as _sys


def _natural(value):
    name = getattr(value, "name", str(value))
    return tuple(int(piece) if piece.isdigit() else piece.lower() for piece in _re.split(r"(\\d+)", name))

_original_glob = _glob.glob
_original_listdir = _os.listdir
_original_path_glob = _pathlib.Path.glob
_original_path_rglob = _pathlib.Path.rglob


def _sorted_glob(*args, **kwargs):
    return sorted(_original_glob(*args, **kwargs), key=_natural)


def _sorted_listdir(*args, **kwargs):
    return sorted(_original_listdir(*args, **kwargs), key=_natural)


def _sorted_path_glob(self, pattern):
    return iter(sorted(_original_path_glob(self, pattern), key=_natural))


def _sorted_path_rglob(self, pattern):
    return iter(sorted(_original_path_rglob(self, pattern), key=_natural))

_glob.glob = _sorted_glob
_os.listdir = _sorted_listdir
_pathlib.Path.glob = _sorted_path_glob
_pathlib.Path.rglob = _sorted_path_rglob

_DIAGNOSTICS = {diagnostics!r}
_original_run = _subprocess.run

def _run(*args, **kwargs):
    try:
        return _original_run(*args, **kwargs)
    except _subprocess.CalledProcessError as exc:
        if _DIAGNOSTICS:
            captured = getattr(exc, "stderr", None)
            if captured:
                if isinstance(captured, bytes):
                    captured = captured.decode("utf-8", errors="replace")
                captured = str(captured)
                _sys.stderr.write("\\n--- MediaForge captured subprocess stderr ---\\n")
                _sys.stderr.write(captured)
                if not captured.endswith("\\n"):
                    _sys.stderr.write("\\n")
                _sys.stderr.write("--- End MediaForge captured subprocess stderr ---\\n")
        raise

_subprocess.run = _run
'''
    (guard_dir / "sitecustomize.py").write_text(guard_code, encoding="utf-8")
    previous = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = str(guard_dir) + (os.pathsep + previous if previous else "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=pathlib.Path)
    parser.add_argument("--job", required=True, type=pathlib.Path)
    parser.add_argument("--lane", required=True)
    parser.add_argument("--locale", required=True)
    parser.add_argument("--output-dir", default="out", type=pathlib.Path)
    args = parser.parse_args()

    bundle = args.bundle.resolve()
    job = args.job.resolve()
    output_dir = args.output_dir.resolve()

    if not bundle.is_file():
        raise SystemExit(f"Encrypted core bundle not found: {bundle}")
    if not job.is_file():
        raise SystemExit(f"Job manifest not found: {job}")

    output_dir.mkdir(parents=True, exist_ok=True)
    plaintext = decrypt_bundle(bundle, load_key())

    with tempfile.TemporaryDirectory(prefix="mediaforge-core-") as tmp:
        core_dir = pathlib.Path(tmp)
        safe_extract_tar_gz(plaintext, core_dir)
        entrypoint = core_dir / "run.py"
        if not entrypoint.is_file():
            raise SystemExit("Encrypted core bundle must contain run.py")

        apply_runtime_source_hardening(core_dir)
        env = os.environ.copy()
        env.update({
            "MEDIAFORGE_LANE": args.lane,
            "MEDIAFORGE_LOCALE": args.locale,
            "MEDIAFORGE_OUTPUT_DIR": str(output_dir),
            "MEDIAFORGE_JOB_PATH": str(job),
            "MEDIAFORGE_STRICT_UNIQUE_MEDIA": "1",
            "MEDIAFORGE_TTS_STRICT_CHUNK_ORDER": "1",
            "MEDIAFORGE_TTS_MAX_WORKERS": "1",
        })
        install_runtime_guards(core_dir, env)
        subprocess.run(["python", str(entrypoint)], check=True, cwd=core_dir, env=env)


if __name__ == "__main__":
    main()
