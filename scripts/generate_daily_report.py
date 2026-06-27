#!/usr/bin/env python3
"""Generate a daily Markdown report from full transcript files."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests


BASE_DIR = Path(__file__).resolve().parents[1]
SPEC_PATH = BASE_DIR / "templates" / "daily_report_llm_spec.md"
REPORTS_DIR = BASE_DIR / "reports"
CATEGORIES = [
    "科技 / AI / VC",
    "商业 / 财经 / 投资",
    "产品 / 创业 / 管理",
    "新闻 / 时评 / 全球议题",
    "文化 / 社会 / 人文",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--items-json", required=True)
    parser.add_argument("--subtitles-dir", default="subtitles")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--require-transcripts",
        action="store_true",
        help="fail before generating or publishing when any non-short item has no transcript",
    )
    parser.add_argument(
        "--llm-policy",
        choices=("extractive", "required"),
        default=os.getenv("LLM_POLICY", "extractive"),
        help="extractive uses a deterministic transcript fallback; required fails without LLM config",
    )
    return parser.parse_args()


def normalize_url(url: str) -> str:
    parsed = urlparse(url or "")
    if "youtube.com" in parsed.netloc and parsed.path == "/watch":
        qs = parse_qs(parsed.query)
        if "v" in qs:
            return "https://www.youtube.com/watch?" + urlencode({"v": qs["v"][0]})
    if "youtube.com" in parsed.netloc and parsed.path == "/playlist":
        qs = parse_qs(parsed.query)
        if "list" in qs:
            return "https://www.youtube.com/playlist?" + urlencode({"list": qs["list"][0]})
    return (url or "").split("?s=")[0].rstrip("/")


def load_transcript_index(subtitles_dir: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for meta_path in subtitles_dir.glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(meta, dict):
            # extraction_results.json / asr_results.json are aggregate arrays,
            # not per-item transcript metadata files.
            continue
        url = normalize_url(meta.get("url", ""))
        text_name = meta.get("text")
        if not text_name:
            continue
        text_path = meta_path.with_name(text_name)
        if not text_path.exists():
            continue
        meta["text_path"] = str(text_path)
        index[url] = meta
    return index


def read_text(path: str | Path, max_chars: int | None = None) -> str:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return text if max_chars is None else text[:max_chars]


def llm_configured() -> bool:
    return bool(os.getenv("LLM_BASE_URL") and os.getenv("LLM_MODEL"))


def llm_chat(messages: list[dict[str, str]], temperature: float = 0.2) -> str:
    base_url = os.environ["LLM_BASE_URL"].rstrip("/")
    model = os.environ["LLM_MODEL"]
    api_key = os.getenv("LLM_API_KEY", "")
    endpoint = base_url
    if not endpoint.endswith("/chat/completions"):
        endpoint = endpoint.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = requests.post(
        endpoint,
        headers=headers,
        json={"model": model, "messages": messages, "temperature": temperature},
        timeout=int(os.getenv("LLM_TIMEOUT", "180")),
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def chunk_text(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        chunks.append(text[start:end])
        start = end
    return chunks


def summarize_item(item: dict[str, Any], transcript: str, spec: str) -> str:
    if not llm_configured():
        return extractive_item(item, transcript)

    chunk_size = int(os.getenv("LLM_CHUNK_CHARS", "30000"))
    chunks = chunk_text(transcript, chunk_size)
    partials: list[str] = []
    for idx, chunk in enumerate(chunks, 1):
        partials.append(
            llm_chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是一位专业的中文播客/视频内容研究助理。你的职责是逐段分析节目字幕，"
                            "提取结构化信息，为后续撰写专业日报做准备。\n\n"
                            "【核心原则】\n"
                            "1. 只基于给定字幕文本提取信息，严禁编造、推测或补充字幕中不存在的内容。\n"
                            "2. 保持客观中立，准确反映讲者原意，不加入个人判断。\n"
                            "3. 如果某类信息在当前片段中未出现，明确标注'未提及'。\n\n"
                            "【提取要求】\n"
                            "对每一段字幕，提取以下六类信息：\n"
                            "1. 人物/机构：所有提及的人名、公司名、组织名及职位。格式：人名（机构，职位）\n"
                            "2. 关键事实：具体事件、产品、政策、研究结论、市场动态\n"
                            "3. 核心观点：讲者明确的判断、预测、建议、批评（区分事实与观点）\n"
                            "4. 重要数据：金额、百分比、时间、人数、增长率等（保留原始数字+上下文）\n"
                            "5. 关键金句：有洞察力的原话（30字内，标注讲者）\n"
                            "6. 结构定位：本段在整体讨论中的角色（背景/论证/案例/总结）\n\n"
                            "【输出格式】\n"
                            "用以下固定格式输出，不要省略任何部分：\n\n"
                            "【第N段分析】\n"
                            "- 人物/机构：\n"
                            "- 关键事实：\n"
                            "- 核心观点：\n"
                            "- 重要数据：\n"
                            "- 关键金句：\n"
                            "- 结构定位："
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"这是第 {idx}/{len(chunks)} 段完整字幕。\n\n"
                            "请严格按照系统指令的格式提取关键信息。特别注意：\n"
                            "- 区分'讲者明确说的'和'你的推测'\n"
                            "- 数据必须带单位/上下文（如'营收增长 35%'而非仅'35%'）\n"
                            "- 如果本段没有某类信息，写'未提及'，不要省略该行\n\n"
                            f"元数据：{json.dumps(item, ensure_ascii=False)}\n\n"
                            f"字幕文本：\n{chunk}"
                        ),
                    },
                ]
            )
        )

    partials_text = "\n\n---\n\n".join(partials)
    return llm_chat(
        [
            {
                "role": "system",
                "content": (
                    "你是一位资深中文播客/视频日报编辑。你的任务是根据提取的要点和格式规范，"
                    "生成一篇结构完整、内容专业的日报条目。\n\n"
                    "【核心原则】\n"
                    "1. 严格遵循格式规范中的结构要求，不增删字段。\n"
                    "2. 所有内容必须基于提供的分段摘要，不编造任何信息。\n"
                    "3. 使用中文为主要语言，保留必要的英文专有名词。\n"
                    "4. 不写代码块，不用表格，不用整块引用（除非金句）。\n\n"
                    "【输出要求】\n"
                    "- 从 `## （N）中文短标题` 开始输出\n"
                    "- 不要输出一级标题（如 `# 1. 科技 / AI / VC`）\n"
                    "- 中文短标题控制在 15 字以内\n"
                    "- 一句话摘要必须是完整的一句话，包含'主题+意义'\n"
                    "- 完整摘要 4-8 个段落，按议题组织，不按时间顺序\n"
                    "- 关键内容按分类适配字段，无内容的字段可省略\n"
                    "- 值得后续整理的问题 2-5 个，支持后续知识库工作\n\n"
                    "【禁止使用三级标题】\n"
                    "以下字段标签必须使用 `**粗体**` 格式，严禁使用 `###` 三级标题：\n"
                    "**嘉宾与机构**、**一句话摘要**、**完整摘要**、**核心观点**、**关键内容**、**值得后续整理的问题**\n"
                    "错误示例：`### 嘉宾与机构`\n"
                    "正确示例：`**嘉宾与机构**`\n\n"
                    "【格式强制规则 — 必须严格遵守】\n"
                    "1. 核心观点必须是带编号的列表：`1. 观点一`、`2. 观点二`。严禁裸段落。\n"
                    "2. 关键金句必须用 `> ` 引用块包裹；翻译或重构的表达必须以 `意译：` 开头。\n"
                    "3. 关键数据有 3 个以上数值时，必须用表格：\n"
                    "   | 指标 | 数值 |\n"
                    "   |------|------|\n"
                    "4. 条目末尾必须输出 `---` 分割线。\n"
                    "5. 所有美元符号必须写成 `\\$`（如 `\\$200`）。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请根据以下信息生成单篇日报条目正文。\n\n"
                    "【重要提醒】\n"
                    "1. 严格按照格式规范中的 'Per-Item Required Structure' 输出\n"
                    "2. 参考格式规范最后的 'Complete Example' 了解正确的输出样式\n"
                    "3. 常见错误（必须避免）：跳过字段、用英文标题、按时间顺序写摘要、核心观点用裸段落而非编号列表、用 ### 三级标题代替 **粗体** 字段标签、关键金句不用 `> ` 引用块、忘记 `---` 分割线\n"
                    "4. 不要编造嘉宾、数据或金句；信息缺失时请如实说明\n"
                    "5. 所有 \\$ 符号必须写成 \\\\$（如 \\\\$200），不要输出裸 $ 符号\n\n"
                    f"格式规范：\n{spec}\n\n"
                    f"元数据：{json.dumps(item, ensure_ascii=False)}\n\n"
                    f"分段提取结果：\n\n{partials_text}"
                ),
            },
        ]
    )


def extract_sentences(text: str, limit: int = 10) -> list[str]:
    cleaned = re.sub(r"WEBVTT|\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->", " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    parts = re.split(r"(?<=[。！？!?])\s+|(?<=[。！？!?])|(?<=[.!?])\s+(?=[A-Z0-9\u4e00-\u9fff])", cleaned)
    sentences: list[str] = []
    seen: set[str] = set()
    for part in parts:
        sentence = part.strip(" -\t\r\n")
        if len(sentence) < 20 or sentence in seen:
            continue
        seen.add(sentence)
        sentences.append(sentence[:320])
        if len(sentences) >= limit:
            break
    return sentences


def extractive_item(item: dict[str, Any], transcript: str) -> str:
    title = short_title(item)
    source_text = transcript.strip() or (item.get("description") or "").strip()
    sentences = extract_sentences(source_text)
    one_line = sentences[0] if sentences else "未取得可用于摘要的字幕或节目描述。"
    overview = " ".join(sentences[:5]) if sentences else one_line
    core = sentences[:4] or [one_line]
    details = sentences[4:9] or sentences[:3] or [one_line]
    transcript_note = "已获取完整字幕" if item.get("transcript_available") else "基于节目公开描述"
    return f"""## （{{index}}）{title}

