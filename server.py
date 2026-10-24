#!/usr/bin/env python3
"""Persian gaming-news reader with source-aware pagination and no dependencies."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
MAX_PAGES = 50
SOURCES = {
    "GS": {"name": "GameSpot", "url": "https://www.gamespot.com/category/news/", "page": "query",
           "feed": "https://www.gamespot.com/feeds/news/"},
    "GI": {"name": "Game Informer", "url": "https://gameinformer.com/news", "page": "zero_query",
           "feed": "https://gameinformer.com/rss.xml"},
    "PS": {"name": "Push Square", "url": "https://www.pushsquare.com/news", "page": "query",
           "feed": "https://www.pushsquare.com/feeds/news"},
    "GE": {"name": "Gematsu", "url": "https://www.gematsu.com/browse", "page": "path",
           "feed": "https://www.gematsu.com/feed"},
    "VGC": {"name": "VGC", "url": "https://www.videogameschronicle.com/category/news/", "page": "path",
            "feed": "https://www.videogameschronicle.com/category/news/feed/"},
}
SOURCE_HOSTS = {urlsplit(source["url"]).hostname for source in SOURCES.values()}
IMAGE_URLS: dict[str, tuple[str, str]] = {}
IMAGE_LOCK = threading.Lock()


def safe_url(value: str, base: str, *, article: bool = False) -> str:
    url = urljoin(base, unescape(value or "").strip())
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    if article and parts.hostname.lower().removeprefix("www.") != urlsplit(base).hostname.lower().removeprefix("www."):
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") or "/", parts.query, ""))


def request_bytes(url: str, *, accept: str, referer: str = "", limit: int = 8 * 1024 * 1024,
                  timeout: int = 20) -> tuple[bytes, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    with urlopen(Request(url, headers=headers), timeout=timeout) as response:
        data = response.read(limit + 1)
        content_type = response.headers.get_content_type()
    if len(data) > limit:
        raise ValueError("Response too large")
    return data, content_type


def image_content_type(data: bytes, reported: str) -> str:
    if reported.startswith("image/"):
        return reported
    signatures = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"),
                  (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"), (b"RIFF", "image/webp"))
    for signature, content_type in signatures:
        if data.startswith(signature):
            return content_type
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return ""


class NewsHTMLParser(HTMLParser):
    """Extract heading links, article metadata, JSON-LD and pagination controls."""
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.items: list[dict] = []
        self.next_url = ""
        self._anchor: dict | None = None
        self._heading = 0
        self._article = 0
        self._article_image = ""
        self._article_date = ""
        self._json_script = False
        self._json_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").lower()
        if tag == "article":
            self._article += 1
            if self._article == 1:
                self._article_image = ""
                self._article_date = ""
        if tag in ("h1", "h2", "h3", "h4"):
            self._heading += 1
        if tag == "a":
            href = safe_url(attrs.get("href", ""), self.base, article=True)
            self._anchor = {"url": href, "text": [], "heading": self._heading > 0,
                            "article": self._article > 0, "class": classes,
                            "image": self._article_image, "published": self._article_date}
            rel = attrs.get("rel", "").lower().split()
            if href and ("next" in rel or "load-more" in classes or "load_more" in classes):
                self.next_url = href
        elif tag in ("img", "source") and self._article:
            image = (attrs.get("data-src") or attrs.get("data-lazy-src") or attrs.get("data-original")
                     or attrs.get("src") or "")
            if not image and attrs.get("srcset"):
                image = attrs["srcset"].split(",")[-1].strip().split()[0]
            self._article_image = self._article_image or safe_url(image, self.base)
        elif tag == "time" and self._article:
            self._article_date = attrs.get("datetime", "") or self._article_date
        elif tag == "link" and "next" in attrs.get("rel", "").lower().split():
            self.next_url = safe_url(attrs.get("href", ""), self.base, article=True)
        elif tag == "script" and "ld+json" in attrs.get("type", "").lower():
            self._json_script = True
            self._json_parts = []

    def handle_data(self, data):
        if self._json_script:
            self._json_parts.append(data)
        if self._anchor is not None:
            self._anchor["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            anchor = self._anchor
            self._anchor = None
            title = re.sub(r"\s+", " ", " ".join(anchor["text"])).strip()
            if anchor["url"] and (anchor["heading"] or anchor["article"]) and 8 <= len(title) <= 300:
                self.items.append({"url": anchor["url"], "id": anchor["url"], "title": title,
                                   "image": anchor["image"] or self._article_image,
                                   "published": anchor["published"] or self._article_date})
            if anchor["url"] and re.search(r"\b(next|older|more)\b", title, re.I):
                self.next_url = anchor["url"]
        elif tag in ("h1", "h2", "h3", "h4"):
            self._heading = max(0, self._heading - 1)
        elif tag == "article":
            self._article = max(0, self._article - 1)
        elif tag == "script" and self._json_script:
            self._json_script = False
            try:
                self._add_json(json.loads("".join(self._json_parts)))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    def _add_json(self, value):
        if isinstance(value, list):
            for entry in value:
                self._add_json(entry)
            return
        if not isinstance(value, dict):
            return
        if "@graph" in value:
            self._add_json(value["@graph"])
        kind = value.get("@type", "")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(item in ("NewsArticle", "Article", "BlogPosting") for item in kinds):
            title = unescape(str(value.get("headline") or value.get("name") or "")).strip()
            raw_url = value.get("url") or value.get("mainEntityOfPage") or ""
            if isinstance(raw_url, dict):
                raw_url = raw_url.get("@id", "")
            image = value.get("image", "")
            if isinstance(image, list):
                image = image[0] if image else ""
            if isinstance(image, dict):
                image = image.get("url", "")
            url = safe_url(str(raw_url), self.base, article=True)
            if title and url:
                self.items.append({"url": url, "id": url, "title": title,
                                   "image": safe_url(str(image), self.base),
                                   "published": str(value.get("datePublished") or "")})


def parse_news_page(data: bytes, base: str) -> tuple[list[dict], str]:
    parser = NewsHTMLParser(base)
    parser.feed(data.decode("utf-8", errors="replace"))
    unique = {}
    listing = safe_url(base, base)
    for item in parser.items:
        if item["url"] == listing or item["url"] in unique:
            continue
        unique[item["url"]] = item
    return list(unique.values()), parser.next_url


def parse_feed(data: bytes, base: str) -> list[dict]:
    """Read RSS/Atom while preserving the publisher's exact title text."""
    root = ET.fromstring(data)
    local = lambda tag: tag.rsplit("}", 1)[-1]
    entries = [node for node in root.iter() if local(node.tag) in ("item", "entry")]
    result = []
    seen = set()
    for entry in entries:
        title = link = published = image = ""
        for node in entry.iter():
            tag = local(node.tag)
            if tag == "title" and not title:
                title = unescape("".join(node.itertext())).strip()
            elif tag == "link" and not link and node.get("rel", "alternate") == "alternate":
                link = node.get("href") or node.text or ""
            elif tag in ("pubDate", "published", "updated") and not published:
                published = (node.text or "").strip()
            elif tag in ("thumbnail", "content", "enclosure"):
                candidate = node.get("url") or node.get("href") or node.get("src") or ""
                if candidate and (tag != "enclosure" or node.get("type", "").startswith("image/")):
                    image = image or candidate
            if tag in ("description", "encoded", "content") and node.text and not image:
                # The small image parser below avoids treating article links as images.
                match = re.search(r"<img[^>]+(?:src|data-src)=[\"']([^\"']+)", node.text, re.I)
                if match:
                    image = match.group(1)
        link = safe_url(link, base, article=True)
        if title and link and link not in seen:
            seen.add(link)
            result.append({"url": link, "id": link, "title": title,
                           "image": safe_url(image, base) if image else "", "published": published})
    if not result:
        raise ValueError("No readable feed entries")
    return result


