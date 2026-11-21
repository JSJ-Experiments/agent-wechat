#!/usr/bin/env python3
"""Read narrowly selected published article cards inside the legacy container.

Run via the collector. Cryptographic material stays in the container and will
never be copied to this tool's host or returned in command results.
"""
from collections import Counter
import ctypes
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from xml.etree.ElementTree import fromstring, ParseError


def quote(value):
    return "'" + value.replace("'", "''") + "'"


def query(path, key, sql, raw=False):
    value = f'"x\'{key}\'"' if raw else quote(key)
    args = ("/usr/local/bin/sqlcipher", "-batch", "-bail", "-json", path.as_uri() + "?mode=ro")
    code = f"PRAGMA key = {value}; PRAGMA cipher_compatibility = 4; PRAGMA query_only = ON; {sql};\n"
    p = subprocess.run(args, input=code, encoding="utf-8", capture_output=True, timeout=8)
    if p.returncode:
        raise RuntimeError("Read-only legacy profile query failed.")
    out = p.stdout.strip()
    last = []
    while out:
        last, n = json.JSONDecoder().raw_decode(out)
        out = out[n:].lstrip()
    return last if last and "ok" not in last[0] else []


def zstd_body(value, lib):
    payload = bytes.fromhex(value)
    if not payload.startswith(b"\x28\xb5\x2f\xfd"):
        return payload.decode("utf-8")
    dest = ctypes.create_string_buffer(1_000_000)
    n = lib.ZSTD_decompress(dest, len(dest), payload, len(payload))
    if n > len(dest):
        raise ValueError("Unsupported or excessive article card body.")
    return dest.raw[:n].decode("utf-8")


def articles(payload):
    if "<!DOCTYPE" in payload.upper() or "<!ENTITY" in payload.upper():
        return
    raw = payload[payload.find("<msg"):].strip()
    try:
        tree = fromstring(raw)
    except ParseError:
        return
    app = tree.find("appmsg")
    if app is None or app.findtext("type") != "5":
        return
    for card in [app, *app.findall("./mmreader/category/item")][:12]:
        url, title = card.findtext("url"), card.findtext("title")
        if isinstance(url, str) and url.startswith(("https://mp.weixin.qq.com/s", "http://mp.weixin.qq.com/s")) and title:
            yield url[:8192], " ".join(title.split())[:600]


def main():
    wanted = json.load(sys.stdin)
    track = "--followed" in sys.argv[1:]
    if not isinstance(wanted, list) or not (0 if track else 1) <= len(wanted) <= 10 or any(
        not isinstance(n, str) or not 1 <= len(n) <= 120 or any(ord(ch) < 32 for ch in n) for n in wanted
    ):
        raise ValueError("Invalid selected publishers.")
    agent = Path("/data/agent.db")
    token = Path("/data/auth-token").read_text().strip()
    records = query(agent, token, """SELECT account_dir,db_name,hex_key FROM wechat_keys
        WHERE db_name IN ('contact.db','biz_message_0.db')""")
    by_name = {r["db_name"]: r for r in records}
    if len(records) != 2 or len({r["account_dir"] for r in records}) != 1:
        raise RuntimeError("Expected exactly one stored legacy profile; no cards read.")

    def profile_db(name):
        record = by_name[name]
        who, key = record["account_dir"], record["hex_key"]
        if not re.fullmatch(r"[A-Za-z0-9_@.-]{1,180}", who) or who in (".", "..") or not re.fullmatch(r"[0-9a-fA-F]{64,96}", key):
            raise RuntimeError("Unsupported official profile metadata.")
        candidates = [p for prefix in ("/home/wechat/xwechat_files", "/home/wechat/Documents/xwechat_files")
                      for p in Path(prefix).joinpath(who, "db_storage").glob("*/" + name)]
        if len(candidates) != 1 or not candidates[0].is_file():
            raise RuntimeError("Configured legacy profile database is unavailable.")
        return lambda sql: query(candidates[0], key, sql, raw=True)

    contact = profile_db("contact.db")
    biz = profile_db("biz_message_0.db")
    # Active official accounts in this supported desktop profile: active
    # contact row with official-account verification bit, not cached strangers.
    active = "(local_type=1 AND (flag & 1)<>0 AND delete_flag=0 AND (verify_flag & 8)<>0)"
    selection = "nick_name IN (" + ",".join(map(quote, wanted)) + ")"
    contacts = contact(f"""SELECT username,nick_name,{active} AS followed FROM contact
                            WHERE substr(username,1,3)='gh_' AND ({selection}{" OR " + active if track else ""})
                            ORDER BY followed DESC, username LIMIT 501""")
    if len(contacts) > 500:
        raise RuntimeError("Too many official accounts in the supported desktop profile.")
    contacts = [r for r in contacts if re.fullmatch(r"gh_[a-z0-9_]{4,100}", r["username"])
                and isinstance(r["nick_name"], str) and r["nick_name"].strip()]
    frequencies = Counter(r["nick_name"] for r in contacts)
    follows = [{"id": "oa-" + hashlib.sha256(r["username"].encode()).hexdigest()[:20],
                "publisher": r["nick_name"]} for r in contacts if r["followed"]]
    inventory = {row["name"] for row in biz("SELECT name FROM sqlite_master WHERE type='table'")}
    lib = ctypes.CDLL("libzstd.so.1")
    lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    lib.ZSTD_decompress.restype = ctypes.c_size_t
    results = []
    for row in contacts:
        if frequencies[row["nick_name"]] != 1:
            continue
        table = "Msg_" + hashlib.md5(row["username"].encode(), usedforsecurity=False).hexdigest()
        if table not in inventory:
            continue
        cards = biz(f"""SELECT create_time, hex(message_content) AS payload FROM {table}
            WHERE (local_type & 4294967295)=49 AND length(message_content) <= 500000
            ORDER BY create_time DESC, local_id DESC LIMIT 12""")
        for card in cards:
            try:
                for url, title in articles(zstd_body(card["payload"], lib)):
                    results.append({"publisher": row["nick_name"], "title": title, "url": url,
                                    "published": card["create_time"]})
            except (ValueError, UnicodeError):
                continue
    results.sort(key=lambda row: row["published"] or 0, reverse=True)
    print(json.dumps({"cards": results[:1500], "follows": follows,
                      "ambiguous_names": sum(1 for count in frequencies.values() if count > 1)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Do not include upstream DB statements, local profile IDs, or key material in errors.
        sys.exit("Could not read the selected legacy article cards.")
