import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from collector import DesktopBusy, Store, observe
from dashboard import overview, render_article, render_dashboard
from test_collector import HTML, URL


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.config = {"publishers": {"test": "Test & News", "empty": "Unused"}}

    def test_distinguishes_delivered_links_saved_bodies_and_held_urls(self):
        alias = "https://mp.weixin.qq.com/s/anotherLinkForSameBody"
        pending = "https://mp.weixin.qq.com/s/waitingForCapture"
        stamp = 1795176120
        card = {"publisher": "Test & News", "title": "Synthetic article", "url": alias, "published": stamp}
        self.store.discover([card, card | {"title": "Uncaptured <story>", "url": pending}], {"Test & News"})
        with patch("collector.fetch", side_effect=OSError("offline")):
            self.store.ingest(URL, HTML, now=stamp)
            self.store.ingest("https://mp.weixin.qq.com/s/unlistedArchive",
                              HTML.replace("Synthetic article", "<b>Imported</b> article"), now=stamp)
        with self.store.open() as db:
            db.execute("INSERT INTO attempts VALUES (?,?,?)", (pending, "verification-or-layout", int(time.time())))
            db.execute("INSERT INTO attempts VALUES (?,?,?)",
                       ("https://mp.weixin.qq.com/s/suppliedURL", "temporarily-unavailable", int(time.time())))
        stats = overview(self.store, self.config)
        t = stats["totals"]
        self.assertEqual((t["discovered"], t["matched"], t["saved"], t["needs_viewer"]), (2, 1, 2, 1))
        self.assertEqual((t["temporarily_unavailable"], t["queued_image_articles"]), (1, 2))
        self.assertIsNotNone(t["public_fetch_paused_until"])
        self.assertEqual(stats["publishers"][1]["discovered"], 0)
        held = render_dashboard(self.store, self.config, {"view": ["pending"], "publisher": ["test"]})
        self.assertIn(b"Uncaptured &lt;story&gt;", held)
        self.assertNotIn(b"Supplied article URL", held)
        self.assertNotIn(b"/s/anotherLinkForSameBody", held)
        saved = render_dashboard(self.store, self.config, {"view": ["saved"]})
        self.assertIn(b'href="article/1"', saved)
        article = render_article(self.store, 1)
        self.assertIn(b"test words", article)
        self.assertNotIn(b"mmbiz", article)
        self.assertNotIn(b"<script>", article)
        self.assertIsNone(render_article(self.store, 100))

    def test_pagination_clamps_and_filters(self):
        cards = [{"publisher": "Test & News", "title": f"Story {i:02}",
                  "url": f"https://mp.weixin.qq.com/s/synthetic{i:02}", "published": i + 1}
                 for i in range(22)]
        self.store.discover(cards, {"Test & News"})
        second = render_dashboard(self.store, self.config, {"view": ["pending"], "page": ["999"]})
        self.assertIn(b"Page 2 of 2", second)
        self.assertIn(b"Story 00", second)
        self.assertNotIn(b"Story 21", second)
        self.assertIn(b"Previous", second)

    def test_activity_distinguishes_skips_and_errors_with_partial_metrics(self):
        metrics = {}
        with self.assertRaises(DesktopBusy), observe(self.store, "desktop check", metrics):
            metrics["pages_scanned"] = 0
            raise DesktopBusy("Official Accounts is not the active main pane.")
        with self.assertRaises(OSError), observe(self.store, "discover (legacy)", {}):
            raise OSError("secret in an underlying error")
        runs = overview(self.store, self.config)["recent_runs"]
        self.assertEqual([r["outcome"] for r in runs], ["error", "skipped"])
        self.assertEqual(runs[1]["metrics"], {"pages_scanned": 0})
        self.assertNotIn("secret", runs[0]["note"])
