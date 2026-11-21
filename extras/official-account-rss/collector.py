#!/usr/bin/env python3
"""Collect WeChat official-account articles and publish local, full-content RSS."""
import argparse
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from email.utils import format_datetime
import fcntl
import hashlib
from html import escape, unescape
from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree.ElementTree import Element, SubElement, tostring
from zoneinfo import ZoneInfo

from lxml import html
from PIL import Image

ROOT = Path(__file__).resolve().parent
ART_HOST = "mp.weixin.qq.com"
IMAGE_HOSTS = frozenset(("mmbiz.qpic.cn", "mmbiz.qlogo.cn"))
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
ASSET_NAME = re.compile(r"[0-9a-f]{64}\.(?:jpg|png|webp|gif)")
ARTICLE_MARKER = "@@LOCAL_IMAGES@@"
PENDING_IMAGE = re.compile(r'<img data-wx-pending="([^"<>]+)" alt="([^"<>]*)" />')
LOCAL_IMAGE = re.compile(r'<img src="@@LOCAL_IMAGES@@/([0-9a-f]{64}\.(?:jpg|png|webp|gif))"')
BAD_XML = re.compile(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
SAFE = frozenset("p section div span br hr a b strong i em u s blockquote pre code h1 h2 h3 h4 h5 h6 ul ol li table thead tbody tr td th figure figcaption sup sub".split())
DROP = frozenset("script style noscript template iframe object embed form input video audio svg math button select".split())
CARD_MATCH = """(a.url=d.url OR (a.publisher=d.publisher AND a.title=d.title AND
    abs(coalesce(a.published,a.found)-coalesce(d.published,d.found))<172800))"""


class Refused(RuntimeError):
    """A page was unavailable, unverified, or outside the collector's scope."""


class DesktopBusy(Refused):
    """The shared desktop is not currently safe to use."""


def tidy(value):
    return " ".join(value.split())


def clean_url(value):
    if not isinstance(value, str) or len(value) > 8192 or re.search(r"""[\x00-\x20\x7f\\'"<>]""", value):
        return None
    if value.startswith("//"):
        value = "https:" + value
    try:
        u = urlsplit(value)
        if u.scheme not in ("http", "https") or not u.hostname or u.username is not None:
            return None
        _ = u.port
    except ValueError:
        return None
    return u._replace(fragment="")


def article_url(value):
    u = clean_url(value)
    if not u or u.netloc != ART_HOST:
        raise Refused("Expected a WeChat article link from mp.weixin.qq.com.")
    if re.fullmatch(r"/s/[A-Za-z0-9_-]{4,100}", u.path):
        return "https://" + ART_HOST + u.path
    if u.path == "/s":
        query = parse_qs(u.query, max_num_fields=100)
        keys = ("__biz", "mid", "idx", "sn")
        if all(len(query.get(k, [])) == 1 and re.fullmatch(r"[A-Za-z0-9_+=/-]{1,128}", query[k][0]) for k in keys):
            return "https://" + ART_HOST + "/s?" + urlencode([(k, query[k][0]) for k in keys])
    raise Refused("Expected a canonical /s/ article URL, not a verification page or account profile.")


def image_url(value):
    u = clean_url(value)
    if u and u.netloc in IMAGE_HOSTS and re.match(r"/(?:sz_)?mmbiz_[a-z0-9]+/", u.path):
        return u._replace(scheme="https").geturl()
    return None


class StrictRedirect(HTTPRedirectHandler):
    max_redirections = 2

    def __init__(self, validate):
        self.validate = validate

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = self.validate(newurl)
        if target is None:
            raise Refused("Request redirected outside allowed WeChat content; stopped.")
        return super().redirect_request(req, fp, code, msg, headers, target)


def fetch(url, validate, maximum):
    opener = build_opener(StrictRedirect(validate))
    target = validate(url)
    if target is None:
        raise Refused("URL is outside allowed WeChat content.")
    with opener.open(Request(target, headers={"User-Agent": "OfficialAccountRSS/0.1"}), timeout=12) as r:
        length = r.headers.get("Content-Length")
        if length is not None and int(length) > maximum:
            raise Refused("Response exceeds the configured limit.")
        payload = r.read(maximum + 1)
    if len(payload) > maximum:
        raise Refused("Response exceeds the configured limit.")
    return payload


def node(tree, identifier):
    return next(iter(tree.xpath(".//*[@id=$identifier]", identifier=identifier)), None)


def attr_text(tree, identifier):
    selected = node(tree, identifier)
    return tidy(selected.text_content()) if selected is not None else ""


def parse_article(raw, timezone):
    if not 100 <= len(raw) <= 4_000_000:
        raise Refused("Missing article HTML or article exceeds capture limit.")
    tree = html.fragment_fromstring(raw, create_parent="div")
    title, publisher = attr_text(tree, "activity-name"), attr_text(tree, "js_name")
    body = node(tree, "js_content")
    if not title or not publisher or body is None or len(tidy(body.text_content())) < 15:
        raise Refused("No verified article body; WeChat may be requesting verification or still loading.")
    visible_date = attr_text(tree, "publish_time")
    published = None
    if visible_date:
        for pattern in ("%b %d, %Y, %I:%M %p", "%Y-%m-%d %H:%M", "%Y年%m月%d日 %H:%M", "%Y年%m月%d日"):
            try:
                published = int(datetime.strptime(visible_date, pattern).replace(tzinfo=timezone).timestamp())
                break
            except ValueError:
                pass
    description = tidy(body.text_content())[:420]
    return title, publisher, body, published, description


def sanitize(body, cache):
    missing = 0
    images = set()
    resolved = {}

    def emit(e):
        nonlocal missing
        if not isinstance(e.tag, str) or e.tag in DROP:
            return ""
        tag = e.tag.lower()
        if tag == "img":
            remote = image_url(e.get("data-src")) or image_url(e.get("src"))
            alt = escape(tidy(e.get("alt", ""))[:400], quote=True)
            if not remote:
                missing += 1
                return "[Image unavailable]"
            if remote not in resolved:
                resolved[remote] = cache(remote)
            name, retryable = resolved[remote]
            if not name:
                missing += 1
                if retryable:
                    return f'<img data-wx-pending="{escape(remote, quote=True)}" alt="{alt}" />'
                return "[Image unavailable]"
            images.add(name)
            return f'<img src="{ARTICLE_MARKER}/{name}" alt="{alt}" />'
        contents = escape(e.text or "") + "".join(emit(c) + escape(c.tail or "") for c in e)
        if tag not in SAFE:
            return contents
        if tag in ("br", "hr"):
            return f"<{tag} />"
        attrs = ""
        if tag == "a":
            href = clean_url(e.get("href"))
            if href:
                attrs = f' href="{escape(href.geturl(), quote=True)}" rel="noreferrer noopener"'
            else:
                return contents
        return f"<{tag}{attrs}>{contents}</{tag}>"

    output = escape(body.text or "") + "".join(emit(e) + escape(e.tail or "") for e in body)
    return output, len(images), missing


class Store:
    def __init__(self, directory):
        self.directory = directory
        self.images = directory / "images"
        self.images.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "articles.sqlite"
        self.last_image = 0.0
        with closing(self.open()) as db, db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS articles (
                    url TEXT PRIMARY KEY, identity TEXT, publisher TEXT NOT NULL, title TEXT NOT NULL,
                    description TEXT NOT NULL, html TEXT NOT NULL, published INTEGER, found INTEGER NOT NULL,
                    image_count INTEGER NOT NULL, missing_images INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS identity_index ON articles(identity);
                CREATE INDEX IF NOT EXISTS publisher_date ON articles(publisher, published DESC, found DESC);
                CREATE TABLE IF NOT EXISTS assets (
                    original TEXT PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS asset_name ON assets(name);
                CREATE TABLE IF NOT EXISTS attempts (
                    url TEXT PRIMARY KEY, state TEXT NOT NULL, updated INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checked_cards (
                    card_key TEXT PRIMARY KEY, checked INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_cursor (
                    destination TEXT PRIMARY KEY, position INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discovered_cards (
                    url TEXT PRIMARY KEY, publisher TEXT NOT NULL, title TEXT NOT NULL,
                    published INTEGER, found INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS image_backoff (
                    original TEXT PRIMARY KEY, retry_after INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS image_repair (
                    article TEXT PRIMARY KEY, checked INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collection_runs (
                    id INTEGER PRIMARY KEY, task TEXT NOT NULL, finished INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL, outcome TEXT NOT NULL, metrics TEXT NOT NULL,
                    note TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    url TEXT PRIMARY KEY, submitted INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS desktop_holds (
                    url TEXT PRIMARY KEY, updated INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS followed_publishers (
                    id TEXT PRIMARY KEY, publisher TEXT NOT NULL
                );
            """)

    def open(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def cached_image(self, db, original):
        old = db.execute("SELECT name FROM assets WHERE original=?", (original,)).fetchone()
        if old and ASSET_NAME.fullmatch(old["name"]) and (self.images / old["name"]).is_file():
            return old["name"]
        return None

    def cache_image(self, original):
        time.sleep(max(0, self.last_image + 0.85 - time.monotonic()))
        self.last_image = time.monotonic()
        try:
            payload = fetch(original, image_url, 12_000_000)
            with Image.open(io.BytesIO(payload)) as img:
                extension = {"GIF": "gif", "PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(img.format)
                if extension is None or img.width * img.height > 45_000_000:
                    return None
                img.verify()
        except (OSError, ValueError, Refused, Image.DecompressionBombError):
            return None
        mime = "image/jpeg" if extension == "jpg" else f"image/{extension}"
        name = f"{hashlib.sha256(payload).hexdigest()}.{extension}"
        target = self.images / name
        if not target.is_file():
            fd, temporary = tempfile.mkstemp(dir=self.images)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(payload)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        with closing(self.open()) as db, db:
            db.execute("INSERT OR REPLACE INTO assets VALUES (?,?,?)", (original, name, mime))
        return name

    def repair_images(self, limit, *, article=None, now=None):
        """Resolve only allowlisted image placeholders; never reopen an article."""
        now = int(time.time()) if now is None else now
        if limit <= 0:
            return 0
        deadline = time.monotonic() + 26
        with closing(self.open()) as db:
            if article:
                rows = db.execute("SELECT url,html FROM articles WHERE url=?", (article,)).fetchall()
            else:
                rows = db.execute("""SELECT a.url,a.html FROM image_repair r
                    JOIN articles a ON a.url=r.article ORDER BY r.checked,a.url LIMIT 20""").fetchall()
        requested = repaired = 0
        for row in rows:
            if requested >= limit or time.monotonic() >= deadline:
                break
            resolved = {}

            def complete(match):
                nonlocal requested
                original = image_url(unescape(match[1]))
                if original is None:
                    return "[Image unavailable]"
                if original not in resolved:
                    with closing(self.open()) as db:
                        name = self.cached_image(db, original)
                        blocked = db.execute("SELECT 1 FROM image_backoff WHERE original=? AND retry_after>?",
                                             (original, now)).fetchone()
                    if not name and not blocked and requested < limit and time.monotonic() < deadline:
                        requested += 1
                        name = self.cache_image(original)
                        with closing(self.open()) as db, db:
                            if name:
                                db.execute("DELETE FROM image_backoff WHERE original=?", (original,))
                            else:
                                db.execute("INSERT OR REPLACE INTO image_backoff VALUES (?,?)",
                                           (original, now + 12 * 3600))
                    resolved[original] = name
                if name := resolved[original]:
                    return f'<img src="{ARTICLE_MARKER}/{name}" alt="{match[2]}" />'
                return match[0]
            content = PENDING_IMAGE.sub(complete, row["html"])
            replaced = len(PENDING_IMAGE.findall(row["html"])) - len(PENDING_IMAGE.findall(content))
            repaired += replaced
            with closing(self.open()) as db, db:
                if replaced:
                    db.execute("""UPDATE articles SET html=?, image_count=?, missing_images=missing_images-?
                                  WHERE url=?""", (content, len(set(LOCAL_IMAGE.findall(content))), replaced, row["url"]))
                if PENDING_IMAGE.search(content):
                    db.execute("INSERT OR REPLACE INTO image_repair VALUES (?,?)", (row["url"], now))
                else:
                    db.execute("DELETE FROM image_repair WHERE article=?", (row["url"],))
        return repaired

    def ingest(self, url, raw, expected=None, timezone=UTC, now=None):
        url = article_url(url)
        title, publisher, body, published, description = parse_article(raw, timezone)
        if expected is not None and tidy(publisher) != tidy(expected):
            raise Refused("Article publisher does not match the selected account.")
        now = now or int(time.time())
        identity = hashlib.sha256(f"{publisher}\0{title}\0{published or ''}\0{description}".encode()).hexdigest()
        with closing(self.open()) as db:
            old = db.execute("SELECT * FROM articles WHERE url=? OR identity=? LIMIT 1", (url, identity)).fetchone()
            if old and old["missing_images"] == 0:
                return old, False
        count = 0

        def cache(candidate):
            nonlocal count
            count += 1
            return self.cached_image(db, candidate), count <= 160

        with closing(self.open()) as db:
            content, image_count, missing = sanitize(body, cache)
        if not content.strip():
            raise Refused("No publishable article body remained after cleaning.")
        canonical = old["url"] if old else url
        with closing(self.open()) as db, db:
            db.execute("""INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET identity=excluded.identity,
                title=excluded.title, description=excluded.description, html=excluded.html,
                published=excluded.published, image_count=excluded.image_count,
                missing_images=excluded.missing_images""",
                       (canonical, identity, publisher, title, description, content, published, now, image_count, missing))
            db.execute("DELETE FROM attempts WHERE url=?", (url,))
            if PENDING_IMAGE.search(content):
                db.execute("INSERT OR IGNORE INTO image_repair VALUES (?,0)", (canonical,))
            else:
                db.execute("DELETE FROM image_repair WHERE article=?", (canonical,))
        # Commit the article first so an interrupted image download can't lose it.
        self.repair_images(16, article=canonical)
        with closing(self.open()) as db:
            return db.execute("SELECT * FROM articles WHERE url=?", (canonical,)).fetchone(), old is None

    def request_article(self, url, expected=None, timezone=UTC, retry=False):
        url = article_url(url)
        with closing(self.open()) as db:
            old = db.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()
            held = db.execute("SELECT * FROM attempts WHERE url=?", (url,)).fetchone()
        if old and old["missing_images"] == 0:
            return old, False
        if held and not retry and (held["state"] == "verification-or-layout" or held["updated"] + 6 * 3600 > time.time()):
            raise Refused("URL was already blocked or temporarily unavailable; not retrying automatically.")
        try:
            data = fetch(url, article_url, 4_000_000)
            return self.ingest(url, data.decode("utf-8"), expected, timezone)
        except (Refused, HTTPError, URLError, UnicodeError) as error:
            hold = isinstance(error, Refused) or isinstance(error, HTTPError) and error.code in (401, 403, 429)
            with closing(self.open()) as db, db:
                db.execute("INSERT OR REPLACE INTO attempts VALUES (?,?,?)",
                           (url, "verification-or-layout" if hold else "temporarily-unavailable", int(time.time())))
            raise Refused("WeChat did not provide this article to a normal web request. Open it in the signed-in client and use capture-open.") from error

    def has_card(self, key):
        with closing(self.open()) as db:
            row = db.execute("SELECT 1 FROM checked_cards WHERE card_key=? AND checked > ?",
                             (key, int(time.time()) - 36 * 3600)).fetchone()
            return row is not None

    def mark_card(self, key):
        with closing(self.open()) as db, db:
            db.execute("INSERT OR REPLACE INTO checked_cards VALUES (?,?)", (key, int(time.time())))

    def discover(self, cards, publishers):
        inserted = 0
        with closing(self.open()) as db, db:
            for card in cards[:1500]:
                publisher, title, stamp = card.get("publisher"), card.get("title"), card.get("published")
                if publisher not in publishers or not isinstance(title, str) or not tidy(title):
                    continue
                try:
                    url = article_url(card.get("url"))
                except (Refused, ValueError):
                    continue
                stamp = stamp if type(stamp) is int and 0 < stamp < 5_000_000_000 else None
                inserted += db.execute("INSERT OR IGNORE INTO discovered_cards VALUES(?,?,?,?,?)",
                                       (url, publisher, tidy(title)[:600], stamp, int(time.time()))).rowcount
        return inserted

    def publisher_map(self, config):
        selected = dict(config.get("publishers", {}))
        if config.get("track_follows"):
            with closing(self.open()) as db:
                names = set(selected.values())
                for row in db.execute("SELECT id,publisher FROM followed_publishers ORDER BY publisher,id"):
                    if row["publisher"] not in names and row["id"] not in selected:
                        selected[row["id"]] = row["publisher"]
                        names.add(row["publisher"])
        return selected

    def replace_follows(self, follows):
        with closing(self.open()) as db, db:
            db.execute("DELETE FROM followed_publishers")
            db.executemany("INSERT INTO followed_publishers VALUES (?,?)", ((f["id"], f["publisher"]) for f in follows))

    def submit_url(self, url):
        url = article_url(url.strip())
        with closing(self.open()) as db, db:
            if db.execute("SELECT 1 FROM articles WHERE url=?", (url,)).fetchone():
                return "saved"
            if db.execute("SELECT count(*) FROM submissions s WHERE NOT EXISTS(SELECT 1 FROM articles a WHERE a.url=s.url)").fetchone()[0] >= 1000:
                return "full"
            db.execute("INSERT OR IGNORE INTO submissions VALUES (?,?)", (url, int(time.time())))
            db.execute("DELETE FROM desktop_holds WHERE url=?", (url,))
            return "queued"

    def pending_desktop_submissions(self):
        with closing(self.open()) as db:
            return {row[0] for row in db.execute("""SELECT s.url FROM submissions s
                WHERE NOT EXISTS(SELECT 1 FROM articles a WHERE a.url=s.url)
                AND NOT EXISTS(SELECT 1 FROM desktop_holds h WHERE h.url=s.url)
                LIMIT 1000""")}

    def hold_desktop_submission(self, url):
        with closing(self.open()) as db, db:
            db.execute("INSERT OR REPLACE INTO desktop_holds VALUES (?,?)", (url, int(time.time())))

    def candidates(self, limit, *, submissions_only=False, publishers=None):
        allowed = sorted(publishers) if publishers is not None else None
        if allowed is None:
            publisher_filter = ""
        elif allowed:
            placeholders = ",".join("?" for _ in allowed)
            publisher_filter = f"AND (d.priority=1 OR d.publisher IN ({placeholders}))"
        else:
            publisher_filter = "AND d.priority=1"
        with closing(self.open()) as db:
            if db.execute("SELECT 1 FROM attempts WHERE state='verification-or-layout' AND updated>? LIMIT 1",
                          (int(time.time()) - 48 * 3600,)).fetchone():
                return []
            return db.execute(f"""WITH queue AS (
                SELECT s.url,d.publisher,d.title,d.published,s.submitted AS found,1 AS priority
                FROM submissions s LEFT JOIN discovered_cards d ON s.url=d.url
                UNION ALL
                SELECT d.*,0 AS priority FROM discovered_cards d
                WHERE NOT EXISTS(SELECT 1 FROM submissions s WHERE s.url=d.url)
            ) SELECT d.* FROM queue d WHERE (d.priority=1 OR NOT ?) {publisher_filter} AND
                NOT EXISTS(SELECT 1 FROM attempts t WHERE t.url=d.url AND
                    (t.state='verification-or-layout' OR t.updated>?))
                AND NOT EXISTS(SELECT 1 FROM articles a WHERE {CARD_MATCH})
                ORDER BY d.priority DESC,CASE WHEN d.priority=1 THEN d.found END,
                         coalesce(d.published,d.found) DESC, d.url LIMIT ?""",
                              (submissions_only, *(allowed or []), int(time.time()) - 6 * 3600, limit)).fetchall()

    def record_run(self, task, started, outcome, metrics, note):
        with closing(self.open()) as db, db:
            db.execute("""INSERT INTO collection_runs(task,finished,duration_ms,outcome,metrics,note)
                VALUES (?,?,?,?,?,?)""", (task, int(time.time()), round((time.monotonic() - started) * 1000),
                                          outcome, json.dumps(metrics), note))
            db.execute("DELETE FROM collection_runs WHERE id <= (SELECT max(id)-100 FROM collection_runs)")

    def latest(self, publisher=None):
        where, params = ("WHERE publisher=?", (publisher,)) if publisher else ("", ())
        with closing(self.open()) as db:
            return db.execute(f"""SELECT * FROM articles {where}
                ORDER BY coalesce(published,found) DESC, url LIMIT 50""", params).fetchall()

    def notify(self, secret_file):
        """Deliver new archive entries directly to the configured Discord channel."""
        parsed = clean_url(Path(secret_file).expanduser().read_text().strip())
        if not parsed or parsed.scheme != "https" or parsed.netloc != "discord.com" or parsed.query \
                or not re.fullmatch(r"/api/webhooks/[0-9]{15,25}/[A-Za-z0-9_-]{40,150}", parsed.path):
            raise Refused("Expected a Discord webhook in the private notification token file.")
        destination = hashlib.sha256(parsed.path.encode()).hexdigest()
        with closing(self.open()) as db, db:
            existing = db.execute("SELECT position FROM notification_cursor WHERE destination=?",
                                  (destination,)).fetchone()
            if existing is None:
                maximum = db.execute("SELECT coalesce(max(rowid),0) FROM articles").fetchone()[0]
                db.execute("INSERT INTO notification_cursor VALUES (?,?)", (destination, maximum))
                return 0
            records = db.execute("""SELECT rowid,url,title,publisher FROM articles WHERE rowid>?
                ORDER BY rowid LIMIT 5""", (existing[0],)).fetchall()
        if not records:
            return 0
        embeds = [{"title": r["title"][:220], "url": r["url"], "author": {"name": r["publisher"][:140]}}
                  for r in records]
        transport = build_opener(StrictRedirect(lambda _: None))
        url = parsed._replace(query="wait=true").geturl()
        body = json.dumps({"embeds": embeds, "allowed_mentions": {"parse": []}}).encode("utf-8")
        try:
            with transport.open(Request(url, data=body, headers={"Content-Type": "application/json"},
                                        method="POST"), timeout=12) as response:
                acknowledged = json.loads(response.read(256_000))
            if not isinstance(acknowledged.get("id"), str):
                raise Refused("Discord did not acknowledge the notification.")
        except (OSError, ValueError):
            raise Refused("Discord notification unavailable; retained pending articles.") from None
        with closing(self.open()) as db, db:
            db.execute("UPDATE notification_cursor SET position=? WHERE destination=?",
                       (records[-1]["rowid"], destination))
        return len(records)


def settings(config):
    value = json.loads(config.read_text())
    state = Path(value.get("state", ROOT / "state")).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = state / "token"
    if not token.exists():
        try:
            with token.open("x") as output:
                output.write(secrets.token_hex(32) + "\n")
        except FileExistsError:
            pass
    token = token.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise Refused("Invalid local feed access token.")
    publishers = value.get("publishers", {})
    if not isinstance(publishers, dict) or any(not SLUG.fullmatch(k) or not isinstance(v, str) or not tidy(v) for k, v in publishers.items()):
        raise Refused("Publishers must map stable IDs to exact WeChat display names.")
    public = clean_url(value.get("base_url", "http://127.0.0.1:16176"))
    if not public or public.query or public.path.rstrip("/"):
        raise Refused("Feed base URL must be an HTTP(S) origin.")
    return value, Store(state), token, public.geturl().rstrip("/")


@contextmanager
def run_lock(store):
    with (store.directory / "collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("Another collector task is running.") from None
        yield


@contextmanager
def observe(store, task, metrics):
    started = time.monotonic()
    outcome, note = "ok", ""
    try:
        yield
    except DesktopBusy as error:
        outcome, note = "skipped", str(error)
        raise
    except (Exception, KeyboardInterrupt):
        outcome, note = "error", "See the collector's service log for details."
        raise
    finally:
        if task and not (task == "submitted URLs" and outcome == "ok" and not metrics.get("matched_submissions")):
            try:
                store.record_run(task, started, outcome, metrics, note)
            except sqlite3.Error:
                print("Collector: could not save run statistics.", file=sys.stderr)


def archived_markup(row, image_base):
    content = PENDING_IMAGE.sub("[Image unavailable]", row["html"])
    return LOCAL_IMAGE.sub(lambda m: f'<img src="{escape(image_base, quote=True)}/{m[1]}"', content)


def render_rss(store, publisher, site, selector):
    tag = "{http://purl.org/rss/1.0/modules/content/}encoded"
    rss = Element("rss", {"version": "2.0"})
    ch = SubElement(rss, "channel")
    def sub(parent, key, value):
        SubElement(parent, key).text = BAD_XML.sub("", str(value))
    sub(ch, "title", publisher or "WeChat official accounts")
    sub(ch, "link", f"{site}/{selector}.xml")
    sub(ch, "description", "Archived articles from the WeChat official-account collector.")
    for row in store.latest(publisher):
        it = SubElement(ch, "item")
        sub(it, "title", row["title"])
        sub(it, "link", row["url"])
        sub(it, "guid", row["url"])
        sub(it, "category", row["publisher"])
        sub(it, "pubDate", format_datetime(datetime.fromtimestamp(row["published"] or row["found"], UTC), usegmt=True))
        sub(it, "description", escape(row["description"]))
        sub(it, tag, archived_markup(row, site + "/image"))
    return tostring(rss, encoding="utf-8", xml_declaration=True)


def feed_route(route, publishers):
    selector = route.removeprefix("discovered/")
    if selector.endswith(".xml") and selector[:-4] in {"all", *publishers}:
        return selector[:-4], route.startswith("discovered/")
    return None


def render_discovered_rss(store, publisher, site, selector):
    rss = Element("rss", {"version": "2.0"})
    channel = SubElement(rss, "channel")

    def sub(parent, key, value):
        SubElement(parent, key).text = BAD_XML.sub("", str(value))

    sub(channel, "title", f"{publisher or 'WeChat official accounts'} — discovered posts")
    sub(channel, "link", f"{site}/discovered/{selector}.xml")
    sub(channel, "description", "Headlines from delivered WeChat cards; this feed never claims to contain full text.")
    with closing(store.open()) as db:
        items = db.execute(f"""SELECT d.*, EXISTS(SELECT 1 FROM articles a WHERE {CARD_MATCH}) AS saved
              FROM discovered_cards d WHERE (? IS NULL OR d.publisher=?)
              ORDER BY coalesce(d.published,d.found) DESC,d.url LIMIT 50""", (publisher, publisher)).fetchall()
    for row in items:
        item = SubElement(channel, "item")
        sub(item, "title", row["title"])
        sub(item, "guid", row["url"])
        sub(item, "link", row["url"])
        sub(item, "category", row["publisher"])
        if row["published"]:
            sub(item, "pubDate", format_datetime(datetime.fromtimestamp(row["published"], UTC), usegmt=True))
        sub(item, "description", "Full text is in the archive feed." if row["saved"] else "Discovered headline only. Open the original in WeChat.")
    return tostring(rss, encoding="utf-8", xml_declaration=True)


def serve(config, store, token, origin):
    from dashboard import render_article, render_dashboard
    from web_access import HTML_HEADERS, PREFIX, WebAccess
    site = origin + "/" + token
    gate = WebAccess(config["web"], token) if config.get("web") else None

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(8)
            super().setup()

        def log_message(self, *_):
            pass  # Access token is in the path.

        def do_GET(self):
            self.reply(include_body=True)

        def do_HEAD(self):
            self.reply(include_body=False)

        def do_POST(self):
            self.reply(include_body=True)

        def reply(self, include_body):
            path, _, query = self.path.partition("?")
            web = bool(gate and path.startswith(PREFIX))
            try:
                publishers = store.publisher_map(config)
                if web:
                    route = gate.route(self, path, store, publishers)
                    if route is None:
                        return
                else:
                    secret, _, route = path.lstrip("/").partition("/")
                    if self.command == "POST" or not re.fullmatch(r"[0-9a-f]{64}", secret) or not hmac.compare_digest(secret, token):
                        self.send_error(404)
                        return
                if route == "dashboard":
                    try:
                        params = parse_qs(query, max_num_fields=8)
                    except ValueError:
                        self.send_error(400)
                        return
                    body = render_dashboard(store, config | {"publishers": publishers}, params, web=web)
                    mime, lifetime = "text/html; charset=utf-8", None
                elif re.fullmatch(r"article/[1-9][0-9]{0,17}", route):
                    body = render_article(store, int(route.split("/")[1]))
                    if body is None:
                        self.send_error(404)
                        return
                    mime, lifetime = "text/html; charset=utf-8", None
                elif feed := feed_route(route, publishers):
                    selector, discovery_feed = feed
                    render = render_discovered_rss if discovery_feed else render_rss
                    body = render(store, None if selector == "all" else publishers[selector],
                                  gate.reader_site() if web else site, selector)
                    mime, lifetime = "application/rss+xml; charset=utf-8", 120
                elif route.startswith("image/") and ASSET_NAME.fullmatch(route[6:]):
                    name = route[6:]
                    with closing(store.open()) as db:
                        asset = db.execute("SELECT mime FROM assets WHERE name=?", (name,)).fetchone()
                    if not asset or not (store.images / name).is_file():
                        self.send_error(404)
                        return
                    body = (store.images / name).read_bytes()
                    mime, lifetime = asset["mime"], 86400
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", f"private, max-age={lifetime}" if lifetime is not None else "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "same-origin" if web and route == "dashboard" else "no-referrer")
                if mime.startswith("text/html"):
                    for name, value in HTML_HEADERS.items():
                        self.send_header(name, value)
                self.end_headers()
                if include_body:
                    self.wfile.write(body)
            except (OSError, sqlite3.Error):
                # A disconnected reader needs no retry or log entry.
                return

    with HTTPServer(("127.0.0.1", int(config.get("port", 16176))), Handler) as server:
        server.serve_forever()


def show(result):
    row, new = result
    print(json.dumps({k: row[k] for k in ("publisher", "title", "url", "image_count", "missing_images")}
                     | {"new": new}, ensure_ascii=False))


def process_candidates(store, limit, zone, metrics, *, submissions_only=False, publishers=None):
    metrics.update(web_requests=0, held_this_run=0, new_articles=0)
    if not limit:
        return
    for index, row in enumerate(store.candidates(limit, submissions_only=submissions_only, publishers=publishers)):
        if index:
            time.sleep(2)
        try:
            metrics["web_requests"] += 1
            result = store.request_article(row["url"], row["publisher"], zone)
            metrics["new_articles"] += int(result[1])
            show(result)
        except Refused:
            metrics["held_this_run"] += 1
            break  # One refusal ends public requests in this run.


def process_desktop_submissions(store, config, zone, metrics):
    from desktop import Desktop, desktop_lease
    metrics.update(matched_submissions=0, article_views_opened=0, desktop_holds=0, new_articles=0)
    pending = store.pending_desktop_submissions()
    if not pending:
        return
    desktop = Desktop(config)
    matched = [(url, item) for url, item in desktop.filehelper_links().items() if url in pending]
    metrics["matched_submissions"] = len(matched)
    if not matched:
        return
    expected, (kind, label) = matched[0]
    idle = max(60, min(int(config.get("minimum_desktop_idle_seconds", 120)), 3600))
    try:
        with desktop_lease():
            url, markup = desktop.capture_received(expected, kind, label, idle, metrics)
    except DesktopBusy:
        raise
    except Refused as error:
        store.hold_desktop_submission(expected)
        metrics["desktop_holds"] += 1
        print(f"Desktop capture paused: {error}")
        return
    if url != expected:
        raise Refused("The viewer reported a different URL; nothing was archived.")
    result = store.ingest(url, markup, timezone=zone)
    metrics["new_articles"] += int(result[1])
    show(result)


def native_links(payload):
    """Only extract explicit article link cards, never arbitrary chat text."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise Refused("Expected a native CLI message-list JSON result.")
    seen = set()
    for message in payload["items"][:100]:
        if not isinstance(message, dict):
            continue
        link = message.get("link")
        if message.get("recall") is not None or not isinstance(link, dict):
            continue
        try:
            url = article_url(link.get("url"))
        except Refused:
            continue
        if url not in seen:
            seen.add(url)
            yield url


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json", help="Private collector configuration.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Serve private feeds and cached images on loopback.")
    u = sub.add_parser("url", help="Print a private RSS URL. Do not share its token.")
    u.add_argument("publisher", nargs="?", default="all", help="Publisher ID or all.")
    u.add_argument("--discovered", action="store_true", help="Use the headlines-only discovery feed.")
    dash = sub.add_parser("dashboard-url", help="Print the browser dashboard URL.")
    dash.add_argument("--local", action="store_true", help="Use the loopback URL with its private access token.")
    sub.add_parser("stats", help="Show archive coverage and recent run statistics as JSON.")
    sub.add_parser("process-submissions", help="Read a queued link from File Transfer in the signed-in desktop viewer.")
    a = sub.add_parser("add", help="Fetch a supplied link through ordinary HTTP; hold it if verification is required.")
    a.add_argument("url")
    a.add_argument("--retry", action="store_true", help="Manually retry a held URL.")
    sub.add_parser("from-native", help="Try the first WeChat article link card from a native CLI message page on stdin.")
    i = sub.add_parser("ingest", help="Import rich HTML copied from a WeChat article and archive its CDN images.")
    i.add_argument("url")
    i.add_argument("html", type=Path)
    i.add_argument("--publisher", help="Require an exact official-account display name.")
    c = sub.add_parser("capture-open", help="Capture the article already foregrounded in the desktop client.")
    c.add_argument("--publisher", help="Require an exact official-account display name.")
    c.add_argument("--url", help="Require the viewer to report this exact official-account article URL.")
    r = sub.add_parser("check", help="Scan the current official-account inbox for recent posts from configured publishers.")
    r.add_argument("--activate", action="store_true", help="Open Official Accounts first when the desktop main window is active.")
    r.add_argument("--skip-busy", action="store_true", help="For a timer: skip when the shared desktop is in use.")
    sub.add_parser("status", help="Show archived article counts without contacting WeChat.")
    notify = sub.add_parser("notify", help="Deliver pending archive entries to the configured Discord webhook.")
    notify.add_argument("--if-configured", action="store_true", help="Skip successfully without a configured webhook.")
    repair = sub.add_parser("repair-images", help="Retry queued public CDN images without opening article pages.")
    repair.add_argument("--limit", type=int, default=12, choices=range(1, 17), metavar="1–16",
                        help="Maximum image requests per run (default: 12).")
    discover = sub.add_parser("discover", help="Find newly delivered cards without using the desktop UI.")
    discover.add_argument("--source", choices=("legacy", "native"), default="legacy")
    discover.add_argument("--fetch", type=int, default=0, choices=(0, 1, 2),
                          help="Try this many ordinary article requests, holding blocked URLs.")
    args = parser.parse_args()
    try:
        config, store, access, origin = settings(args.config)
        zone = ZoneInfo(config.get("article_timezone", "UTC"))
        if args.command == "serve":
            serve(config, store, access, origin)
        elif args.command == "url":
            if args.publisher != "all" and args.publisher not in store.publisher_map(config):
                raise Refused("Unknown publisher ID.")
            print(f"{origin}/{access}/{'discovered/' if args.discovered else ''}{args.publisher}.xml")
        elif args.command == "dashboard-url":
            if not args.local and config.get("web"):
                print(config["web"]["origin"].rstrip("/") + "/wx/oa/")
            else:
                print(f"{origin}/{access}/dashboard")
        elif args.command == "stats":
            from dashboard import overview
            print(json.dumps(overview(store, config | {"publishers": store.publisher_map(config)}), ensure_ascii=False, indent=2))
        elif args.command == "status":
            with closing(store.open()) as db:
                for row in db.execute("SELECT publisher,count(*) AS articles,sum(image_count) AS images,sum(missing_images) AS missing FROM articles GROUP BY publisher"):
                    print(json.dumps(dict(row), ensure_ascii=False))
                print(json.dumps({"discovered_article_links": db.execute("SELECT count(*) FROM discovered_cards").fetchone()[0],
                                  "held_verification": db.execute("SELECT count(*) FROM attempts WHERE state='verification-or-layout'").fetchone()[0],
                                  "articles_with_queued_images": db.execute("SELECT count(*) FROM image_repair").fetchone()[0]}))
        else:
            task = {"discover": f"discover ({getattr(args, 'source', '')})", "check": "desktop check",
                    "notify": "alerts", "repair-images": "image repair", "capture-open": "desktop capture",
                    "process-submissions": "submitted URLs"}.get(args.command)
            metrics = {}
            with run_lock(store), observe(store, task, metrics):
                if args.command == "add":
                    show(store.request_article(args.url, retry=args.retry, timezone=zone))
                elif args.command == "from-native":
                    raw = sys.stdin.read(2_000_001)
                    if len(raw) > 2_000_000:
                        raise Refused("Native message list is too large.")
                    url = next(native_links(json.loads(raw)), None)
                    if url is None:
                        raise Refused("No explicit WeChat article link cards in this message page.")
                    show(store.request_article(url, timezone=zone))
                elif args.command == "ingest":
                    with args.html.open("r", encoding="utf-8") as f:
                        captured = f.read(4_000_001)
                    show(store.ingest(args.url, captured, args.publisher, zone))
                elif args.command == "notify":
                    notification_file = config.get("discord_webhook_file")
                    if not notification_file and not args.if_configured:
                        raise Refused("No Discord webhook has been configured.")
                    metrics.update(configured=bool(notification_file),
                                   delivered=store.notify(notification_file) if notification_file else 0)
                    print(json.dumps({"delivered": metrics["delivered"]}))
                elif args.command == "repair-images":
                    metrics["resolved_image_references"] = store.repair_images(args.limit)
                    print(json.dumps(metrics))
                elif args.command == "process-submissions":
                    process_desktop_submissions(store, config, zone, metrics)
                    print(json.dumps(metrics))
                elif args.command == "discover":
                    from discovery import legacy_snapshot, native_cards
                    if args.source == "legacy":
                        snapshot = legacy_snapshot(config)
                        cards = snapshot["cards"]
                        if config.get("track_follows"):
                            store.replace_follows(snapshot["follows"])
                            metrics.update(followed_publishers=len(snapshot["follows"]),
                                           ambiguous_names=snapshot["ambiguous_names"])
                    else:
                        cards = native_cards(config)
                    publishers = set(store.publisher_map(config).values())
                    added = store.discover(cards, publishers)
                    metrics.update(discovered=added, links_read=len(cards))
                    process_candidates(store, args.fetch, zone, metrics, publishers=publishers)
                    print(json.dumps(metrics | {"cards_read": metrics["links_read"]}))
                else:
                    from desktop import Desktop, desktop_lease
                    desktop = Desktop(config | {"publishers": store.publisher_map(config)})
                    if args.command == "capture-open":
                        expected = article_url(args.url) if args.url else None
                        with desktop_lease():
                            url, clipboard = desktop.capture()
                        if expected and expected != url:
                            raise Refused("The viewer reported a different article URL; nothing was archived.")
                        result = store.ingest(url, clipboard, args.publisher, zone)
                        metrics["new_articles"] = int(result[1])
                        show(result)
                    else:
                        if args.activate and args.skip_busy:
                            raise Refused("--activate is for manual runs; use --skip-busy alone in a timer.")
                        idle = max(60, min(int(config.get("minimum_desktop_idle_seconds", 120)), 3600)) if args.skip_busy else 0
                        with desktop_lease():
                            desktop.check(store, zone, args.activate, minimum_idle=idle, metrics=metrics)
                        if not args.skip_busy and config.get("discord_webhook_file"):
                            print(json.dumps({"delivered": store.notify(config["discord_webhook_file"])}))
    except DesktopBusy as error:
        if args.command == "process-submissions" or (args.command == "check" and args.skip_busy):
            print(f"Skipped: {error}")
        else:
            parser.exit(1, f"Collector: {error}\n")
    except (OSError, ValueError, Refused, sqlite3.Error) as error:
        parser.exit(1, f"Collector: {error}\n")


if __name__ == "__main__":
    sys.modules.setdefault("collector", sys.modules[__name__])
    main()
