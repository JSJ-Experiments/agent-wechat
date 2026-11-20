import hashlib
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from native_shadow import Client, MigrationError, classify, skipped_report


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.client = Client("http://127.0.0.1:1234", "fake", "example")
        self.base = Path(self.enterContext(TemporaryDirectory()))
        self.dest = self.base / "original.jpg"

    def test_unknown_is_not_dropped_or_guessed(self):
        value = {"type": 3123, "appMessageType": None, "id": "message", "body": None}
        report = classify(value)
        self.assertEqual(report["kind"], "unknown")
        self.assertIs(report["original_response"], value)
        self.assertEqual(classify({"type": 49, "appMessageType": 123})["kind"], "unknown")
        self.assertEqual(classify({"type": 49, "appMessageType": 6})["kind"], "attachment")

    def test_cached_download_checks_hash_and_quality_and_never_overwrites(self):
        data = b"synthetic bytes"
        asset = {"id": "asset", "state": "available", "actualVariant": "original", "byteLength": len(data),
                 "sha256": hashlib.sha256(data).hexdigest()}
        with patch.object(self.client, "open", side_effect=lambda _: BytesIO(data)):
            result = self.client.save_cached(asset, self.dest, "original")
            self.assertEqual(result["bytes"], len(data))
            self.assertEqual(self.dest.read_bytes(), data)
            with self.assertRaises(FileExistsError):
                self.client.save_cached(asset, self.dest, "original")
        with self.assertRaises(MigrationError):
            self.client.save_cached(asset | {"actualVariant": "thumbnail"}, self.base / "bad", "original")
        with patch.object(self.client, "open", return_value=BytesIO(b"incorrect data")), self.assertRaises(MigrationError):
            self.client.save_cached(asset, self.base / "invalid", "original")
        self.assertFalse((self.base / "invalid").exists())

    def test_skipped_reports_numeric_labels_only_and_retains_resume_cursor(self):
        page = {"nextCursor": "104", "items": [
            {"kind": "account.control_observed", "data": {"projection": "skipped",
             "control": {"commandId": 5, "messageType": 42, "payload": "secret"}}},
            {"kind": "account.control_observed", "data": {"projection": "skipped",
             "control": {"commandId": 999, "messageType": None}}},
            {"kind": "account.control_observed", "data": {"projection": "observed"}},
            {"kind": "message.committed", "data": {"body": "private"}}]}
        with patch.object(self.client, "get", return_value=page) as get:
            report = skipped_report(self.client, 8, "100")
            get.assert_called_once_with("/events?limit=8&after=100")
        self.assertEqual(report, {"events_sampled": 4, "next_cursor": "104", "skipped_controls": 2,
                                  "skipped_by_command": {"5": 1, "999": 1}, "skipped_by_message_type": {"42": 1}})

    def test_skipped_expired_cursor_is_not_silently_reset(self):
        with patch.object(self.client, "get", side_effect=HTTPError("unused", 410, "gone", None, None)):
            with self.assertRaisesRegex(MigrationError, "cursor expired"):
                skipped_report(self.client, 8, "1")

    def test_no_pending_fake_download(self):
        with patch.object(self.client, "open") as open_request, self.assertRaises(MigrationError):
            self.client.save_cached({"state": "pending", "actualVariant": None}, self.dest, "original")
        open_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
