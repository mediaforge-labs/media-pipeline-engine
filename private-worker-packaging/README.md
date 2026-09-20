# Private Worker Packaging

This directory contains the public packaging utility used to convert proprietary core source into an encrypted runtime bundle.

## Generate a 256-bit key

Run once in a trusted environment:

```bash
python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

Store the result securely as `MEDIAFORGE_CORE_KEY_B64`. In GitHub Actions, create a repository secret with the same name. Never commit the value.

## Build the encrypted bundle

```bash
export MEDIAFORGE_CORE_KEY_B64='...'
python private-worker-packaging/build_core_bundle.py \
  /path/to/private-core \
  private-worker-core/core.bundle.enc
```

The private source directory must contain `run.py`, which becomes the runtime entrypoint after decryption.
