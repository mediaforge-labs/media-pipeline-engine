#!/usr/bin/env python3
"""Public finalizer for MediaForge worker outputs.

Packages a worker output directory and encrypts it with AES-256-GCM before the
result is uploaded as a GitHub Actions artifact.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import pathlib
import tarfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"MFR1"
NONCE_SIZE = 12
KEY_ENV = "MEDIAFORGE_DATA_KEY_B64"


def load_key() -> bytes:
    raw = os.environ.get(KEY_ENV, "")
    if not raw:
        raise SystemExit(f"Missing required environment variable: {KEY_ENV}")
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise SystemExit(f"{KEY_ENV} must be valid base64") from exc
    if len(key) != 32:
        raise SystemExit(f"{KEY_ENV} must decode to exactly 32 bytes")
    return key


def pack_directory(source_dir: pathlib.Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(source_dir))
    return buffer.getvalue()


def encrypt(payload: bytes, key: bytes) -> bytes:
    nonce = os.urandom(NONCE_SIZE)
    ciphertext = AESGCM(key).encrypt(nonce, payload, MAGIC)
    return MAGIC + nonce + ciphertext


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()

    source = args.input_dir.resolve()
    if not source.is_dir():
        raise SystemExit(f"Output directory does not exist: {source}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encrypt(pack_directory(source), load_key()))
    print(f"Encrypted result written to {args.output}")


if __name__ == "__main__":
    main()