def fetch_feed(source: dict) -> list[dict]:
    feed = source.get("feed")
    if not feed:
        return []
    data, content_type = request_bytes(feed, accept="application/rss+xml,application/atom+xml,application/xml,text/xml,*/*;q=0.5")
    if content_type not in ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml", "text/plain"):
        raise ValueError("Unexpected feed response")
    return parse_feed(data, source["url"])


def fallback_page_url(source: dict, page: int) -> str:
    base = source["url"]
    if source["page"] == "path":
        return urljoin(base.rstrip("/") + "/", f"page/{page}/")
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query))
    query["page"] = str(page - 1 if source["page"] == "zero_query" else page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def crawl_source(code: str, known_ids: set[str]) -> tuple[str, dict]:
    source = SOURCES[code]
    try:
        feed_items = fetch_feed(source)
    except Exception:
        feed_items = []
    feed_by_id = {item["id"]: item for item in feed_items}
    current = source["url"]
    visited_pages = set()
    collected = []
    collected_ids = set()
    reached_known = False
    for page in range(1, MAX_PAGES + 1):
        if current in visited_pages:
            break
        visited_pages.add(current)
        try:
            data, content_type = request_bytes(current, accept="text/html,application/xhtml+xml")
            if content_type not in ("text/html", "application/xhtml+xml"):
                raise ValueError("Unexpected response")
            items, next_url = parse_news_page(data, current)
        except Exception as exc:
            if page == 1:
                if feed_items:
                    return code, {"items": feed_items, "error": None, "complete": True}
                return code, {"items": [], "error": f"Could not fetch {source['name']} news ({type(exc).__name__}).",
                              "complete": False}
            return code, {"items": collected, "error": f"Page {page} of {source['name']} failed; its checkpoint was not advanced.",
                          "complete": False}
        if not items:
            if page == 1:
                if feed_items:
                    return code, {"items": feed_items, "error": None, "complete": True}
                return code, {"items": [], "error": f"Could not read the {source['name']} news page.", "complete": False}
            break
        page_ids = {item["id"] for item in items}
        reached_known = bool(page_ids & known_ids)
        for item in items:
            if item["id"] in known_ids:
                continue
            if item["id"] not in collected_ids:
                collected_ids.add(item["id"])
                collected.append(feed_by_id.get(item["id"], item))
        if reached_known or not known_ids:
            break
        candidate = next_url or fallback_page_url(source, page + 1)
        current = safe_url(candidate, source["url"], article=True)
        if not current:
            break
    complete = reached_known or not known_ids
    # RSS is also used to add posts omitted by a dynamically rendered listing.
    existing = {item["id"] for item in collected}
    collected[:0] = [item for item in feed_items if item["id"] not in existing]
    error = None if complete else f"The crawler did not reach the last saved {source['name']} item after {MAX_PAGES} pages."
    return code, {"items": collected, "error": error, "complete": complete}


def add_image_proxies(items: list[dict]) -> list[dict]:
    for item in items:
        if item.get("image"):
            token = hashlib.sha256(item["image"].encode()).hexdigest()
            with IMAGE_LOCK:
                IMAGE_URLS[token] = (item["image"], item["url"])
            item["image"] = "/api/image/" + token
    return items


def parse_request_body(handler) -> dict:
    try:
        length = min(int(handler.headers.get("Content-Length", "0")), 512 * 1024)
        body = json.loads(handler.rfile.read(length)) if length else {}
        return body if isinstance(body, dict) else {}
    except (ValueError, json.JSONDecodeError):
        return {}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def json_response(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if urlsplit(self.path).path != "/api/news":
            self.send_error(404)
            return
        body = parse_request_body(self)
        known = body.get("known", {}) if isinstance(body.get("known", {}), dict) else {}
        known_by_source = {code: {value for value in known.get(code, []) if isinstance(value, str)}
                           for code in SOURCES}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
            result = dict(pool.map(lambda code: crawl_source(code, known_by_source[code]), SOURCES))
        new_items = [item for source in result.values() for item in source["items"]]
        add_image_proxies(new_items)
        self.json_response(result)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/health":
            self.json_response({"ok": True, "sources": len(SOURCES)})
        elif path.startswith("/api/image/"):
            token = path.rsplit("/", 1)[-1]
            with IMAGE_LOCK:
                image = IMAGE_URLS.get(token)
            if not image:
                self.send_error(404)
                return
            try:
                data, reported = request_bytes(image[0], accept="image/avif,image/webp,image/*,*/*;q=0.8",
                                               referer=image[1], limit=10 * 1024 * 1024, timeout=15)
                image_type = image_content_type(data, reported)
                if not image_type:
                    raise ValueError("Not an image")
            except Exception:
                self.send_error(502)
                return
            self.send_response(200)
            self.send_header("Content-Type", image_type)
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path in ("/", "/index.html", "/favicon.svg", "/fonts.css", "/styles.css", "/layout-ltr.css", "/app.js") or path.startswith("/fonts/"):
            super().do_GET()
        else:
            self.send_error(404)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    print(f"News reader: http://127.0.0.1:{args.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
