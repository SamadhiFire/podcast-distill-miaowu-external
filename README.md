# 🎧 Podcast Distill — 播客/视频日报自动化

> 每天早上 6:15（北京时间），自动抓取你关注的播客和视频频道，提取完整字幕，生成结构化中文摘要，发布到飞书知识库，并推送到飞书群。

## 📖 日报在哪里看

所有日报自动发布到飞书知识库，每日更新：

👉 **[点击查看每日播客/视频日报](https://my.feishu.cn/wiki/space/7655607441056337129?ccm_open_type=lark_wiki_spaceLink&open_tab_from=wiki_home)**

## ✨ 它能做什么

- **多源采集**：自动从小宇宙播客、YouTube 频道、B 站等平台抓取每日更新
- **字幕提取**：优先获取官方字幕，无字幕时使用 Whisper.cpp 本地语音识别转录
- **智能摘要**：配置大语言模型时生成语义摘要；未配置时使用确定性的字幕抽取摘要
- **分类整理**：按「科技/AI/VC」「商业/财经/投资」「产品/创业/管理」「新闻/时评/全球议题」「文化/社会/人文」五大板块分类
- **自动发布**：每日早上自动运行；任一长内容缺少字幕时停止发布，避免把不完整日报写入飞书

## 📋 日报包含什么

每则内容包含：

- **基本信息**：原始标题、栏目/频道、平台、更新时间、分类、推荐星级
- **嘉宾与机构**：出场人物和相关机构
- **一句话摘要**：快速了解核心内容
- **完整摘要**：详细内容概括
- **核心观点**：提炼关键论点
- **关键内容**：值得记录的细节与数据
- **值得后续整理的问题**：启发思考的延伸话题

## ⏰ 运行时间

- **每日北京时间 06:15** 自动运行（数据窗口在 06:00 关闭后再启动）
- 采集窗口为前一天 06:00 至当天 06:00 的更新
- 也支持手动触发，可指定日期补跑

## 🛠 技术栈

- **字幕提取**：yt-dlp（视频元数据/字幕）+ Whisper.cpp（ASR 语音识别，small-q5_1 量化模型）
- **摘要生成**：OpenAI 兼容 API（可选）+ 无模型时的确定性字幕抽取降级
- **文档发布**：飞书开放 API + lark-cli
- **自动化**：GitHub Actions 定时调度
- **运行环境**：GitHub Actions Ubuntu CI + Windows 本地预编译 Whisper 二进制

## 📁 项目结构

```
podcast-distill/
├── .github/workflows/    # GitHub Actions 工作流
├── config/               # 播客源配置（urls.txt, podcasts.txt 等）
├── scripts/              # 核心脚本
│   ├── collect_daily_items.py   # 每日更新采集
│   ├── generate_daily_report.py # AI 摘要生成
│   └── publish_feishu.py        # 飞书文档发布
├── templates/            # 日报格式规范
├── whisper-bin-x64/      # Windows Whisper 预编译二进制+模型
├── extract_subtitles.py  # 字幕提取主程序
└── requirements.txt      # Python 依赖
```

## GitHub Actions 配置

主工作流为 `.github/workflows/daily-digest.yml`。在 GitHub 仓库的 Actions Secrets 中配置：

- 必需：`YOUTUBE_API_KEY`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_WIKI_SPACE_ID`、`FEISHU_NOTIFY_WEBHOOK`
- 可选：`FEISHU_PARENT_NODE_TOKEN`、`YTDLP_COOKIES_B64`、`BILIBILI_COOKIE`
- 语义级摘要：`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`

不配置 LLM 时，Actions 仍能完成采集、字幕/ASR、规则摘要、飞书写入和群通知，但摘要质量不会等同于人工或大模型语义整理。工作流固定使用 whisper.cpp v1.9.1 与 `small-q5_1` 模型，并在生成日报前检查所有五分钟以上内容是否已有完整转录。
