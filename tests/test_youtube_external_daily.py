import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.youtube_external_daily import main, unavailable_transcript_reason


class YouTubeExternalDailyTests(unittest.TestCase):
    def test_no_captions_is_explicitly_unavailable(self) -> None:
        self.assertEqual(
            unavailable_transcript_reason({"error": "No captions available"}),
            "No captions available",
        )

    def test_unknown_missing_bundle_remains_fatal(self) -> None:
        self.assertEqual(unavailable_transcript_reason({"error": "upstream request timed out"}), "")
        self.assertEqual(
            unavailable_transcript_reason(
                {"error": "Transcript unavailable: upstream request timed out"}
            ),
            "",
        )

    def test_explicitly_unavailable_transcript_does_not_fail_daily_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items_path = root / "items.json"
            manifest_path = root / "manifest.json"
            status_path = root / "status.json"
            bundle_path = root / "bundle.zip"
            subtitles_dir = root / "subtitles"
            external_item = {
                "platform": "youtube",
                "url": "https://www.youtube.com/watch?v=CxXSL5j_iyM",
                "title": "Unavailable transcript",
                "duration": 2856,
                "duration_seconds": 2856,
                "error": "No captions available",
            }
            argv = [
                "youtube_external_daily.py",
                "--date",
                "2026-07-25",
                "--items-json",
                str(items_path),
                "--manifest-json",
                str(manifest_path),
                "--status-json",
                str(status_path),
                "--bundle-path",
                str(bundle_path),
                "--subtitles-dir",
                str(subtitles_dir),
                "--media-token",
                "test-token",
            ]
            payload = (
                {"id": "job-test", "status": "success"},
                [external_item],
                {"total_videos": 1, "videos_failed": 1},
                {},
            )

            with patch.object(sys, "argv", argv), patch(
                "scripts.youtube_external_daily.collect_external_payload",
                return_value=payload,
            ):
                result = main()

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            items = json.loads(items_path.read_text(encoding="utf-8"))
            self.assertEqual(result, 0)
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["failure_count"], 0)
            self.assertEqual(manifest["skipped_count"], 1)
            self.assertEqual(items[0]["transcript_status"], "skipped_unavailable")


if __name__ == "__main__":
    unittest.main()
