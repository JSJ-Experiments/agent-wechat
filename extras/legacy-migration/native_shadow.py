#!/usr/bin/env python3
"""Read-only preflight for migrating the legacy recorder to Agent WeChat's native API."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

KNOWN = {1, 3, 34, 42, 43, 47, 48, 49, 50, 51, 10000, 10002}
KNOWN_APP = {3, 4, 5, 6, 8, 19, 33, 36, 51, 57, 74}
ATTACHMENT = {3, 34, 43, 47}


class MigrationError(RuntimeError):
    """Native preflight cannot prove the requested behaviour."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        raise MigrationError("Unexpected redirect from native daemon.")


class Client:
    def __init__(self, origin, token, account):
        u = urlsplit(origin)
        if u.scheme != "http" or u.hostname not in ("127.0.0.1", "::1") or u.username or u.path not in ("", "/") or u.query or u.fragment:
            raise MigrationError("Use the isolated native daemon's loopback URL.")
        if not account or not token or not token.isascii() or any(c.isspace() for c in token):
            raise MigrationError("A valid native account and API token are required.")
        self.origin = origin.rstrip("/")
        self.token = token
        self.account = account
        self.prefix = "/v1/accounts/" + quote(account, safe="")
        self.opener = build_opener(ProxyHandler({}), NoRedirect)

    def get(self, suffix):
        return self._get(self.prefix + suffix)

    def _get(self, suffix):
        with self.open(suffix) as response:
            data = response.read(12_000_001)
        if len(data) > 12_000_000:
            raise MigrationError("Native response exceeds the shadow-reader cap.")
        return json.loads(data)

    def open(self, absolute_path):
        if not absolute_path.startswith(self.prefix + "/") or any(c in absolute_path for c in ("\r", "\n", "\\")):
            raise MigrationError("Unexpected URL in native daemon response.")
        req = Request(self.origin + absolute_path, headers={"Authorization": "Bearer " + self.token})
        return self.opener.open(req, timeout=25)

    def messages(self, thread, limit, cursor=None):
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self.get(f"/threads/{quote(thread, safe='')}/messages?{urlencode(params)}")

    def events(self, limit, after=None):
        params = {"limit": limit}
        if after is None:
            params["newest"] = "true"
        else:
            params["after"] = after
        try:
            return self.get(f"/events?{urlencode(params)}")
        except HTTPError as error:
            if error.code == 410:
                raise MigrationError("Requested event cursor expired; continuity cannot be verified.") from None
            raise

    def media(self, message):
        response = self.get(f"/messages/{quote(message, safe='')}/media")
        if isinstance(response, list):
            return response
        for key in ("assets", "items"):
            if isinstance(response.get(key), list):
                return response[key]
        if isinstance(response.get("id"), str):
            return [response]
        raise MigrationError("Unexpected native media manifest shape.")

    def save_cached(self, asset, destination, variant=None, max_size=800_000_000):
        if asset.get("state") != "available":
            raise MigrationError("Native attachment is not available; no preview was substituted.")
        if variant and asset.get("actualVariant") != variant:
            raise MigrationError("Cached bytes are not the requested media quality.")
        ident = asset.get("id")
        sha = asset.get("sha256")
        size = asset.get("byteLength")
        if not isinstance(ident, str) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise MigrationError("Cached media was not accompanied by a verified digest.")
        if type(size) is not int or not 0 < size <= max_size:
            raise MigrationError("Invalid or excessive media size.")
        path = f"{self.prefix}/media/{quote(ident, safe='')}/content"
        dest = Path(destination)
        if not dest.parent.is_dir():
            raise MigrationError("Destination parent must already exist.")
        fd, staged = tempfile.mkstemp(dir=dest.parent)
        try:
            digest, total = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as out, self.open(path) as resp:
                while block := resp.read(256 * 1024):
                    total += len(block)
                    if total > size:
                        raise MigrationError("Cached media length exceeded the verified manifest.")
                    digest.update(block)
                    out.write(block)
                if total != size or digest.hexdigest() != sha:
                    raise MigrationError("Cached media did not match its verified manifest.")
                out.flush()
                os.fsync(out.fileno())
            os.link(staged, dest)  # Never overwrite an existing archive or cloud symlink.
        finally:
            os.unlink(staged)
        return {"bytes": total, "sha256": sha, "actual_variant": asset.get("actualVariant")}


def classify(row):
    t, subtype = row.get("type"), row.get("appMessageType")
    if type(t) is not int or t not in KNOWN or t == 49 and (type(subtype) is not int or subtype not in KNOWN_APP):
        kind = "unknown"
    elif t in ATTACHMENT or (t, subtype) in ((49, 6), (49, 74)):
        kind = "attachment"
    else:
        kind = "readable"
    return {"kind": kind, "numeric_type": t, "numeric_subtype": subtype, "original_response": row}


