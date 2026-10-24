#!/usr/bin/env python3
"""Persian gaming-news reader with source-aware pagination and no dependencies."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent
IMAGE_CACHE_FILE = ROOT / ".image-urls.json"
IMAGE_FILES = ROOT / ".image-cache"
IMAGE_FETCH_LOCKS = [threading.Lock() for _ in range(64)]
IMAGE_DOWNLOADS = threading.BoundedSemaphore(6)
IMAGE_ACCEPT = "image/jpeg,image/png,image/webp,image/*;q=0.8"
IMAGE_ERRORS: dict[str, dict] = {}
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
def load_image_urls() -> dict[str, tuple[str, str]]:
    try:
        raw = json.loads(IMAGE_CACHE_FILE.read_text("utf-8"))
        return {token: (value[0], value[1]) for token, value in raw.items()
                if re.fullmatch(r"[a-f0-9]{64}", token) and isinstance(value, list) and len(value) == 2
                and all(isinstance(part, str) for part in value)}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


IMAGE_URLS: dict[str, tuple[str, str]] = load_image_urls()
IMAGE_LOCK = threading.Lock()


def safe_url(value: str, base: str, *, article: bool = False) -> str:
    if not value or not value.strip():
        return ""
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


def failure_description(exc: Exception, url: str) -> str:
    host = urlsplit(url).hostname or "publisher"
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code} from {host}"
    reason = exc.reason if isinstance(exc, URLError) else exc
    proxy_denial = re.search(r"Tunnel connection failed: (\d{3})", str(reason))
    if proxy_denial:
        return f"Proxy denied access to {host} (HTTP {proxy_denial.group(1)})"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"TLS certificate verification failed for {host}"
    if isinstance(reason, (TimeoutError,)):
        return f"Connection to {host} timed out"
    if isinstance(reason, ssl.SSLError):
        return f"TLS connection to {host} failed"
    if isinstance(exc, ImageFetchError):
        return str(exc)
    return f"Could not connect to {host} ({type(reason).__name__})"


class ImageFetchError(Exception):
    def __init__(self, message, candidates=()):
        super().__init__(message)
        self.candidates = list(candidates)


def request_image_transport(url: str, *, accept: str, referer: str = "",
                            limit: int = 10 * 1024 * 1024, timeout: int = 12):
    """Use OS curl when Python's HTTPS stack fails. TLS checks stay enabled."""
    try:
        return request_bytes(url, accept=accept, referer=referer, limit=limit, timeout=timeout)
    except Exception as python_error:
        curl = shutil.which("curl.exe") or shutil.which("curl")
        if not curl:
            raise ImageFetchError(failure_description(python_error, url)) from python_error
        with tempfile.TemporaryDirectory(prefix="checkpoint-image-") as directory:
            output = Path(directory) / "response"
            command = [curl, "--silent", "--show-error", "--location", "--compressed",
                       "--proto", "=http,https", "--proto-redir", "=http,https",
                       "--max-time", str(timeout), "--connect-timeout", "8",
                       "--max-filesize", str(limit), "--output", str(output),
                       "--write-out", "%{http_code}\n%{content_type}",
                       "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
                       "--header", "Accept: " + accept]
            if referer:
                command.extend(["--referer", referer])
            command.append(url)
            try:
                response = subprocess.run(command, capture_output=True, timeout=timeout + 3, check=False)
                metadata = response.stdout.decode("utf-8", errors="replace").splitlines()
                status = int(metadata[0]) if metadata and metadata[0].isdigit() else 0
                host = urlsplit(url).hostname
                if response.returncode:
                    detail = {60: "TLS certificate verification failed", 28: "Connection timed out",
                              6: "DNS lookup failed", 5: "Proxy lookup failed", 35: "TLS connection failed"}.get(response.returncode, f"curl error {response.returncode}")
                    raise ImageFetchError(f"{failure_description(python_error, url)}; OS transport: {detail} for {host}")
                if status < 200 or status >= 300:
                    raise ImageFetchError(f"{failure_description(python_error, url)}; OS transport: HTTP {status} from {host}")
                data = output.read_bytes()
                if len(data) > limit:
                    raise ImageFetchError("Publisher response exceeded the image size limit")
                return data, (metadata[1].split(";", 1)[0] if len(metadata) > 1 else "application/octet-stream")
            except subprocess.TimeoutExpired as exc:
                raise ImageFetchError(f"Connection to {urlsplit(url).hostname} timed out in both transports") from exc


