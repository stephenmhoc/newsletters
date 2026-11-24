"""The three RSS feeds, built per request from the database.

- Newsletters: Daily Digest (/digest/<token>/feed.xml): one item per published day.
- Newsletters: Reading List (/full/<token>/feed.xml): newsletters and web pages added on the site, in full.
- Newsletters: Everything (/all/<token>/feed.xml): every summarized newsletter, in full.

Each token is HMAC(WEB_SECRET, "<kind>-feed"). Channel dates come from the
items, never the clock, so an unchanged feed has an unchanged ETag.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Callable, Iterable
from datetime import datetime
from email.utils import format_datetime
from html import escape
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlsplit
from xml.etree import ElementTree as ET

from .config import AppConfig
from .digest import DailyDigest
from .full_content import clean_html, drop_leading_title
from .gmail_client import GMAIL_ID_PATTERN, gmail_link
from .render import (
    ADD_LABEL,
    STATUS_LABELS,
    digest_body,
    digest_page_url,
    digest_title,
    excerpt_note,
    full_item_byline,
    item_url,
    key_points,
    late_heading,
    link,
    long_date,
    missed_message_html,
    newsletter_meta,
    original_url,
    publication_name,
    read_url,
)
from .store import FullItem, NewsletterSummary

if TYPE_CHECKING:
    from .run_state import RunAlert

__all__ = [
    "STATUS_LABELS",
    "digest_title",
    "late_heading",
    "long_date",
    "missed_message_html",
]

ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
CONTENT_NAMESPACE = "http://purl.org/rss/1.0/modules/content/"
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
FULL_FEED_TITLE = "Newsletters: Reading List"
ALL_FEED_TITLE = "Newsletters: Everything"
# Public home pages and icons for the feeds, served by the public listener
# under /newsletters/ so readers (FreshRSS, NetNewsWire) can show distinct icons.
FEED_KINDS = ("digest", "full", "all")
FEED_DESCRIPTIONS = {
    "digest": "Each newsletter from the day, summarized in up to three bullets.",
    "full": "Newsletters and web pages you chose to read in full.",
    "all": "Every summarized newsletter as its own item: the key points, then the full text.",
}

ImageRewriter = Callable[[str], str]


def web_secret(config: AppConfig) -> str:
    return os.environ.get(config.web.secret_env, "")


def feed_token(secret: str, kind: str) -> str:
    """The bearer secret in a feed URL: HMAC(WEB_SECRET, "<kind>-feed")."""
    return hmac.new(secret.encode("utf-8"), f"{kind}-feed".encode(), hashlib.sha256).hexdigest()


def full_feed_token(secret: str) -> str:
    return feed_token(secret, "full")


def digest_feed_token(secret: str) -> str:
    return feed_token(secret, "digest")


def all_feed_token(secret: str) -> str:
    return feed_token(secret, "all")


def public_base(config: AppConfig) -> str:
    """The directory of rss.public_url, e.g. https://rss.example.com."""
    parts = urlsplit(config.rss.public_url)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}{parts.path.rsplit('/', 1)[0]}"


def public_origin(config: AppConfig) -> str:
    """https://rss.example.com, the public listener's origin."""
    return _origin(config.rss.public_url)


def feed_url(config: AppConfig, secret: str, kind: str) -> str:
    return f"{public_base(config)}/{kind}/{feed_token(secret, kind)}/feed.xml"


def digest_feed_url(config: AppConfig, secret: str) -> str:
    return feed_url(config, secret, "digest")


def full_feed_url(config: AppConfig, secret: str) -> str:
    return feed_url(config, secret, "full")


def all_feed_url(config: AppConfig, secret: str) -> str:
    return feed_url(config, secret, "all")


def feed_page_title(config: AppConfig, kind: str) -> str:
    return {"digest": config.rss.title, "full": FULL_FEED_TITLE, "all": ALL_FEED_TITLE}[kind]


def site_url(config: AppConfig) -> str | None:
    """The private site's origin when the web UI is enabled, for reading-view links."""
    return config.web.public_url.rstrip("/") if config.web.enabled and config.web.public_url else None


def email_link(web_public_url: str, gmail_id: str) -> str:
    """Where to read an email: the site's reading view when it is enabled, else Gmail."""
    if web_public_url and GMAIL_ID_PATTERN.match(gmail_id):
        return item_url(web_public_url, gmail_id)
    return gmail_link(gmail_id)


