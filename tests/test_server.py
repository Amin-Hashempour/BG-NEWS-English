import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server


PAGE = b'''<!doctype html><html><head>
<link rel="next" href="/news/page/2/">
<script type="application/ld+json">{"@type":"NewsArticle","headline":"Sony reveals PS5 update","url":"https://example.org/story-one","image":"/one.jpg","datePublished":"2026-10-24T08:00:00Z"}</script>
</head><body><article><a href="/story-two"><img data-src="/two.jpg"><h2>Second gaming story appears today</h2></a><time datetime="2026-10-23T10:00:00Z"></time></article></body></html>'''

RSS = b'''<rss xmlns:media="http://search.yahoo.com/mrss/"><channel><item>
<title>Exact Publisher Title &amp; Punctuation</title><link>https://example.org/news/exact</link>
<pubDate>Fri, 23 Oct 2026 15:26:11 +0000</pubDate>
<media:content url="https://cdn.example.org/image.webp" type="image/webp" />
</item></channel></rss>'''


class ParserTests(unittest.TestCase):
    def test_structured_data_cards_and_next_page(self):
        items, next_url = server.parse_news_page(PAGE, "https://example.org/news/")
        self.assertEqual({item["url"] for item in items}, {"https://example.org/story-one", "https://example.org/story-two"})
        self.assertEqual(next_url, "https://example.org/news/page/2")
        structured = next(item for item in items if item["url"].endswith("story-one"))
        self.assertEqual(structured["image"], "https://example.org/one.jpg")

    def test_external_and_unsafe_article_links_are_rejected(self):
        html = b'<article><h2><a href="javascript:alert(1)">Unsafe title here</a></h2></article><article><h2><a href="https://other.test/a">External title here</a></h2></article>'
        self.assertEqual(server.parse_news_page(html, "https://example.org/news")[0], [])

    def test_source_page_patterns(self):
        self.assertEqual(server.fallback_page_url(server.SOURCES["GI"], 2), "https://gameinformer.com/news?page=1")
        self.assertEqual(server.fallback_page_url(server.SOURCES["GE"], 3), "https://www.gematsu.com/browse/page/3/")

    def test_feed_preserves_exact_title_date_and_image(self):
        item = server.parse_feed(RSS, "https://example.org/news")[0]
        self.assertEqual(item["title"], "Exact Publisher Title & Punctuation")
        self.assertEqual(item["published"], "Fri, 23 Oct 2026 15:26:11 +0000")
        self.assertEqual(item["image"], "https://cdn.example.org/image.webp")

    def test_image_signature_fallback(self):
        self.assertEqual(server.image_content_type(b"RIFF1234WEBP", "application/octet-stream"), "image/webp")

    def test_html_mislabeled_as_an_image_is_rejected(self):
        self.assertEqual(server.image_content_type(b"<html>Blocked</html>", "image/jpeg"), "")

    def test_vgc_lazy_srcset_and_missing_image(self):
        html = b'''<article><a href="/news/a-story"><img data-srcset="/files/a-320.jpg 320w, /files/a-480.jpg 480w, /files/a.jpg 1920w"><h2>The exact story title here</h2></a></article>'''
        items, _ = server.parse_news_page(html, "https://www.videogameschronicle.com/category/news/")
        self.assertEqual(items[0]["image"], "https://www.videogameschronicle.com/files/a-480.jpg")
        self.assertEqual(server.safe_url("", "https://www.videogameschronicle.com/category/news/"), "")

    def test_image_url_is_self_contained_without_a_saved_mapping(self):
        item = {"id": "story", "url": "https://example.org/story", "title": "Story",
                "image": "https://cdn.example.org/image.webp", "published": ""}
        server.add_image_proxies([item])
        self.assertEqual(item["originalImage"], "https://cdn.example.org/image.webp")
        query = server.parse_qs(server.urlsplit(item["image"]).query)
        self.assertEqual(query["article"], [item["url"]])
        self.assertEqual(query["image"], [item["originalImage"]])

    @patch("server.request_bytes")
    def test_wrong_legacy_image_is_recovered_and_cached_across_restarts(self, request):
        article = "https://www.gematsu.com/2026/10/story"
        wrong_image = "https://www.gematsu.com/browse"
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aSncAAAAASUVORK5CYII=")
        request.side_effect = [
            (b'<meta property="og:image" content="https://www.gematsu.com/wp-content/uploads/story.jpg">', "text/html"),
            (png, "application/octet-stream"),
        ]
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "IMAGE_FILES", Path(directory)):
            data, kind = server.get_story_image(article, wrong_image)
            self.assertEqual(data, png)
            self.assertEqual(kind, "image/png")
            self.assertEqual(request.call_args_list[0].args[0], article)
            # A new process has no in-memory mapping; only the cached bytes remain.
            with patch.dict(server.IMAGE_URLS, {}, clear=True):
                request.side_effect = AssertionError("Offline: must read the image from disk")
                self.assertEqual(server.get_story_image(article, wrong_image), (png, "image/png"))

    def test_image_endpoint_rejects_unrelated_hosts(self):
        with self.assertRaises(ValueError):
            server.get_story_image("http://127.0.0.1/private", "")
        self.assertEqual(server.allowed_image_url("https://other.test/image.jpg", "https://www.gematsu.com/story"), "")

    @patch("server.request_bytes")
    def test_crawler_follows_pages_until_known_item(self, request):
        first = b'<article><h2><a href="/new">A completely new gaming story</a></h2></article><a rel="next" href="/news?page=2">Next</a>'
        second = b'<article><h2><a href="/known">A previously known gaming story</a></h2></article>'
        request.side_effect = [(first, "text/html"), (second, "text/html")]
        with patch.dict(server.SOURCES, {"X": {"name": "Example", "url": "https://example.org/news", "page": "query"}}, clear=True):
            code, result = server.crawl_source("X", {"https://example.org/known"})
        self.assertEqual(code, "X")
        self.assertTrue(result["complete"])
        self.assertEqual([item["id"] for item in result["items"]], ["https://example.org/new"])
        self.assertEqual(request.call_count, 2)

if __name__ == "__main__":
    unittest.main()
