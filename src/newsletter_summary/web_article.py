"""Web pages added to the reading list with "Add a link".

A submitted URL is fetched through safe_fetch (public addresses only, HTML
only, at most MAX_PAGE_BYTES), decoded with its declared or sniffed charset,
and reduced to the article with readability-lxml. Metadata comes from the
page's own tags: Open Graph, article:*, <meta name=author>, JSON-LD, and
<link rel=canonical>. The article HTML then goes through the same sanitizer
as newsletter email (nh3 last), so it is stored and rendered exactly like
newsletter content.

Web items have IDs "web-<first 20 hex of sha256(canonical URL)>", which can
never match a Gmail ID, so nothing about them can reach the Gmail API.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape, unescape
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag, UnicodeDammit

from . import safe_fetch
from .email_extract import strip_credentials
from .full_content import sanitize_email, word_count

WEB_ID_PREFIX = "web-"
WEB_ID_PATTERN = re.compile(r"^web-[0-9a-f]{20}$")
MAX_PAGE_BYTES = 5 * 1024 * 1024
# Less visible text than this after extraction means we probably got a
# paywall, a cookie wall, or a page that renders with JavaScript.
THIN_WORDS = 150
# Browser-like, so ordinary sites serve their normal page, but naming the app.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36 newsletter-summary/0.1 (personal reading list)"
)
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.I)
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""

# Friendly explanations for safe_fetch failures, keyed by FetchError.reason.
FETCH_ERROR_MESSAGES = {
    "blocked": "That address points at a private or local network, so it was not fetched.",
    "invalid": "Only public http and https links on the standard ports can be added.",
    "type": "That link is not a web page (HTML), so it could not be added.",
    "too_large": "That page is larger than 5 MB, so it was not added.",
    "timeout": "The site took too long to respond.",
    "status": "The site answered with an error",
    "unreachable": "The site could not be reached.",
}

PageFetcher = Callable[[str], safe_fetch.Fetched]

# readability logs every failure with a traceback, and chatty progress at INFO;
# _readable logs the outcome itself.
logging.getLogger("readability").setLevel(logging.CRITICAL)


@dataclass(frozen=True)
class WebArticle:
    """What a fetched page contributes to the reading list."""

    canonical_url: str
    title: str
    site_name: str
    byline: str
    published: str  # ISO 8601, or ""
    lead_image: str
    # Extracted article HTML (with the lead image when the body lacked it):
    # stored as the raw body, so a newer sanitizer can re-derive content_html.
    raw_html: str
    content_html: str  # sanitized
    words: int
    description: str = ""

    @property
    def thin(self) -> bool:
        return self.words < THIN_WORDS

    @property
    def web_id(self) -> str:
        return web_id(self.canonical_url)


def web_id(canonical_url: str) -> str:
    return WEB_ID_PREFIX + hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:20]


def is_web_id(item_id: str) -> bool:
    return bool(WEB_ID_PATTERN.match(item_id))


def guid_hash(item_id: str) -> str:
    return item_id.removeprefix(WEB_ID_PREFIX)


def first_url(text: str) -> str:
    """The first http(s) URL in text: share sheets often send "Title https://…"."""
    text = (text or "").strip()
    if not text:
        return ""
    match = _URL_IN_TEXT.search(text)
    if not match:
        return ""
    url = match.group(0)
    # Keep a closing parenthesis that belongs to the URL, as in Wikipedia links.
    while url and url[-1] in _TRAILING_PUNCTUATION:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url if safe_fetch.url_problem(url) is None else ""


def normalized_url(url: str) -> str:
    """The URL as stored and compared: no fragment, no subscriber credentials."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    return strip_credentials(urlunsplit(parts._replace(fragment="")))


def is_html_type(media_type: str) -> bool:
    return media_type in HTML_TYPES


def fetch_page(url: str, **options: Any) -> safe_fetch.Fetched:
    """A web page through every safe_fetch guard; options (resolver, manager, ...) are for tests."""
    return safe_fetch.fetch(
        url,
        max_bytes=MAX_PAGE_BYTES,
        accept_type=is_html_type,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        },
        **options,
    )


def decode_html(data: bytes, charset: str = "") -> str:
    """Bytes to text: the header's charset, else a BOM or <meta charset>, else a guess."""
    dammit = UnicodeDammit(data, known_definite_encodings=[charset] if charset else [], is_html=True)
    if dammit.unicode_markup is not None:
        return dammit.unicode_markup
    return data.decode("utf-8", errors="replace")


