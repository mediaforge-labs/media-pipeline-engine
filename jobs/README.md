# Job Manifests

A MediaForge batch manifest uses schema version `1` and maps one job to each execution lane.

```json
{
  "version": 1,
  "mode": "smoke",
  "jobs": {
    "pt-1": {
      "id": "example-pt-1",
      "locale": "pt-BR",
      "title": "Example title",
      "shorts_requested": 5,
      "script": "Narration script with at least twenty words."
    }
  }
}
```

The smoke workflow uses these lanes:

- `pt-1` / `pt-BR`
- `pt-2` / `pt-BR`
- `en-1` / `en-US`
- `en-2` / `en-US`

The encrypted runtime validates that the lane locale matches the job locale and requires at least five Shorts per production job.

## Smoke manifest

`smoke-batch.json` is a deterministic integration batch. It exercises job loading, story planning, four logical TTS shards, caption generation, horizontal rendering, five vertical Shorts, result validation, result encryption, and artifact upload without consuming production TTS.

## Production manifest

`production-test.json` is the first real-media validation manifest. It currently contains `pt-1` and `en-1` and is designed to be run one lane at a time through **Media Production Test**.

Production jobs add a `media` array:

```json
{
  "id": "production-example",
  "locale": "pt-BR",
  "title": "Production example",
  "shorts_requested": 5,
  "script": "Production narration...",
  "media": [
    {
      "url": "https://example.com/official-image.jpg",
      "source": "Official source",
      "safety": "official"
    }
  ]
}
```

The private runtime accepts production media only when `safety` is `official` or `licensed`. The initial test manifest uses official Rockstar Games image URLs and is intended to validate real Chatterbox narration plus real image-based FFmpeg rendering before production is expanded to additional parallel lanes.
