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

Each item must follow this structure. **All field labels use `**粗体**` format, NOT `###` headings.**

```md
## （N）中文短标题

**原始标题**：Original title ｜ **栏目**：Source name ｜ **平台**：YouTube/小宇宙 ｜ **更新**：YYYY-MM-DD HH:MM ｜ **时长**：1时23分45秒 ｜ **分类**：中文分类 ｜ **推荐**：★★★★☆
**链接**：https://...

**嘉宾与机构**

**一句话摘要**

**完整摘要**

**核心观点**

**关键内容**

**值得后续整理的问题**
```

Metadata should be one line, with the source link on a second line. Never include `处理状态`.

**Important**: `**嘉宾与机构**`, `**一句话摘要**`, `**完整摘要**`, `**核心观点**`, `**关键内容**`, `**值得后续整理的问题**` are bold text labels, NOT headings. Never use `###` for these fields.

## Guest And Organization Rules

Under `**嘉宾与机构**`, list speakers and organizations when they can be inferred from the transcript or metadata.

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

Under `**一句话摘要**`, write exactly one concise Chinese sentence.

It must explain:

- What the episode is about.
- Why it matters.

## Full Summary Rules

Under `**完整摘要**`, write a structured summary based on the full transcript.

Rules:

- Use 4 to 8 paragraphs.
- Organize by issues and arguments, not by timestamp.
- Explain who is talking, what they argue, what evidence or cases they use, and what changes or implications matter.
- Include enough context for someone who did not listen to the episode.
- Do not overquote. Use paraphrase by default.

## Reader Hierarchy And Emphasis Rules

The report should be scannable first and complete second:

- Use first-level headings only for the report overview and category sections.
- Use second-level headings only for individual items.
- Use bold for field labels and a few short key terms; never bold a whole sentence or paragraph.
- Use numbered lists for claims that have an order or need to be compared, and bullets for parallel facts.
- Use blockquotes only for exact or near-verbatim speaker wording. If wording is reconstructed or translated, prefix it with `意译：`.
- Do not use blockquotes for editor notes, status messages, ordinary summaries, or recommendations.
- Keep each paragraph focused on one issue so readers can stop after the one-sentence summary or continue for the full context.

## Core Viewpoints Rules

Under `**核心观点**`, list 3 to 7 numbered points.

**CRITICAL**: Each point MUST be a numbered list item starting with `1. `, `2. `, etc. NEVER use bare paragraphs without numbers. In Feishu, bare paragraphs are visually indistinguishable from the full summary above them.

Each point should be a real claim, not a topic label.

Good:

```md
1. 旧 benchmark 会被模型快速刷穿，因此评测必须更接近真实任务。
2. 评测滞后于模型发展是结构性问题，而非暂时现象。
```

Bad:

```md
旧 benchmark 会被模型快速刷穿，因此评测必须更接近真实任务。
评测滞后于模型发展是结构性问题。
```

## Key Content Rules

`**关键内容**` must adapt to the category. Do not force technology fields onto business or humanities episodes.

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

If the transcript contains memorable lines, include only short quotes or clearly labeled paraphrases under `关键金句`. Each key quote MUST be wrapped in `> ` blockquote syntax, NOT as a plain list item. Prefix translated or reconstructed wording with `意译：`; never present a paraphrase as a verbatim quote.

Example:

```md
- **关键金句**：

> 意译：当 benchmark 成为目标时，它就不再是好的 benchmark
```

## Data Table Rules

When `关键数据` or `关键时间线/数据` contains 3 or more numeric values, use a markdown table instead of a list:

```md
- **关键数据**：

| 指标 | 数值 |
|------|------|
| 霍尔木兹海峡日均通过量 | 2000万桶 |
| 净损失 | 1300万桶/日 |
| 中国进口下降 | 500万桶/日 |
```

For 1-2 values, a list is acceptable.

## Entry Divider Rules

After `**值得后续整理的问题**`, every entry MUST end with a `---` divider line. This creates a clear visual boundary between entries in Feishu.

```md
**值得后续整理的问题**

- 问题1
- 问题2

---
```

## Follow-Up Questions Rules

Under `**值得后续整理的问题**`, list 2 to 5 questions.

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

## Complete Example (One Item)

Below is a complete example of a single item. Follow this exact structure, heading levels, and spacing:

