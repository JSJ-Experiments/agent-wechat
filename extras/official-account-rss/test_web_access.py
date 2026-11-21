from contextlib import contextmanager
from http.server import HTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from xml.etree.ElementTree import fromstring

from collector import Refused, Store, render_discovered_rss, serve
from test_collector import URL
from web_access import WebAccess, password_key

ORIGIN = "https://archive.example.test"
PREFIX = "/wx/oa/"
TOKEN = "0" * 64


class WebAccessTests(unittest.TestCase):
    def setUp(self):
        path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(path)
        verifier = path / "verifier"
        salt = bytes(range(16))
        verifier.write_text(salt.hex() + ":" + password_key("test password", salt).hex())
        self.config = {"origin": ORIGIN, "password_hash_file": str(verifier)}
        self.auth = WebAccess(self.config, TOKEN)

    def test_signature_cannot_be_forged_or_replayed_past_expiry(self):
        cookie = self.auth.cookie(self.auth.issue(now=10), 43200)
        self.assertTrue(self.auth.valid(cookie, now=11))
        self.assertFalse(self.auth.valid(cookie, now=50000))
        self.assertFalse(self.auth.valid(cookie.replace("43210.", "43211."), now=11))
        self.assertFalse(self.auth.valid("wxoa_session=" + self.auth.reader))
        self.assertNotEqual(self.auth.reader, TOKEN)

    def test_rate_limit(self):
        for _ in range(8):
            self.assertEqual(self.auth.auth("not it"), 401)
        self.assertEqual(self.auth.auth("test password"), 429)

    def test_follows_refresh_and_manual_names_take_precedence(self):
        self.store.replace_follows([{"id": "oa-a", "publisher": "Manual"}, {"id": "oa-b", "publisher": "Follow"}])
        config = {"track_follows": True, "publishers": {"manual": "Manual"}}
        self.assertEqual(self.store.publisher_map(config), {"manual": "Manual", "oa-b": "Follow"})
        self.store.replace_follows([])
        self.assertEqual(self.store.publisher_map(config), {"manual": "Manual"})

    def test_submission_precedes_discovered_and_does_not_allow_a_different_host(self):
        self.store.discover([{"publisher": "News", "title": "A headline", "url": URL}], {"News"})
        with self.assertRaises(Refused):
            self.store.submit_url("https://internal.invalid/s/private")
        other = "https://mp.weixin.qq.com/s/submitted"
        self.assertEqual(self.store.submit_url(other+"?tracking=x"), "queued")
        self.assertEqual(self.store.submit_url(other), "queued")
        self.assertEqual([row["url"] for row in self.store.candidates(2, submissions_only=True)], [other])
        self.assertEqual([row["url"] for row in self.store.candidates(2)], [other, URL])
        self.assertEqual([row["url"] for row in self.store.candidates(2, publishers={"Another"})], [other])
        root = fromstring(render_discovered_rss(self.store, None, ORIGIN + "/test", "all"))
        self.assertEqual([row.findtext("title") for row in root.findall(".//item")], ["A headline"])

    @contextmanager
    def _server(self):
        started = threading.Event()
        config = {"web": self.config, "port": 0, "publishers": {}}
        shared = []

        class Local(HTTPServer):
            def serve_forever(self):
                shared.append(self)
                started.set()
                super().serve_forever(poll_interval=0.02)

        with patch("collector.HTTPServer", Local):
            thread = threading.Thread(target=serve, args=(config, self.store, TOKEN, ORIGIN), daemon=True)
            thread.start()
            if not started.wait(3):
                self.fail("Test server did not start.")
            try:
                yield f"http://127.0.0.1:{shared[0].server_port}"
            finally:
                shared[0].shutdown()
                thread.join(3)

    def test_browser_form_feed_scope_and_csrf(self):
        # No redirects: inspect 303 cookie and location directly.
        from urllib.request import HTTPRedirectHandler

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *_):
                return None

        client = build_opener(ProxyHandler({}), NoRedirect)
        with self._server() as base:
            def make(path, *, fields=None, cookie="", origin=ORIGIN):
                headers = {"Origin": origin, "Cookie": cookie}
                return client.open(Request(base + PREFIX + path, None if fields is None else urlencode(fields).encode(),
                                           headers=headers), timeout=3)

            def rejects(path, code, **kwargs):
                with self.assertRaises(HTTPError) as held:
                    make(path, **kwargs)
                self.assertEqual(held.exception.code, code)
                return held.exception.headers

            with make("") as reply:
                self.assertIn(b'autocomplete="current-password"', reply.read())
            rejects("article/1", 404)
            rejects("login", 403, fields={"username": "owner", "password": "test password"}, origin="https://else.invalid")
            headers = rejects("login", 303, fields={"username": "owner", "password": "test password"})
            cookie = headers["Set-Cookie"].split(";")[0]
            self.assertIn("Secure", headers["Set-Cookie"])
            self.assertIn("HttpOnly", headers["Set-Cookie"])
            with make("", cookie=cookie) as reply:
                self.assertIn(b"Queue article", reply.read())
            with make(f"reader/{self.auth.reader}/discovered/all.xml") as reply:
                self.assertEqual(fromstring(reply.read()).tag, "rss")
            rejects(f"reader/{self.auth.reader}/dashboard", 404)
            rejects("submit", 403, fields={"url": URL})
            self.assertIn("submission=queued", rejects("submit", 303, fields={"url": URL}, cookie=cookie)["Location"])
