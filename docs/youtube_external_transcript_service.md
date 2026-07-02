# YouTube external transcript service

GitHub Actions should not extract YouTube subtitles directly. The workflow now
uses the external Bolt/Supabase service as a transcript relay, then downloads
and converts the outputs into this repository's existing artifact format.

## GitHub configuration

Add this required repository secret:

```text
MEDIA_API_TOKEN
```

Optional repository variables:

```text
MEDIA_API_BASE
YOUTUBE_SOURCES_PROFILE
```

`MEDIA_API_BASE` defaults to:

```text
https://gnevobefaowwiwwtfowj.supabase.co/functions/v1
```

`YOUTUBE_SOURCES_PROFILE` defaults to:

```text
youtube-default
```

The YouTube API key and Supabase service key are used by the external service
itself. They do not need to be configured in this GitHub repository unless a
future workflow step calls YouTube directly again.

## Flow

```text
GitHub Actions
  -> POST /daily-collector/daily-collect
  -> poll /daily-collector/daily-collect/{job_id}
  -> download daily_items.json, manifest.json, subtitles_bundle.json
  -> write reports/daily_items_YYYY-MM-DD.json
  -> write subtitles/youtube_{video_id}.*
  -> rebuild subtitles_bundle.zip
  -> validate, generate LLM report, publish to Feishu
```

The raw `https://youtube-transcript-s-tis4.bolt.host` URL is the frontend. The
callable API is the Supabase functions base URL above.

## Local single-video test

PowerShell:

```powershell
$env:MEDIA_API_TOKEN="..."

python scripts\youtube_external_daily.py `
  --single-url "https://www.youtube.com/watch?v=aircAruvnKk" `
  --date 2026-07-02 `
  --items-json .tmp\youtube_items.json `
  --manifest-json .tmp\youtube_manifest.json `
  --status-json .tmp\youtube_status.json `
  --subtitles-dir .tmp\youtube_subtitles `
  --bundle-path .tmp\youtube_bundle.zip `
  --poll-interval 2 `
  --timeout-seconds 180

python scripts\validate_transcript_bundle.py `
  --items-json .tmp\youtube_items.json `
  --manifest-json .tmp\youtube_manifest.json `
  --subtitles-dir .tmp\youtube_subtitles `
  --bundle-zip .tmp\youtube_bundle.zip `
  --min-coverage 0.95 `
  --min-duration-seconds 300
```
