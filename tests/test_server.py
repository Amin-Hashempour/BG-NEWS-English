import base64
import ssl
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

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
        item = {"id": "story", "url": "https://www.gematsu.com/story", "title": "Story",
                "image": "https://www.gematsu.com/image.webp", "published": ""}
        server.add_image_proxies([item])
        self.assertEqual(item["originalImage"], "https://www.gematsu.com/image.webp")
        query = server.parse_qs(server.urlsplit(item["image"]).query)
        self.assertEqual(query["article"], [item["url"]])
        self.assertEqual(query["image"], [item["originalImage"]])

    @patch("server.request_image_transport")
    def test_push_square_uses_first_party_image_when_cdn_is_forbidden(self, request):
        article = "https://www.pushsquare.com/news/2026/10/story"
        cdn = "https://images.pushsquare.com/6976f1890c71d/small.jpg"
        first_party = "https://www.pushsquare.com/images/6976f1890c71d/small.jpg"
        png = b"\x89PNG\r\n\x1a\nreal-image-data"
        def respond(url, **kwargs):
            if server.urlsplit(url).hostname == "images.pushsquare.com":
                raise server.HTTPError(url, 403, "Forbidden", {}, None)
            self.assertEqual(url, first_party)
            return png, "image/png"
        request.side_effect = respond
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "IMAGE_FILES", Path(directory)):
            self.assertEqual(server.get_story_image(article, cdn), (png, "image/png"))
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args.args[0], first_party)

    @patch("server.request_image_transport")
    def test_push_square_retries_cdn_when_main_domain_is_forbidden(self, request):
        article = "https://www.pushsquare.com/news/2026/10/story"
        first_party = "https://www.pushsquare.com/images/6976f1890c71d/small.jpg"
        cdn = "https://images.pushsquare.com/6976f1890c71d/small.jpg"
        png = b"\x89PNG\r\n\x1a\nreal-image-data"
        def respond(url, **kwargs):
            if url == first_party:
                raise server.HTTPError(url, 403, "Forbidden", {}, None)
            self.assertEqual(url, cdn)
            return png, "image/png"
        request.side_effect = respond
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "IMAGE_FILES", Path(directory)):
            self.assertEqual(server.get_story_image(article, first_party), (png, "image/png"))
        self.assertEqual(request.call_args.args[0], cdn)

    @patch("server.request_standard_image_transport", side_effect=server.ImageFetchError("HTTP 403"))
    @patch("server.request_browser_image", return_value=(b"image", "image/jpeg"))
    @patch("server.browser_requests", new=object())
    def test_push_square_uses_browser_https_without_rewriting_original_url(self, browser, standard):
        url = "https://images.pushsquare.com/6976f1890c71d/small.jpg"
        self.assertEqual(server.request_image_transport(url, accept=server.IMAGE_ACCEPT), (b"image", "image/jpeg"))
        self.assertEqual(browser.call_args.args[0], url)
        standard.assert_not_called()

    @patch("server.BROWSER_SESSIONS", new_callable=server.threading.local)
    @patch("server.browser_requests")
    def test_browser_https_keeps_verification_and_limits_response_size(self, browser_requests, sessions):
        response = MagicMock(status_code=200, headers={"Content-Type": "image/jpeg"})
        response.iter_content.return_value = iter([b"too", b"large"])
        browser_requests.Session.return_value.get.return_value = response
        with self.assertRaisesRegex(server.ImageFetchError, "size limit"):
            server.request_browser_image("https://images.pushsquare.com/a/small.jpg", accept=server.IMAGE_ACCEPT, limit=4)
        browser_requests.Session.assert_called_once_with(impersonate="chrome", verify=True)
        response.close.assert_called_once()

    @patch("server.request_image_transport")
    def test_story_cache_is_reused_if_thumbnail_hint_changes(self, request):
        article = "https://www.gematsu.com/2026/10/story"
        png = b"\x89PNG\r\n\x1a\nreal-image-data"
        request.return_value = (png, "image/png")
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "IMAGE_FILES", Path(directory)):
            self.assertEqual(server.get_story_image(article, "https://www.gematsu.com/small.jpg"), (png, "image/png"))
            request.side_effect = AssertionError("The already downloaded story must not be fetched again")
            self.assertEqual(server.get_story_image(article, "https://www.gematsu.com/large.jpg"), (png, "image/png"))
        self.assertEqual(request.call_count, 1)

    @patch("server.fetch_feed")
    @patch("server.request_bytes")
    def test_listing_thumbnail_keeps_feed_title_and_date(self, request, feed):
        article = "https://example.org/news/story"
        feed.return_value = [{"url": article, "id": article, "title": "Exact publisher title", "published": "2026-10-24T10:00:00Z", "image": "https://example.org/full.jpg"}]
        request.return_value = (b'<article><a href="/news/story"><img data-srcset="/preview-480.jpg 480w, /full.jpg 1920w"><h2>Story card with other metadata</h2></a></article>', "text/html")
        with patch.dict(server.SOURCES, {"X": {"name": "Example", "url": "https://example.org/news", "page": "query"}}, clear=True):
            _, result = server.crawl_source("X", set())
        self.assertEqual(result["items"][0]["title"], "Exact publisher title")
        self.assertEqual(result["items"][0]["published"], "2026-10-24T10:00:00Z")
        self.assertEqual(result["items"][0]["image"], "https://example.org/preview-480.jpg")

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

    @patch("server.subprocess.run")
    @patch("server.shutil.which", return_value="/usr/bin/curl")
    @patch("server.request_bytes", side_effect=ssl.SSLCertVerificationError("certificate verify failed"))
    def test_os_transport_recovers_python_tls_failure_without_disabling_tls(self, request, which, run):
        png = b"\x89PNG\r\n\x1a\nimage-data"
        def curl_result(command, **kwargs):
            self.assertNotIn("--insecure", command)
            self.assertNotIn("-k", command)
            Path(command[command.index("--output") + 1]).write_bytes(png)
            return subprocess.CompletedProcess(command, 0, b"200\nimage/png", b"")
        run.side_effect = curl_result
        data, kind = server.request_image_transport("https://www.gematsu.com/image.png", accept=server.IMAGE_ACCEPT)
        self.assertEqual((data, kind), (png, "image/png"))

    @patch("server.shutil.which", return_value=None)
    @patch("server.request_bytes", side_effect=server.HTTPError("https://www.gematsu.com/image.jpg", 403, "Forbidden", {}, None))
    def test_publisher_denial_retains_exact_status_when_no_os_transport(self, request, which):
        with self.assertRaisesRegex(server.ImageFetchError, "HTTP 403 from www.gematsu.com"):
            server.request_image_transport("https://www.gematsu.com/image.jpg", accept=server.IMAGE_ACCEPT)

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
