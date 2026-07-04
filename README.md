# Podcast Distill

一个很薄的播客 / 视频日报自动化仓库。

当前主线：

- 小宇宙：扫描播客更新，解析 episode 音频，调用通义听悟 ASR，输出完整逐字稿。
- YouTube：通过外部字幕服务提取官方字幕 / 自动字幕，避免 GitHub Actions 直接访问 YouTube 被屏蔽；本地字幕工具保留为备用入口。
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

## YouTube 字幕服务

当前 YouTube 字幕提取通过外部中转服务完成：

- 服务地址：https://youtube-transcript-s-tis4.bolt.host
- 服务源码：[SamadhiFire/YouTube-boltnew](https://github.com/SamadhiFire/YouTube-boltnew)

复用者可以直接调用该服务，或 fork 这个 Bolt.new 项目部署自己的字幕提取服务。本仓库的 GitHub Actions 只负责向外部服务提交 YouTube 链接、下载字幕结果，再交给 LLM 生成日报并发布到飞书。

## 最终发布位置

日报最终会发布到飞书知识库“半脑互搏”，也可以通过飞书知识问答入口检索归档内容：

- 飞书知识库：https://my.feishu.cn/wiki/space/7655607441056337129?ccm_open_type=lark_wiki_spaceLink&open_tab_from=wiki_home
- 飞书知识问答：https://ask.feishu.cn/shared-space/7655607441056337129

扫码也可以直接查看知识库：

![半脑互搏飞书知识库二维码](docs/assets/feishu-knowledge-base-qr.png)

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

YouTube 字幕服务：

```text
MEDIA_API_TOKEN
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
  --title "2026-07-02 播客与视频更新日报"
```

## 发布门禁

以下情况会失败并阻止发布：

- 5 分钟以上内容缺少逐字稿。
- `sha256` 本地复算不一致。
- `text_chars` 为空或与本地文本长度不一致。
- `coverage_ratio < 0.95`。
- 字幕来源不是 `official_caption`、`auto_caption` 或 `asr`。
- 待发布内容出现明显编码损坏，例如连续问号乱码。

无论成功或失败，GitHub Actions 都会上传运行 artifacts，便于排查。