def image_content_type(data: bytes, reported: str) -> str:
    signatures = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"),
                  (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))
    for signature, content_type in signatures:
        if data.startswith(signature):
            return content_type
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def image_from_attrs(attrs: dict, base: str) -> str:
    # VGC supplies only data-srcset; Gematsu uses both src and data-srcset.
    srcset = attrs.get("data-srcset") or attrs.get("srcset") or ""
    candidates = []
    for part in srcset.split(","):
        fields = part.strip().split()
        if fields:
            width = int(fields[1][:-1]) if len(fields) > 1 and re.fullmatch(r"\d+w", fields[1]) else 0
            candidates.append((width, fields[0]))
    if candidates:
        suitable = sorted(candidate for candidate in candidates if candidate[0] >= 480)
        return safe_url((suitable[0] if suitable else max(candidates))[1], base)
    return safe_url(attrs.get("data-src") or attrs.get("data-lazy-src") or
                    attrs.get("data-original") or attrs.get("src") or "", base)


class ArticleImages(HTMLParser):
    """Prefer the article's Open Graph image over logos and unrelated images."""
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.preferred = []
        self.other = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and (attrs.get("property") or attrs.get("name")) in (
                "og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"):
            value = safe_url(attrs.get("content", ""), self.base)
            if value:
                self.preferred.append(value)
        elif tag == "img":
            value = image_from_attrs(attrs, self.base)
            if value:
                self.other.append(value)

    @property
    def urls(self):
        return list(dict.fromkeys(self.preferred + self.other))


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
            if tag == "img" or attrs.get("type", "").startswith("image/"):
                self._article_image = self._article_image or image_from_attrs(attrs, self.base)
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
        original = item.get("originalImage") or item.get("image", "")
        item["originalImage"] = original
        # Each URL carries its own recovery information; no RAM token or JSON
        # mapping is needed, even for old browser history on a fresh installation.
        item["image"] = "/api/story-image?" + urlencode({"article": item["url"], "image": original})
    return items


def source_for_article(article: str) -> dict:
    parts = urlsplit(article)
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError("Invalid article URL")
    host = (parts.hostname or "").removeprefix("www.")
    for source in SOURCES.values():
        if host == urlsplit(source["url"]).hostname.removeprefix("www."):
            return source
    raise ValueError("Unknown news source")


def allowed_image_url(value: str, article: str) -> str:
    source = source_for_article(article)
    url = safe_url(value, article)
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    source_host = urlsplit(source["url"]).hostname.removeprefix("www.")
    allowed_hosts = {source_host}
    if source_host == "pushsquare.com":
        allowed_hosts.add("images.pushsquare.com")
    if source_host == "gamespot.com":
        allowed_hosts.update(("static.gamespot.com", "www.gamespot.com", "www.giantbomb.com"))
    if (parts.scheme not in ("http", "https") or parts.username or parts.password or
            parts.port not in (None, 80, 443) or host not in allowed_hosts):
        return ""
    # Reject listing/article pages that older versions accidentally saved as images.
    if not re.search(r"\.(?:jpe?g|png|webp|gif|avif)(?:$|/)", parts.path, re.I):
        return ""
    return url


