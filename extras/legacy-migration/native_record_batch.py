#!/usr/bin/env python3
"""Archive one page of committed native messages in a separate canary database."""

import argparse
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from native_shadow import Client, MigrationError, classify

APPLICATION_ID = 0x57584E52  # WXNR


def position(value):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,19}", value):
        raise MigrationError("Native reply contains an invalid event cursor.")
    return int(value)


def archive_db(path):
    if path.is_symlink():
        raise MigrationError("Use a private canary database, not a symlink.")
    db = sqlite3.connect(path)
    try:
        application_id = db.execute("PRAGMA application_id").fetchone()[0]
        if application_id != APPLICATION_ID:
            if application_id != 0 or db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                raise MigrationError("Not a native canary database; the existing database was not modified.")
            db.executescript(f"""
                BEGIN IMMEDIATE;
                PRAGMA application_id={APPLICATION_ID};
                CREATE TABLE IF NOT EXISTS checkpoint (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    origin TEXT NOT NULL, account TEXT NOT NULL, cursor TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recorded_messages (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, server_id TEXT,
                    cursor TEXT NOT NULL, category TEXT NOT NULL, type INTEGER,
                    app_type INTEGER, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS skipped_controls (
                    cursor TEXT PRIMARY KEY, command_id INTEGER, message_type INTEGER
                );
                COMMIT;
            """)
    except Exception:
        db.close()
        raise
    return db


def checkpoint(db):
    return db.execute("SELECT origin, account, cursor FROM checkpoint WHERE singleton=1").fetchone()


def archive_page(client, db, limit):
    saved = checkpoint(db)
    if saved is None:
        page = client.events(1)
        if not isinstance(page, dict):
            raise MigrationError("Unexpected native event response; not establishing a baseline.")
        baseline = page.get("nextCursor")
        position(baseline)
        with db:
            db.execute("INSERT INTO checkpoint VALUES (1,?,?,?)", (client.origin, client.account, baseline))
        return {"baseline_only": True, "next_cursor": baseline}

    origin, account, before = saved
    if (origin, account) != (client.origin, client.account):
        raise MigrationError("The canary database belongs to a different native source or account.")
    page = client.events(limit, after=before)
    if not isinstance(page, dict) or not isinstance(page.get("items"), list) or len(page["items"]) > limit:
        raise MigrationError("Unexpected native event page; cursor left unchanged.")
    next_cursor = page.get("nextCursor")
    last = position(before)
    messages, skipped, counted = {}, [], Counter()
    for event in page["items"]:
        if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
            raise MigrationError("Invalid native event; cursor left unchanged.")
        cursor, data = event.get("cursor"), event.get("data")
        if position(cursor) <= last:
            raise MigrationError("Native events arrived out of order; cursor left unchanged.")
        last = position(cursor)
        kind = event.get("kind")
        if kind in ("message.committed", "account.control_observed") and not isinstance(data, dict):
            raise MigrationError("Native event has no usable details; cursor left unchanged.")
        if kind == "message.committed":
            ident, thread = data.get("messageId"), data.get("threadId")
            if not all(isinstance(value, str) and value for value in (ident, thread)):
                raise MigrationError("A committed message lacks its native identifiers; cursor left unchanged.")
            body = client.get("/messages/" + quote(ident, safe=""))
            if (not isinstance(body, dict) or body.get("id") != ident or body.get("threadId") != thread
                    or body.get("accountId") != account):
                raise MigrationError("A committed message is not readable under its ID yet; cursor left unchanged.")
            result = classify(body)
            messages[ident] = (ident, thread, body.get("serverId"), cursor, result["kind"],
                               result["numeric_type"], result["numeric_subtype"], json.dumps(body, ensure_ascii=False))
        elif kind == "account.control_observed" and data.get("projection") == "skipped":
            control = data.get("control") or {}
            control = control if isinstance(control, dict) else {}
            command, msg_type = control.get("commandId"), control.get("messageType")
            skipped.append((cursor, command if type(command) is int else None,
                            msg_type if type(msg_type) is int else None))
    if position(next_cursor) != last:
        raise MigrationError("Native page cursor does not match the last event; cursor left unchanged.")

    with db:
        db.execute("BEGIN IMMEDIATE")
        if checkpoint(db) != saved:
            raise MigrationError("Another recorder updated this canary; rerun the batch.")
        for row in messages.values():
            counted[row[4]] += 1
            db.execute("""INSERT INTO recorded_messages VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET server_id=excluded.server_id, cursor=excluded.cursor,
                category=excluded.category,type=excluded.type,app_type=excluded.app_type,payload=excluded.payload""", row)
        db.executemany("INSERT INTO skipped_controls VALUES (?,?,?)", skipped)
        db.execute("UPDATE checkpoint SET cursor=? WHERE singleton=1", (next_cursor,))
    return {"events": len(page["items"]), "messages": len(messages), "categories": dict(counted),
            "skipped_controls": len(skipped), "next_cursor": next_cursor}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="Loopback native daemon URL.")
    parser.add_argument("--token-file", type=Path, required=True, help="Private native API read token.")
    parser.add_argument("--account", required=True, help="Explicit isolated native account.")
    parser.add_argument("--archive", type=Path, required=True, help="Private, separate native-canary SQLite file.")
    parser.add_argument("--limit", type=int, default=20, choices=range(1, 51), metavar="1–50",
                        help="Maximum events read in this batch (default: 20).")
    args = parser.parse_args()
    try:
        client = Client(args.server, args.token_file.read_text().strip(), args.account)
        with closing(archive_db(args.archive)) as db:
            result = archive_page(client, db, args.limit)
        print(json.dumps(result))
    except (MigrationError, sqlite3.Error, OSError, ValueError) as error:
        parser.exit(1, f"Native recorder canary: {error}\n")


if __name__ == "__main__":
    main()
