import unittest
from unittest.mock import MagicMock, patch

import server


PAGE = b'''<!doctype html><html><head>
<link rel="next" href="/news/page/2/">
<script type="application/ld+json">{"@type":"NewsArticle","headline":"Sony reveals PS5 update","url":"https://example.org/story-one","image":"/one.jpg","datePublished":"2026-10-24T08:00:00Z"}</script>
</head><body><article><a href="/story-two"><img data-src="/two.jpg"><h2>Second gaming story appears today</h2></a><time datetime="2026-10-23T10:00:00Z"></time></article></body></html>'''


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
