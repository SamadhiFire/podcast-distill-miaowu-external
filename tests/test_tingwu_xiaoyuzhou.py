import unittest
from unittest.mock import Mock, patch

from scripts.tingwu_xiaoyuzhou_daily import (
    probe_audio_size,
    public_item,
    restricted_episode_reason,
    validate_public_audio,
)


class TingwuXiaoyuzhouTests(unittest.TestCase):
    def test_paid_private_episode_is_restricted(self) -> None:
        item = {"_pay_type": "PAY_EPISODE", "_media_access_mode": "PRIVATE"}
        self.assertEqual(
            restricted_episode_reason(item),
            "Xiaoyuzhou episode requires paid access (PAY_EPISODE)",
        )

    def test_free_public_episode_is_processable(self) -> None:
        item = {"_pay_type": "FREE", "_media_access_mode": "PUBLIC"}
        self.assertIsNone(restricted_episode_reason(item))

    def test_internal_access_metadata_is_not_published(self) -> None:
        item = {"url": "https://example.test/episode", "_pay_type": "FREE", "_media_size": 123}
        self.assertEqual(public_item(item), {"url": "https://example.test/episode"})

    @patch("scripts.tingwu_xiaoyuzhou_daily.requests.get")
    def test_probe_audio_size_uses_content_range(self, get: Mock) -> None:
        response = Mock()
        response.status_code = 206
        response.headers = {"Content-Range": "bytes 0-0/9924", "Content-Length": "1"}
        get.return_value = response

        self.assertEqual(probe_audio_size("https://example.test/audio.m4a"), 9924)
        response.raise_for_status.assert_called_once_with()
        response.close.assert_called_once_with()

    @patch("scripts.tingwu_xiaoyuzhou_daily.probe_audio_size", return_value=9924)
    def test_long_episode_with_tiny_public_audio_is_rejected(self, _probe: Mock) -> None:
        item = {
            "url": "https://example.test/episode",
            "audio_url": "https://example.test/audio.m4a",
            "duration_seconds": 2573,
            "_media_size": 32_541_288,
        }
        with self.assertRaisesRegex(RuntimeError, "unexpectedly small"):
            validate_public_audio(item)


if __name__ == "__main__":
    unittest.main()