**原始标题**：{item.get('original_title') or item.get('title') or ''} ｜ **栏目**：{item.get('source_name', '')} ｜ **平台**：{platform_cn(item.get('platform'))} ｜ **更新**：{fmt_time(item.get('published_at'))} ｜ **时长**：{fmt_duration(item.get('duration'))} ｜ **分类**：{item.get('category', '待分类')} ｜ **推荐**：★★★☆☆
**链接**：{item.get('url', '')}

**嘉宾与机构**

- 规则模式未可靠识别；请结合原始标题、描述与字幕确认。

**一句话摘要**

{one_line}

**完整摘要**

{overview}

**核心观点**

{chr(10).join(f'{idx}. {sentence}' for idx, sentence in enumerate(core, 1))}

**关键内容**

{chr(10).join(f'- **内容 {idx}**：{sentence}' for idx, sentence in enumerate(details, 1))}

**值得后续整理的问题**

- 《{title}》中的核心判断依赖哪些事实和前提？
- 节目观点在不同市场、组织或时间尺度下是否仍然成立？

摘要模式：规则抽取（{transcript_note}）；配置 LLM 后可生成语义级中文摘要。

---
"""


def platform_cn(value: str | None) -> str:
    if value == "xiaoyuzhou":
        return "小宇宙"
    if value == "youtube":
        return "YouTube"
    return value or ""


def fmt_time(value: str | None) -> str:
    if not value:
        return "未知"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}时{m}分{s}秒"
    return f"{m}分{s}秒"


def short_title(item: dict[str, Any]) -> str:
    title = item.get("title") or item.get("original_title") or "未命名内容"
    title = re.sub(r"\s+", " ", title).strip()
    return title[:60]


def generate_overview(items: list[dict[str, Any]], spec: str) -> str:
    if not items:
        return "今天未检测到新增播客或视频。"
    if not llm_configured():
        by_platform: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for item in items:
            by_platform[platform_cn(item.get("platform"))] = by_platform.get(platform_cn(item.get("platform")), 0) + 1
            by_category[item.get("category", "待分类")] = by_category.get(item.get("category", "待分类"), 0) + 1
        return (
            f"今天共检测到 {len(items)} 条更新。"
            f"平台分布：{format_counts(by_platform)}。"
            f"分类分布：{format_counts(by_category)}。"
            "本次未配置大语言模型，正文使用字幕或节目描述进行规则抽取，不包含模型推断。"
        )
    brief = json.dumps(
        [{k: item.get(k) for k in ["platform", "category", "source_name", "title", "published_at", "duration"]} for item in items],
        ensure_ascii=False,
    )
    return llm_chat(
        [
            {
                "role": "system",
                "content": (
                    "你是一位中文播客/视频日报主编。根据今日所有条目信息，生成'概览'部分的正文。"
                    "语言简洁专业，用紧凑的段落，不用表格。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请生成日报中 `# 概览` 下的正文，不要输出标题。\n\n"
                    "【内容要求】\n"
                    "必须覆盖以下五方面：\n"
                    "1. 总更新数\n"
                    "2. 平台分布（YouTube / 小宇宙等）\n"
                    "3. 今日主要主题（用 2-3 个关键词概括）\n"
                    "4. 跨来源的模式或分歧（如有）\n"
                    "5. 阅读建议：哪些适合深度阅读、哪些适合归档、哪些可跳过\n\n"
                    "【格式要求】\n"
                    "- 用紧凑的段落，不用表格\n"
                    "- 语言简洁专业\n"
                    "- 控制在 200-400 字\n\n"
                    f"格式规范：\n{spec}\n\n"
                    f"今日条目摘要：\n{brief}"
                ),
            },
        ]
    )


def generate_noteworthy(items: list[dict[str, Any]], item_markdowns: list[str], spec: str) -> str:
    if not items:
        return "今天没有可评选的新增内容。"
    if not llm_configured():
        chosen = items[: min(5, len(items))]
        return "\n".join(
            f"{idx}. **{short_title(item)}**\n   来源：{item.get('source_name', '')}。推荐理由：本窗口内较新的更新，"
            f"{'已取得字幕，可继续深度整理' if item.get('transcript_available') else '已取得公开描述，可继续跟进'}。"
            for idx, item in enumerate(chosen, 1)
        )
    item_markdowns_text = "\n\n---\n\n".join(item_markdowns)
    return llm_chat(
        [
            {
                "role": "system",
                "content": (
                    "你是一位中文播客/视频日报主编。根据条目正文，评选本日最值得关注的内容。"
                    "评选标准明确，不受播放量等早期数据影响。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请生成日报中 `# 本日最值得关注的内容` 下的正文，不要输出标题。\n\n"
                    "【评选标准】（按优先级排序）\n"
                    "1. 信息密度：是否包含具体观点、案例、框架、数字、方法\n"
                    "2. 一手程度：讲者是否为创始人、研究者、高管、投资者、直接参与者\n"
                    "3. 时效/趋势价值：是否解释当前转变、转折点、新模式或持续辩论\n"
                    "4. 可迁移性：能否成为知识库笔记、写作素材、投资视角、产品洞察\n"
                    "5. 稀缺性：是否包含 uncommon 经验、内部视角、深度回顾、跨学科洞察\n\n"
                    "【禁止事项】\n"
                    "- 禁止按播放量、点赞数、评论数排名\n"
                    "- 禁止以'因为是最新发布的'作为唯一推荐理由\n\n"
                    "【输出格式】\n"
                    "每条推荐必须严格使用以下格式：\n"
                    "1. **中文短标题**\n"
                    "   来源：栏目名。推荐理由：1-2句话说明为什么值得关注。\n"
                    "2. **中文短标题**\n"
                    "   来源：栏目名。推荐理由：...\n\n"
                    "每条前面必须有数字编号（1. 2. 3.），严禁裸段落。\n\n"
                    "候选条目正文：\n"
                    f"{item_markdowns_text}"
                ),
            },
        ]
    )


def format_counts(counts: dict[str, int]) -> str:
    return "，".join(f"{key} {value} 条" for key, value in counts.items())


def replace_index(markdown: str, index: int) -> str:
    return markdown.replace("## （{index}）", f"## （{index}）", 1)


def main() -> int:
    args = parse_args()
    if args.llm_policy == "required" and not llm_configured():
        print("LLM is required but LLM_BASE_URL and LLM_MODEL are not configured")
        return 2
    spec = SPEC_PATH.read_text(encoding="utf-8")
    items = json.loads(Path(args.items_json).read_text(encoding="utf-8-sig"))
    # Filter out short clips (duration < 5 minutes = 300 seconds)
    original_count = len(items)
    items = [it for it in items if (it.get("duration") or 0) >= 300]
    skipped = original_count - len(items)
    if skipped:
        print(f"Skipped {skipped} short clip(s) (duration < 5min)")
    transcript_index = load_transcript_index(Path(args.subtitles_dir))

    prepared_items: list[tuple[dict[str, Any], str]] = []
    missing_transcripts: list[dict[str, Any]] = []
    for item in items:
        meta = transcript_index.get(normalize_url(item.get("url", "")))
        transcript = ""
        if meta and meta.get("text_path"):
            transcript = read_text(meta["text_path"])
        item["transcript_available"] = bool(transcript)
        if not transcript:
            missing_transcripts.append(item)
        prepared_items.append((item, transcript))

    if args.require_transcripts and missing_transcripts:
        print("Refusing to generate an incomplete report; transcripts are missing for:")
        for item in missing_transcripts:
            print(f"- {item.get('title') or item.get('url')}")
        return 3

    item_markdowns: list[tuple[dict[str, Any], str]] = []
    for item, transcript in prepared_items:
        item_markdown = summarize_item(item, transcript or item.get("description", ""), spec)
        item_markdowns.append((item, item_markdown))

    lines = [
        f"# {args.date} 播客/视频更新日报",
        "",
        "# 概览",
        "",
        generate_overview(items, spec),
        "",
    ]
    lines += [
        "# 本日最值得关注的内容",
        "",
        generate_noteworthy(items, [md for _, md in item_markdowns], spec),
        "",
    ]

    by_category: dict[str, list[tuple[dict[str, Any], str]]] = {cat: [] for cat in CATEGORIES}
    for item, md in item_markdowns:
        by_category.setdefault(item.get("category", "待分类"), []).append((item, md))

    for cat_idx, category in enumerate(CATEGORIES, 1):
        lines += [f"# {cat_idx}. {category}", ""]
        entries = by_category.get(category, [])
        if not entries:
            lines += ["今日无新增。", ""]
            continue
        for idx, (_, md) in enumerate(entries, 1):
            lines.append(replace_index(md, idx).strip())
            lines.append("")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Daily report written: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
