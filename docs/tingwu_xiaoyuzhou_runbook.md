# 通义听悟小宇宙转写链路

这条链路负责小宇宙播客：

1. 根据日报日期计算北京时间窗口：`D-1 06:00:00` 到 `D 06:00:00`。
2. 扫描 `config/xiaoyuzhou_sources.json` 里的播客。
3. 找出窗口内新发布的 episode。
4. 解析真实音频地址。
5. 调用通义听悟转写。
6. 输出 `daily_items.json`、`manifest.json`、`subtitles_bundle.zip`。
7. 校验通过后生成日报并发布飞书。

## GitHub Secrets

在 `Settings -> Secrets and variables -> Actions` 配置：

```text
TINGWU_APP_KEY
ALIBABA_CLOUD_ACCESS_KEY_ID
ALIBABA_CLOUD_ACCESS_KEY_SECRET
LLM_BASE_URL
LLM_API_KEY
LLM_MODEL
FEISHU_APP_ID
FEISHU_APP_SECRET
FEISHU_WIKI_SPACE_ID
FEISHU_NOTIFY_WEBHOOK
```

不要把 AccessKey、Secret 或飞书凭证提交到仓库。

## 只扫描更新

```bash
python scripts/tingwu_xiaoyuzhou_daily.py \
  --date 2026-07-02 \
  --discover-only
```

对应窗口：

```text
2026-07-01T06:00:00+08:00
2026-07-02T06:00:00+08:00
```

## 本地转写

macOS / Linux：

```bash
export TINGWU_APP_KEY="..."
export ALIBABA_CLOUD_ACCESS_KEY_ID="..."
export ALIBABA_CLOUD_ACCESS_KEY_SECRET="..."

python scripts/tingwu_xiaoyuzhou_daily.py \
  --date 2026-07-02 \
  --poll-interval 20 \
  --timeout-seconds 18000
```

Windows PowerShell：

```powershell
$env:TINGWU_APP_KEY="..."
$env:ALIBABA_CLOUD_ACCESS_KEY_ID="..."
$env:ALIBABA_CLOUD_ACCESS_KEY_SECRET="..."

python scripts\tingwu_xiaoyuzhou_daily.py `
  --date 2026-07-02 `
  --poll-interval 20 `
  --timeout-seconds 18000
```

## 输出文件

```text
reports/daily_items_YYYY-MM-DD.json
reports/daily_items_YYYY-MM-DD.manifest.json
reports/tingwu_xiaoyuzhou_YYYY-MM-DD.status.json
subtitles_bundle.zip
subtitles/
  xiaoyuzhou_{episode_id}.txt
  xiaoyuzhou_{episode_id}.srt
  xiaoyuzhou_{episode_id}.vtt
  xiaoyuzhou_{episode_id}.json
```

## 校验

```bash
python scripts/validate_transcript_bundle.py \
  --items-json reports/daily_items_2026-07-02.json \
  --manifest-json reports/daily_items_2026-07-02.manifest.json \
  --subtitles-dir subtitles \
  --bundle-zip subtitles_bundle.zip \
  --min-coverage 0.95 \
  --min-duration-seconds 300
```

## 常见错误

`PRE.AudioDurationQuotaLimit`

含义：通义听悟项目可用音频时长额度不足。代码链路本身正常，需要在阿里云控制台开通商用或提升音频转写额度后重试。

## 缓存

本地缓存目录：

```text
.cache/tingwu/
```

同一 episode 成功转写后，脚本会复用缓存，避免重复提交通义听悟任务。
