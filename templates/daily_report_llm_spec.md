# Daily Podcast/Video Report Format Specification

This specification is written for an LLM. Follow it exactly when generating the daily report.

## Core Goal

Generate a Chinese daily report from full transcripts of newly updated podcasts/videos. The report is for knowledge-base ingestion, not casual chat.

Do not summarize from platform descriptions alone. Platform descriptions may be used only as auxiliary metadata. The main summary must be based on the full transcript text.

## Document Title

Use this exact title format:

```md
# YYYY-MM-DD 播客/视频更新日报
```

## Required Top-Level Structure

Use exactly these first-level headings:

```md
# YYYY-MM-DD 播客/视频更新日报

# 概览

# 本日最值得关注的内容

# 1. 科技 / AI / VC

# 2. 商业 / 财经 / 投资

# 3. 产品 / 创业 / 管理

# 4. 新闻 / 时评 / 全球议题

# 5. 文化 / 社会 / 人文
```

All category headings must be present even when a category has no updates.

## Overview Rules

Under `# 概览`, write a complete integrated overview of today's updates.

The overview must cover:

- Total number of updates.
- Platform distribution, such as YouTube and Xiaoyuzhou.
- Main themes across all updates.
- Important cross-source patterns or disagreements.
- Which updates are suitable for deep reading, light archive, or skipping.

Do not use a table for the overview. Use compact paragraphs.

## Most Noteworthy Content Rules

Under `# 本日最值得关注的内容`, select 3 to 5 items when possible. Do not rank by views, likes, comments, or early engagement because newly published items are not comparable.

Use this value standard:

1. 信息密度: concrete ideas, cases, frameworks, numbers, methods.
2. 一手程度: speaker is a founder, researcher, executive, investor, policy participant, creator, or direct witness.
3. 时效/趋势价值: explains a current shift, turning point, new pattern, or ongoing debate.
4. 可迁移性: can become a knowledge-base note, framework, writing material, investment lens, or product insight.
5. 稀缺性: contains uncommon experience, inside view, deep retrospective, or cross-disciplinary insight.

Output format:

```md
1. **中文短标题**
   来源：栏目名。推荐理由：用 1-2 句话说明为什么值得关注。
```

## Category Item Heading Rules

Inside each category, each item must use a second-level heading.

Use Chinese parenthesized numbering:

```md
## （1）中文短标题
## （2）中文短标题
```

Do not use `## 1.` numbering. Do not use English-only titles. If the original title is English, translate it into short Chinese.

## Per-Item Required Structure

Each item must follow this structure:

```md
## （N）中文短标题

**原始标题**：Original title ｜ **栏目**：Source name ｜ **平台**：YouTube/小宇宙 ｜ **更新**：YYYY-MM-DD HH:MM ｜ **分类**：中文分类 ｜ **推荐**：★★★★☆
**链接**：https://...

### 嘉宾与机构

### 一句话摘要

### 完整摘要

### 核心观点

### 关键内容

### 值得后续整理的问题
```

Metadata should be one line, with the source link on a second line. Never include `处理状态`.

## Guest And Organization Rules

Under `### 嘉宾与机构`, list speakers and organizations when they can be inferred from the transcript or metadata.

Use this format:

```md
- 中文名（English Name）：中文公司名（English Company），职位1 / 职位2
```

Rules:

- Chinese name comes first.
- English name goes in parentheses.
- If a company name is English, provide a Chinese translation first and the English name in parentheses when reasonable.
- Include no more than 3 roles per person.
- If the host is important, include the host.
- If speakers are unknown, write `- 未明确识别：节目未提供足够清晰的嘉宾信息。`

## One-Sentence Summary Rules

Under `### 一句话摘要`, write exactly one concise Chinese sentence.

It must explain:

- What the episode is about.
- Why it matters.

## Full Summary Rules

Under `### 完整摘要`, write a structured summary based on the full transcript.

Rules:

- Use 4 to 8 paragraphs.
- Organize by issues and arguments, not by timestamp.
- Explain who is talking, what they argue, what evidence or cases they use, and what changes or implications matter.
- Include enough context for someone who did not listen to the episode.
- Do not overquote. Use paraphrase by default.

## Core Viewpoints Rules

Under `### 核心观点`, list 3 to 7 numbered points.

Each point should be a real claim, not a topic label.

Good:

```md
1. 旧 benchmark 会被模型快速刷穿，因此评测必须更接近真实任务。
```

Bad:

```md
1. Benchmark。
```

## Key Content Rules

`### 关键内容` must adapt to the category. Do not force technology fields onto business or humanities episodes.

For `科技 / AI / VC`, prefer:

- **关键概念**：
- **技术判断**：
- **产品/研究启发**：
- **关键数据**：
- **关键金句**：

For `商业 / 财经 / 投资`, prefer:

- **关键公司/行业**：
- **商业判断**：
- **市场信号**：
- **关键数据**：
- **关键金句**：

For `产品 / 创业 / 管理`, prefer:

- **关键问题**：
- **方法论**：
- **组织/增长启发**：
- **可复用框架**：
- **关键金句**：

For `新闻 / 时评 / 全球议题`, prefer:

- **事件背景**：
- **核心争议**：
- **各方立场**：
- **关键时间线/数据**：
- **关键金句**：

For `文化 / 社会 / 人文`, prefer:

- **讨论主题**：
- **人物/作品/社会现象**：
- **核心洞察**：
- **情绪或价值判断**：
- **关键金句**：

Only include fields that have meaningful content. It is acceptable to omit irrelevant fields.

If the transcript contains important numbers, dates, amounts, percentages, counts, or rankings, include them under `关键数据` or `关键时间线/数据`.

If the transcript contains memorable lines, include only short Chinese paraphrases or short quotes under `关键金句`.

## Follow-Up Questions Rules

Under `### 值得后续整理的问题`, list 2 to 5 questions.

Questions should support later knowledge-base work, such as:

- Future research.
- Topic clustering.
- Investment/product/content judgment.
- Follow-up reading.

## Style Rules

- Use Chinese as the main language.
- Preserve original English names when useful.
- Be concise but complete.
- Do not use code blocks in the generated report unless the episode itself is about code and the code is important.
- Use blockquotes only for truly important short quotes.
- Do not invent guests, companies, data, or quotes.
- If information is missing, say it is not clearly mentioned.