def get_story_image(article: str, hint: str = "") -> tuple[bytes, str]:
    source_for_article(article)
    key = hashlib.sha256((article + "\n" + hint).encode()).hexdigest()
    cache_file = IMAGE_FILES / (key + ".img")
    with IMAGE_FETCH_LOCKS[int(key[:2], 16) % len(IMAGE_FETCH_LOCKS)]:
        try:
            cached = cache_file.read_bytes()
            kind = image_content_type(cached, "")
            if kind:
                return cached, kind
        except OSError:
            pass
        with IMAGE_DOWNLOADS:
            tried = set()
            errors = []

            def download(candidate):
                url = allowed_image_url(candidate, article)
                if not url or url in tried:
                    return None
                tried.add(url)
                # Prefer no Referer: this avoids sending localhost to hotlink
                # protection. Some publishers require their own article Referer.
                for referer in ("", article):
                    try:
                        data, reported = request_image_transport(url, accept=IMAGE_ACCEPT, referer=referer,
                                                                 timeout=12, limit=10 * 1024 * 1024)
                        kind = image_content_type(data, reported)
                        if kind:
                            return data, kind
                        errors.append(f"{urlsplit(url).hostname} returned a non-image response")
                    except Exception as exc:
                        errors.append(failure_description(exc, url))
                return None

            result = download(hint)
            if result is None:
                try:
                    data, kind = request_image_transport(article, accept="text/html,application/xhtml+xml",
                                                         timeout=15, limit=4 * 1024 * 1024)
                    parser = ArticleImages(article)
                    parser.feed(data.decode("utf-8", errors="replace"))
                    for candidate in parser.urls[:8]:
                        result = download(candidate)
                        if result is not None:
                            break
                except Exception as exc:
                    errors.append(failure_description(exc, article))
            if result is None:
                detail = "; ".join(dict.fromkeys(errors[-3:])) or "The publisher did not provide an article image"
                raise ImageFetchError(detail, tried)
        try:
            IMAGE_FILES.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=IMAGE_FILES, delete=False) as temporary:
                temporary.write(result[0])
                temporary_path = Path(temporary.name)
            temporary_path.replace(cache_file)
        except OSError:
            pass  # A read-only project directory must not prevent image display.
        return result


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

    def end_headers(self):
        if urlsplit(self.path).path in ("/", "/index.html", "/app.js", "/styles.css", "/layout-ltr.css"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def serve_story_image(self, article, hint):
        try:
            source_for_article(article)
            if len(article) > 4096 or len(hint) > 4096:
                raise ValueError("Image request too long")
        except ValueError:
            self.send_error(400)
            return
        try:
            data, image_type = get_story_image(article, hint)
        except Exception as exc:
            detail = failure_description(exc, article)
            candidates = getattr(exc, "candidates", [])
            key = hashlib.sha256((article + "\n" + hint).encode()).hexdigest()
            with IMAGE_LOCK:
                if len(IMAGE_ERRORS) >= 512:
                    IMAGE_ERRORS.pop(next(iter(IMAGE_ERRORS)))
                IMAGE_ERRORS[key] = {"error": detail, "candidates": candidates}
            self.log_error("Publisher image failed: %s", detail)
            self.json_response({"error": detail}, 502)
            return
        key = hashlib.sha256((article + "\n" + hint).encode()).hexdigest()
        with IMAGE_LOCK:
            IMAGE_ERRORS.pop(key, None)
        self.send_response(200)
        self.send_header("Content-Type", image_type)
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

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
            self.json_response({"ok": True, "sources": len(SOURCES), "version": "1.3.1"})
        elif path == "/api/image-status":
            query = parse_qs(urlsplit(self.path).query)
            article = query.get("article", [""])[0]
            hint = query.get("image", [""])[0]
            key = hashlib.sha256((article + "\n" + hint).encode()).hexdigest()
            with IMAGE_LOCK:
                status = IMAGE_ERRORS.get(key, {"error": "The browser could not display this image", "candidates": []})
            self.json_response(status)
        elif path == "/api/story-image":
            query = parse_qs(urlsplit(self.path).query)
            self.serve_story_image(query.get("article", [""])[0], query.get("image", [""])[0])
        elif path.startswith("/api/image/"):
            token = path.rsplit("/", 1)[-1]
            with IMAGE_LOCK:
                image = IMAGE_URLS.get(token)
            if not image:
                self.send_error(404)
                return
            self.serve_story_image(image[1], image[0])
        elif path in ("/", "/index.html", "/favicon.svg", "/fonts.css", "/styles.css", "/layout-ltr.css", "/app.js") or path.startswith("/fonts/"):
            super().do_GET()
        else:
            self.send_error(404)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Checkpoint 1.3.1: http://127.0.0.1:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
