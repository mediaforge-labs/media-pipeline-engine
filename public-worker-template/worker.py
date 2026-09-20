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


def enable_subprocess_stderr_diagnostics(core_dir: pathlib.Path, env: dict[str, str]) -> None:
    """Expose stderr captured by subprocess.run inside the encrypted core.

    The private core intentionally captures tool stderr. During a canary failure
    that can hide the actual ffmpeg diagnostic and leave only CalledProcessError.
    When MEDIAFORGE_DEBUG_SUBPROCESS_STDERR is enabled, inject a sitecustomize
    module into the child interpreter that prints captured stderr before
    re-raising the original exception. Runtime behavior is otherwise unchanged.
    """
    enabled = os.environ.get("MEDIAFORGE_DEBUG_SUBPROCESS_STDERR", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return

    debug_dir = core_dir / ".mediaforge-debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / "sitecustomize.py").write_text(
        """import subprocess\n"
        "import sys\n\n"
        "_mediaforge_original_run = subprocess.run\n\n"
        "def _mediaforge_run_with_stderr(*args, **kwargs):\n"
        "    try:\n"
        "        return _mediaforge_original_run(*args, **kwargs)\n"
        "    except subprocess.CalledProcessError as exc:\n"
        "        captured = getattr(exc, 'stderr', None)\n"
        "        if captured:\n"
        "            if isinstance(captured, bytes):\n"
        "                captured = captured.decode('utf-8', errors='replace')\n"
        "            sys.stderr.write('\\n--- MediaForge captured subprocess stderr ---\\n')\n"
        "            sys.stderr.write(str(captured))\n"
        "            if not str(captured).endswith('\\n'):\n"
        "                sys.stderr.write('\\n')\n"
        "            sys.stderr.write('--- End MediaForge captured subprocess stderr ---\\n')\n"
        "        raise\n\n"
        "subprocess.run = _mediaforge_run_with_stderr\n"
        """,
        encoding="utf-8",
    )

    previous = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = str(debug_dir) + (os.pathsep + previous if previous else "")


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

        env = os.environ.copy()
        env.update({
            "MEDIAFORGE_LANE": args.lane,
            "MEDIAFORGE_LOCALE": args.locale,
            "MEDIAFORGE_OUTPUT_DIR": str(output_dir),
            "MEDIAFORGE_JOB_PATH": str(job),
        })
        enable_subprocess_stderr_diagnostics(core_dir, env)
        subprocess.run(["python", str(entrypoint)], check=True, cwd=core_dir, env=env)


if __name__ == "__main__":
    main()
