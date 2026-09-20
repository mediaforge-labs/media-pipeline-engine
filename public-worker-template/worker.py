#!/usr/bin/env python3
"""Public MediaForge worker bootstrap.

Decrypts the encrypted proprietary core into a temporary directory and invokes
its entrypoint without persisting plaintext core files in the repository.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
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
TRUE_VALUES = {"1", "true", "yes", "on"}


def enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE_VALUES


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
    """Harden only the ephemeral decrypted copy; plaintext is never committed.

    The migrated v7 selector used a soft reuse penalty, which still allowed a used
    clip to win. Strict mode converts that into a hard exclusion. TTS-related files
    are also inspected; if they use Python executors, worker count is forced to one
    so completion order cannot scramble narration chunks.
    """
    stats = {
        "python_sources": 0,
        "unique_media_sources": 0,
        "unique_media_patches": 0,
        "tts_sources_seen": 0,
        "tts_parallel_sources": 0,
        "tts_worker_patches": 0,
    }

    for path in core_dir.rglob("*.py"):
        stats["python_sources"] += 1
        text = path.read_text(encoding="utf-8", errors="ignore")
        original = text
        lower = text.lower()
        is_tts_source = "chunk" in lower and ("wav" in lower or "tts" in lower)

        if is_tts_source:
            stats["tts_sources_seen"] += 1
            if "threadpoolexecutor" in lower or "processpoolexecutor" in lower or "as_completed" in lower:
                stats["tts_parallel_sources"] += 1

            # Even if the private source does not consume MEDIAFORGE_TTS_MAX_WORKERS,
            # force local executor concurrency to one in strict mode. With one worker,
            # as_completed() cannot reorder simultaneously completed narration chunks.
            if enabled("MEDIAFORGE_TTS_STRICT_CHUNK_ORDER", True):
                patterns = (
                    r'(ThreadPoolExecutor\s*\(\s*max_workers\s*=\s*)[^,)]+',
                    r'(ProcessPoolExecutor\s*\(\s*max_workers\s*=\s*)[^,)]+',
                )
                for pattern in patterns:
                    text, count = re.subn(pattern, r'\g<1>1', text)
                    stats["tts_worker_patches"] += count

        # Common selector inherited from the Leonidanos renderer.
        if "use_counts" in text and "relative_path" in text:
            stats["unique_media_sources"] += 1
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

            # Remove the legacy soft penalty after hard exclusion. Reuse must never be
            # a score tradeoff in strict mode.
            text = re.sub(
                r'^\s*score\s*-=?\s*use_counts\[path\]\s*\*\s*[0-9.]+\s*$',
                '',
                text,
                flags=re.MULTILINE,
            )

        if text != original:
            path.write_text(text, encoding="utf-8")

    return stats


def verify_runtime_hardening(stats: dict[str, int]) -> None:
    """Fail before TTS/render if the encrypted core cannot prove strict guarantees."""
    strict_unique = enabled("MEDIAFORGE_STRICT_UNIQUE_MEDIA", True)
    strict_tts = enabled("MEDIAFORGE_TTS_STRICT_CHUNK_ORDER", True)

    errors: list[str] = []
    if strict_unique:
        if stats["unique_media_sources"] < 1:
            errors.append("no private media selector containing use_counts/relative_path was found")
        if stats["unique_media_patches"] < 1:
            errors.append("strict no-reuse guard was not injected into the private media selector")

    if strict_tts:
        if stats["tts_sources_seen"] < 1:
            errors.append("no private TTS/chunk source was found for ordered-audio verification")
        if stats["tts_parallel_sources"] > 0 and stats["tts_worker_patches"] < 1:
            # The sitecustomize natural-sort guard still protects filesystem ordering,
            # but an unpatched concurrent executor could reorder in-memory results.
            errors.append("parallel TTS code was detected but its executor could not be forced to one worker")

    payload = {
        "runtime_hardening": {
            **stats,
            "strict_unique_media": strict_unique,
            "strict_tts_chunk_order": strict_tts,
            "verified": not errors,
        }
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    if errors:
        raise SystemExit("Runtime hardening verification failed before TTS/render: " + "; ".join(errors))


def install_runtime_guards(core_dir: pathlib.Path, env: dict[str, str]) -> None:
    """Install deterministic filesystem ordering and subprocess diagnostics.

    chunk-0001.wav, chunk-0002.wav, ... are always returned in natural numeric order
    from glob, pathlib and os.listdir inside the private runtime.
    """
    guard_dir = core_dir / ".mediaforge-guards"
    guard_dir.mkdir(parents=True, exist_ok=True)
    diagnostics = enabled("MEDIAFORGE_DEBUG_SUBPROCESS_STDERR", False)
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
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Decrypt, harden and verify the private core without starting TTS/render",
    )
    args = parser.parse_args()

    bundle = args.bundle.resolve()
    job = args.job.resolve()
    output_dir = args.output_dir.resolve()

    if not bundle.is_file():
        raise SystemExit(f"Encrypted core bundle not found: {bundle}")
    if not job.is_file():
        raise SystemExit(f"Job manifest not found: {job}")

    if not args.verify_only:
        output_dir.mkdir(parents=True, exist_ok=True)
    plaintext = decrypt_bundle(bundle, load_key())

    with tempfile.TemporaryDirectory(prefix="mediaforge-core-") as tmp:
        core_dir = pathlib.Path(tmp)
        safe_extract_tar_gz(plaintext, core_dir)
        entrypoint = core_dir / "run.py"
        if not entrypoint.is_file():
            raise SystemExit("Encrypted core bundle must contain run.py")

        stats = apply_runtime_source_hardening(core_dir)
        verify_runtime_hardening(stats)
        if args.verify_only:
            print(json.dumps({"status": "private_core_preflight_ok"}))
            return

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