def extract(html: str, final_url: str) -> WebArticle:
    """The article and its metadata from a page's HTML."""
    soup = BeautifulSoup(html, "html.parser")
    meta = _meta_tags(soup)
    ld = _json_ld(soup)
    canonical = _canonical(soup, final_url)
    host = (urlsplit(canonical).hostname or "").removeprefix("www.")

    body, readable_title = _readable(html, final_url)
    title = _clean(
        meta.get("og:title") or meta.get("twitter:title") or str(ld.get("headline") or "") or readable_title
    ) or _clean(soup.title.get_text() if soup.title else "") or host or canonical
    site_name = _clean(meta.get("og:site_name") or meta.get("application-name") or _ld_name(ld.get("publisher"))) or host
    byline = _clean(
        meta.get("author") or meta.get("article:author") or meta.get("parsely-author") or meta.get("sailthru.author")
        or _ld_name(ld.get("author"))
    )
    if byline.startswith(("http://", "https://")):
        # article:author is often a profile URL; not a name.
        byline = _clean(_ld_name(ld.get("author")))
    published = _iso_date(
        meta.get("article:published_time") or meta.get("og:published_time") or str(ld.get("datePublished") or "")
        or meta.get("parsely-pub-date") or meta.get("date") or meta.get("pubdate") or _time_tag(soup)
    )
    lead_image = _absolute(meta.get("og:image") or meta.get("twitter:image") or "", final_url)
    description = _clean(meta.get("og:description") or meta.get("description") or "")

    if lead_image and lead_image not in unescape(body):
        body = f'<figure><img src="{escape(lead_image)}" alt=""></figure>\n{body}'
    if not BeautifulSoup(body, "html.parser").get_text(strip=True) and description:
        body += f"\n<p>{escape(description)}</p>"
    content_html = sanitize_email(body, "").html
    return WebArticle(
        canonical_url=canonical,
        title=title,
        site_name=site_name,
        byline=byline,
        published=published,
        lead_image=lead_image,
        raw_html=body,
        content_html=content_html,
        words=word_count(content_html),
        description=description,
    )


def _readable(html: str, url: str) -> tuple[str, str]:
    """(article HTML, title) from readability; ("", "") when it cannot find one."""
    from readability import Document

    try:
        document = Document(html, url=url)
        return document.summary(html_partial=True), document.short_title()
    except Exception as exc:
        # readability raises on empty or very odd documents.
        logging.info("Readability found no article: %s", type(exc).__name__)
        return "", ""


def _meta_tags(soup: BeautifulSoup) -> dict[str, str]:
    """Lowercased property/name -> first non-empty content."""
    found: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        if not isinstance(tag, Tag):
            continue
        key = _attribute(tag, "property") or _attribute(tag, "name") or _attribute(tag, "itemprop")
        content = _attribute(tag, "content").strip()
        if key and content and key.lower() not in found:
            found[key.lower()] = content
    return found


def _json_ld(soup: BeautifulSoup) -> dict:
    """The first JSON-LD object that looks like an article."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.get_text() or "")
        except ValueError:
            continue
        candidates = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            kind = candidate.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(isinstance(k, str) and (k.endswith("Article") or k in {"BlogPosting", "Report"}) for k in kinds):
                return candidate
    return {}


def _ld_name(value: object) -> str:
    """A name from a JSON-LD person/organization, or a list of them."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("name") or "")
    if isinstance(value, list):
        return ", ".join(name for name in (_ld_name(item) for item in value) if name)
    return ""


def _canonical(soup: BeautifulSoup, final_url: str) -> str:
    for tag in soup.find_all("link"):
        if not isinstance(tag, Tag):
            continue
        rel = tag.get("rel") or []
        rels = rel if isinstance(rel, list) else str(rel).split()
        if "canonical" in [value.lower() for value in rels]:
            href = _absolute(_attribute(tag, "href"), final_url)
            if href:
                return normalized_url(href)
    return normalized_url(final_url)


def _absolute(href: str, base: str) -> str:
    href = href.strip()
    if not href:
        return ""
    url = urljoin(base, href)
    return url if safe_fetch.url_problem(url) is None else ""


def _time_tag(soup: BeautifulSoup) -> str:
    tag = soup.find("time", attrs={"datetime": True})
    return _attribute(tag, "datetime") if isinstance(tag, Tag) else ""


def _iso_date(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


def _clean(value: str) -> str:
    return " ".join((value or "").split())[:500]


def _attribute(tag: Tag, name: str) -> str:
    value = tag.get(name)
    if isinstance(value, list):
        return " ".join(value)
    return value or ""


def fetch_error_message(exc: safe_fetch.FetchError) -> str:
    message = FETCH_ERROR_MESSAGES.get(exc.reason, FETCH_ERROR_MESSAGES["unreachable"])
    if exc.reason == "status":
        # "HTTP 404" never contains the URL.
        message = f"{message} ({exc}), so nothing was added."
    return message
