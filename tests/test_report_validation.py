import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.generate_daily_report import (
    DIGEST_CACHE_VERSION,
    DirectFileIdContractError,
    deterministic_item_failure_digest,
    evidence_fallback_enabled,
    generate_report_themes,
    is_context_length_error,
    load_digest_cache,
    metadata_fallback_enabled,
    report_theme_candidates,
    should_use_direct_fileid,
    summarize_item_contract,
    validate_report_themes,
    validate_final_digest,
)
from scripts.report_contract import (
    CATEGORIES,
    build_report,
    enrich_report_from_legacy_markdown,
    normalize_digest,
    report_to_feishu_xml,
    report_to_markdown,
)
from scripts.publish_feishu import (
    FEISHU_API,
    create_wiki_doc,
    get_daily_hub_node,
    get_or_create_daily_wiki_doc,
    list_wiki_nodes,
    update_wiki_node_title,
    verify_daily_report_child,
)
from scripts.validate_transcript_bundle import (
    has_required_transcript_items,
    validate_bundle_zip,
    validate_items,
    validate_transcript_meta,
)


class ReportValidationTests(unittest.TestCase):
    def test_empty_daily_items_allow_empty_transcript_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subtitles_dir = root / "subtitles"
            subtitles_dir.mkdir()
            bundle_path = root / "subtitles_bundle.zip"
            with zipfile.ZipFile(bundle_path, "w"):
                pass

            failures: list[str] = []
            warnings: list[str] = []
            items: list[dict[str, object]] = []

            self.assertFalse(has_required_transcript_items(items, 300))
            validate_bundle_zip(bundle_path, failures, require_subtitles_root=False)
            transcript_index = validate_transcript_meta(
                subtitles_dir,
                0.95,
                300,
                failures,
                warnings,
                require_metadata=False,
            )
            item_count, required_count = validate_items(items, transcript_index, 0.95, 300, [], failures)

            self.assertEqual(failures, [])
            self.assertEqual(item_count, 0)
            self.assertEqual(required_count, 0)
            self.assertEqual(transcript_index, {})

    def test_long_items_still_require_transcript_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subtitles_dir = root / "subtitles"
            subtitles_dir.mkdir()
            bundle_path = root / "subtitles_bundle.zip"
            with zipfile.ZipFile(bundle_path, "w"):
                pass

            failures: list[str] = []
            warnings: list[str] = []
            items = [
                {
                    "platform": "youtube",
                    "url": "https://www.youtube.com/watch?v=abc12345678",
                    "duration": 300,
                }
            ]

            self.assertTrue(has_required_transcript_items(items, 300))
            validate_bundle_zip(bundle_path, failures, require_subtitles_root=True)
            transcript_index = validate_transcript_meta(
                subtitles_dir,
                0.95,
                300,
                failures,
                warnings,
                require_metadata=True,
            )
            validate_items(items, transcript_index, 0.95, 300, [], failures)

            self.assertTrue(any("zip does not contain subtitles/ root" in failure for failure in failures))
            self.assertTrue(any("no per-item transcript metadata found" in failure for failure in failures))
            self.assertTrue(any("missing transcript metadata" in failure for failure in failures))

    def test_empty_report_has_no_update_placeholders(self) -> None:
        report = build_report("2026-07-06", [])

        markdown = report_to_markdown(report)
        self.assertIn("# 3 分钟速览\n\n今日无新增。", markdown)
        for category in CATEGORIES:
            self.assertIn(f"## {category}\n\n今日无新增。", markdown)

        xml = report_to_feishu_xml(report)
        self.assertIn("今日无新增", xml)

    def test_information_map_uses_whiteboard_import(self) -> None:
        report = {
            "date": "2026-07-10",
            "items": [{"short_title": "测试", "category": "科技 / AI / VC"}],
            "themes": ["大模型", "AI 记忆", "端侧 AI"],
            "platform_counts": {"YouTube": 1},
            "read_minutes": 3,
            "top_items": [],
        }

        xml = report_to_feishu_xml(report)
        self.assertIn("<h1>今日信息地图</h1>\n<whiteboard type=\"mermaid\">", xml)
        self.assertIn("flowchart LR", xml)

    def test_information_map_excludes_placeholder_digests_until_report_level_synthesis(self) -> None:
        failed_item = {
            "title": "异常内容",
            "category": "商业 / 财经 / 投资",
            "platform": "youtube",
            "published_at": "2026-07-15T06:00:00+08:00",
        }
        valid_item = {
            "title": "火箭回收",
            "category": "科技 / AI / VC",
            "platform": "youtube",
            "published_at": "2026-07-15T06:10:00+08:00",
        }
        second_valid_item = {
            "title": "AI 就业",
            "category": "产品 / 创业 / 管理",
            "platform": "youtube",
            "published_at": "2026-07-15T06:20:00+08:00",
        }
        report = build_report(
            "2026-07-15",
            [
                (failed_item, deterministic_item_failure_digest(failed_item, "contract")),
                (
                    valid_item,
                    {
                        "short_title": "火箭回收",
                        "one_liner": "验证内容",
                        "why_it_matters": "验证主题聚合",
                        "topics": ["可回收火箭"],
                        "importance_score": 4,
                    },
                ),
                (
                    second_valid_item,
                    {
                        "short_title": "AI 就业",
                        "one_liner": "验证内容",
                        "why_it_matters": "验证主题聚合",
                        "topics": ["AI 就业"],
                        "importance_score": 4,
                    },
                ),
            ],
        )

        self.assertEqual(report["themes"], [])
        self.assertNotIn("摘要受限", report["themes"])
        self.assertEqual(report["top_items"], [2, 1])
        xml = report_to_feishu_xml(report)
        self.assertNotIn("摘要受限", xml)
        self.assertNotIn("<h1>今日信息地图</h1>", xml)

    def test_report_level_themes_reject_placeholder_and_invalid_source_ids(self) -> None:
        items = [
            {
                "short_title": "火箭回收",
                "one_liner": "海上回收验证了可复用火箭的工程路径。",
                "summary": ["回收系统降低了未来发射成本。"],
                "quality": "llm_evidence_validated",
            },
            {
                "short_title": "AI 与就业",
                "one_liner": "AI 推动岗位能力重组而非简单替代。",
                "summary": ["教育与职业训练需要转向人机协作能力。"],
                "quality": "llm_evidence_validated",
            },
            {
                "short_title": "失败条目",
                "one_liner": "模型未通过摘要格式校验。",
                "quality": "direct_fileid_contract_failed",
            },
        ]
        with patch("scripts.generate_daily_report.llm_configured", return_value=True), patch(
            "scripts.generate_daily_report.llm_json",
            return_value=[
                {"title": "可复用火箭走向工程验证", "source_item_ids": ["I1"]},
                {"title": "AI 重组教育与职业能力", "source_item_ids": ["I2"]},
            ],
        ):
            self.assertEqual(
                generate_report_themes(items, 3),
                [
                    {"title": "可复用火箭走向工程验证", "source_item_ids": ["I1"]},
                    {"title": "AI 重组教育与职业能力", "source_item_ids": ["I2"]},
                ],
            )

    def test_report_theme_validation_rejects_placeholders_and_unknown_sources(self) -> None:
        candidates = report_theme_candidates(
            [
                {
                    "short_title": "rocket recovery",
                    "one_liner": "A sea recovery test validated a reusable rocket path.",
                    "quality": "llm_evidence_validated",
                },
                {
                    "short_title": "AI and education",
                    "one_liner": "AI is reshaping education and workplace skills.",
                    "quality": "llm_evidence_validated",
                },
                {
                    "short_title": "failed item",
                    "one_liner": "This must not appear in editorial synthesis.",
                    "quality": "provider_input_rejected",
                },
            ]
        )
        self.assertEqual([item["item_id"] for item in candidates], ["I1", "I2"])

        themes = validate_report_themes(
            {
                "themes": [
                    {"title": "可复用火箭完成工程回收验证", "source_item_ids": ["I1"]},
                    {"title": "AI推动教育与职业能力重组", "source_item_ids": ["I2"]},
                ]
            },
            candidates,
        )
        self.assertEqual(len(themes), 2)

        with self.assertRaises(ValueError):
            validate_report_themes(
                {
                    "themes": [
                        {"title": "摘要受限正在影响内容生成", "source_item_ids": ["I1"]},
                        {"title": "AI推动教育与职业能力重组", "source_item_ids": ["I2"]},
                    ]
                },
                candidates,
            )

        with self.assertRaises(ValueError):
            validate_report_themes(
                {
                    "themes": [
                        {"title": "可复用火箭完成工程回收验证", "source_item_ids": ["I1"]},
                        {"title": "AI推动教育与职业能力重组", "source_item_ids": ["I3"]},
                    ]
                },
                candidates,
            )

    def test_context_classifier_does_not_hide_unrelated_invalid_parameters(self) -> None:
        self.assertFalse(is_context_length_error(RuntimeError("invalid_parameter_error: bad temperature")))
        self.assertTrue(
            is_context_length_error(
                RuntimeError("invalid_parameter_error: input length exceeds token limit")
            )
        )

    def test_metadata_fallback_can_be_disabled_for_required_runs(self) -> None:
        with patch.dict(os.environ, {"LLM_METADATA_FALLBACK_ENABLED": "0"}, clear=False):
            self.assertFalse(metadata_fallback_enabled())

    def test_metadata_fallback_is_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(metadata_fallback_enabled())

    def test_direct_required_disables_segment_evidence_fallback_by_default(self) -> None:
        with patch.dict(os.environ, {"LLM_FILEID_DIRECT_REQUIRED": "1"}, clear=True):
            self.assertFalse(evidence_fallback_enabled())

    def test_explicit_evidence_fallback_can_be_enabled_for_nonproduction_runs(self) -> None:
        env = {
            "LLM_FILEID_DIRECT_REQUIRED": "1",
            "LLM_EVIDENCE_FALLBACK_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(evidence_fallback_enabled())

    def test_known_item_failure_uses_a_transparent_non_llm_placeholder(self) -> None:
        item = {"title": "受限节目", "url": "https://example.test/blocked"}
        digest = deterministic_item_failure_digest(item, "provider_input_inspection")

        self.assertEqual(digest["quality"], "provider_input_rejected")
        self.assertIn("未生成", digest["summary"][0])
        self.assertGreaterEqual(len(digest["core_points"]), 2)

    def test_direct_fileid_contract_error_is_distinct_from_transport_failures(self) -> None:
        self.assertIsInstance(DirectFileIdContractError("format"), RuntimeError)

    @patch("scripts.generate_daily_report.summarize_item_fileid_direct")
    @patch("scripts.generate_daily_report.llm_configured", return_value=True)
    def test_direct_required_contract_failure_does_not_enter_segment_extraction(
        self, _configured, direct_digest
    ) -> None:
        direct_digest.side_effect = RuntimeError("LLM output failed validation after 3 attempt(s): summary")
        env = {
            "LLM_FILEID_DIRECT_ENABLED": "1",
            "LLM_FILEID_DIRECT_REQUIRED": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(DirectFileIdContractError):
                summarize_item_contract(
                    {"title": "测试", "duration": 300},
                    "一段完整转写。",
                    max_attempts=1,
                )

    @patch("scripts.publish_feishu.requests.post")
    def test_wiki_title_update_uses_official_update_title_endpoint(self, post) -> None:
        post.return_value.json.return_value = {"code": 0, "msg": "success"}
        with patch.dict(os.environ, {"FEISHU_WIKI_SPACE_ID": "space-1"}, clear=False):
            update_wiki_node_title("tenant-token", "node-1", "日报")
        post.assert_called_once()
        self.assertEqual(
            post.call_args.args[0],
            f"{FEISHU_API}/wiki/v2/spaces/space-1/nodes/node-1/update_title",
        )
        self.assertEqual(post.call_args.kwargs["json"], {"title": "日报"})

    @patch("scripts.publish_feishu.requests.post")
    def test_new_report_is_created_under_requested_parent(self, post) -> None:
        post.return_value.json.return_value = {
            "code": 0,
            "data": {"node": {"obj_token": "doc-1", "node_token": "new-node"}},
        }
        with patch.dict(os.environ, {"FEISHU_WIKI_SPACE_ID": "space-1"}, clear=False):
            self.assertEqual(
                create_wiki_doc("tenant-token", "new-report", parent_node_token="hub"),
                ("doc-1", "new-node"),
            )

        post.assert_called_once()
        self.assertEqual(
            post.call_args.args[0],
            f"{FEISHU_API}/wiki/v2/spaces/space-1/nodes",
        )
        self.assertEqual(post.call_args.kwargs["json"]["parent_node_token"], "hub")
        self.assertNotIn("/move", post.call_args.args[0])

    @patch("scripts.publish_feishu.requests.get")
    def test_root_node_listing_reads_every_page(self, get) -> None:
        first = MagicMock()
        first.json.return_value = {
            "code": 0,
            "data": {
                "items": [{"node_token": "one", "title": "第一页"}],
                "has_more": True,
                "page_token": "next-page",
            },
        }
        second = MagicMock()
        second.json.return_value = {
            "code": 0,
            "data": {
                "items": [{"node_token": "two", "title": "第二页"}],
                "has_more": False,
            },
        }
        get.side_effect = [first, second]

        with patch.dict(os.environ, {"FEISHU_WIKI_SPACE_ID": "space-1"}, clear=False):
            nodes = list_wiki_nodes("tenant-token")

        self.assertEqual([node["node_token"] for node in nodes], ["one", "two"])
        self.assertNotIn("parent_node_token", get.call_args_list[0].kwargs["params"])
        self.assertEqual(get.call_args_list[1].kwargs["params"]["page_token"], "next-page")

    def test_publisher_contains_no_wiki_move_endpoint(self) -> None:
        publisher = Path(__file__).parents[1] / "scripts" / "publish_feishu.py"
        source = publisher.read_text(encoding="utf-8")
        self.assertNotIn("/move", source)
        self.assertNotIn("move_wiki_node", source)

    def test_unique_root_hub_is_resolved(self) -> None:
        hub = {"node_token": "hub", "title": "🎧 播客蒸馏室"}
        self.assertEqual(get_daily_hub_node("tenant-token", root_nodes=[hub]), hub)

        with self.assertRaisesRegex(RuntimeError, "exactly one root Wiki hub"):
            get_daily_hub_node("tenant-token", root_nodes=[])

    def test_existing_exact_title_child_node_is_reused(self) -> None:
        hub = {"node_token": "hub", "title": "🎧 播客蒸馏室"}
        report = {"node_token": "jul-15", "title": "2026-07-15 播客与视频更新日报"}
        with patch("scripts.publish_feishu.list_root_nodes", return_value=[hub]), patch(
            "scripts.publish_feishu.list_wiki_nodes", return_value=[report]
        ), patch(
            "scripts.publish_feishu.get_wiki_doc_token", return_value=("doc-15", "jul-15")
        ) as resolve, patch("scripts.publish_feishu.create_wiki_doc") as create:
            result = get_or_create_daily_wiki_doc("tenant-token", report["title"])

        self.assertEqual(result, ("doc-15", "jul-15", "hub", False))
        resolve.assert_called_once_with("tenant-token", "jul-15")
        create.assert_not_called()

    def test_new_daily_report_is_created_as_hub_child(self) -> None:
        hub = {"node_token": "hub", "title": "🎧 播客蒸馏室"}
        with patch("scripts.publish_feishu.list_root_nodes", return_value=[hub]), patch(
            "scripts.publish_feishu.list_wiki_nodes", return_value=[]
        ), patch(
            "scripts.publish_feishu.create_wiki_doc", return_value=("doc-new", "node-new")
        ) as create:
            result = get_or_create_daily_wiki_doc(
                "tenant-token", "2026-07-19 播客与视频更新日报"
            )

        self.assertEqual(result, ("doc-new", "node-new", "hub", True))
        create.assert_called_once_with(
            "tenant-token",
            "2026-07-19 播客与视频更新日报",
            parent_node_token="hub",
        )

    def test_unmigrated_root_report_blocks_duplicate_creation(self) -> None:
        title = "2026-07-15 播客与视频更新日报"
        roots = [
            {"node_token": "hub", "title": "🎧 播客蒸馏室"},
            {"node_token": "old-root", "title": title},
        ]
        with patch("scripts.publish_feishu.list_root_nodes", return_value=roots), patch(
            "scripts.publish_feishu.list_wiki_nodes"
        ) as list_children, patch("scripts.publish_feishu.create_wiki_doc") as create:
            with self.assertRaisesRegex(RuntimeError, "one-time hub migration"):
                get_or_create_daily_wiki_doc("tenant-token", title)

        list_children.assert_not_called()
        create.assert_not_called()

    def test_daily_report_child_verification_is_read_only(self) -> None:
        hub = {"node_token": "hub", "title": "🎧 播客蒸馏室"}
        july_15 = {"node_token": "jul-15", "title": "2026-07-15 播客与视频更新日报"}
        with patch("scripts.publish_feishu.list_root_nodes", return_value=[hub]), patch(
            "scripts.publish_feishu.list_wiki_nodes", return_value=[july_15]
        ):
            verify_daily_report_child("tenant-token", july_15["title"], "jul-15")

        with patch("scripts.publish_feishu.list_root_nodes", return_value=[hub]), patch(
            "scripts.publish_feishu.list_wiki_nodes", return_value=[]
        ):
            with self.assertRaisesRegex(RuntimeError, "hierarchy verification failed"):
                verify_daily_report_child("tenant-token", july_15["title"], "jul-15")

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

    def test_long_transcripts_use_direct_fileid_without_default_maximum(self) -> None:
        env = {
            "LLM_FILEID_DIRECT_ENABLED": "1",
            "LLM_FILEID_DIRECT_MIN_DURATION_SECONDS": "300",
            "LLM_FILEID_DIRECT_MIN_CHARS": "1",
            "LLM_FILEID_DIRECT_MAX_DURATION_SECONDS": "0",
            "LLM_FILEID_DIRECT_MAX_CHARS": "0",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertTrue(should_use_direct_fileid({"duration": 3601}, "x" * 1000))
            self.assertTrue(should_use_direct_fileid({"duration": 600}, "x" * 155863))

    def test_direct_fileid_optional_maximum_is_still_enforced(self) -> None:
        env = {
            "LLM_FILEID_DIRECT_ENABLED": "1",
            "LLM_FILEID_DIRECT_MIN_DURATION_SECONDS": "300",
            "LLM_FILEID_DIRECT_MIN_CHARS": "1",
            "LLM_FILEID_DIRECT_MAX_DURATION_SECONDS": "1800",
            "LLM_FILEID_DIRECT_MAX_CHARS": "30000",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertFalse(should_use_direct_fileid({"duration": 3601}, "x" * 1000))
            self.assertFalse(should_use_direct_fileid({"duration": 600}, "x" * 30001))
            self.assertTrue(should_use_direct_fileid({"duration": 600}, "x" * 1000))

    def test_ungrounded_numbers_are_removed_instead_of_failing_digest(self) -> None:
        raw = {
            "short_title": "测试",
            "one_liner": {"text": "结论", "source_refs": ["S001"]},
            "why_it_matters": {"text": "原因", "source_refs": ["S001"]},
            "content_density": "brief",
            "summary": [
                {"text": "摘要一。最近一次为 1971 年。", "source_refs": ["S001"]},
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
        digest = validate_final_digest(raw, {}, {"S001": "the constitution changed 17 times"}, contract)
        self.assertNotIn("1971", " ".join(digest["summary"]))
        self.assertEqual(digest["key_facts"], [])

    def test_number_grounding_accepts_spoken_number_forms(self) -> None:
        raw = {
            "short_title": "测试",
            "one_liner": {"text": "用 400 家门店说明模式。", "source_refs": ["S001"]},
            "why_it_matters": {"text": "它解释了食品成本下降的机制。", "source_refs": ["S001"]},
            "content_density": "brief",
            "summary": [
                {"text": "案例围绕 400 家门店的运营方式展开。", "source_refs": ["S001"]},
                {"text": "团队把成本优势拆成供应链、选品和店内流程。", "source_refs": ["S001"]},
                {"text": "这些做法帮助读者理解折扣零售的结构性优势。", "source_refs": ["S001"]},
            ],
            "core_points": [
                {"text": "门店规模支撑采购效率。", "source_refs": ["S001"]},
                {"text": "少量 SKU 降低复杂度。", "source_refs": ["S001"]},
                {"text": "流程标准化压低运营成本。", "source_refs": ["S001"]},
            ],
            "key_facts": [
                {
                    "label": "门店数量",
                    "value": "400 家",
                    "context": "原文用 four hundred stores 描述规模。",
                    "source_refs": ["S001"],
                },
                {
                    "label": "方法数量",
                    "value": "4 种",
                    "context": "中文原文说四种配速。",
                    "source_refs": ["S002"],
                },
            ],
            "takeaways": ["用规模、复杂度和流程三个层次拆解成本。"],
            "guests": [{"text": "嘉宾", "source_refs": ["S001"]}],
            "topics": ["零售"],
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
        digest = validate_final_digest(
            raw,
            {},
            {
                "S001": "The operator described four hundred stores and the supply chain choices.",
                "S002": "读书方法里提到四种配速。",
            },
            contract,
        )
        self.assertEqual(digest["key_facts"][0]["value"], "400 家")

    def test_extra_list_items_are_truncated_instead_of_failing_digest(self) -> None:
        raw = {
            "short_title": "测试",
            "one_liner": {"text": "结论成立。", "source_refs": ["S001"]},
            "why_it_matters": {"text": "它帮助读者理解主题。", "source_refs": ["S001"]},
            "content_density": "brief",
            "summary": [
                {"text": f"摘要段落{label}说明同一主题。", "source_refs": ["S001"]}
                for label in ("甲", "乙", "丙", "丁", "戊", "己")
            ],
            "core_points": [
                {"text": f"核心观点{label}。", "source_refs": ["S001"]}
                for label in ("甲", "乙", "丙", "丁", "戊", "己")
            ],
            "key_facts": [],
            "takeaways": ["行动一。", "行动二。", "行动三。", "行动四。"],
            "guests": [
                {"text": f"嘉宾{label}", "source_refs": ["S001"]}
                for label in ("甲", "乙", "丙", "丁", "戊", "己")
            ],
            "topics": ["测试"],
            "tensions": [
                {"text": f"限制{label}。", "source_refs": ["S001"]}
                for label in ("甲", "乙", "丙", "丁")
            ],
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
        digest = validate_final_digest(raw, {}, {"S001": "节目讨论了同一主题。"}, contract)

        self.assertEqual(len(digest["summary"]), 5)
        self.assertEqual(len(digest["core_points"]), 5)
        self.assertEqual(len(digest["takeaways"]), 3)
        self.assertEqual(len(digest["guests"]), 5)
        self.assertEqual(len(digest["tensions"]), 3)

    def test_malformed_trailing_number_sentence_is_removed(self) -> None:
        raw = {
            "short_title": "测试",
            "one_liner": {"text": "结论成立。", "source_refs": ["S001"]},
            "why_it_matters": {"text": "它帮助读者理解主题。", "source_refs": ["S001"]},
            "content_density": "brief",
            "summary": [
                {"text": "电子价签节省人力。鸡胸肉售价可低至每磅 2.", "source_refs": ["S001"]},
                {"text": "摘要二说明同一主题。", "source_refs": ["S001"]},
                {"text": "摘要三说明同一主题。", "source_refs": ["S001"]},
            ],
            "core_points": [
                {"text": "核心观点甲。", "source_refs": ["S001"]},
                {"text": "核心观点乙。", "source_refs": ["S001"]},
                {"text": "核心观点丙。", "source_refs": ["S001"]},
            ],
            "key_facts": [],
            "takeaways": ["行动一。"],
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
        digest = validate_final_digest(
            raw,
            {},
            {"S001": "The transcript mentions two stores and electronic shelf labels."},
            contract,
        )

        joined = " ".join(digest["summary"])
        self.assertIn("电子价签节省人力", joined)
        self.assertNotIn("每磅 2.", joined)

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