def feed_home_url(feed_url: str, kind: str) -> str:
    """https://host/newsletters/<kind>/ on the host that serves feed_url."""
    origin = _origin(feed_url)
    return f"{origin}/newsletters/{kind}/" if origin else ""


def feed_icon_url(feed_url: str, kind: str) -> str:
    origin = _origin(feed_url)
    return f"{origin}/newsletters/icons/{kind}.png" if origin else ""


def latest(moments: Iterable[datetime | None]) -> datetime | None:
    known = [moment for moment in moments if moment is not None]
    return max(known) if known else None


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _channel(
    kind: str, *, title: str, description: str, self_url: str, fallback_link: str, updated: datetime | None,
    namespaces: dict[str, str] | None = None,
) -> tuple[ET.Element, ET.Element]:
    rss = ET.Element("rss", {"version": "2.0", "xmlns:atom": ATOM_NAMESPACE, **(namespaces or {})})
    channel = ET.SubElement(rss, "channel")
    _text(channel, "title", title)
    _text(channel, "description", description)
    home = feed_home_url(self_url, kind)
    _text(channel, "link", home or fallback_link)
    if self_url:
        ET.SubElement(channel, "atom:link", {"href": self_url, "rel": "self", "type": "application/rss+xml"})
    icon = feed_icon_url(self_url, kind)
    if icon:
        ET.SubElement(channel, "atom:icon").text = icon
        image = ET.SubElement(channel, "image")
        _text(image, "url", icon)
        _text(image, "title", title)
        _text(image, "link", home)
    if updated is not None:
        # The newest item's date, not the clock, so the ETag stays stable.
        _text(channel, "lastBuildDate", format_datetime(updated))
    _text(channel, "generator", "newsletter-summary")
    return rss, channel


def _finish(rss: ET.Element) -> bytes:
    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def render_digest_feed(
    digests: list[DailyDigest],
    *,
    title: str,
    description: str,
    self_url: str = "",
    web_public_url: str = "",
    alert: RunAlert | None = None,
) -> bytes:
    """RSS for the daily digest.

    An alert item (failed or stale run) comes first, so a dead run is visible
    in the reader instead of silently producing no entry. With the site
    enabled, each item links to its day's page and each newsletter title to
    its reading view.
    """
    site = web_public_url.rstrip("/") or None
    updated = latest([*(digest.end for digest in digests), alert.published if alert else None])
    rss, channel = _channel(
        "digest", title=title, description=description, self_url=self_url, fallback_link=_origin(self_url),
        updated=updated,
    )
    if alert is not None:
        item = ET.SubElement(channel, "item")
        _text(item, "title", alert.title)
        _text(item, "guid", alert.guid, isPermaLink="false")
        _text(item, "pubDate", format_datetime(alert.published))
        _text(item, "description", alert.html)
    for digest in digests:
        item = ET.SubElement(channel, "item")
        _text(item, "title", digest_title(digest))
        if site:
            _text(item, "link", digest_page_url(site, digest.day))
        _text(item, "guid", digest.id, isPermaLink="false")
        _text(item, "pubDate", format_datetime(digest.end))
        _text(item, "description", digest_body(digest, site=site))
    return _finish(rss)


def render_full_text_feed(
    items: list[FullItem],
    *,
    self_url: str = "",
    site_url: str = "",
    rewrite_image: ImageRewriter | None = None,
) -> bytes:
    """RSS for the reading list; items carry their content from the content store."""
    rss, channel = _channel(
        "full", title=FULL_FEED_TITLE, description=FEED_DESCRIPTIONS["full"], self_url=self_url,
        fallback_link=site_url, updated=latest(_parse(item.added_at) for item in items),
        namespaces={"xmlns:content": CONTENT_NAMESPACE, "xmlns:dc": DC_NAMESPACE},
    )
    for full_item in items:
        item = ET.SubElement(channel, "item")
        _text(item, "title", full_item.title)
        _text(item, "link", full_item.url or full_item.source_gmail_url)
        guid = (
            f"newsletter-web:{full_item.gmail_id.removeprefix('web-')}"
            if full_item.is_web
            else f"newsletter-full:{full_item.gmail_id}"
        )
        _text(item, "guid", guid, isPermaLink="false")
        _text(item, "pubDate", format_datetime(datetime.fromisoformat(full_item.added_at)))
        creator = ", ".join(full_item.authors) or (full_item.publication if full_item.is_web else "")
        if creator:
            _text(item, "dc:creator", creator)
        if full_item.publication:
            _text(item, "category", full_item.publication)
        _text(item, "description", _full_item_description(full_item))
        _text(item, "content:encoded", _full_item_content(full_item, rewrite_image))
    return _finish(rss)


