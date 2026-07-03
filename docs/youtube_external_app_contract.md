# External YouTube App Contract And Test Case

This document is the contract for the external Bolt/Supabase YouTube transcript
service used by GitHub Actions.

## Responsibility Split

GitHub Actions owns:

- Compute the daily time window.
- Call the external YouTube service.
- Poll job status.
- Download `daily_items.json`, `manifest.json`, and `subtitles_bundle.json`.
- Convert outputs into local `subtitles/youtube_{video_id}.*` artifacts.
- Validate transcripts, generate the LLM report, and publish to Feishu.

The external app owns:

- Keep the YouTube source list/profile aligned with this repository.
- Discover YouTube videos published in the requested window.
- Extract captions or ASR transcripts.
- Return complete metadata and transcript payloads.

## API Base

```text
https://gnevobefaowwiwwtfowj.supabase.co/functions/v1
```

The Bolt host is the frontend only. GitHub Actions calls Supabase Edge
Functions directly.

## Authentication

Every request must include:

```text
Authorization: Bearer <MEDIA_API_TOKEN>
Content-Type: application/json
```

## Daily Request

Endpoint:

```text
POST /daily-collector/daily-collect
```

Request body:

```json
{
  "date": "2026-07-03",
  "window_start": "2026-07-01T22:00:00Z",
  "window_end": "2026-07-02T22:00:00Z",
  "sources_profile": "youtube-default",
  "require_transcripts": true,
  "allow_asr": true
}
```

Window semantics:

- `window_start` is inclusive.
- `window_end` is exclusive.
- GitHub's daily report date `2026-07-03` maps to Beijing time
  `2026-07-02 06:00:00` through `2026-07-03 06:00:00`.
- In UTC this is `2026-07-01T22:00:00Z` through `2026-07-02T22:00:00Z`.

## Source Profile Requirement

`sources_profile: youtube-default` must cover the same YouTube sources as
`config/sources_by_category.md` in this repository.

At minimum, the external app must preserve:

- source URL
- source name
- category
- optional source rules such as `min_duration=600`

If the external app's source profile is empty or different, GitHub will still
download a syntactically valid result, but the daily report will be incomplete.

## Required `daily_items.json` Shape

`daily_items.json` must be an array. Each item should include:

```json
{
  "platform": "youtube",
  "category": "科技 / AI / VC",
  "source_name": "No Priors",
  "source_url": "https://www.youtube.com/@NoPriorsPodcast/videos",
  "title": "Video title",
  "original_title": "Video title",
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "published_at": "2026-07-02T15:00:00Z",
  "duration": 3687,
  "duration_seconds": 3687,
  "description": "Video description",
  "video_id": "VIDEO_ID"
}
```

Duration must be in seconds. GitHub filters `<300` second items before report
generation, but the external app should preferably avoid transcript extraction
for videos below 300 seconds.

## Required Transcript Payload

`subtitles_bundle.json` must allow GitHub to match transcripts by video URL or
video ID. Each transcript record should include:

```json
{
  "video_id": "VIDEO_ID",
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "title": "Video title",
  "duration_seconds": 3687,
  "last_timestamp_seconds": 3660,
  "coverage_ratio": 0.98,
  "transcript_source": "official_caption",
  "language": "en",
  "transcript_text": "Plain text transcript...",
  "transcript_vtt": "WEBVTT\n\n00:00:00.000 --> ..."
}
```

Allowed `transcript_source` values:

- `official_caption`
- `auto_caption`
- `asr`

For every video with `duration >= 300` and `require_transcripts: true`, the
external app should return transcript text and timed subtitles. If any required
transcript is missing, the job should either fail or report an explicit error.

Quality thresholds expected by GitHub:

- `coverage_ratio >= 0.95` for videos at least 300 seconds long.
- `text_chars > 0`.
- timed VTT or SRT must be present.

## Status API

Create response:

```json
{
  "job_id": "daily_xxx",
  "status": "queued"
}
```

Poll endpoint:

```text
GET /daily-collector/daily-collect/{job_id}
```

Successful terminal status:

```json
{
  "id": "daily_xxx",
  "job_type": "daily-collect",
  "status": "success",
  "request_data": {},
  "result_data": {},
  "error_type": null,
  "error_message": null
}
```

Download endpoints:

```text
GET /daily-collector/daily-collect/{job_id}/files/daily_items.json
GET /daily-collector/daily-collect/{job_id}/files/manifest.json
GET /daily-collector/daily-collect/{job_id}/files/subtitles_bundle.json
```

## 2026-07-03 Alignment Test

Test window:

```text
Beijing: 2026-07-02 06:00:00 <= published_at < 2026-07-03 06:00:00
UTC:     2026-07-01T22:00:00Z <= published_at < 2026-07-02T22:00:00Z
```

External app test result on 2026-07-03:

```text
job_id: daily_mr4ak2p8_69jwub
status: success
total_videos: 0
daily_items: []
```

Local YouTube Data API baseline from this repository for the same window:

```text
YouTube total: 9
YouTube duration >= 300s: 6
```

Expected long-form YouTube videos in the baseline:

```text
3666s | Bloomberg Podcasts | Venture Capital During the AI Revolution: Masters in Business with Mamoon Hamid
2475s | Cannonball with Wesley Morris | The History of Potato Salad
8655s | Bloomberg Television | Bloomberg Surveillance 7/2/2026
3687s | No Priors: AI, Machine Learning, Tech, & Startups | How Nuclear Will Unlock Energy Abundance with Valar Atomics Founder Isaiah Taylor
2926s | Bloomberg Podcasts | What Dan Wang Saw on His Last Trip to China | Odd Lots
5680s | Bloomberg Television | Tech Giants Lift China Stocks as Rest of Asia Slumps | The China Show | 7/2/2026
```

Conclusion:

The external API and GitHub bridge are structurally aligned, but the external
`youtube-default` source profile did not match this repository's YouTube source
set for this test. The external app must fix source profile alignment before the
daily YouTube path can be considered complete.

## Local Reproduction

```powershell
$env:MEDIA_API_TOKEN="..."

python scripts\youtube_external_daily.py `
  --date 2026-07-03 `
  --window-start "2026-07-01T22:00:00Z" `
  --window-end "2026-07-02T22:00:00Z" `
  --items-json .tmp\youtube_20260703\daily_items.json `
  --manifest-json .tmp\youtube_20260703\manifest.json `
  --status-json .tmp\youtube_20260703\status.json `
  --subtitles-dir .tmp\youtube_20260703\subtitles `
  --bundle-path .tmp\youtube_20260703\subtitles_bundle.zip
```

Optional independent baseline:

```powershell
$env:YOUTUBE_API_KEY="..."

python scripts\collect_daily_items.py `
  --date "2026-07-03T06:00:00+08:00" `
  --lookback-hours 24 `
  --youtube-backend api `
  --youtube-scan-limit 25 `
  --output-json .tmp\youtube_api_20260703\daily_items_api.json `
  --manifest-json .tmp\youtube_api_20260703\manifest_api.json `
  --output-urls .tmp\youtube_api_20260703\daily_urls_api.txt `
  --channel-cache .tmp\youtube_api_20260703\youtube_channel_cache.json
```
