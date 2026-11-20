import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree.ElementTree import fromstring

from PIL import Image

from collector import Refused, Store, article_url, image_url, native_links, render_rss
from desktop import card_bands, publisher_match
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

    def test_holds_verification_instead_of_retrying(self):
        with patch("collector.fetch", side_effect=Refused("Verification")) as fetch:
            for _ in range(2):
                with self.assertRaises(Refused):
                    self.store.request_article(URL)
        fetch.assert_called_once()

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