def _full_item_description(item: FullItem) -> str:
    parts = []
    if item.bullets:
        parts.append("<ul>" + "".join(f"<li>{escape(bullet)}</li>" for bullet in item.bullets) + "</ul>")
    parts.append(excerpt_note(item.content_access, web=item.is_web))
    return "".join(parts)


def _full_item_content(item: FullItem, rewrite_image: ImageRewriter | None = None) -> str:
    links = []
    if item.url:
        links.append(link(item.url, "Original"))
    if item.source_gmail_url:
        links.append(link(item.source_gmail_url, "Open in Gmail"))
    byline = full_item_byline(item)
    parts = [
        f"<p>{byline}</p>" if byline else "",
        excerpt_note(item.content_access, web=item.is_web),
        # nh3 runs last, so nothing re-serialized afterwards can bypass it.
        clean_html(drop_leading_title(item.content_html, item.title), rewrite_image),
        f"<hr><p>{' · '.join(links)}</p>" if links else "",
    ]
    return "\n".join(part for part in parts if part)


def render_all_feed(
    entries: list[tuple[NewsletterSummary, str | None]],
    *,
    self_url: str = "",
    site: str | None = None,
    rewrite_image: ImageRewriter | None = None,
) -> bytes:
    """RSS with one item per summarized newsletter, newest first.

    entries pairs each summary with its stored sanitized HTML (None when the
    email's content is not stored). The publication is the author, the
    bullets are the lede, and the full text follows.
    """
    rss, channel = _channel(
        "all", title=ALL_FEED_TITLE, description=FEED_DESCRIPTIONS["all"], self_url=self_url,
        fallback_link=site or "", updated=latest(summary.email_received_at for summary, _ in entries),
        namespaces={"xmlns:content": CONTENT_NAMESPACE, "xmlns:dc": DC_NAMESPACE},
    )
    for newsletter, content_html in entries:
        gmail_id = newsletter.source_gmail_id
        readable = site is not None and bool(GMAIL_ID_PATTERN.match(gmail_id))
        item = ET.SubElement(channel, "item")
        _text(item, "title", newsletter.title)
        _text(item, "link", item_url(site or "", gmail_id) if readable else newsletter.url or gmail_link(gmail_id))
        _text(item, "guid", f"newsletter-all:{gmail_id}", isPermaLink="false")
        _text(item, "pubDate", format_datetime(newsletter.email_received_at))
        _text(item, "dc:creator", publication_name(newsletter))
        _text(item, "category", publication_name(newsletter))
        _text(item, "description", key_points(newsletter) + excerpt_note(newsletter.content_access))
        _text(item, "content:encoded", _all_item_content(newsletter, content_html, site, rewrite_image))
    return _finish(rss)


def _all_item_content(
    newsletter: NewsletterSummary, content_html: str | None, site: str | None, rewrite_image: ImageRewriter | None
) -> str:
    gmail_id = newsletter.source_gmail_id
    readable = site is not None and bool(GMAIL_ID_PATTERN.match(gmail_id))
    links = []
    if readable:
        links.append(link(read_url(site or "", gmail_id), ADD_LABEL))
    original = original_url(newsletter.url, site)
    if original:
        links.append(link(original, "Original"))
    if GMAIL_ID_PATTERN.match(gmail_id):
        links.append(link(gmail_link(gmail_id), "Open in Gmail"))
    if content_html:
        # nh3 runs last, so nothing re-serialized afterwards can bypass it.
        body = clean_html(drop_leading_title(content_html, newsletter.title), rewrite_image)
    elif readable:
        body = f"<p><em>The full text of this email is not stored. {link(item_url(site or '', gmail_id), 'Open it on the site')}.</em></p>"
    else:
        body = ""
    parts = [
        f"<p>{newsletter_meta(newsletter)}</p>",
        key_points(newsletter),
        excerpt_note(newsletter.content_access),
        f"<p>{' · '.join(links)}</p>" if links else "",
        "<hr>" if body else "",
        body,
    ]
    return "\n".join(part for part in parts if part)


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _text(parent: ET.Element, tag: str, value: str, **attributes) -> ET.Element:
    child = ET.SubElement(parent, tag, attributes)
    child.text = value
    return child
