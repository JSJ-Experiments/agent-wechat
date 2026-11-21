from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from native_record_batch import archive_db, archive_page, checkpoint
from native_shadow import Client, MigrationError


def event(cursor, kind, **data):
    return {"cursor": str(cursor), "kind": kind, "data": data}


class RecordBatchTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(TemporaryDirectory())) / "shadow.db"
        self.client = Client("http://127.0.0.1:1234", "fake", "example")

    def baseline(self):
        with closing(archive_db(self.path)) as db, patch.object(self.client, "events", return_value={
            "nextCursor": "10", "items": [event(10, "message.committed", messageId="prior", threadId="filehelper")],
        }) as events, patch.object(self.client, "get") as get:
            self.assertEqual(archive_page(self.client, db, 10), {"baseline_only": True, "next_cursor": "10"})
            events.assert_called_once_with(1)
            get.assert_not_called()

    def test_resume_after_missing_row_and_archive_exact_unknown_shape(self):
        self.baseline()
        page = {"nextCursor": "13", "items": [
            event(11, "message.committed", messageId="first", threadId="filehelper"),
            event(12, "message.committed", messageId="second", threadId="filehelper"),
            event(13, "account.control_observed", projection="skipped", control={"commandId": 12,"messageType": 8228}),
        ]}
        base = {"accountId": "example", "threadId": "filehelper", "direction": "outgoing"}
        first = base | {"id": "first", "type": 1, "body": "sample", "serverId": "90071992547409981"}
        second = base | {"id": "second", "type": 49, "appMessageType": 8228, "extra": {"preserved": True}}
        with closing(archive_db(self.path)) as db, patch.object(self.client, "events", return_value=page), \
                patch.object(self.client, "get", side_effect=[first, HTTPError("", 404, "not visible", None, None)]):
            with self.assertRaises(HTTPError):
                archive_page(self.client, db, 10)
            self.assertEqual(checkpoint(db)[2], "10")
            self.assertEqual(db.execute("SELECT count(*) FROM recorded_messages").fetchone()[0], 0)
        with closing(archive_db(self.path)) as db, patch.object(self.client, "events", return_value=page) as events, \
                patch.object(self.client, "get", side_effect=[first, second]):
            report = archive_page(self.client, db, 10)
            events.assert_called_once_with(10, after="10")
            self.assertEqual(report["categories"], {"readable": 1, "unknown": 1})
            self.assertEqual(checkpoint(db)[2], "13")
            self.assertEqual(db.execute("SELECT server_id FROM recorded_messages WHERE id='first'").fetchone()[0], first["serverId"])
            self.assertEqual(json.loads(db.execute("SELECT payload FROM recorded_messages WHERE id='second'").fetchone()[0]), second)
            self.assertEqual(db.execute("SELECT * FROM skipped_controls").fetchone(), ("13", 12, 8228))
        with closing(archive_db(self.path)) as db, patch.object(self.client, "events", return_value={"nextCursor": "13", "items": []}) as events:
            self.assertEqual(archive_page(self.client, db, 10)["messages"], 0)
            events.assert_called_once_with(10, after="13")

    def test_expired_or_inconsistent_cursor_is_never_silently_reset(self):
        self.baseline()
        with closing(archive_db(self.path)) as db, \
                patch.object(self.client, "get", side_effect=HTTPError("", 410, "gone", None, None)):
            with self.assertRaisesRegex(MigrationError, "cursor expired"):
                archive_page(self.client, db, 10)
            self.assertEqual(checkpoint(db)[2], "10")
        with closing(archive_db(self.path)) as db, \
                patch.object(self.client, "events", return_value={"nextCursor": "11", "items": []}):
            with self.assertRaisesRegex(MigrationError, "does not match"):
                archive_page(self.client, db, 10)
            self.assertEqual(checkpoint(db)[2], "10")

    def test_refuses_to_change_an_existing_recorder_database(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE messages(id INTEGER)")
        with self.assertRaisesRegex(MigrationError, "not modified"):
            archive_db(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_schema").fetchall(), [("messages",)])
