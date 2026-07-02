# Podcast Distill Miaoda

一个很薄的播客 / 视频日报自动化仓库。

当前主线：

- 小宇宙：扫描播客更新，解析 episode 音频，调用通义听悟 ASR，输出完整逐字稿。
- YouTube：保留本地字幕提取工具，可按需提取官方字幕 / 自动字幕。
- GitHub Actions：定时或手动运行，校验字幕包，基于完整逐字稿生成结构化中文日报，发布到飞书知识库并通知群机器人。

仓库不再保留旧回填数据、本地 Whisper 二进制、调试脚本、运行产物和测试输出。

## 目录

```text
.github/workflows/daily-digest.yml     GitHub Actions 主流程
config/                                信源配置
docs/                                  运维说明
scripts/                               采集、校验、日报生成、飞书发布脚本
templates/                             LLM 日报内容契约
extract_subtitles.py                   YouTube 字幕提取入口
```

## GitHub Secrets

小宇宙 / 通义听悟：

```text
TINGWU_APP_KEY
ALIBABA_CLOUD_ACCESS_KEY_ID
ALIBABA_CLOUD_ACCESS_KEY_SECRET
```

日报生成模型：

```text
LLM_BASE_URL
LLM_API_KEY
LLM_MODEL
```

飞书：

```text
FEISHU_APP_ID
FEISHU_APP_SECRET
FEISHU_WIKI_SPACE_ID
FEISHU_NOTIFY_WEBHOOK
```

不要把任何 token、AccessKey、飞书 secret 写进仓库文件。

## 手动运行

安装依赖：

```bash
pip install -r requirements.txt
```

采集小宇宙并转写：

```bash
python scripts/tingwu_xiaoyuzhou_daily.py \
  --date 2026-07-02 \
  --timezone Asia/Shanghai \
  --items-json reports/daily_items_2026-07-02.json \
  --manifest-json reports/daily_items_2026-07-02.manifest.json \
  --bundle-path subtitles_bundle.zip \
  --status-json reports/tingwu_xiaoyuzhou_2026-07-02.status.json \
  --subtitles-dir subtitles
```

校验字幕包：

```bash
python scripts/validate_transcript_bundle.py \
  --items-json reports/daily_items_2026-07-02.json \
  --manifest-json reports/daily_items_2026-07-02.manifest.json \
  --subtitles-dir subtitles \
  --bundle-zip subtitles_bundle.zip \
  --min-coverage 0.95 \
  --min-duration-seconds 300
```

生成日报：

```bash
python scripts/generate_daily_report.py \
  --date 2026-07-02 \
  --items-json reports/daily_items_2026-07-02.json \
  --subtitles-dir subtitles \
  --require-transcripts \
  --llm-policy required \
  --output reports/daily-2026-07-02.md \
  --output-json reports/daily-2026-07-02.json
```

发布飞书：

```bash
python scripts/publish_feishu.py \
  --file reports/daily-2026-07-02.md \
  --title "2026-07-02 小宇宙播客日报"
```

## 发布门禁

以下情况会失败并阻止发布：

- 5 分钟以上内容缺少逐字稿。
- `sha256` 本地复算不一致。
- `text_chars` 为空或与本地文本长度不一致。
- `coverage_ratio < 0.95`。
- 字幕来源不是 `official_caption`、`auto_caption` 或 `asr`。
- 待发布内容出现明显编码损坏，例如连续 `????`。

无论成功或失败，GitHub Actions 都会上传运行 artifacts，便于排查。
