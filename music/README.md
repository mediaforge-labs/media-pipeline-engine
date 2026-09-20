# MediaForge Music Library

The production pipeline supports a dynamic background-music bed using tracks supplied by the project owner from the YouTube Audio Library.

## Strategy

Music is selected from `catalog.json` using the video's title/script, estimated narrative structure and track metadata.

Default behavior:

- under ~3m30: 1 track
- ~3m30 to ~7m: up to 2 tracks
- ~7m to ~11m: up to 3 tracks
- longer videos: up to 4 tracks

A change is made only when the video is long enough and a useful narrative boundary exists. The engine prefers scene/loop boundaries instead of arbitrary clock intervals.

The mixer applies:

- 1.5 second crossfades between tracks
- narration-triggered sidechain ducking
- a conservative music gain under voice
- artist/track de-duplication within the same video
- content-aware selection for themes such as nightlife, driving, crime/tension, tropical/Latin, rural, exploration and action

Voice calibration jobs explicitly disable music so pronunciation/timbre issues remain audible.

## Storage

Audio binaries are intentionally not committed to this public repository. The catalog is public metadata only. Production audio files live in private project storage and are fetched by the encrypted MediaForge runtime when needed.

## Licensing metadata

The current files were supplied as downloads from the YouTube Audio Library. The pipeline records the track title and artist used in every render manifest. Attribution requirements should be preserved/verified from the Audio Library metadata when applicable.
