import io
import json
from datetime import UTC
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
from xml.etree.ElementTree import fromstring

from PIL import Image, ImageDraw

from collector import DesktopBusy, Refused, Store, article_url, image_url, native_links, process_desktop_submissions, render_rss
from desktop import Desktop, bubble_point, card_bands, filehelper_articles, message_point, message_row, publisher_match
from read_legacy_cards import articles

URL = "https://mp.weixin.qq.com/s/fixtureTest"
ASSET = "https://mmbiz.qpic.cn/mmbiz_jpg/fixture/0?wx_fmt=jpeg"
HTML = '''<h1 id="activity-name">Synthetic article</h1><a id="js_name">Test &amp; News</a>
<em id="publish_time">Nov 20, 2026, 12:02 PM</em><div id="js_content">
<p>Real <strong>test words for the generated RSS item.</strong></p>
<img src="data:image/svg+xml,bad" data-src="https://mmbiz.qpic.cn/mmbiz_jpg/fixture/0?wx_fmt=jpeg">
<img data-src="https://mmbiz.qpic.cn/mmbiz_jpg/fixture/0?wx_fmt=jpeg">
<img src="https://untrusted.example/private.png" onerror="bad()">
<a href="javascript:bad()">unsafe link text</a><iframe src="https://untrusted.example/">outer</iframe>
<script>bad()</script></div>'''


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.tmp)
        self.image = io.BytesIO()
        Image.new("RGB", (5, 4)).save(self.image, "JPEG")

    def test_archive_full_content_with_cached_dedup_and_rss(self):
        with patch("collector.fetch", return_value=self.image.getvalue()) as fetch:
            row, new = self.store.ingest(URL + "?ignored=1", HTML, "Test & News")
        self.assertTrue(new)
        self.assertEqual((row["image_count"], row["missing_images"]), (1, 1))
        self.assertEqual(len(list((self.tmp / "images").iterdir())), 1)
        self.assertEqual(fetch.call_args.args[0], ASSET)
        with patch("collector.fetch") as fetch2:
            _, repeat = self.store.ingest(URL, HTML, "Test & News")
        self.assertFalse(repeat)
        fetch2.assert_not_called()
        data = fromstring(render_rss(self.store, "Test & News", "http://localhost:16176/key", "test"))
        item, = data.findall(".//item")
        content = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
        self.assertIn("<strong>test words", content)
        self.assertEqual(content.count("http://localhost:16176/key/image/"), 2)
        self.assertNotIn("javascript:", content)
        self.assertNotIn("untrusted.example", content)
        self.assertNotIn("bad()", content)
        self.assertEqual(item.findtext("pubDate"), "Fri, 20 Nov 2026 12:02:00 GMT")

    def test_invalid_publisher_does_not_download(self):
        with patch("collector.fetch") as fetch, self.assertRaises(Refused):
            self.store.ingest(URL, HTML, "Not the publisher")
        fetch.assert_not_called()

    def test_archive_then_retry_images_without_fetching_article(self):
        with patch("collector.fetch", side_effect=URLError("offline")) as fetch:
            row, new = self.store.ingest(URL, HTML, "Test & News")
            fetch.assert_called_once()
        self.assertTrue(new)
        self.assertEqual((row["image_count"], row["missing_images"]), (0, 3))
        before = render_rss(self.store, None, "http://localhost:16176/key", "all")
        self.assertNotIn(b"mmbiz", before)
        self.assertNotIn(b"data-wx-pending", before)
        self.assertIn(b"test words", before)

        # Restart the collector. A temporary failure is remembered, and no
        # request to the article page (or chance of another challenge) is needed.
        store = Store(self.tmp)
        with patch("collector.fetch", return_value=self.image.getvalue()) as fetch:
            self.assertEqual(store.repair_images(1, now=row["found"] + 3600), 0)
            fetch.assert_not_called()
            self.assertEqual(store.repair_images(1, now=row["found"] + 13 * 3600), 2)
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args[0], ASSET)
            self.assertEqual(store.repair_images(1, now=row["found"] + 24 * 3600), 0)
        fixed, = store.latest()
        self.assertEqual((fixed["image_count"], fixed["missing_images"]), (1, 1))
        self.assertEqual(fixed["found"], row["found"])
        after = render_rss(store, None, "http://localhost:16176/key", "all")
        self.assertEqual(after.count(b"/key/image/"), 2)

    def test_archive_commits_before_a_download_can_be_interrupted(self):
        with patch("collector.fetch", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.store.ingest(URL, HTML, "Test & News")
        (row,) = self.store.latest()
        self.assertEqual(row["missing_images"], 3)
        with patch("collector.fetch", return_value=self.image.getvalue()):
            self.assertEqual(self.store.repair_images(1), 2)

    def test_holds_verification_instead_of_retrying(self):
        with patch("collector.fetch", side_effect=Refused("Verification")) as fetch:
            for _ in range(2):
                with self.assertRaises(Refused):
                    self.store.request_article(URL)
        fetch.assert_called_once()

    def test_submitted_urls_wait_for_a_matching_received_link(self):
        self.store.submit_url(URL)
        metrics = {}
        with patch("desktop.Desktop.filehelper_links", return_value={}), patch("desktop.Desktop.capture_received") as capture:
            process_desktop_submissions(self.store, {}, UTC, metrics)
            capture.assert_not_called()
        self.assertEqual(metrics["matched_submissions"], 0)
        self.assertEqual(self.store.pending_desktop_submissions(), {URL})

    def test_desktop_capture_is_independent_of_a_public_verification_hold(self):
        self.store.submit_url(URL)
        with patch("collector.fetch", side_effect=Refused("verification")), self.assertRaises(Refused):
            self.store.request_article(URL)
        with patch("desktop.Desktop.filehelper_links", return_value={URL: ("text", URL)}), \
                patch("desktop.Desktop.capture_received", return_value=(URL, HTML)) as capture, \
                patch("collector.fetch", return_value=self.image.getvalue()) as fetch, patch("collector.show"):
            metrics = {}
            process_desktop_submissions(self.store, {}, UTC, metrics)
            self.assertEqual(capture.call_args.args[:4], (URL, "text", URL, 120))
            self.assertEqual([c.args[0] for c in fetch.call_args_list], [ASSET])
        self.assertEqual(metrics["new_articles"], 1)
        self.assertFalse(self.store.pending_desktop_submissions())

    def test_viewer_refusal_pauses_that_submission_until_user_requeues_it(self):
        self.store.submit_url(URL)
        with patch("desktop.Desktop.filehelper_links", return_value={URL: ("card", "Fixture headline")}), \
                patch("desktop.Desktop.capture_received", side_effect=Refused("no article body")), patch("builtins.print"):
            process_desktop_submissions(self.store, {}, UTC, {})
        self.assertFalse(self.store.pending_desktop_submissions())
        self.assertFalse(self.store.latest())
        self.assertEqual(self.store.submit_url(URL), "queued")
        self.assertEqual(self.store.pending_desktop_submissions(), {URL})

    def test_exact_filehelper_links_and_card_title_lookup(self):
        card = URL + "Card"
        msgs = [
            {"chatId": "other", "type": 1, "content": card},
            {"chatId": "filehelper", "type": 1, "content": "See " + card},
            {"chatId": "filehelper", "type": 1, "content": URL + "?from=wechat"},
            {"chatId": "filehelper", "type": 49, "appMsgType": 5,
             "rawContent": f"<msg><appmsg><title>Fixture &amp; News</title><url>{card}</url></appmsg></msg>"},
        ]
        self.assertEqual(filehelper_articles(msgs), {URL: ("text", URL + "?from=wechat"),
                                                    card: ("card", "Fixture & News")})
        node = {"role": "label", "name": "Fixture & News", "bounds": {"x": 20, "y": 20, "width": 80, "height": 30}}
        rect = (0, 0, 130, 130)
        self.assertEqual(message_point([node], rect, "card", "Fixture & News"), (60, 35))
        self.assertIsNone(message_point([node], (30, 0, 130, 130), "card", "Fixture & News"))
        self.assertIsNone(message_point([node, node | {"bounds": node["bounds"] | {"y": 60}}], rect, "card", "Fixture & News"))

    def test_filehelper_row_fallback_requires_both_exact_text_and_an_outgoing_bubble(self):
        bounds = {"x": 5, "y": 20, "width": 704, "height": 54}
        node = {"role": "list-item", "name": URL + "\n ", "bounds": bounds}
        row = (5, 20, 704, 54)
        rect = (0, 3, 710, 120)
        self.assertEqual(message_row([node], rect, "text", URL), row)
        self.assertIsNone(message_row([node], rect, "text", URL + "other"))
        dupe = node | {"bounds": bounds | {"y": 60}}
        self.assertIsNone(message_row([node, dupe], rect, "text", URL))
        screen = Image.new("RGB", (710, 130), (237, 237, 237))
        self.assertIsNone(bubble_point(screen, row, "text"))
        ImageDraw.Draw(screen).rectangle((435, 28, 637, 64), fill=(149, 236, 100))
        self.assertEqual(bubble_point(screen, row, "text"), (461, 44))
        self.assertIsNone(bubble_point(screen, row, "card"))

    def test_reject_nonwechat_and_active_image_urls(self):
        for raw in ("https://example.org/s/article", "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha",
                    "https://mp.weixin.qq.com@127.0.0.1/s/article"):
            with self.subTest(raw=raw), self.assertRaises(Refused):
                article_url(raw)
        self.assertIsNone(image_url("data:image/svg+xml,whatever"))
        self.assertIsNone(image_url("https://mmbiz.qpic.cn.untrusted.example/mmbiz_jpg/example"))
        self.assertEqual(image_url(ASSET), ASSET)

    def test_native_import_only_reads_explicit_unrecalled_wechat_cards(self):
        payload = {"items": [
            {"body": URL},
            {"link": {"url": URL + "?from=native"}},
            {"link": {"url": URL}},
            {"link": {"url": "https://mp.weixin.qq.com/s/anotherValid"}, "recall": {}},
        ]}
        self.assertEqual(list(native_links(payload)), [URL])

    def test_publisher_recognition_and_card_bands(self):
        self.assertEqual(publisher_match("北京 理工 大学", ("北京理工大学",)), "北京理工大学")
        self.assertIsNone(publisher_match("别的学校", ("北京理工大学",)))
        pane = Image.new("RGB", (704, 600), (237, 237, 237))
        pane.paste("#ffffff", (60, 65, 646, 180))
        self.assertEqual(list(card_bands(pane)), [(65, 180)])

    def test_scheduled_desktop_guard_fails_closed(self):
        desktop = Desktop({})
        for result in (b"not available", b"119999", Refused("X server unavailable")):
            if isinstance(result, Exception):
                guard = patch.object(desktop, "docker", side_effect=result)
            else:
                guard = patch.object(desktop, "docker", return_value=result)
            with guard, self.assertRaises(DesktopBusy):
                desktop.require_idle(120)
        with patch.object(desktop, "docker", return_value=b"120001"):
            desktop.require_idle(120)

    def test_one_official_delivery_can_include_multiple_article_links(self):
        payload = f"""<msg><appmsg><type>5</type><url>{URL}</url><title>Headline</title>
        <mmreader><category><item><url>https://mp.weixin.qq.com/s/fixtureNext</url>
        <title>Second article</title></item></category></mmreader></appmsg></msg>"""
        self.assertEqual(list(articles(payload)), [(URL, "Headline"),
                                                    ("https://mp.weixin.qq.com/s/fixtureNext", "Second article")])

    def test_discovery_dedupes_and_does_not_publish_unverified_html(self):
        card = {"publisher": "Test & News", "title": "Link card", "url": URL, "published": 1795176120}
        self.assertEqual(self.store.discover([card, card], {"Test & News"}), 1)
        self.assertEqual(len(self.store.candidates(1)), 1)
        self.assertEqual(self.store.latest(), [])
        with patch("collector.fetch", side_effect=Refused("Verification")), self.assertRaises(Refused):
            self.store.request_article(URL)
        other = card | {"url": "https://mp.weixin.qq.com/s/anotherHeadline"}
        self.assertEqual(self.store.discover([other], {"Test & News"}), 1)
        self.assertEqual(self.store.candidates(1), [])


if __name__ == "__main__":
    unittest.main()
