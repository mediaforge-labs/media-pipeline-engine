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
  voice-calibration.json

music/
  README.md
  catalog.json

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
3. The workflow loads that encrypted bundle from `MEDIAFORGE_CORE_BUNDLE_B64`.
4. The canonical human voice reference is stored separately in `MEDIAFORGE_VOICE_REFERENCE_B64` so neither secret exceeds GitHub's size limit.
5. `public-worker-template/worker.py` decrypts the core only into an ephemeral temporary directory.
6. The worker passes its lane, locale, output directory, voice-reference path, and job manifest to the private `run.py` entrypoint.
7. The runtime produces planning data, four logical TTS shards, captions, one horizontal long-form render, and at least five vertical Shorts.
8. Production mode uses Chatterbox Multilingual V3 voice cloning, approved real media assets, and an optional dynamic background-music bed.
9. The public finalizer encrypts the complete lane output before GitHub artifact upload.

## Human voice profile v4

The canonical Leonidanos human voice reference is no longer packed inside the encrypted core. It is materialized only inside the GitHub runner from `MEDIAFORGE_VOICE_REFERENCE_B64`, converted to 24 kHz mono WAV, used for Chatterbox conditioning, and discarded with the runner.

Portuguese production uses the `pt-br-human-v3` pronunciation profile. TTS-only spoken forms are applied to recurring terms such as `GTA`, `gameplay`, `minigame`, `Rockstar Games`, `crossplay`, `PlayStation`, `Xbox`, `Vice City`, `Jason`, `Extended Look` and `HUD`. Captions and visible copy keep their original spelling.

The natural-voice profile uses:

- `temperature = 0.80`
- `exaggeration = 0.50`
- `cfg_weight = 0.35` for `pt-BR`
- no artificial tempo acceleration (`tempo = 1.00`)
- one conditioning profile reused across all chunks
- one deterministic seed reused across chunks to reduce voice drift

## Dynamic music system

The production runtime now supports a YouTube Audio Library catalog with content-aware selection and multiple tracks per video when that improves pacing.

Default behavior:

- under ~3m30: one track for cohesion
- ~3m30 to ~7m: up to two tracks
- ~7m to ~11m: up to three tracks
- longer videos: up to four tracks

Track changes are aligned to narrative/loop boundaries rather than arbitrary clock intervals. The mixer applies crossfades, narration-triggered sidechain ducking, conservative background gain, artist de-duplication and theme matching (nightlife, driving, crime/tension, tropical/Latin, rural, exploration, action and related moods).

Every render records the selected track title, artist, segment timing and mixing strategy in the output manifest. Voice-calibration jobs explicitly disable music.

The public `music/catalog.json` contains metadata only; MP3 binaries stay out of public Git and are served from private project storage through the encrypted runtime.

## Smoke pipeline

`jobs/smoke-batch.json` contains four deterministic integration jobs:

- `pt-1` — `pt-BR`
- `pt-2` — `pt-BR`
- `en-1` — `en-US`
- `en-2` — `en-US`

The smoke workflow exercises the complete orchestration path without consuming production TTS.

## Production voice calibration

Before another full production render, `Media Production Test` defaults to `jobs/voice-calibration.json`. The PT-BR calibration intentionally tests terms that previously caused poor pronunciation, including `GTA`, `gameplay`, `minigame`, `Rockstar Games`, `crossplay`, `PlayStation`, `Xbox`, `Vice City`, `Jason`, `Extended Look` and `HUD`.

The production workflow:

1. Installs Chatterbox and FFmpeg.
2. Restores/caches Chatterbox model files.
3. Materializes the encrypted production core.
4. Materializes the canonical human voice reference from its separate secret.
5. Converts the reference to 24 kHz mono WAV.
6. Runs Chatterbox Multilingual V3 using the human voice profile and pronunciation rules.
7. Renders the test output and validates the TTS profile.
8. Encrypts the result before artifact upload.

## Required GitHub secrets

```text
MEDIAFORGE_CORE_KEY_B64
MEDIAFORGE_DATA_KEY_B64
MEDIAFORGE_CORE_BUNDLE_B64
MEDIAFORGE_VOICE_REFERENCE_B64
```

- `MEDIAFORGE_CORE_KEY_B64` decrypts the proprietary runtime bundle.
- `MEDIAFORGE_DATA_KEY_B64` encrypts completed lane outputs before artifact upload.
- `MEDIAFORGE_CORE_BUNDLE_B64` holds only the encrypted private runtime code.
- `MEDIAFORGE_VOICE_REFERENCE_B64` holds the optimized canonical human voice reference separately from the core.

Never commit the secret values or print them into workflow logs.

## Run the production calibration

1. Keep the currently configured voice-calibration core and human voice reference.
2. Open **Actions → Media Production Test**.
3. Choose `pt-1`.
4. Keep `jobs/voice-calibration.json` as the manifest.
5. Run the workflow and listen to the resulting narration before approving any full production render.

The music-enabled production core is packaged separately and should only replace the calibration core after the PT-BR voice is approved and the private music library is uploaded.

## Security boundary

Do not commit plaintext proprietary core code, canonical voice-reference audio, API credentials, OAuth refresh tokens, `.env` files, decrypted runtime material, or other secrets. See `SECURITY.md` for the repository security policy.
