# MediaForge Canonical Architecture

Status: **AUTHORITATIVE**

## Production responsibility

`mediaforge-labs/media-pipeline-engine` is the only video factory.

All production compute runs on GitHub-hosted runners through:

- `.github/workflows/media-factory.yml`

The production factory owns:
- PT-BR and EN-US narration/TTS;
- semantic scene selection;
- temporary download of required media assets;
- long-form rendering;
- Shorts rendering;
- validation;
- production checkpoints/results.

## Dell responsibility

The Dell is a remote file supplier only.

MediaForge reaches the Dell through the active gateway/control contract and downloads only selected media assets. The Dell may host files, maintain its catalog, expose the gateway and answer health/catalog/asset requests.

The Dell is not allowed to run MediaForge production, TTS, semantic production planning, FFmpeg final rendering, muxing, Shorts generation or YouTube uploads.

## Queue contract

Production work is read from `youtube_factory_jobs` with:
- `preferred_backend=github`;
- production-compatible stage (`mediaforge`, `production`, or `render`);
- pending or recoverable lease state.

## Active gateway contract

The GitHub factory validates the current Dell supplier contract before selecting/downloading media. The expected active gateway version is `3.2-active-gta-vi-rest`, with scope `media/gta_vi-active-edit-only` and active edit files ending in `-mudo.mp4`.

## Disabled alternatives

Legacy `media-pipeline`, postprocess-music, production-test, Dell canary/resume, calibration, temporary E2E render and canary promotion/repair paths are disabled so there is no competing factory.

## Recovery

Self-heal and retry logic must recover work back into the GitHub-hosted production factory. It must never move production work onto a Dell/self-hosted runner.
