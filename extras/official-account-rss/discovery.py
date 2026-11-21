"""Find received publisher cards without moving the desktop or starting a sync."""
from datetime import datetime, UTC
import json
from pathlib import Path
import re
import subprocess
import sys
from collector import Refused, article_url

ROOT = Path(__file__).resolve().parent


def legacy_snapshot(config):
    name = config.get("desktop", {}).get("container", "agent-wechat")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        raise Refused("Invalid legacy container.")
    names = list(config.get("publishers", {}).values())
    track = bool(config.get("track_follows"))
    if not (0 if track else 1) <= len(names) <= 10:
        raise Refused("Choose at most ten manual publishers; specify one if auto-tracking is off.")
    probe = ROOT.joinpath("read_legacy_cards.py").read_text()
    try:
        result = subprocess.run(["docker", "exec", "-i", name, "python3", "-c", probe, *(["--followed"] if track else [])],
                                input=json.dumps(names).encode(), capture_output=True, timeout=45)
    except subprocess.TimeoutExpired:
        raise Refused("Legacy profile query timed out.") from None
    if result.returncode:
        raise Refused("The selected legacy profile or article layout couldn’t be read.")
    snapshot = json.loads(result.stdout)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("cards"), list) or not isinstance(snapshot.get("follows"), list):
        raise Refused("Unexpected legacy discovery result.")
    return snapshot


def native_cards(config):
    # Import the same strict loopback and token handling used by the migration verifier.
    sys.path.insert(0, str(ROOT.parent / "legacy-migration"))
    from native_shadow import Client, MigrationError
    d = config.get("native", {})
    threads = d.get("publisher_threads", {})
    if not isinstance(threads, dict) or not 1 <= len(threads) <= 10:
        raise Refused("Configure one to ten native publisher thread IDs; no native reads made.")
    publishers = config.get("publishers", {})
    for selector, thread in threads.items():
        if selector not in publishers or not isinstance(thread, str) or not re.fullmatch(r"gh_[a-z0-9_]{4,100}", thread):
            raise Refused("Native publisher IDs must map a configured publisher to an exact gh_ thread.")
    try:
        client = Client(d.get("server", "http://127.0.0.1:16174"),
                        Path(d["token_file"]).expanduser().read_text().strip(), d["account"])
    except (KeyError, MigrationError):
        raise Refused("A loopback native server, account, and private token file are required.") from None
    results = []
    for selector, thread in threads.items():
        try:
            data = client.messages(thread, 15)
        except (OSError, ValueError, MigrationError):
            raise Refused("Could not read a configured publisher thread from the native daemon.") from None
        for msg in data.get("items", []):
            link = msg.get("link")
            if msg.get("recall") is not None or not isinstance(link, dict) or not link.get("title"):
                continue
            try:
                url = article_url(link.get("url"))
            except Refused:
                continue
            try:
                parsed = datetime.fromisoformat(msg.get("createdAt") or "")
                published = int((parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp())
            except (ValueError, OverflowError):
                published = None
            results.append({"publisher": publishers[selector], "title": link["title"][:600],
                            "url": url, "published": published})
    return results
