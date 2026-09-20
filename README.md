# Media Pipeline Engine

Public orchestration and encrypted-runtime layer for the MediaForge video production pipeline.

## Current architecture

The repository contains a validated four-lane PT/EN smoke pipeline plus a separate single-lane production validation workflow. Proprietary runtime source remains outside the public Git tree.

```text
.github/workflows/
  media-pipeline.yml
  media-production-test.yml

jobs/
  README.md
  smoke-batch.json
  production-test.json

public-worker-template/
  worker.py
  requirements.txt
  requirements-production.txt

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
3. The workflow loads that encrypted bundle from `MEDIAFORGE_CORE_BUNDLE_B64` or, for the smoke workflow, from an optional repository path.
4. `public-worker-template/worker.py` decrypts the core only into an ephemeral temporary directory.
5. The worker passes its lane, locale, output directory, and job manifest to the private `run.py` entrypoint.
6. The runtime produces planning data, four logical TTS shards, captions, one horizontal long-form render, and at least five vertical Shorts.
7. Production mode can use Chatterbox Multilingual V3 voice cloning and approved real media assets.
8. The public finalizer encrypts the complete lane output before GitHub artifact upload.
9. Encrypted workflow artifacts are retained for two days.

## Smoke pipeline

`jobs/smoke-batch.json` contains four deterministic integration jobs:

- `pt-1` — `pt-BR`
- `pt-2` — `pt-BR`
- `en-1` — `en-US`
- `en-2` — `en-US`

The smoke workflow exercises the complete orchestration path without consuming production TTS. It creates a consolidated WAV, timed SRT captions, a 16:9 MP4, five 9:16 MP4 Shorts per lane, encrypted outputs, and four parallel GitHub Actions artifacts.

## Human voice profile v3

The production core now carries the canonical Leonidanos voice reference **inside the encrypted runtime bundle**. The reference is not a previously generated TTS file and is never committed in plaintext.

Portuguese production uses a dedicated `pt-br-human-v3` pronunciation profile. The profile keeps acronyms connected and provides TTS-only spoken forms for recurring English/gaming vocabulary such as `GTA`, `gameplay`, `minigame`, `Rockstar Games`, `crossplay`, `Vice City` and related terms. Captions and visible copy keep the original spelling.

The Chatterbox production profile was also moved back into a stable/natural range:

- `temperature = 0.80`
- `exaggeration = 0.50`
- `cfg_weight = 0.35` for `pt-BR`
- no artificial `1.10x` tempo acceleration (`tempo = 1.00`)
- one canonical conditioning profile reused across all chunks
- one deterministic seed reused across chunks to reduce voice drift

## Production validation

`Media Production Test` is intentionally single-lane while the real voice and visual stack is being validated. Its initial choices are:

- `pt-1` — `pt-BR`
- `en-1` — `en-US`

The production workflow:

1. Installs the production Chatterbox dependency and FFmpeg.
2. Restores/caches the Chatterbox model files.
3. Materializes the encrypted proprietary core, including its canonical voice profile.
4. Runs Chatterbox Multilingual V3 using the encrypted voice reference and pronunciation profile.
5. Downloads only manifest-approved `official` or `licensed` media assets.
6. Renders a 1920×1080 long-form video and at least five 1080×1920 Shorts.
7. Validates the real TTS provider, pronunciation profile and rendered media.
8. Encrypts the complete result before artifact upload.

After PT and EN production validation pass, the same runtime can be scaled back out to parallel production lanes/shards.

## Required GitHub secrets

Both the smoke and production workflows use only these MediaForge repository secrets:

```text
MEDIAFORGE_CORE_KEY_B64
MEDIAFORGE_DATA_KEY_B64
MEDIAFORGE_CORE_BUNDLE_B64
```

- `MEDIAFORGE_CORE_KEY_B64` decrypts the proprietary runtime bundle.
- `MEDIAFORGE_DATA_KEY_B64` encrypts completed lane outputs before artifact upload.
- `MEDIAFORGE_CORE_BUNDLE_B64` holds the base64 representation of the encrypted runtime bundle, including the encrypted canonical voice reference.

Never commit the secret values or print them into workflow logs.

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

1. Open **Actions → Media Pipeline**.
2. Choose **Run workflow**.
3. Keep `jobs/smoke-batch.json` as the job manifest.
4. Run the workflow.

## Run the production validation

After `MEDIAFORGE_CORE_BUNDLE_B64` is updated to the current human-voice bundle:

1. Open **Actions → Media Production Test**.
2. Choose `pt-1` first.
3. Keep `jobs/production-test.json` as the manifest.
4. Run the workflow and validate its encrypted artifact.
5. Repeat with `en-1` only after PT succeeds.

## Security boundary

Do not commit plaintext proprietary core code, canonical voice-reference audio, API credentials, OAuth refresh tokens, `.env` files, decrypted runtime material, or other secrets. See `SECURITY.md` for the repository security policy.
