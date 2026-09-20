# Media Pipeline Engine

GitHub-hosted audiovisual factory for Leonidanos. Editorial decisions stay in `lovacademy/portal-leonidanos`; MediaForge consumes production jobs through Supabase and returns finished long-form videos, narration, captions and Shorts.

## Production architecture

```text
portal-leonidanos
  editorial / PT+EN metadata / thumbnail / schedule
            |
            v
Supabase youtube_video_variants + youtube_factory_jobs
            |
            v
mediaforge-labs/media-pipeline-engine
  4 GitHub-hosted lanes
  Chatterbox Multilingual V3
  approved B-roll library
  dynamic YouTube Audio Library music
  captions / long-form / 5+ Shorts / QA
            |
            v
Supabase mediaforge-assets
            |
            v
portal-leonidanos
  YouTube upload / scheduling / publication state
```

The Dell workstation is not a production compute backend. It is used only for the one-time synchronization of the owner-curated video library in `C:\LeonidanosVideoPipeline` into private object storage.

## GitHub-hosted factory

`.github/workflows/media-factory.yml` runs four independent lanes on GitHub-hosted Ubuntu runners:

- `pt-1` — `pt-BR`
- `pt-2` — `pt-BR`
- `en-1` — `en-US`
- `en-2` — `en-US`

The workflow is intentionally **manual during canary validation**. After PT-BR voice approval and one successful end-to-end production canary, the schedule is enabled for continuous queue polling. Each lane atomically leases one pending Supabase job, so the same variant cannot be processed by two runners at once.

`public-worker-template/factory_worker.py`:

1. leases a `youtube_factory_jobs` row;
2. loads the associated `youtube_queue` and `youtube_video_variants` row;
3. creates the private runtime manifest using the variant `tts_text`/script;
4. invokes the encrypted MediaForge core;
5. uploads the long-form video, narration, captions, manifest and Shorts into the private `mediaforge-assets` bucket;
6. updates the variant and factory-job status in Supabase.

## Encrypted runtime

The proprietary runtime source is not stored in plaintext in this public repository.

1. `private-worker-packaging/build_core_bundle.py` creates an AES-256-GCM encrypted runtime bundle.
2. `MEDIAFORGE_CORE_BUNDLE_B64` stores that encrypted bundle.
3. `MEDIAFORGE_CORE_KEY_B64` is the separate decryption key.
4. `public-worker-template/worker.py` decrypts the runtime only into an ephemeral runner directory.
5. Final test artifacts can also be encrypted with `MEDIAFORGE_DATA_KEY_B64`.

## Human voice

The canonical human voice reference is stored separately in `MEDIAFORGE_VOICE_REFERENCE_B64`, materialized only inside the GitHub runner and discarded with the runner.

Production is pinned to a Chatterbox source revision containing explicit **Multilingual V3** support. The PT-BR natural-voice profile uses:

- `temperature = 0.80`
- `exaggeration = 0.50`
- `cfg_weight = 0.35`
- `tempo = 1.00`
- one reusable voice-conditioning profile across chunks
- deterministic seed reuse to reduce speaker drift
- `pt-br-human-v3` pronunciation preprocessing only for TTS text

Visible copy and captions retain normal spelling. The pronunciation layer handles recurring terms such as `GTA`, `gameplay`, `minigame`, `Rockstar Games`, `crossplay`, `PlayStation`, `Xbox`, `Vice City`, `Jason`, `Extended Look` and `HUD`.

## Approved video library

All video files curated by the project owner under `C:\LeonidanosVideoPipeline` are treated as approved assets for this factory.

`tools/sync_video_library.ps1` is the one-command helper and calls `tools/sync_video_library.py`. The process is a **one-time upload only**; it does not render on the Dell. It scans videos plus the three editorial indexes:

- `Repositorio GTA`
- `Catálogo geral`
- `Transcrição Visual GTA VI`

The synchronizer extracts index context, probes video duration, uploads binaries to `mediaforge-assets/video-library/videos/`, uploads the index documents, and writes `mediaforge-assets/video-library/catalog.json`.

During production the encrypted runtime searches this catalog against the current title/script, downloads only the selected clips and uses them as B-roll. It does not download the whole library on every run.

## Dynamic music

Thirty owner-supplied tracks from the YouTube Audio Library live privately in `youtube-assets/youtube-audio-library/`.

Production can use multiple tracks strategically:

- under ~3m30: normally 1 track
- ~3m30–7m: up to 2
- ~7–11m: up to 3
- longer: up to 4

Changes prefer narrative boundaries instead of arbitrary time intervals. The mixer applies crossfades, narration-triggered ducking, conservative gain, content/theme matching and artist de-duplication. Voice-calibration jobs disable music.

## Supabase control plane

The factory uses:

- `youtube_queue`
- `youtube_video_variants`
- `youtube_factory_jobs`
- private bucket `mediaforge-assets`

New eligible PT/EN variants are automatically enqueued for the GitHub backend. Job leasing uses `FOR UPDATE SKIP LOCKED` to support concurrent lanes safely.

Finished renders are stored under paths similar to:

```text
mediaforge-assets/
  renders/<queue-id>/<locale>/<github-run-id>/
    long-form.mp4
    narration.wav
    long-form.srt
    manifest.json
    shorts/
      short-01.mp4
      ...
```

## Voice calibration

`Media Production Test` defaults to `jobs/voice-calibration.json`. It intentionally tests terms that previously caused poor pronunciation, while keeping music disabled so timbre and articulation can be judged clearly.

Voice approval is a manual quality gate before the full automated publication schedule is enabled.

## Required GitHub secrets

```text
MEDIAFORGE_CORE_KEY_B64
MEDIAFORGE_DATA_KEY_B64
MEDIAFORGE_CORE_BUNDLE_B64
MEDIAFORGE_VOICE_REFERENCE_B64
SUPABASE_SECRET_KEY
```

`SUPABASE_SECRET_KEY` must be a dedicated server-side Supabase secret key for the MediaForge backend. It is used only inside GitHub Actions for the factory queue and private object storage. Never commit or print secret values.

## Repository boundary

MediaForge owns audiovisual production. `portal-leonidanos` owns editorial selection, source article/metadata, thumbnails, scheduling, channel OAuth and final YouTube publication. Supabase is the persistent handoff between the two systems.

## Security boundary

Do not commit plaintext private-core code, the voice-reference audio, Supabase secret keys, YouTube OAuth credentials, decrypted runtime material or `.env` files. See `SECURITY.md`.
