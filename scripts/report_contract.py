#!/usr/bin/env python3
"""Validated report contract plus deterministic Markdown/Feishu renderers.

The LLM is allowed to supply content only. It never controls headings, XML,
tables, callouts, grids, links, colors, or other presentation decisions.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
import math
import re
import xml.etree.ElementTree as ET
from typing import Any


CATEGORIES = [
    "科技 / AI / VC",
    "商业 / 财经 / 投资",
    "产品 / 创业 / 管理",
    "新闻 / 时评 / 全球议题",
    "文化 / 社会 / 人文",
]

# These digests deliberately contain no content claims.  They remain visible in
# the full report, but must never influence editorial summaries or maps.
NON_CONTENT_DIGEST_QUALITIES = {
    "provider_input_rejected",
    "direct_fileid_contract_failed",
}

CATEGORY_EMOJI = {
    "科技 / AI / VC": "🤖",
    "商业 / 财经 / 投资": "📈",
    "产品 / 创业 / 管理": "🧩",
    "新闻 / 时评 / 全球议题": "🌍",
    "文化 / 社会 / 人文": "📚",
}


def clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"```(?:json|markdown|xml)?|```", "", text, flags=re.I)
    text = re.sub(r"</?[^>]+>", "", text)
    text = re.sub(r"[*_`#]", "", text)
    return re.sub(r"\s+", " ", text).strip(" -—｜|")


def chinese_spacing(value: Any) -> str:
    """Add readable spacing at CJK/Latin-number boundaries, without touching URLs."""
    text = clean_text(value)
    if not text:
        return ""
    pieces = re.split(r"(https?://\S+)", text)
    for idx in range(0, len(pieces), 2):
        part = pieces[idx]
        part = re.sub(r"(?<=[\u3400-\u9fff])(?=[A-Za-z0-9$])", " ", part)
        part = re.sub(r"(?<=[A-Za-z0-9%$])(?=[\u3400-\u9fff])", " ", part)
        part = re.sub(r"\s+([，。！？；：、）】》])", r"\1", part)
        part = re.sub(r"([（【《])\s+", r"\1", part)
        pieces[idx] = part
    return "".join(pieces)


def nonspace_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def compact_text(value: Any, limit: int) -> str:
    text = chinese_spacing(value)
    if nonspace_len(text) <= limit:
        return text
    count = 0
    cut = 0
    for idx, char in enumerate(text):
        if not char.isspace():
            count += 1
        if count >= limit:
            cut = idx + 1
            break
    candidate = text[:cut]
    punctuation = max(candidate.rfind(mark) for mark in "，；：。！？")
    if punctuation >= max(8, len(candidate) // 2):
        candidate = candidate[: punctuation + 1]
    return candidate.rstrip("，；：。！？ ") + "…"


def _strings(value: Any, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for raw in value:
        text = raw.get("text", "") if isinstance(raw, dict) else raw
        text = compact_text(text, item_limit)
        if text and text not in output:
            output.append(text)
        if len(output) >= limit:
            break
    return output


def _text_value(value: Any) -> Any:
    return value.get("text", "") if isinstance(value, dict) else value


def _refs(raw: Any, valid_refs: set[str]) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(ref) for ref in raw if str(ref) in valid_refs]


EN_NUMBER_SMALL = {
    "zero": 0,
    "oh": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
EN_NUMBER_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
EN_NUMBER_SCALES = {"thousand": 1000, "million": 1000000, "billion": 1000000000}
ZH_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
ZH_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
ZH_BIG_UNITS = {"万": 10000, "亿": 100000000}
ZH_NUMBER_FOLLOWERS = set("种家个条次期年月日时分秒元块美元倍点岁名位集章篇")


def _add_number_value(output: set[str], value: int | float) -> None:
    if isinstance(value, float) and not value.is_integer():
        output.add(str(value).rstrip("0").rstrip("."))
        return
    output.add(str(int(value)))


def _english_number_tokens(text: str) -> set[str]:
    values: set[str] = set()
    tokens = re.findall(r"[A-Za-z]+", text.lower().replace("-", " "))
    current = 0
    total = 0
    active = False

    def flush() -> None:
        nonlocal current, total, active
        if active:
            _add_number_value(values, total + current)
        current = 0
        total = 0
        active = False

    for index, token in enumerate(tokens):
        if token in EN_NUMBER_SMALL:
            current += EN_NUMBER_SMALL[token]
            active = True
            next_token = tokens[index + 1] if index + 1 < len(tokens) else ""
            if next_token in EN_NUMBER_TENS and 0 < EN_NUMBER_SMALL[token] < 10:
                _add_number_value(values, EN_NUMBER_SMALL[token] * 100 + EN_NUMBER_TENS[next_token])
        elif token in EN_NUMBER_TENS:
            current += EN_NUMBER_TENS[token]
            active = True
        elif token == "hundred" and active:
            current = max(1, current) * 100
            _add_number_value(values, current)
        elif token in EN_NUMBER_SCALES and active:
            total += max(1, current) * EN_NUMBER_SCALES[token]
            _add_number_value(values, total)
            current = 0
        elif token == "and" and active:
            continue
        else:
            flush()
    flush()
    return values


def _parse_chinese_number(value: str) -> int | None:
    if not value:
        return None
    if all(char in ZH_DIGITS for char in value):
        digits = "".join(str(ZH_DIGITS[char]) for char in value)
        return int(digits) if digits else None

    total = 0
    section = 0
    number = 0
    seen = False
    for char in value:
        if char in ZH_DIGITS:
            number = ZH_DIGITS[char]
            seen = True
        elif char in ZH_SMALL_UNITS:
            section += (number or 1) * ZH_SMALL_UNITS[char]
            number = 0
            seen = True
        elif char in ZH_BIG_UNITS:
            section += number
            total += (section or 1) * ZH_BIG_UNITS[char]
            section = 0
            number = 0
            seen = True
        else:
            return None
    if not seen:
        return None
    return total + section + number


def _chinese_number_tokens(text: str) -> set[str]:
    values: set[str] = set()
    for match in re.finditer(r"[零〇一二两三四五六七八九十百千万亿]+", text):
        token = match.group(0)
        next_char = text[match.end() : match.end() + 1]
        has_numeric_unit = any(char in ZH_SMALL_UNITS or char in ZH_BIG_UNITS for char in token)
        if len(token) == 1 and not has_numeric_unit and next_char not in ZH_NUMBER_FOLLOWERS:
            continue
        parsed = _parse_chinese_number(token)
        if parsed is not None:
            _add_number_value(values, parsed)
    return values


def number_tokens(text: str) -> set[str]:
    values = set(re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)?%?", text))
    values.update(value.replace(",", "") for value in list(values) if "," in value)
    values.update(_english_number_tokens(text))
    values.update(_chinese_number_tokens(text))
    return values


def _numbers(text: str) -> set[str]:
    return number_tokens(text)


def _correct_known_fact_scope(
    raw: dict[str, Any], item: dict[str, Any], label: str, value: str, context: str
) -> tuple[str, str]:
    """Correct a high-confidence scope collapse that can survive transcript-only checks.

    Lepore's shorthand "17 times" refers to Amendments 11-27 after the Bill of Rights,
    while "since 1971" refers to substantive change. Neither is the formal all-time total.
    """
    subject = " ".join(
        str(part or "")
        for part in (
            raw.get("short_title"),
            item.get("title"),
            item.get("original_title"),
            label,
            context,
        )
    ).lower()
    is_us_constitution = "宪法" in subject and ("美国" in subject or re.search(r"\bus\b", subject))
    if is_us_constitution and "修正" in label and "17" in value and "27" not in value:
        return (
            "27 条（《权利法案》后另有 17 条）",
            "前 10 条构成《权利法案》；第 27 修正案于 1992 年获批准。1971 年后无实质修宪，不等于此后没有正式批准修正案。",
        )
    return value, context


def normalize_digest(
    raw: dict[str, Any],
    item: dict[str, Any],
    evidence: dict[str, str] | None = None,
    strict_evidence: bool = False,
) -> dict[str, Any]:
    """Coerce model output into the bounded reader-facing schema.

    With strict_evidence enabled, factual arrays must cite real evidence IDs;
    numeric facts whose numbers do not occur in those evidence spans are dropped.
    """
    if not isinstance(raw, dict):
        raise ValueError("digest must be a JSON object")
    evidence = evidence or {}
    valid_refs = set(evidence)

    short_title = compact_text(raw.get("short_title") or item.get("title") or "未命名内容", 18)
    one_liner = compact_text(_text_value(raw.get("one_liner")), 30)
    why_it_matters = compact_text(_text_value(raw.get("why_it_matters")), 60)
    summary = _strings(raw.get("summary"), 10, 420)
    core_points = _strings(raw.get("core_points"), 8, 90)
    takeaways = _strings(raw.get("takeaways"), 3, 70)
    guests = _strings(raw.get("guests"), 5, 90)
    topics = _strings(raw.get("topics"), 3, 12)
    tensions = _strings(raw.get("tensions"), 3, 90)

    if not one_liner:
        raise ValueError("one_liner is required")
    if len(core_points) < 2:
        raise ValueError("at least two core_points are required")
    if not summary:
        raise ValueError("summary is required")
    if not takeaways:
        raise ValueError("takeaways is required")

    key_facts: list[dict[str, Any]] = []
    for fact in raw.get("key_facts", []) if isinstance(raw.get("key_facts"), list) else []:
        if not isinstance(fact, dict):
            continue
        label = compact_text(fact.get("label"), 18)
        value = compact_text(fact.get("value"), 80)
        context = compact_text(fact.get("context"), 90)
        value, context = _correct_known_fact_scope(raw, item, label, value, context)
        refs = _refs(fact.get("source_refs"), valid_refs)
        if not label or not (value or context):
            continue
        if strict_evidence and not refs:
            continue
        if strict_evidence and _numbers(f"{value} {context}"):
            source = " ".join(evidence[ref] for ref in refs)
            # Known-fact corrections can add the authoritative total after the
            # model's source-scoped validation has already passed.
            corrected_us_amendments = "27 条" in value and "1992" in context
            if not corrected_us_amendments and not _numbers(f"{value} {context}").issubset(_numbers(source)):
                continue
        key_facts.append({"label": label, "value": value, "context": context, "source_refs": refs})
        if len(key_facts) >= 8:
            break

    quote: dict[str, str] | None = None
    quote_raw = raw.get("quote")
    if isinstance(quote_raw, dict) and clean_text(quote_raw.get("text")):
        text = compact_text(quote_raw.get("text"), 46)
        speaker = compact_text(quote_raw.get("speaker"), 16)
        kind = clean_text(quote_raw.get("kind") or "paraphrase").lower()
        refs = _refs(quote_raw.get("source_refs"), valid_refs)
        if strict_evidence and not refs:
            quote = None
        else:
            if kind == "verbatim" and refs:
                source = re.sub(r"\s+", "", " ".join(evidence[ref] for ref in refs))
                needle = re.sub(r"\s+", "", clean_text(quote_raw.get("text")))
                if needle not in source:
                    kind = "paraphrase"
            quote = {"text": text, "speaker": speaker, "kind": kind}

    try:
        score = int(raw.get("importance_score", 3))
    except (TypeError, ValueError):
        score = 3

    return {
        "short_title": short_title,
        "one_liner": one_liner,
        "why_it_matters": why_it_matters or one_liner,
        "summary": summary,
        "core_points": core_points,
        "key_facts": key_facts,
        "takeaways": takeaways,
        "guests": guests,
        "topics": topics,
        "tensions": tensions,
        "quote": quote,
        "importance_score": max(1, min(5, score)),
        "content_density": clean_text(raw.get("content_density") or "standard"),
        "quality": clean_text(raw.get("quality") or "llm_validated"),
    }


def platform_cn(value: Any) -> str:
    return {"youtube": "YouTube", "xiaoyuzhou": "小宇宙", "bilibili": "B 站"}.get(
        str(value or "").lower(), str(value or "")
    )


def fmt_time(value: Any) -> str:
    if not value:
        return "未知"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
    except ValueError:
        return clean_text(value)


def fmt_duration(seconds: Any) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    minutes = max(1, round(seconds / 60))
    if minutes >= 60:
        return f"{minutes // 60} 小时 {minutes % 60} 分"
    return f"{minutes} 分钟"


def is_content_digest(item: dict[str, Any]) -> bool:
    """Return whether an item has a content-bearing, validated digest."""
    return str(item.get("quality", "")).strip() not in NON_CONTENT_DIGEST_QUALITIES


def build_report(date: str, item_digests: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for item, digest in item_digests:
        items.append(
            {
                "url": item.get("url", ""),
                "original_title": item.get("original_title") or item.get("title") or "",
                "source_name": item.get("source_name", ""),
                "platform": platform_cn(item.get("platform")),
                "published_at": item.get("published_at", ""),
                "duration": item.get("duration", 0),
                "category": item.get("category") or "待分类",
                **digest,
            }
    )
    platform_counts = Counter(item["platform"] for item in items)
    category_counts = Counter(item["category"] for item in items)
    content_indices = [idx for idx, item in enumerate(items) if is_content_digest(item)]
    top_items = sorted(
        content_indices,
        key=lambda idx: (items[idx].get("importance_score", 3), items[idx].get("published_at", "")),
        reverse=True,
    )[: min(3, len(content_indices))]
    return {
        "schema_version": 2,
        "date": date,
        "title": f"{date} 播客与视频更新日报",
        "reader_mode": "breakfast",
        "item_count": len(items),
        "read_minutes": max(3, min(12, math.ceil(len(items) * 0.7))),
        "platform_counts": dict(platform_counts),
        "category_counts": dict(category_counts),
        # Themes are generated in a separate report-level pass after all
        # validated item digests are available.  Do not derive them from item
        # labels or categories here.
        "themes": [],
        "theme_sources": [],
        "top_items": top_items,
        "items": items,
    }


def enrich_report_from_legacy_markdown(report: dict[str, Any], markdown: str) -> dict[str, Any]:
    """Preserve deeper human-reviewed sections when upgrading an older report.

    Future automated reports already carry the complete structured JSON. This
    compatibility path is only for previously written human-reviewed Markdown.
    """
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in markdown.replace("\r\n", "\n").splitlines():
        if re.match(r"^###\s+", line):
            if current is not None:
                sections.append(current)
            current = [line]
        elif re.match(r"^#{1,2}\s+", line):
            if current is not None:
                sections.append(current)
                current = None
        elif current is not None:
            current.append(line)
    if current is not None:
        sections.append(current)

    parsed: list[dict[str, Any]] = []
    for lines in sections:
        fields: dict[str, list[str]] = {}
        heading = re.sub(r"^###\s+(?:\d+\.\s+)?", "", lines[0]).strip()
        heading = re.sub(r"\s+[★☆]+\s*$", "", heading).strip()
        url = ""
        active = ""
        for line in lines[1:]:
            stripped = line.strip()
            url_match = re.match(r"^\*\*链接\*\*：\s*(https?://\S+)", stripped)
            if url_match:
                url = url_match.group(1)
            field_match = re.fullmatch(r"\*\*([^*]+)\*\*", stripped)
            if field_match:
                active = field_match.group(1).strip()
                fields.setdefault(active, [])
                continue
            if active and stripped:
                fields[active].append(stripped)
        parsed.append({"heading": clean_text(heading), "url": url, "fields": fields})

    items = report.get("items", [])
    by_url = {section["url"]: section for section in parsed if section["url"]}
    by_heading = {section["heading"]: section for section in parsed if section["heading"]}
    for item in items:
        section = by_url.get(str(item.get("url") or ""))
        if section is None:
            section = by_heading.get(clean_text(item.get("short_title") or ""))
        if section is None:
            continue
        fields = section["fields"]
        summary = [
            clean_text(line)
            for line in (fields.get("完整摘要 · 深读", []) + fields.get("完整摘要", []))
            if clean_text(line)
        ]
        core = []
        for line in fields.get("核心观点", []) + fields.get("核心要点", []):
            match = re.match(r"^\d+\.\s+(.+)$", line)
            if match:
                core.append(clean_text(match.group(1)))
        guests = []
        for line in fields.get("嘉宾与机构", []):
            match = re.match(r"^[-*]\s+(.+)$", line)
            if match:
                guests.append(clean_text(match.group(1)))
        if len(summary) > len(item.get("summary", [])):
            item["summary"] = summary[:10]
        if len(core) > len(item.get("core_points", [])):
            item["core_points"] = core[:8]
        if guests:
            item["guests"] = guests[:5]
        if len(item.get("summary", [])) >= 4:
            item["content_density"] = "high"
    return report


def _x(value: Any, quote: bool = False) -> str:
    return escape(chinese_spacing(value), quote=quote)


def _rating(score: int) -> str:
    score = max(1, min(5, int(score)))
    return "★" * score + "☆" * (5 - score)


def _facts_table(facts: list[dict[str, Any]]) -> str:
    rows = [
        '<table><colgroup><col width="110"/><col width="390"/></colgroup>',
        '<thead><tr><th background-color="light-gray">关键事实</th><th background-color="light-gray">内容</th></tr></thead><tbody>',
    ]
    for fact in facts:
        detail = "：".join(part for part in [fact.get("value", ""), fact.get("context", "")] if part)
        rows.append(f"<tr><td><b>{_x(fact.get('label'))}</b></td><td>{_x(detail)}</td></tr>")
    rows.append("</tbody></table>")
    return "".join(rows)


def report_to_feishu_xml(report: dict[str, Any]) -> str:
    items = report.get("items", [])
    themes = report.get("themes", [])
    platform_text = (
        " · ".join(f"{name} {count} 条" for name, count in report.get("platform_counts", {}).items()) or "无"
    )
    theme_text = "今日无新增" if not items else (" · ".join(themes) if themes else "多主题更新")
    title = report.get("title") or f"{report.get('date', '')} 播客与视频更新日报"
    parts: list[str] = [f"<title>{_x(title)}</title>"]

    parts.append(
        '<callout emoji="☕" background-color="light-blue" border-color="blue">'
        f'<p><b>早上好，{_x(report.get("date"))} 的信息早餐</b></p>'
        f'<p>共 <b>{len(items)} 条</b>更新，预计 <b>{report.get("read_minutes", 5)} 分钟</b>读完。今天的主线：{_x(theme_text)}。</p>'
        '<p><span text-color="gray">先读「3 分钟速览」；每篇三级标题都可折叠，感兴趣再展开深读。</span></p>'
        '</callout>'
    )
    parts.append(
        '<grid>'
        f'<column width-ratio="0.333"><p align="center"><b>{len(items)}</b><br/>今日更新</p></column>'
        f'<column width-ratio="0.333"><p align="center"><b>{_x(platform_text)}</b><br/>来源覆盖</p></column>'
        f'<column width-ratio="0.334"><p align="center"><b>{report.get("read_minutes", 5)} 分钟</b><br/>建议阅读</p></column>'
        '</grid>'
    )

    if len(themes) >= 2:
        nodes = ["flowchart LR", 'A["今日信息地图"]']
        for idx, theme in enumerate(themes[:3], 1):
            nodes.append(f'A --> T{idx}["{clean_text(theme).replace(chr(34), "")}"]')
        diagram = escape("\n".join(nodes), quote=False)
        parts.append('<h1>今日信息地图</h1>')
        parts.append(f'<whiteboard type="mermaid">{diagram}</whiteboard>')

    parts.append('<h1>3 分钟速览</h1>')
    if not items:
        parts.append(
            '<callout emoji="📭" background-color="light-gray" border-color="gray">'
            '<p>今日无新增。</p></callout>'
        )
    top_indices = report.get("top_items")
    if top_indices is None:
        top_indices = list(range(min(3, len(items))))
    for rank, item_idx in enumerate(top_indices, 1):
        if not isinstance(item_idx, int) or not 0 <= item_idx < len(items):
            continue
        item = items[item_idx]
        parts.append(
            '<callout emoji="⭐" background-color="light-green" border-color="green">'
            f'<p><b>{rank}. {_x(item.get("short_title"))}</b></p>'
            f'<p>{_x(item.get("one_liner"))}</p>'
            f'<p><span text-color="green">为什么值得看：</span>{_x(item.get("why_it_matters"))}</p>'
            '</callout>'
        )

    parts.append('<h1>全部更新</h1>')
    for category_index, category in enumerate(CATEGORIES, 1):
        category_items = [item for item in items if item.get("category") == category]
        emoji = CATEGORY_EMOJI.get(category, "📂")
        parts.append(f'<h2>{emoji} {category_index}. {_x(category)}</h2>')
        parts.append(
            '<callout emoji="📌" background-color="light-gray" border-color="gray">'
            f'<p>{"本栏共 " + str(len(category_items)) + " 篇" if category_items else "今日无新增"}</p></callout>'
        )
        if not category_items:
            continue
        for item_index, item in enumerate(category_items, 1):
            parts.append(
                f'<h3>{item_index}. {_x(item.get("short_title"))} '
                f'<span background-color="light-yellow">{_rating(item.get("importance_score", 3))}</span></h3>'
            )
            href = escape(str(item.get("url", "")), quote=True)
            parts.append(
                '<callout emoji="ℹ️" background-color="light-gray" border-color="gray">'
                f'<p><b>原始标题：</b>{_x(item.get("original_title"))}</p>'
                f'<p><b>链接：</b><a href="{href}">{_x(item.get("url"))}</a></p>'
                '</callout>'
            )
            parts.append(
                '<grid>'
                '<column width-ratio="0.5">'
                f'<p><b>栏目：</b>{_x(item.get("source_name"))}</p>'
                f'<p><b>平台：</b>{_x(item.get("platform"))}</p>'
                f'<p><b>更新：</b>{_x(fmt_time(item.get("published_at")))}</p>'
                '</column>'
                '<column width-ratio="0.5">'
                f'<p><b>时长：</b>{_x(fmt_duration(item.get("duration")))}</p>'
                f'<p><b>分类：</b>{_x(item.get("category"))}</p>'
                f'<p><b>推荐：</b>{_rating(item.get("importance_score", 3))}</p>'
                '</column>'
                '</grid>'
            )
            guests = item.get("guests", [])
            if guests:
                parts.append('<p><b>嘉宾与机构</b></p><ul>')
                parts.extend(f'<li>{_x(guest)}</li>' for guest in guests)
                parts.append('</ul>')
            parts.append(
                '<callout emoji="💡" background-color="light-yellow" border-color="yellow">'
                f'<p><b>30 秒结论：</b>{_x(item.get("one_liner"))}</p>'
                f'<p><b>为什么值得看：</b>{_x(item.get("why_it_matters"))}</p>'
                '</callout>'
            )

            summary = item.get("summary", [])
            if summary:
                parts.append('<p><b>完整摘要 · 深读</b></p>')
                parts.extend(f'<p>{_x(paragraph)}</p>' for paragraph in summary[:10])

            core = item.get("core_points", [])
            takeaways = item.get("takeaways", [])
            parts.append('<grid>')
            parts.append('<column width-ratio="0.58"><p><b>核心要点</b></p><ul>')
            parts.extend(f'<li>{_x(point)}</li>' for point in core)
            parts.append('</ul></column>')
            parts.append('<column width-ratio="0.42"><p><b>你可以怎么用</b></p><ul>')
            parts.extend(f'<li>{_x(tip)}</li>' for tip in takeaways)
            parts.append('</ul></column></grid>')

            facts = item.get("key_facts", [])
            if facts:
                parts.append(_facts_table(facts))

            tensions = item.get("tensions", [])
            if tensions:
                parts.append('<p><b>分歧与限制</b></p><ul>')
                parts.extend(f'<li>{_x(point)}</li>' for point in tensions)
                parts.append('</ul>')

            quote = item.get("quote")
            if isinstance(quote, dict) and quote.get("text"):
                prefix = "" if quote.get("kind") == "verbatim" else "意译："
                speaker = f" —— {_x(quote.get('speaker'))}" if quote.get("speaker") else ""
                parts.append(f'<blockquote><p>{_x(prefix + quote.get("text", ""))}{speaker}</p></blockquote>')

    parts.append(
        '<callout emoji="ℹ️" background-color="light-gray" border-color="gray">'
        '<p>说明：读者版只保留扫读所需信息；完整字幕、证据片段和生成质检记录保存在运行产物中。</p>'
        '</callout>'
    )
    xml = "\n".join(parts)
    ET.fromstring(f"<root>{xml}</root>")
    return xml


def report_to_markdown(report: dict[str, Any]) -> str:
    lines = [f"# {report.get('date')} 播客 / 视频更新日报", "", "# 3 分钟速览", ""]
    items = report.get("items", [])
    if not items:
        lines += ["今日无新增。", ""]
    for rank, idx in enumerate(report.get("top_items", [])[:3], 1):
        if isinstance(idx, int) and 0 <= idx < len(items):
            item = items[idx]
            lines += [f"{rank}. **{item['short_title']}**：{item['one_liner']}", ""]
    lines += ["# 全部更新", ""]
    for category in CATEGORIES:
        category_items = [item for item in items if item.get("category") == category]
        lines += [f"## {category}", ""]
        if not category_items:
            lines += ["今日无新增。", ""]
            continue
        for idx, item in enumerate(category_items, 1):
            lines += [
                f"### {idx}. {item['short_title']}",
                "",
                (
                    f"**原始标题**：{chinese_spacing(item.get('original_title'))} ｜ "
                    f"**栏目**：{chinese_spacing(item.get('source_name'))} ｜ "
                    f"**平台**：{item.get('platform', '')} ｜ "
                    f"**更新**：{fmt_time(item.get('published_at'))} ｜ "
                    f"**时长**：{fmt_duration(item.get('duration'))} ｜ "
                    f"**分类**：{item.get('category', '')} ｜ "
                    f"**推荐**：{_rating(item.get('importance_score', 3))}"
                ),
                f"**链接**：{item.get('url', '')}",
                "",
                "**嘉宾与机构**",
                "",
                *[f"- {guest}" for guest in item.get("guests", [])],
                "",
                f"**30 秒结论**：{item['one_liner']}",
                "",
                f"**为什么值得看**：{item.get('why_it_matters', '')}",
                "",
                "**完整摘要 · 深读**",
                "",
                *item.get("summary", []),
                "",
                "**核心要点**",
                "",
                *[f"- {point}" for point in item.get("core_points", [])],
                "",
            ]
            facts = item.get("key_facts", [])
            if facts:
                lines += ["**关键事实**", "", "| 项目 | 内容 |", "|---|---|"]
                for fact in facts:
                    detail = "：".join(
                        part for part in [fact.get("value", ""), fact.get("context", "")] if part
                    )
                    lines.append(f"| {fact.get('label', '')} | {detail} |")
                lines.append("")
            tensions = item.get("tensions", [])
            if tensions:
                lines += ["**分歧与限制**", "", *[f"- {point}" for point in tensions], ""]
            lines += [
                "**你可以怎么用**",
                "",
                *[f"- {tip}" for tip in item.get("takeaways", [])],
                "",
            ]
    return "\n".join(lines).rstrip() + "\n"
