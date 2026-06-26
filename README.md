# Podcast Distill

This repository collects new podcast/video updates, extracts full subtitles/transcripts, generates a structured Chinese daily report, publishes it into Feishu Wiki, and notifies a Feishu group bot.

## Daily Report Format

The LLM-readable format specification is stored at:

- `templates/daily_report_llm_spec.md`

Daily report title format:

```md
# 2026-06-26 播客/视频更新日报
```

Required first-level headings:

```md
# 概览
# 本日最值得关注的内容
# 1. 科技 / AI / VC
# 2. 商业 / 财经 / 投资
# 3. 产品 / 创业 / 管理
# 4. 新闻 / 时评 / 全球议题
# 5. 文化 / 社会 / 人文
```

Items under each category use Chinese parenthesized numbering:

```md
## （1）中文短标题
## （2）中文短标题
```

Each item must include:

```md
**原始标题**：... ｜ **栏目**：... ｜ **平台**：... ｜ **更新**：... ｜ **分类**：... ｜ **推荐**：★★★★☆
**链接**：...

### 嘉宾与机构
### 一句话摘要
### 完整摘要
### 核心观点
### 关键内容
### 值得后续整理的问题
```

The report must be generated from full subtitles/transcripts. Platform descriptions are only auxiliary metadata.

## Source Categories

Mixed Xiaoyuzhou + YouTube categories are stored at:

- `config/sources_by_category.md`

Machine-readable YouTube sources are stored at:

- `config/youtube_sources.txt`

Xiaoyuzhou podcast sources are stored at:

- `config/podcasts.txt`

## GitHub Actions

Main workflow:

- `.github/workflows/daily-digest.yml`

Manual run options:

- `date`: report date, `YYYY-MM-DD`. Empty means today in Asia/Shanghai.
- `dry_run_feishu`: generate report but skip real Feishu publishing.

Default schedule:

- 23:30 Beijing time every day.

## Required GitHub Secrets

Configure these in:

`GitHub repo -> Settings -> Secrets and variables -> Actions -> New repository secret`

### Feishu Wiki Publishing

- `FEISHU_APP_ID`: Feishu app ID.
- `FEISHU_APP_SECRET`: Feishu app secret.
- `FEISHU_WIKI_SPACE_ID`: Feishu wiki space ID.
- `FEISHU_PARENT_NODE_TOKEN`: optional. If set, the daily doc is created under this parent wiki node.

The Feishu app must have permissions to create wiki nodes and edit docx documents in the target wiki space. Also make sure the app is allowed to access that wiki space.

### Feishu Group Notification

- `FEISHU_NOTIFY_WEBHOOK`: Feishu group bot webhook URL.

The workflow sends a message similar to:

```text
今日日报已完成：2026-06-26 播客/视频更新日报
```

### LLM

These can be left empty for now. If empty, the workflow still creates a structured scaffold report, but summaries are placeholders.

- `LLM_BASE_URL`: OpenAI-compatible chat completions endpoint or base URL.
- `LLM_API_KEY`: API key.
- `LLM_MODEL`: model name.

Examples of accepted `LLM_BASE_URL` styles:

```text
https://api.example.com/v1
https://api.example.com/v1/chat/completions
```

### Subtitle Extraction / Platform Access

- `XIAOYUZHOU_ACCESS_TOKEN`: optional but recommended. Used to fetch Xiaoyuzhou official full transcript sentences when available.
- `BILIBILI_COOKIE`: optional. Used if Bilibili subtitles require login.
- `YTDLP_COOKIES_B64`: optional. Base64 encoded Netscape `cookies.txt` for `yt-dlp`, useful for YouTube/Bilibili rate limits or login-gated captions.

Do not commit raw cookies, app secrets, webhooks, or access tokens to the repository.

## Local Usage

Install dependencies:

```powershell
& "C:\Users\AS\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Collect daily updated items:

```powershell
.\.venv\Scripts\python.exe scripts\collect_daily_items.py --date 2026-06-26 --output-json reports\daily_items_2026-06-26.json --output-urls config\daily_urls.txt
```

Extract full subtitles:

```powershell
.\.venv\Scripts\python.exe extract_subtitles.py --batch config\daily_urls.txt --output subtitles
```

Generate the daily report:

```powershell
.\.venv\Scripts\python.exe scripts\generate_daily_report.py --date 2026-06-26 --items-json reports\daily_items_2026-06-26.json --subtitles-dir subtitles --output reports\daily-2026-06-26.md
```

Publish to Feishu in dry-run mode:

```powershell
.\.venv\Scripts\python.exe scripts\publish_feishu.py --file reports\daily-2026-06-26.md --title "2026-06-26 播客/视频更新日报" --dry-run
```

## Important Notes

- YouTube can usually provide full manual or automatic captions. If not, the workflow falls back to audio transcription through `whisper.cpp`.
- Xiaoyuzhou public podcast pages expose only recent episode metadata. Official full transcript access requires login state through `XIAOYUZHOU_ACCESS_TOKEN`; otherwise the workflow falls back to ASR.
- The daily report is intended for knowledge-base use, so it uses stable headings and structured item sections.