```markdown
## （1）OpenAI 为何放弃传统 Benchmark

**原始标题**：Why Traditional Benchmarks Fail Modern AI Models with OpenAI Research Lead ｜ **栏目**：The Gradient ｜ **平台**：YouTube ｜ **更新**：2024-06-15 08:30 ｜ **时长**：36分19秒 ｜ **分类**：科技 / AI / VC ｜ **推荐**：★★★★★
**链接**：https://www.youtube.com/watch?v=AZrU6y3pUcU

**嘉宾与机构**

- OpenAI 研究负责人（未公开具体姓名）：OpenAI，评测与对齐团队
- 主持人：The Gradient 播客

**一句话摘要**

OpenAI 研究负责人系统阐述了传统 NLP benchmark 被大模型快速刷穿的原因，并提出未来评测必须转向真实任务评估与动态对抗测试。

**完整摘要**

节目围绕 AI 模型评测危机展开深度讨论。嘉宾指出，传统静态 benchmark（如 GLUE、SuperGLUE）在 GPT-4 时代已失去区分度，模型通过规模化和训练数据覆盖即可达到人类水平，这种"刷榜"行为掩盖了模型在真实场景中的脆弱性。

嘉宾回顾了评测方法的三次演变：从早期任务-specific 的指标，到通用语言理解 benchmark，再到当前以人类偏好对齐为核心的评估范式。他强调，每一次评测升级都伴随着模型能力的跃迁，但评测本身始终滞后于模型发展。

针对解决方案，嘉宾提出两个核心方向。第一是"真实任务评估"，即让模型在实际工作流中接受测试，而非在标准化数据集上比拼分数。第二是"动态对抗测试"，通过持续生成新难度样本，迫使模型展现真正的推理能力而非记忆能力。

讨论还涉及评测商业化的伦理问题。嘉宾警告，如果评测标准被少数机构垄断，可能导致研究方向的人为扭曲，因此倡导开源社区参与评测标准的制定与更新。

**核心观点**

1. 静态 benchmark 的生命周期正在缩短，GPT-4 级别的模型可以在数月内刷穿原本设计给人类专家的测试集。
2. 评测滞后于模型发展是结构性问题，而非暂时现象，需要从根本上改变评测范式。
3. 真实任务评估比标准化分数更能反映模型的实际效用，但实施成本更高、更难规模化。
4. 动态对抗测试可以有效区分"记忆"与"推理"，但需要持续投入且存在被逆向工程的风险。
5. 评测标准的制定权不应集中在少数机构手中，开源参与是防止方向扭曲的关键。

**关键内容**

- **关键概念**：静态 benchmark、动态对抗测试、真实任务评估、人类偏好对齐、刷榜（benchmark saturation）
- **技术判断**：传统 benchmark 已无法有效区分前沿模型；未来 2-3 年内评测行业将经历范式转移
- **产品/研究启发**：企业在选型大模型时应设计自己的真实任务测试集，而非依赖公开 leaderboard
- **关键数据**：GLUE 基准从 2018 年提出到被刷穿约 4 年；SuperGLUE 仅维持约 2 年有效区分度
- **关键金句**：

> 意译：当评测成为目标时，它就不再是好的评测

**值得后续整理的问题**

- 动态对抗测试的具体实现机制是什么？有哪些已有框架可以参考？
- 国内企业如何建立适合自己的真实任务评估体系？
- 评测标准开源化是否会带来新的安全和滥用风险？

---
```

## Common Mistakes to Avoid

When generating the report, NEVER do the following:

1. **Do not skip required fields.** Every item must have: 嘉宾与机构, 一句话摘要, 完整摘要, 核心观点, 关键内容, 值得后续整理的问题.
2. **Do not use English-only headings.** All headings must be in Chinese.
3. **Do not output the document title or category headings inside a single item.** A single item starts with `## （N）中文短标题` and ends after 值得后续整理的问题.
4. **Do not rank by view count or likes** in the "最值得关注" section.
5. **Do not invent information.** If a guest's name is unclear, say so. If no data is mentioned, omit the data field.
6. **Do not use tables** in the overview section. Tables are only allowed in `关键数据` when there are 3+ numeric values.
7. **Do not wrap the entire output in a code block.** Output raw Markdown.
8. **Do not use `###` for field labels.** Guest, summary, viewpoints, key content, and follow-up questions are bold text (`**嘉宾与机构**`), NOT third-level headings (`### 嘉宾与机构`).
9. **Do not use bare paragraphs for core viewpoints.** Every viewpoint MUST be a numbered list item (`1. `, `2. `, etc.).
10. **Do not use bare paragraphs for most-noteworthy.** Every item in "本日最值得关注的内容" MUST be numbered (`1. **title**`, `2. **title**`, etc.).
11. **Do not forget the `---` divider at the end of each entry.** Every entry must end with `---` after 值得后续整理的问题.
12. **Do not skip `> ` blockquote for key quotes.** Every key quote under `关键金句` must use `> ` blockquote syntax.
13. **Do not skip table format for 3+ data points.** Use `| 指标 | 数值 |` table format when 关键数据 has 3 or more values.
14. **Do not write bare `$` signs.** Always write `\$` for literal dollar signs (e.g., `\$200`).
