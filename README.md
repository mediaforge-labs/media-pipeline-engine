# Media Pipeline Engine

Public orchestration and worker layer for the MediaForge video production pipeline.

## Current architecture

The repository is structured for four parallel PT/EN rendering lanes while keeping proprietary processing logic outside the public source tree.

```text
.github/workflows/
  media-pipeline.yml

public-worker-template/
  worker.py
  requirements.txt

private-worker-packaging/
  build_core_bundle.py
  README.md

private-worker-core/
  README.md
  core.bundle.enc        # generated encrypted runtime bundle; not committed yet

.gitignore
LICENSE.md
SECURITY.md
```

## Pipeline model

1. Proprietary core source stays outside this public repository.
2. `private-worker-packaging/build_core_bundle.py` creates an AES-256-GCM encrypted bundle.
3. The encrypted bundle is placed at `private-worker-core/core.bundle.enc`.
4. GitHub Actions launches four parallel lanes: two `pt-BR` and two `en-US`.
5. `public-worker-template/worker.py` decrypts the core only into a temporary runtime directory and invokes its `run.py` entrypoint.
6. Each lane writes its result under `out/<lane>`.
7. Workflow artifacts are retained for two days.

## Required GitHub secret

Create this repository secret before running the production workflow:

```text
MEDIAFORGE_CORE_KEY_B64
```

It must be a base64-encoded 32-byte key. Never commit the key.

## Build the private core bundle

From a trusted machine or trusted packaging environment:

```bash
python -m pip install -r public-worker-template/requirements.txt

export MEDIAFORGE_CORE_KEY_B64='...'
python private-worker-packaging/build_core_bundle.py \
  /path/to/private-core \
  private-worker-core/core.bundle.enc
```

The private core source must contain `run.py`.

## Run the workflow

Open **Actions → Media Pipeline → Run workflow** after the encrypted bundle and GitHub secret are configured.

The current matrix is:

- `pt-1` — `pt-BR`
- `pt-2` — `pt-BR`
- `en-1` — `en-US`
- `en-2` — `en-US`

This provides four concurrent execution lanes and is the initial orchestration layer for the target production volume of 4 long-form videos per day plus at least 5 Shorts per video.

## Security boundary

Do not commit plaintext proprietary core code, API credentials, OAuth refresh tokens, `.env` files, decrypted runtime material, or other secrets. See `SECURITY.md` for the repository security policy.
