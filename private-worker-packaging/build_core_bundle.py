#!/usr/bin/env python3
"""Package proprietary MediaForge core code into an AES-256-GCM encrypted bundle.

The plaintext source directory is never written to the repository. Run this script
from a trusted machine/CI context that has access to the private core source.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import pathlib
import tarfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"MFP1"
NONCE_SIZE = 12
KEY_ENV = "MEDIAFORGE_CORE_KEY_B64"


def load_key() -> bytes:
    raw = os.environ.get(KEY_ENV, "")
    if not raw:
        raise SystemExit(f"Missing required environment variable: {KEY_ENV}")
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise SystemExit(f"{KEY_ENV} must be valid base64") from exc
    if len(key) != 32:
        raise SystemExit(f"{KEY_ENV} must decode to exactly 32 bytes (AES-256)")
    return key


def build_tar_gz(source_dir: pathlib.Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(source_dir))
    return buffer.getvalue()


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    nonce = os.urandom(NONCE_SIZE)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, MAGIC)
    return MAGIC + nonce + ciphertext


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=pathlib.Path, help="Private core source directory")
    parser.add_argument("output", type=pathlib.Path, help="Encrypted bundle output path")
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.is_dir():
        raise SystemExit(f"Source directory does not exist: {source}")

    payload = build_tar_gz(source)
    encrypted = encrypt(payload, load_key())

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encrypted)
    print(f"Encrypted core bundle written to {args.output} ({len(encrypted)} bytes)")


if __name__ == "__main__":
    main()
