#!/usr/bin/env python3
"""Evidence-first helpers for high quality item digests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


SEGMENT_TARGET_CHARS = 8500
SEGMENT_MAX_CHARS = 12000
DEEP_SUMMARY_GUIDANCE = (
    "Section responsibilities: summary means '完整摘要 · 深读' and must be continuous analytical "
    "paragraphs, not bullets. It should explain the central question, argument arc, causal mechanism, "
    "important evidence, turning points, implications, and unresolved caveats. core_points are short "
    "portable claims only; key_facts are checkable names, numbers, events, papers, companies, policies, "
    "or products only; tensions are disagreements, limits, risks, and unanswered questions only; "
    "takeaways are reusable reader lenses or actions only. Avoid duplicating the same sentence across "
    "sections. Every summary paragraph must contain a claim plus support plus meaning. Preserve each "
    "number's scope, unit, currency, time period, comparison baseline, and qualifiers. Never turn a "
    "subset, post-baseline count, or 'meaningful/substantive' claim into an all-time total."
)


def compact(value: Any, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", "" if value is None else str(value)).strip()
    return text[:limit]


def parse_seconds(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def timestamp_to_seconds(value: str) -> float:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0.0


def item_artifact_id(item: dict[str, Any]) -> str:
    raw = (
        item.get("video_id")
        or item.get("episode_id")
        or item.get("url")
        or item.get("title")
        or "item"
    )
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", str(raw)).strip("_")
    if text and len(text) <= 80:
        return text
    return hashlib.sha1(str(raw).encode("utf-8")).hexdigest()[:16]


def build_transcript_profile(
    item: dict[str, Any],
    transcript: str,
    transcript_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = transcript_meta or {}
    duration = parse_seconds(item.get("duration") or item.get("duration_seconds"))
    if not duration:
        duration = parse_seconds(meta.get("duration_seconds"))
    coverage = parse_seconds(meta.get("coverage_ratio"))
    source = meta.get("source_method") or meta.get("source") or item.get("transcript_source") or ""
    text = transcript or ""
    return {
        "duration_seconds": duration,
        "duration_minutes": round(duration / 60, 1) if duration else 0,
        "transcript_source": source,
        "coverage_ratio": coverage,
        "text_chars": len(text),
        "line_count": len([line for line in text.splitlines() if line.strip()]),
        "has_description_chapters": bool(parse_chapters(str(item.get("description") or ""))),
        "source_name": item.get("source_name", ""),
        "title": item.get("title") or item.get("original_title") or "",
        "platform": item.get("platform", ""),
    }


def parse_chapters(description: str) -> list[dict[str, Any]]:
    chapters: list[dict[str, Any]] = []
    pattern = re.compile(
        r"(?m)^\s*(?:[-*]\s*)?(?P<time>(?:\d{1,2}:)?\d{1,2}:\d{2})"
        r"(?:\s+|[.)]\s*|-+\s*)(?P<title>.+?)\s*$"
    )
    for match in pattern.finditer(description or ""):
        title = compact(match.group("title"), 120)
        if not title or title.startswith("http"):
            continue
        chapters.append({"start": timestamp_to_seconds(match.group("time")), "topic": title})
    chapters = sorted(chapters, key=lambda chapter: chapter["start"])
    deduped: list[dict[str, Any]] = []
    seen: set[float] = set()
    for chapter in chapters:
        if chapter["start"] in seen:
            continue
        seen.add(chapter["start"])
        deduped.append(chapter)
    return deduped


def parse_vtt_cues(caption: str) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    time_pattern = re.compile(
        r"(?P<start>\d{1,2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*"
        r"(?P<end>\d{1,2}:\d{2}:\d{2}[.,]\d{3})"
    )
    current: dict[str, Any] | None = None
    lines: list[str] = []
    for raw in (caption or "").splitlines():
        line = raw.strip()
        match = time_pattern.search(line)
        if match:
            if current and lines:
                current["text"] = compact(" ".join(lines), 2000)
                cues.append(current)
            current = {
                "start": timestamp_to_seconds(match.group("start").replace(",", ".").split(".")[0]),
                "end": timestamp_to_seconds(match.group("end").replace(",", ".").split(".")[0]),
            }
            lines = []
            continue
        if not current or not line or line == "WEBVTT" or re.fullmatch(r"\d+", line):
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            continue
        clean = re.sub(r"<[^>]+>", "", line)
        clean = re.sub(r"\{[^}]*\}", "", clean)
        if clean:
            lines.append(clean)
    if current and lines:
        current["text"] = compact(" ".join(lines), 2000)
        cues.append(current)
    return cues


def chunk_text(text: str, target_chars: int = SEGMENT_TARGET_CHARS) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        lines = [re.sub(r"\s+", " ", text or "").strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        if current and current_len + len(line) + 1 > target_chars:
            chunks.append(" ".join(current).strip())
            current = []
            current_len = 0
        if len(line) > SEGMENT_MAX_CHARS:
            for start in range(0, len(line), target_chars):
                part = line[start : start + target_chars].strip()
                if part:
                    chunks.append(part)
            continue
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append(" ".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def segment_from_cues(
    transcript: str,
    cues: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    duration_seconds: float,
) -> list[dict[str, Any]]:
    if not cues or not chapters:
        return []
    segments: list[dict[str, Any]] = []
    for idx, chapter in enumerate(chapters):
        start = float(chapter["start"])
        end = (
            float(chapters[idx + 1]["start"])
            if idx + 1 < len(chapters)
            else duration_seconds or max((cue.get("end", 0) for cue in cues), default=start)
        )
        text = " ".join(
            cue.get("text", "")
            for cue in cues
            if parse_seconds(cue.get("start")) >= start and parse_seconds(cue.get("start")) < end
        ).strip()
        if text:
            segments.append(
                {
                    "segment_id": f"S{len(segments) + 1:03d}",
                    "start": start,
                    "end": end,
                    "topic": chapter["topic"],
                    "text": text[:SEGMENT_MAX_CHARS],
                }
            )
    if segments:
        return segments
    return build_topic_segments({}, transcript, "", duration_seconds=duration_seconds)


def build_topic_segments(
    item: dict[str, Any],
    transcript: str,
    timed_caption: str = "",
    duration_seconds: float | None = None,
) -> list[dict[str, Any]]:
    duration = duration_seconds or parse_seconds(item.get("duration") or item.get("duration_seconds"))
    chapters = parse_chapters(str(item.get("description") or ""))
    cues = parse_vtt_cues(timed_caption)
    segments = segment_from_cues(transcript, cues, chapters, duration)
    if segments:
        return segments

    chunks = chunk_text(transcript)
    if not chunks:
        return []
    output: list[dict[str, Any]] = []
    total = len(chunks)
    for idx, chunk in enumerate(chunks):
        topic = ""
        if chapters:
            chapter_idx = min(idx, len(chapters) - 1)
            topic = chapters[chapter_idx]["topic"]
        elif total > 1:
            topic = f"Transcript segment {idx + 1}"
        else:
            topic = "Full transcript"
        start = (duration / total * idx) if duration else 0
        end = (duration / total * (idx + 1)) if duration else 0
        output.append(
            {
                "segment_id": f"S{idx + 1:03d}",
                "start": round(start, 2),
                "end": round(end, 2),
                "topic": topic,
                "text": chunk[:SEGMENT_MAX_CHARS],
            }
        )
    return output


def build_evidence_map_from_segments(segments: list[dict[str, Any]]) -> dict[str, str]:
    return {str(segment["segment_id"]): str(segment.get("text") or "") for segment in segments}


def _normalize_evidence_array(
    raw: Any,
    segment_id: str,
    limit: int,
    text_limit: int = 260,
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return values
    for entry in raw:
        if isinstance(entry, str):
            text = compact(entry, text_limit)
            speaker = ""
            support = ""
        elif isinstance(entry, dict):
            text = compact(entry.get("text") or entry.get("claim") or entry.get("value"), text_limit)
            speaker = compact(entry.get("speaker"), 80)
            support = compact(entry.get("support") or entry.get("context"), text_limit)
        else:
            continue
        if text:
            values.append(
                {
                    "text": text,
                    "speaker": speaker,
                    "support": support,
                    "evidence_ref": segment_id,
                }
            )
        if len(values) >= limit:
            break
    return values


def build_segment_extraction_messages(
    item: dict[str, Any],
    profile: dict[str, Any],
    segment: dict[str, Any],
) -> list[dict[str, str]]:
    metadata = {
        key: item.get(key)
        for key in ("platform", "category", "source_name", "title", "published_at", "duration")
    }
    segment_brief = {
        "segment_id": segment["segment_id"],
        "start": segment.get("start"),
        "end": segment.get("end"),
        "topic": segment.get("topic"),
    }
    return [
        {
            "role": "system",
            "content": (
                "You are an evidence extractor for a Chinese daily digest. "
                "Do not summarize yet. Extract only information grounded in the transcript segment. "
                "Separate claims, evidence, examples, numbers, mechanisms, tensions, quotes, and terms. "
                "Prefer concrete mechanisms, examples, numbers, tradeoffs, and non-obvious claims. "
                "For every number, retain its unit, scope, time period, baseline, and nearby qualifier. "
                "Do not invent context or expand a subset into a total. Output one JSON object only. "
                "Every array item must include text; speaker/support are optional. "
                "Use concise Chinese for text fields unless an English proper noun is necessary."
            ),
        },
        {
            "role": "user",
            "content": (
                "Return JSON with fixed keys: central_question, claims, examples, numbers, "
                "mechanisms, tensions, quotes, terms.\n\n"
                f"Item metadata:\n{json.dumps(metadata, ensure_ascii=False)}\n\n"
                f"Transcript profile:\n{json.dumps(profile, ensure_ascii=False)}\n\n"
                f"Segment:\n{json.dumps(segment_brief, ensure_ascii=False)}\n\n"
                f"Transcript text:\n{segment.get('text', '')}"
            ),
        },
    ]


def normalize_segment_evidence(raw: dict[str, Any], segment_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("segment evidence must be a JSON object")
    return {
        "segment_id": segment_id,
        "central_question": compact(raw.get("central_question"), 220),
        "claims": _normalize_evidence_array(raw.get("claims"), segment_id, 5),
        "examples": _normalize_evidence_array(raw.get("examples"), segment_id, 4),
        "numbers": _normalize_evidence_array(raw.get("numbers"), segment_id, 4),
        "mechanisms": _normalize_evidence_array(raw.get("mechanisms"), segment_id, 4),
        "tensions": _normalize_evidence_array(raw.get("tensions"), segment_id, 3),
        "quotes": _normalize_evidence_array(raw.get("quotes"), segment_id, 3, 180),
        "terms": _normalize_evidence_array(raw.get("terms"), segment_id, 5, 120),
    }


def compact_segment_evidence_for_prompt(
    segment_evidence: list[dict[str, Any]],
    aggressive: bool = False,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if aggressive:
        limits = {
            "claims": 2,
            "examples": 1,
            "numbers": 1,
            "mechanisms": 1,
            "tensions": 1,
            "quotes": 0,
            "terms": 1,
        }
        text_limit = 120
        support_limit = 80
    else:
        limits = {
            "claims": 3,
            "examples": 2,
            "numbers": 2,
            "mechanisms": 2,
            "tensions": 2,
            "quotes": 1,
            "terms": 2,
        }
        text_limit = 180
        support_limit = 140
    for segment in segment_evidence:
        compacted: dict[str, Any] = {
            "segment_id": segment.get("segment_id"),
            "central_question": compact(segment.get("central_question"), 100 if aggressive else 160),
        }
        for field, limit in limits.items():
            entries: list[dict[str, Any]] = []
            for entry in segment.get(field, [])[:limit] if isinstance(segment.get(field), list) else []:
                if not isinstance(entry, dict):
                    continue
                text = compact(entry.get("text"), text_limit)
                if not text:
                    continue
                entries.append(
                    {
                        "text": text,
                        "support": compact(entry.get("support"), support_limit),
                        "evidence_ref": entry.get("evidence_ref") or segment.get("segment_id"),
                    }
                )
            compacted[field] = entries
        output.append(compacted)
    return output


def build_ranking_messages(
    item: dict[str, Any],
    profile: dict[str, Any],
    segment_evidence: list[dict[str, Any]],
) -> list[dict[str, str]]:
    evidence_json = json.dumps(compact_segment_evidence_for_prompt(segment_evidence), ensure_ascii=False)
    if len(evidence_json) > 45000:
        evidence_json = evidence_json[:45000]
    metadata = {
        key: item.get(key)
        for key in ("platform", "category", "source_name", "title", "description", "duration")
    }
    return [
        {
            "role": "system",
            "content": (
                "You are a senior editor ranking podcast/video evidence by reader value. "
                "Find what a smart reader would not know before listening. "
                "Prefer mechanisms, examples, numbers, tradeoffs, direct operator experience, "
                "and arguments that explain a current change. Do not praise the episode. "
                "Output one JSON object only. Write Chinese text fields."
            ),
        },
        {
            "role": "user",
            "content": (
                "Score and rank the evidence. Return JSON keys: central_question, argument_spine, "
                "ranked_insights, key_guests, topics, recommendation_reason.\n"
                "Each ranked_insights item must contain text, why_valuable, evidence_refs, and scores "
                "with novelty, specificity, decision_value, source_authority, argument_importance, timeliness "
                "as 1-5 integers. Keep only the best 5-9 insights for long episodes.\n\n"
                f"Item metadata:\n{json.dumps(metadata, ensure_ascii=False)}\n\n"
                f"Transcript profile:\n{json.dumps(profile, ensure_ascii=False)}\n\n"
                f"Extracted segment evidence:\n{evidence_json}"
            ),
        },
    ]


def normalize_ranked_insights(raw: dict[str, Any], valid_refs: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("ranked insights must be a JSON object")
    insights: list[dict[str, Any]] = []
    for entry in raw.get("ranked_insights", []) if isinstance(raw.get("ranked_insights"), list) else []:
        if not isinstance(entry, dict):
            continue
        refs = [str(ref) for ref in entry.get("evidence_refs", []) if str(ref) in valid_refs]
        text = compact(entry.get("text"), 260)
        why = compact(entry.get("why_valuable"), 220)
        if not text or not refs:
            continue
        scores_raw = entry.get("scores") if isinstance(entry.get("scores"), dict) else {}
        scores: dict[str, int] = {}
        for key in (
            "novelty",
            "specificity",
            "decision_value",
            "source_authority",
            "argument_importance",
            "timeliness",
        ):
            try:
                score = int(scores_raw.get(key, 3))
            except (TypeError, ValueError):
                score = 3
            scores[key] = max(1, min(5, score))
        insights.append({"text": text, "why_valuable": why, "evidence_refs": refs[:3], "scores": scores})
        if len(insights) >= 9:
            break
    if not insights:
        raise ValueError("ranked_insights must contain at least one cited insight")
    spine = [
        compact(value, 220)
        for value in raw.get("argument_spine", [])
        if isinstance(raw.get("argument_spine"), list) and compact(value)
    ][:7]
    guests = [
        compact(value.get("text") if isinstance(value, dict) else value, 140)
        for value in raw.get("key_guests", [])
        if isinstance(raw.get("key_guests"), list) and compact(value.get("text") if isinstance(value, dict) else value)
    ][:5]
    topics = [
        compact(value, 30)
        for value in raw.get("topics", [])
        if isinstance(raw.get("topics"), list) and compact(value)
    ][:4]
    return {
        "central_question": compact(raw.get("central_question"), 220),
        "argument_spine": spine,
        "ranked_insights": insights,
        "key_guests": guests,
        "topics": topics,
        "recommendation_reason": compact(raw.get("recommendation_reason"), 260),
    }


def build_final_digest_messages(
    item: dict[str, Any],
    profile: dict[str, Any],
    evidence: dict[str, str],
    segment_evidence: list[dict[str, Any]],
    ranked: dict[str, Any],
    schema: dict[str, Any],
) -> list[dict[str, str]]:
    excerpts = []
    for ref, text in evidence.items():
        excerpts.append({"ref": ref, "excerpt": compact(text, 180)})
    payload = {
        "profile": profile,
        "ranked": ranked,
        "segment_evidence": compact_segment_evidence_for_prompt(segment_evidence, aggressive=True),
        "source_excerpts": excerpts[:8],
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    payload_limit = int(os.getenv("LLM_FINAL_PAYLOAD_CHARS", "18000"))
    if len(payload_json) > payload_limit:
        payload_json = payload_json[:payload_limit]
    contract = schema.get("contract", {}) if isinstance(schema.get("contract"), dict) else {}
    count_contract = (
        f"Hard count contract: content_density={schema.get('content_density')}; "
        f"summary items={contract.get('summary_items', 'follow schema')}; "
        f"summary chars/item<={contract.get('summary_char_limit', 'follow schema')}; "
        f"core_points items={contract.get('core_points_items', 'follow schema')}; "
        f"takeaways items={contract.get('takeaways_items', '1..3')}. "
        "The item is eligible for the report; do not omit it because of a low score."
    )
    return [
        {
            "role": "system",
            "content": (
                "You are a Chinese daily digest editor. Preserve depth while staying concise. "
                "Fill the existing report fields only; do not output Markdown. "
                "The goal is high-value understanding, not a chronological recap. "
                "The 30-second conclusion must teach something more specific than the title. "
                "Why-it-matters must give a concrete reason to spend time: uncommon experience, "
                "new data, mechanism, market signal, policy implication, or reusable framework. "
                f"{DEEP_SUMMARY_GUIDANCE} "
                "Core points must be claims with support, not topic labels. "
                "Takeaways must be usable actions or reader lenses, not research questions. "
                "Every factual or interpretive field with source_refs must cite valid evidence refs. "
                "Cite the smallest segment that supports the whole claim, including all numbers and qualifiers. "
                f"{count_contract} "
                "Output one JSON object only, in Chinese, with necessary English proper nouns preserved."
            ),
        },
        {
            "role": "user",
            "content": (
                f"{count_contract}\n\n"
                "Return JSON that matches this schema exactly:\n"
                f"{json.dumps(schema, ensure_ascii=False)}\n\n"
                f"Item metadata:\n{json.dumps(item, ensure_ascii=False)}\n\n"
                f"Evidence package:\n{payload_json}"
            ),
        },
    ]


def write_evidence_artifacts(
    evidence_dir: Path,
    item: dict[str, Any],
    segments: list[dict[str, Any]],
    segment_evidence: list[dict[str, Any]],
    ranked: dict[str, Any],
) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stem = item_artifact_id(item)
    (evidence_dir / f"item_segments_{stem}.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (evidence_dir / f"item_evidence_{stem}.json").write_text(
        json.dumps(segment_evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (evidence_dir / f"item_ranked_insights_{stem}.json").write_text(
        json.dumps(ranked, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
