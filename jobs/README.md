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

The initial workflow expects these lanes:

- `pt-1` / `pt-BR`
- `pt-2` / `pt-BR`
- `en-1` / `en-US`
- `en-2` / `en-US`

The encrypted runtime validates that the lane locale matches the job locale and requires at least five Shorts per production job.

`smoke-batch.json` is a deterministic integration batch. It exercises job loading, story planning, four TTS shards, caption generation, horizontal rendering, five vertical Shorts, result validation, result encryption, and artifact upload without consuming a paid TTS or generative-video API.
