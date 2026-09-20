# Media Pipeline Engine

Public orchestration and encrypted-runtime layer for the MediaForge video production pipeline.

## Current architecture

The repository now contains a working four-lane PT/EN smoke pipeline while keeping the private runtime source outside the public Git tree.

```text
.github/workflows/
  media-pipeline.yml

jobs/
  README.md
  smoke-batch.json

public-worker-template/
  worker.py
  requirements.txt

private-worker-packaging/
  build_core_bundle.py
  README.md

private-worker-core/
  README.md
  public_finalize.py

.gitignore
LICENSE.md
SECURITY.md
```

## Pipeline model

1. Proprietary core source stays outside this public repository.
2. `private-worker-packaging/build_core_bundle.py` creates an AES-256-GCM encrypted runtime bundle.
3. The workflow can load that encrypted bundle from a repository path or from the `MEDIAFORGE_CORE_BUNDLE_B64` GitHub secret.
4. GitHub Actions launches four parallel lanes: two `pt-BR` and two `en-US`.
5. `public-worker-template/worker.py` decrypts the core only into an ephemeral temporary directory.
6. The worker passes its lane, locale, output directory, and batch job manifest to the private `run.py` entrypoint.
7. The runtime produces planning data, four TTS shards, captions, one horizontal long-form render, and at least five vertical Shorts.
8. The public finalizer encrypts the complete lane output before GitHub artifact upload.
9. Encrypted workflow artifacts are retained for two days.

## Current smoke pipeline

`jobs/smoke-batch.json` contains four deterministic integration jobs:

- `pt-1` — `pt-BR`
- `pt-2` — `pt-BR`
- `en-1` — `en-US`
- `en-2` — `en-US`

The current encrypted core exercises the full orchestration path without consuming paid APIs. Its mock voice adapter splits narration into four shards, creates a consolidated WAV, produces timed SRT captions, renders a 16:9 MP4, and derives five 9:16 MP4 Shorts per lane.

This smoke implementation is the integration baseline for the production target of 4 long-form videos per day plus at least 5 Shorts per video.

## Required GitHub secrets

The smoke workflow supports these repository secrets:

```text
MEDIAFORGE_CORE_KEY_B64
MEDIAFORGE_DATA_KEY_B64
MEDIAFORGE_CORE_BUNDLE_B64
```

- `MEDIAFORGE_CORE_KEY_B64` decrypts the proprietary runtime bundle.
- `MEDIAFORGE_DATA_KEY_B64` encrypts completed lane outputs before artifact upload.
- `MEDIAFORGE_CORE_BUNDLE_B64` can hold the base64 representation of the encrypted runtime bundle when the bundle is not committed to the repository.

Never commit the secret values.

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

To keep even the encrypted binary out of the public repository, base64-encode `core.bundle.enc` and store that value in `MEDIAFORGE_CORE_BUNDLE_B64`. The workflow materializes it only inside the runner.

## Run the smoke workflow

After the three GitHub secrets are configured:

1. Open **Actions → Media Pipeline**.
2. Choose **Run workflow**.
3. Keep `jobs/smoke-batch.json` as the job manifest.
4. Run the workflow.

Each successful lane must contain a result manifest, a non-empty long-form MP4, and at least five non-empty Short MP4 files before its encrypted artifact is uploaded.

## Security boundary

Do not commit plaintext proprietary core code, API credentials, OAuth refresh tokens, `.env` files, decrypted runtime material, or other secrets. See `SECURITY.md` for the repository security policy.