def shadow_report(client, thread, legacy, limit):
    records = client.messages(thread, limit)
    items = records.get("items", [])
    report = Counter(classify(x)["kind"] for x in items)
    report["native_messages"] = len(items)
    report["coverage_partial"] = int(bool((records.get("coverage") or {}).get("partial")))
    if not items:
        raise MigrationError("No native messages to compare; check profile history and thread selection.")
    ids = [x.get("serverId") for x in items if isinstance(x.get("serverId"), str)]
    report["with_string_server_id"] = len(ids)
    if legacy is not None and ids:
        with closing(sqlite3.connect(legacy.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            base = {}
            for row in db.execute(f"""SELECT server_id,type,app_msg_type,media_status,content,raw_content
                                      FROM messages WHERE chat_id=? AND server_id IN ({','.join('?' for _ in ids)})""",
                                  (thread, *ids)):
                base[row["server_id"]] = row
        report["also_in_legacy"] = len(base)
        for item in items:
            previous = base.get(item.get("serverId"))
            if previous is None:
                continue
            report["matched_numeric_types"] += int(item.get("type") == previous["type"])
            report["matched_numeric_subtypes"] += int(item.get("appMessageType") == previous["app_msg_type"])
            if item.get("type") == 1:
                report["matched_text_bodies"] += int(item.get("body") == previous["content"])
            if previous["raw_content"] and classify(item)["kind"] == "unknown":
                report["legacy_unknown_raw_not_exposed"] += 1
    report["official_account_discovery_supported"] = 0
    return dict(report)


def skipped_report(client, limit, after=None):
    """Report only counts and numeric labels; don't print unknown payloads."""
    page = client.events(limit, after)
    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
        raise MigrationError("Unexpected native event page shape.")
    cursor = page.get("nextCursor")
    if not isinstance(cursor, str) or not re.fullmatch(r"[0-9]{1,20}", cursor):
        raise MigrationError("Native event page doesn't have a valid resume cursor.")
    skipped = Counter()
    numeric_messages = Counter()
    for event in page["items"]:
        if not isinstance(event, dict) or event.get("kind") != "account.control_observed":
            continue
        data = event.get("data")
        if not isinstance(data, dict) or data.get("projection") != "skipped":
            continue
        control = data.get("control")
        command = control.get("commandId") if isinstance(control, dict) else None
        message_type = control.get("messageType") if isinstance(control, dict) else None
        skipped[str(command) if type(command) is int else "unlabeled"] += 1
        if type(message_type) is int:
            numeric_messages[str(message_type)] += 1
    return {
        "events_sampled": len(page["items"]),
        "next_cursor": cursor,
        "skipped_controls": sum(skipped.values()),
        "skipped_by_command": dict(skipped),
        "skipped_by_message_type": dict(numeric_messages),
    }


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="Explicit loopback base URL for the native daemon.")
    parser.add_argument("--token-file", required=True, type=Path, help="Read-only API credential, never printed.")
    parser.add_argument("--account", required=True, help="Isolated native account ID or alias.")
    commands = parser.add_subparsers(dest="command", required=True)
    shadow = commands.add_parser("compare", help="Compare a bounded message page to the optional legacy recorder.")
    shadow.add_argument("thread", help="WeChat thread identifier.")
    shadow.add_argument("--legacy", type=Path, help="Read-only path to the legacy recorder database.")
    shadow.add_argument("--limit", type=int, default=25, help="Sample between 1 and 100 recent messages.")
    skipped = commands.add_parser("skipped", help="Count native skipped controls without exposing their payloads.")
    skipped.add_argument("--after", help="Resume after a previously reported cursor; default: newest page.")
    skipped.add_argument("--limit", type=int, default=100, help="Sample between 1 and 250 events.")
    download = commands.add_parser("download", help="Download and verify an already-cached native asset.")
    download.add_argument("message", help="Opaque native message ID.")
    download.add_argument("output", type=Path, help="New file; existing destinations are never overwritten.")
    download.add_argument("--variant", required=True, choices=("thumbnail", "standard", "original", "normal", "encoded"),
                          help="Require this actual quality. There is no thumbnail fallback.")
    args = parser.parse_args()
    try:
        client = Client(args.server, args.token_file.read_text().strip(), args.account)
        if args.command == "compare":
            result = shadow_report(client, args.thread, args.legacy, max(1, min(100, args.limit)))
        elif args.command == "skipped":
            if args.after is not None and not re.fullmatch(r"[0-9]{1,20}", args.after):
                raise MigrationError("Expected a decimal event cursor.")
            result = skipped_report(client, max(1, min(250, args.limit)), args.after)
        else:
            assets = [x for x in client.media(args.message)
                      if x.get("state") == "available" and x.get("actualVariant") == args.variant]
            if len(assets) != 1:
                raise MigrationError("Expected exactly one available asset of the requested quality.")
            result = client.save_cached(assets[0], args.output, args.variant)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (MigrationError, OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Native migration check: {error}\n")


if __name__ == "__main__":
    main()
