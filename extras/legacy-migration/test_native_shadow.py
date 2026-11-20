import hashlib
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from native_shadow import Client, MigrationError, classify


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

    def test_no_pending_fake_download(self):
        with patch.object(self.client, "open") as open_request, self.assertRaises(MigrationError):
            self.client.save_cached({"state": "pending", "actualVariant": None}, self.dest, "original")
        open_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
