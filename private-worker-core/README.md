# Private Worker Core

This directory is intentionally designed to contain **encrypted runtime bundles only**.

Do not commit proprietary plaintext source code, credentials, tokens, refresh tokens, API keys, or decrypted runtime files here.

Expected production artifact:

```text
private-worker-core/core.bundle.enc
```

The bundle is generated with `private-worker-packaging/build_core_bundle.py` from a trusted environment and decrypted only at worker runtime using the GitHub Actions secret `MEDIAFORGE_CORE_KEY_B64`.

The decrypted bundle must contain a `run.py` entrypoint. The public worker injects these environment variables before execution:

- `MEDIAFORGE_LANE`
- `MEDIAFORGE_LOCALE`
- `MEDIAFORGE_OUTPUT_DIR`
