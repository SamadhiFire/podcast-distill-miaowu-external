import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.generate_daily_report import (
    DIGEST_CACHE_VERSION,
    load_digest_cache,
    should_use_direct_fileid,
    validate_final_digest,
)
from scripts.report_contract import enrich_report_from_legacy_markdown, normalize_digest


class ReportValidationTests(unittest.TestCase):
    def test_legacy_markdown_enrichment_never_crosses_item_boundaries(self) -> None:
        report = {
            "items": [
                {
                    "url": "https://example.test/future",
                    "short_title": "未来三年的真实图景",
                    "summary": ["未来原摘要"],
                    "core_points": [],
                    "guests": ["Steve"],
                },
                {
                    "url": "https://example.test/aldi",
                    "short_title": "阿尔迪如何压低食品成本",
                    "summary": ["阿尔迪原摘要"],
                    "core_points": [],
                    "guests": ["Scott"],
                },
            ]
        }
        markdown = """# 全部更新

## 商业 / 财经 / 投资

### 1. 阿尔迪如何压低食品成本

**链接**：https://example.test/aldi

**嘉宾与机构**

- Scott Patton

**完整摘要 · 深读**

阿尔迪摘要一。
阿尔迪摘要二。

## 产品 / 创业 / 管理

### 1. 未来三年的真实图景

**链接**：https://example.test/future

**嘉宾与机构**

- Steve Jurvetson

**完整摘要 · 深读**

未来摘要一。
未来摘要二。
"""
        enriched = enrich_report_from_legacy_markdown(report, markdown)
        future, aldi = enriched["items"]
        self.assertEqual(future["summary"], ["未来摘要一。", "未来摘要二。"])
        self.assertEqual(future["guests"], ["Steve Jurvetson"])
        self.assertEqual(aldi["summary"], ["阿尔迪摘要一。", "阿尔迪摘要二。"])
        self.assertEqual(aldi["guests"], ["Scott Patton"])

    def test_long_transcripts_use_segmented_evidence(self) -> None:
        item = {"duration": 3601}
        with patch.dict(os.environ, {}, clear=False):
            self.assertFalse(should_use_direct_fileid(item, "x" * 1000))
            self.assertFalse(should_use_direct_fileid({"duration": 600}, "x" * 30001))
            self.assertTrue(should_use_direct_fileid({"duration": 600}, "x" * 1000))

    def test_key_fact_context_numbers_must_exist_in_cited_segment(self) -> None:
        raw = {
            "short_title": "测试",
            "one_liner": {"text": "结论", "source_refs": ["S001"]},
            "why_it_matters": {"text": "原因", "source_refs": ["S001"]},
            "content_density": "brief",
            "summary": [
                {"text": "摘要一", "source_refs": ["S001"]},
                {"text": "摘要二", "source_refs": ["S001"]},
                {"text": "摘要三", "source_refs": ["S001"]},
            ],
            "core_points": [
                {"text": "观点一", "source_refs": ["S001"]},
                {"text": "观点二", "source_refs": ["S001"]},
                {"text": "观点三", "source_refs": ["S001"]},
            ],
            "key_facts": [
                {
                    "label": "年份",
                    "value": "17 次",
                    "context": "最近一次为 1971 年",
                    "source_refs": ["S001"],
                }
            ],
            "takeaways": ["回到原文核对。"],
            "guests": [{"text": "嘉宾", "source_refs": ["S001"]}],
            "topics": ["测试"],
            "tensions": [],
            "quote": None,
            "importance_score": 3,
        }
        contract = {
            "content_density": "brief",
            "summary_min": 3,
            "summary_max": 5,
            "summary_char_limit": 280,
            "core_points_min": 3,
            "core_points_max": 5,
        }
        with self.assertRaisesRegex(ValueError, "1971"):
            validate_final_digest(raw, {}, {"S001": "the constitution changed 17 times"}, contract)

    def test_us_constitution_scope_is_corrected(self) -> None:
        raw = {
            "short_title": "美国宪法的困境",
            "one_liner": "修宪机制长期停滞。",
            "why_it_matters": "理解制度僵化。",
            "summary": ["摘要。"],
            "core_points": ["观点一。", "观点二。"],
            "key_facts": [
                {
                    "label": "宪法修正次数",
                    "value": "17 次",
                    "context": "自 1787 年以来仅修正 17 次，最近一次为 1971 年。",
                }
            ],
            "takeaways": ["区分正式修正与实质性修宪。"],
            "guests": [],
            "topics": ["美国宪法"],
            "tensions": [],
            "quote": None,
        }
        digest = normalize_digest(raw, {"title": "Historian on the US Constitution"})
        fact = digest["key_facts"][0]
        self.assertIn("27", fact["value"])
        self.assertIn("1992", fact["context"])

    def test_old_digest_cache_is_invalidated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = {"video_id": "abc", "url": "https://example.test/abc"}
            path = root / "2026-07-04" / "abc.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"cache_version": DIGEST_CACHE_VERSION - 1, "model": "m", "digest": {"x": 1}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"LLM_MODEL": "m"}):
                self.assertIsNone(load_digest_cache(root, "2026-07-04", item))


if __name__ == "__main__":
    unittest.main()
