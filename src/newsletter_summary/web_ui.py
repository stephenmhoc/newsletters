"""Private web UI on its own listener, behind Cloudflare Access.

It shows today's newsletters so far, past digests (one page per day), every
email the service has handled with a reading view for each, the reading list
and the feed URLs, and a status page. "Add a link" (/add) saves any public web
page to the reading list. The public feed port never serves these routes.
Every request must carry a verified Access JWT for an allowed email and the
configured Host; every POST must also carry the public Origin and an HMAC CSRF
token bound to the user, action, and item ID. Only emails already in the store
are ever fetched from Gmail; web items ("web-<hash>") never reach Gmail.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from html import escape
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import requests

from .access_auth import AccessDenied, AccessVerifier
from .config import AppConfig
from .digest import DailyDigest, load_digests, load_summaries, today_so_far
from .email_content import content_from_message, current_content
from .email_extract import email_to_markdown, extract_links
from .full_content import SANITIZER_VERSION, clean_html, drop_leading_title
from .gmail_client import (
    GMAIL_ID_PATTERN,
    NewsletterMessage,
    build_gmail_service,
    fetch_message,
    gmail_link,
    sender_name,
)
from .http_util import BaseHandler
from .image_proxy import ImageCache, image_rewriter
from .openrouter import SummaryItem, summarize_article
from .render import (
    ADD_LABEL,
    STATUS_LABELS,
    clock,
    digest_body,
    excerpt_note,
    full_item_byline,
    link,
    long_date,
    missed_message_html,
    newsletter_entry,
    original_url,
    reading_time,
)
from .rss import (
    ALL_FEED_TITLE,
    FULL_FEED_TITLE,
    all_feed_url,
    digest_feed_url,
    full_feed_token,
    full_feed_url,
    public_origin,
    web_secret,
)
from .safe_fetch import FetchError
from .status import gather_status, record_refresh
from .store import (
    MAX_SUMMARY_ATTEMPTS,
    WEB_MIGRATION,
    FullItem,
    NewsletterSummary,
    Store,
    StoredContent,
    StoreFeatureUnavailable,
)
from .web_article import (
    PageFetcher,
    WebArticle,
    decode_html,
    extract,
    fetch_error_message,
    first_url,
    is_web_id,
    normalized_url,
    web_id,
)
from .web_article import fetch_page as fetch_web_page

__all__ = ["csrf_token", "full_feed_token", "full_feed_url", "gmail_link", "web_secret"]

ACCESS_HEADER = "Cf-Access-Jwt-Assertion"
DEV_EMAIL = "dev@localhost"
DIGESTS_PER_PAGE = 7
EMAILS_PER_PAGE = 50
EMAILS_SCAN_LIMIT = 5000
FULL_PAGE_LIMIT = 500
MIGRATION_HINT = "The full-text tables are not installed yet. Apply deploy/sql/2026-11-full-text.sql."
WEB_MIGRATION_HINT = f"Adding web links is not installed yet. Apply {WEB_MIGRATION}."
FAVICON = Path(__file__).with_name("static") / "digest.png"
# Pages that list things may be revalidated from the browser cache; the rest,
# and every POST response, are never stored. no-transform also stops
# Cloudflare's Email Obfuscation from rewriting addresses into
# "[email protected]" links.
LIST_CACHE_CONTROL = "private, no-cache, no-transform"
NO_STORE = "no-store, no-transform"
FRESHRSS_REMOVAL_NOTE = (
    "Removing an item takes it off this list and out of the reading list feed, but FreshRSS keeps "
    "articles it has already fetched: mark it read or delete it there too."
)
NAV = (("/", "Digests"), ("/emails", "Emails"), ("/full", "Reading list"), ("/status", "Status"))

MessageFetcher = Callable[[str], NewsletterMessage | None]
ReaderRefresher = Callable[[], None]
RefreshRecorder = Callable[[bool, str], None]
# FreshRSS reuses its cached copy of a feed for cache_duration seconds (60 at
# its minimum), so a second request after that catches back-to-back adds.
REFRESH_DELAYS_SECONDS = (0, 70)
TokenVerifier = Callable[[str], str]
WebSummarizer = Callable[[WebArticle], SummaryItem | None]
# Runs a job off the request thread (tests pass one that runs it at once).
BackgroundRunner = Callable[[Callable[[], None]], None]


def csrf_token(secret: str, email: str, action: str, gmail_id: str) -> str:
    message = f"{email}|{action}|{gmail_id}".encode()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def gmail_fetcher(config: AppConfig) -> MessageFetcher:
    def fetch(gmail_id: str) -> NewsletterMessage | None:
        service = build_gmail_service()
        message = fetch_message(service, config.gmail.user_id, gmail_id, config.gmail.tzinfo)
        return message if message.has_body else None

    return fetch


def web_summarizer(config: AppConfig) -> WebSummarizer:
    """Key points for a saved web page: the newsletter model, schema, and bullet rules."""

    def summarize(article: WebArticle) -> SummaryItem | None:
        return summarize_article(
            api_key_env=config.summary.openrouter_api_key_env,
            keychain_service=config.summary.openrouter_keychain_service,
            keychain_account=config.summary.openrouter_keychain_account,
            model=config.summary.model,
            max_input_chars=config.summary.max_input_chars,
            app_name=config.summary.app_name,
            site_url=config.summary.site_url,
            base_prompt=config.summary.prompt,
            title=article.title,
            url=article.canonical_url,
            site_name=article.site_name,
            byline=article.byline,
            body_markdown=email_to_markdown(article.content_html, ""),
            links=extract_links(article.content_html, ""),
        )

    return summarize


def run_in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="web-summary", daemon=True).start()


def refresh_recorder(config: AppConfig) -> RefreshRecorder:
    """Store the latest FreshRSS ping result for /status."""

    def record(ok: bool, detail: str) -> None:
        try:
            with Store(config.storage.sqlite_path) as store:
                record_refresh(store, ok=ok, detail=detail)
        except Exception:
            logging.exception("Could not record the FreshRSS refresh result")

    return record


def reader_refresher(config: AppConfig, record: RefreshRecorder | None = None) -> ReaderRefresher | None:
    """Ask the RSS reader to re-fetch the reading list feed, in the background.

    The URL carries the reader's token, so it is never logged or recorded.
    """
    url = os.environ.get(config.web.reader_refresh_url_env, "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    record = record or refresh_recorder(config)

    def run() -> None:
        for delay in REFRESH_DELAYS_SECONDS:
            time.sleep(delay)
            try:
                response = requests.get(url, timeout=30)
                logging.info("Asked FreshRSS to refresh the reading list feed (HTTP %s)", response.status_code)
                record(response.status_code < 400, f"HTTP {response.status_code}")
            except requests.RequestException as exc:
                logging.warning("FreshRSS refresh request failed: %s", type(exc).__name__)
                record(False, type(exc).__name__)

    def refresh() -> None:
        threading.Thread(target=run, name="reader-refresh", daemon=True).start()

    return refresh


def web_config_problem(config: AppConfig, secret: str) -> str | None:
    """Why the web listener must not start, or None when it may."""
    web = config.web
    if not web.enabled:
        return "web.enabled is false"
    if not secret:
        return f"${web.secret_env} is not set"
    parsed = urlparse(web.public_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "web.public_url must be an absolute http(s) URL"
    if web.dev_insecure_no_auth:
        return None
    if not web.access_team_domain or not web.access_aud:
        return "web.access_team_domain and web.access_aud are required (or web.dev_insecure_no_auth for local use)"
    if not web.allowed_emails:
        return "web.allowed_emails is empty"
    return None


class WebHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        config: AppConfig,
        *,
        secret: str,
        verify: TokenVerifier | None,
        fetch: MessageFetcher,
        refresh_reader: ReaderRefresher | None = None,
        fetch_page: PageFetcher = fetch_web_page,
        summarize_web: WebSummarizer | None = None,
        background: BackgroundRunner = run_in_thread,
    ):
        if verify is None and not config.web.dev_insecure_no_auth:
            raise ValueError("An Access token verifier is required")
        if not secret:
            raise ValueError("A web secret is required")
        parsed = urlparse(config.web.public_url)
        self.config = config
        self.secret = secret
        self.verify = verify
        self.fetch = fetch
        self.refresh_reader = refresh_reader
        self.fetch_page = fetch_page
        self.summarize_web = summarize_web or web_summarizer(config)
        self.background = background
        self.expected_host = parsed.netloc.lower()
        self.expected_origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        self.rewrite_image = image_rewriter(config, secret)
        # Proxied images come from the public listener; nothing else may load.
        proxy_origin = public_origin(config) if self.rewrite_image else ""
        self.img_src = f"'self' data: {proxy_origin}" if proxy_origin else "https: http: data:"
        super().__init__(server_address, WebHandler)


def build_web_server(
    config: AppConfig,
    *,
    address: tuple[str, int] | None = None,
    verify: TokenVerifier | None = None,
    fetch: MessageFetcher | None = None,
    secret: str | None = None,
    refresh_reader: ReaderRefresher | None = None,
    fetch_page: PageFetcher | None = None,
    summarize_web: WebSummarizer | None = None,
    background: BackgroundRunner | None = None,
) -> WebHTTPServer:
    """Build the listener, refusing (ValueError) when it would not be closed to strangers."""
    secret = web_secret(config) if secret is None else secret
    problem = web_config_problem(config, secret)
    if problem:
        raise ValueError(problem)
    if verify is None and not config.web.dev_insecure_no_auth:
        verify = AccessVerifier(
            team_domain=config.web.access_team_domain,
            audience=config.web.access_aud,
            allowed_emails=config.web.allowed_emails,
        ).verify
    return WebHTTPServer(
        address or (config.web.bind, config.web.port),
        config,
        secret=secret,
        verify=verify,
        fetch=fetch or gmail_fetcher(config),
        refresh_reader=refresh_reader or reader_refresher(config),
        fetch_page=fetch_page or fetch_web_page,
        summarize_web=summarize_web,
        background=background or run_in_thread,
    )


def start_web_listener(config: AppConfig) -> WebHTTPServer | None:
    if not config.web.enabled:
        return None
    try:
        server = build_web_server(config)
    except ValueError as exc:
        logging.error("Web UI is disabled (the public feeds are unaffected): %s", exc)
        return None
    if config.web.dev_insecure_no_auth:
        logging.warning("Web UI is running WITHOUT Cloudflare Access verification (web.dev_insecure_no_auth)")
    threading.Thread(target=server.serve_forever, name="web-ui", daemon=True).start()
    host, port = server.server_address[:2]
    logging.info("Serving the web UI at http://%s:%s/ for %s", host, port, config.web.public_url)
    return server


class WebHandler(BaseHandler):
    server: WebHTTPServer
    _store: Store | None = None
    _email = ""
    _nonce = ""
    _cache_control = NO_STORE

    @property
    def config(self) -> AppConfig:
        return self.server.config

    @property
    def store(self) -> Store:
        """Opened on first use, so pages that need no database survive an outage."""
        if self._store is None:
            self._store = Store(self.config.storage.sqlite_path)
        return self._store

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._head(lambda: self._dispatch("GET"))

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, format: str, *args: Any) -> None:
        logging.info("web %s - %s", self.address_string(), format % args)

    def extra_headers(self, status: HTTPStatus) -> dict[str, str]:
        return {
            "Content-Security-Policy": (
                f"default-src 'none'; img-src {self.server.img_src}; style-src 'unsafe-inline'; "
                f"script-src 'nonce-{self._nonce}'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            # Not "no-referrer": browsers then send "Origin: null" on form POSTs,
            # which would defeat the Origin check. Cross-origin requests still get
            # no referrer.
            "Referrer-Policy": "same-origin",
            "Cache-Control": self._cache_control if status == HTTPStatus.OK else NO_STORE,
        }

    def _dispatch(self, method: str) -> None:
        self._nonce = secrets.token_urlsafe(16)
        self._response_started = False
        self._cache_control = NO_STORE
        parsed = urlparse(self.path)
        if (self.headers.get("Host") or "").lower() != self.server.expected_host:
            self._send_text("Forbidden: unexpected host\n", HTTPStatus.FORBIDDEN)
            return
        email = self._authenticate()
        if not email:
            self._send_text("Forbidden\n", HTTPStatus.FORBIDDEN)
            return
        self._email = email
        try:
            form = self._read_form() if method == "POST" else {}
        except ValueError as exc:
            self._send_text(f"Bad request: {exc}\n", HTTPStatus.BAD_REQUEST)
            return
        query = parse_qs(parsed.query)
        parts = parsed.path.strip("/").split("/") if parsed.path.strip("/") else []

        self._store = None
        try:
            self._route(method, parts, query, form)
        except StoreFeatureUnavailable:
            logging.exception("Full-text tables are unavailable")
            if not self._response_started:
                self._send_page("Unavailable", f"<p class=error>{escape(MIGRATION_HINT)}</p>", HTTPStatus.SERVICE_UNAVAILABLE)
        except Exception:
            logging.exception("Unhandled web UI error serving %s %s", method, parsed.path)
            if not self._response_started:
                self._send_text("Internal server error\n", HTTPStatus.INTERNAL_SERVER_ERROR)
        finally:
            if self._store is not None:
                self._store.close()
                self._store = None

    def _route(self, method: str, parts: list[str], query: dict[str, list[str]], form: dict[str, list[str]]) -> None:
        get = method == "GET"
        if get and parts in (["favicon.png"], ["favicon.ico"]):
            self._send_bytes(FAVICON.read_bytes(), "image/png", HTTPStatus.OK)
        elif get and parts == []:
            self._index(_page_number(query))
        elif get and len(parts) == 2 and parts[0] == "digest":
            self._digest_page(parts[1])
        elif get and parts == ["emails"]:
            self._emails(_page_number(query), feed=_value(query, "feed").strip(), search=_value(query, "q").strip())
        elif get and parts == ["full"]:
            self._full(removed=_value(query, "removed") == "1")
        elif get and parts == ["status"]:
            self._status()
        elif get and parts == ["add"]:
            self._add_link_page(query)
        elif method == "POST" and parts == ["add"]:
            self._add_link(form)
        elif len(parts) == 2 and parts[0] == "read" and get:
            self._read_page(parts[1])
        elif len(parts) == 2 and parts[0] == "read" and method == "POST":
            self._add(parts[1], form)
        elif len(parts) == 2 and parts[0] == "item" and get:
            self._item(parts[1], added=_value(query, "added") == "1")
        elif len(parts) == 3 and parts[0] == "item" and parts[2] == "remove" and method == "POST":
            self._remove(parts[1], form)
        else:
            self._send_text("Not found\n", HTTPStatus.NOT_FOUND)

    def _authenticate(self) -> str:
        token = self.headers.get(ACCESS_HEADER, "")
        if self.config.web.dev_insecure_no_auth and self.server.verify is None:
            return DEV_EMAIL
        verify = self.server.verify
        if not token or verify is None:
            return ""
        try:
            return verify(token)
        except AccessDenied as exc:
            logging.warning("Rejected web UI request: %s", exc)
            return ""

    # Pages

    def _index(self, page: int) -> None:
        now = datetime.now(self.config.gmail.tzinfo)
        digests = load_digests(self.store, self.config, now=now)
        in_feed, notice = self._full_ids()
        start = (page - 1) * DIGESTS_PER_PAGE
        shown = digests[start : start + DIGESTS_PER_PAGE]
        sections = [self._digest_section(digest, in_feed) for digest in shown]
        body = notice
        if page == 1:
            body += self._today_section(now, in_feed)
        body += "\n".join(sections) if sections else '<p class="meta">No digests yet.</p>'
        body += _pager("/", page, has_more=start + DIGESTS_PER_PAGE < len(digests))
        self._send_page("Digests", body, cacheable=True, active="/")

    def _today_section(self, now: datetime, in_feed: set[str]) -> str:
        _, closes, newsletters, problems = today_so_far(self.store, self.config, now=now)
        count = len(newsletters)
        meta = (
            f"{count} newsletter{'' if count == 1 else 's'} so far; "
            f"this digest publishes {escape(clock(closes))}."
        )
        entries = "\n".join(
            f'<div class="entry">{self._newsletter_entry(newsletter, in_feed)}</div>' for newsletter in newsletters
        )
        missed = (
            '<h3 class="section">Not summarized</h3><ul class="missed">'
            + "".join(f"<li>{missed_message_html(message, '')}</li>" for message in problems)
            + "</ul>"
            if problems
            else ""
        )
        return f"""<section class="today" id="today">
  <h2>Today so far</h2>
  <p class="meta">{meta}</p>
  {entries or '<p class="meta">Nothing yet.</p>'}
  {missed}
</section>"""

    def _digest_section(self, digest: DailyDigest, in_feed: set[str]) -> str:
        return f"""<section>
  <h2><a href="/digest/{digest.day.isoformat()}">{escape(long_date(digest.day))}</a></h2>
  <p class="meta">{_digest_counts(digest)}</p>
  {digest_body(digest, site="", entry=lambda newsletter: self._newsletter_entry(newsletter, in_feed), section_tag="h3")}
</section>"""

    def _digest_page(self, value: str) -> None:
        try:
            day = date.fromisoformat(value)
        except ValueError:
            self._send_text("Not found\n", HTTPStatus.NOT_FOUND)
            return
        now = datetime.now(self.config.gmail.tzinfo)
        digest = next((d for d in load_digests(self.store, self.config, now=now) if d.day == day), None)
        if digest is None:
            self._send_text("Not found\n", HTTPStatus.NOT_FOUND)
            return
        in_feed, notice = self._full_ids()
        body = f"""{notice}<p class="meta">{_digest_counts(digest)}</p>
{digest_body(digest, site="", entry=lambda newsletter: self._newsletter_entry(newsletter, in_feed))}"""
        self._send_page(long_date(day), body, cacheable=True)

    def _newsletter_entry(self, newsletter: NewsletterSummary, in_feed: set[str]) -> str:
        actions = []
        gmail_id = newsletter.source_gmail_id
        if GMAIL_ID_PATTERN.match(gmail_id):
            actions.append(link(gmail_link(gmail_id), "Gmail", new_tab=True))
            if gmail_id in in_feed:
                actions.append(f'<a href="/item/{gmail_id}">On reading list ✓</a>')
            else:
                actions.append(self._form(f"/read/{gmail_id}", "add", gmail_id, ADD_LABEL, quiet=True))
        return newsletter_entry(newsletter, site="", actions=actions, new_tab=True)

    def _emails(self, page: int, *, feed: str = "", search: str = "") -> None:
        # A few hundred rows a year: filter in Python so the feed list and
        # search work the same on SQLite and PostgreSQL.
        rows = self.store.list_messages(EMAILS_SCAN_LIMIT, 0)
        summaries = {summary.source_gmail_id: summary for summary in load_summaries(self.store, self.config)}
        feeds: dict[str, int] = {}
        for row in rows:
            name = sender_name(str(row["sender"]))
            if name:
                feeds[name] = feeds.get(name, 0) + 1
        if feed:
            rows = [row for row in rows if sender_name(str(row["sender"])) == feed]
        if search:
            needle = search.casefold()
            rows = [row for row in rows if needle in _search_text(row, summaries.get(str(row["gmail_id"])))]
        offset = (page - 1) * EMAILS_PER_PAGE
        page_rows = rows[offset : offset + EMAILS_PER_PAGE]
        in_feed, notice = self._full_ids()
        items = [self._email_row(row, summaries.get(str(row["gmail_id"])), in_feed) for row in page_rows]
        options = "".join(
            f'<option value="{escape(name)}"{" selected" if name == feed else ""}>{escape(name)} ({count})</option>'
            for name, count in sorted(feeds.items(), key=lambda pair: pair[0].lower())
        )
        clear = ' <a href="/emails" class="tap">Clear</a>' if feed or search else ""
        filters = f"""<form class="filters" method="get" action="/emails" role="search">
  <input type="search" name="q" value="{escape(search)}" placeholder="Search titles, key points, publications" aria-label="Search">
  <select name="feed" aria-label="Newsletter"><option value="">All newsletters</option>{options}</select>
  <button type="submit" class="quiet">Filter</button>{clear}
</form>
<p class="meta">{len(rows)} email{"" if len(rows) == 1 else "s"}</p>"""
        empty = '<p class="meta">No matching emails.</p>' if feed or search else '<p class="meta">No emails yet.</p>'
        body = notice + filters + (f'<ul class="emails">{"".join(items)}</ul>' if items else empty)
        query = "&".join(f"{key}={quote(value)}" for key, value in (("feed", feed), ("q", search)) if value)
        body += _pager(f"/emails?{query}" if query else "/emails", page, has_more=len(rows) > offset + EMAILS_PER_PAGE)
        self._send_page("Emails", body, cacheable=True, active="/emails")

    def _email_row(self, row: dict, summary: NewsletterSummary | None, in_feed: set[str]) -> str:
        gmail_id = str(row["gmail_id"])
        status = str(row["status"])
        valid = bool(GMAIL_ID_PATTERN.match(gmail_id))
        readable = valid and status != "skipped_no_body"
        actions = []
        if valid:
            actions.append(link(gmail_link(gmail_id), "Gmail", new_tab=True))
            if gmail_id in in_feed:
                actions.append(f'<a href="/item/{gmail_id}">On reading list ✓</a>')
            elif readable:
                actions.append(self._form(f"/read/{gmail_id}", "add", gmail_id, ADD_LABEL, quiet=True))
        # Only problems need explaining; "summarized" rows speak for themselves.
        detail = (
            f'<p class="meta">{escape(str(row["detail"]))}</p>'
            if row.get("detail") and status != "summarized"
            else ""
        )
        badge = (
            ""
            if status == "summarized"
            else f'<span class="status status-{escape(status)}">{escape(STATUS_LABELS.get(status, status))}</span> '
        )
        title = (summary.title if summary else "") or str(row["subject"]) or "(no subject)"
        heading = f'<a href="/item/{gmail_id}">{escape(title)}</a>' if readable else escape(title)
        meta = [
            summary.publication if summary and summary.publication else sender_name(str(row["sender"])),
            self._local_time(str(row["received_at"])),
            reading_time(summary.word_count) if summary else "",
        ]
        return f"""<li>
  <p class="title">{heading}</p>
  <p class="meta">{badge}{" · ".join(escape(part) for part in meta if part)}</p>
  {detail}
  <div class="actions">{" ".join(actions)}</div>
</li>"""

    def _full(self, *, removed: bool = False) -> None:
        items = self.store.list_full_items(FULL_PAGE_LIMIT)
        secret = self.server.secret
        notice = f'<p class="notice">Removed. {escape(FRESHRSS_REMOVAL_NOTE)}</p>' if removed else ""
        rows = [
            f"""<li>
  <p class="title"><a href="/item/{escape(item.gmail_id)}">{escape(item.title)}</a></p>
  <p class="meta">{'<span class="status">Web</span> ' if item.is_web else ""}{full_item_byline(item)} · added {escape(self._local_time(item.added_at))}</p>
  <div class="actions">{link(item.url, "Original", new_tab=True) if item.is_web else link(item.source_gmail_url, "Gmail", new_tab=True)}
    {self._form(f"/item/{item.gmail_id}/remove", "remove", item.gmail_id, "Remove", danger=True)}</div>
</li>"""
            for item in items
        ]
        body = f"""{notice}{self._add_link_panel()}<section class="panel">
  <h2>Feeds</h2>
  <p>Subscribe to these in FreshRSS or NetNewsWire. Keep the URLs private: anyone with one can read that feed.</p>
  {_copy_field("digest-url", f"{self.config.rss.title} (one summary per day)", digest_feed_url(self.config, secret))}
  {_copy_field("full-url", f"{FULL_FEED_TITLE} (what you add, in full)", full_feed_url(self.config, secret))}
  {_copy_field("all-url", f"{ALL_FEED_TITLE} (every newsletter, in full)", all_feed_url(self.config, secret))}
</section>
<h2>On your reading list</h2>
<p class="meta">{escape(FRESHRSS_REMOVAL_NOTE)}</p>
<ul class="emails">{"".join(rows) if rows else '<li class="meta">Nothing on your reading list yet.</li>'}</ul>"""
        self._send_page("Reading list", body, script=COPY_SCRIPT, cacheable=True, active="/full")

    def _add_link_panel(self) -> str:
        """The "Add a link" box, the desktop bookmarklet, and iPhone Shortcut instructions."""
        base = self.config.web.public_url.rstrip("/")
        add_prefix = f"{base}/add?url="
        bookmarklet = f"javascript:location.href='{add_prefix}'+encodeURIComponent(location.href)"
        unavailable = (
            f'<p class="error">{escape(WEB_MIGRATION_HINT)}</p>' if not self.store.web_items_available() else ""
        )
        return f"""<section class="panel" id="add-link">
  <h2>Add a link</h2>
  <p>Save any web page to {escape(FULL_FEED_TITLE)}: the article, its key points, and its images.</p>
  {unavailable}
  <form class="filters" method="post" action="/add">
    <input type="text" inputmode="url" name="url" required placeholder="https://…" aria-label="Web page URL" autocomplete="off">
    <input type="hidden" name="csrf" value="{self._add_link_token()}">
    <button type="submit">Add</button>
  </form>
  <details>
    <summary>From any page</summary>
    <p><strong>Desktop:</strong> drag this bookmarklet to your bookmarks bar, then click it on any page:
      <a class="tap" href="{escape(bookmarklet)}">Add to reading list</a></p>
    <p><strong>iPhone:</strong> in Shortcuts, make a new shortcut and turn on <em>Show in Share Sheet</em> (receiving URLs and text).
      Add <em>URL Encode</em> on <em>Shortcut Input</em>, then <em>Open URLs</em> with the address below followed by the
      <em>URL Encoded Text</em>. Then share any page to that shortcut.</p>
    {_copy_field("add-url-prefix", "Shortcut address (add the URL-encoded Shortcut Input at the end)", add_prefix)}
  </details>
</section>"""

    def _add_link_token(self) -> str:
        # Not bound to the URL: the box on /full posts whatever is typed.
        return csrf_token(self.server.secret, self._email, "add-link", "")

    def _add_link_page(self, query: dict[str, list[str]]) -> None:
        """GET /add?url=…: no side effects. The page submits its own POST once visible, like /read."""
        raw = _value(query, "url") or _value(query, "text")
        url = first_url(raw)
        if not url:
            self._send_page(
                "Add a link",
                '<p class="error">That does not contain a web link (http or https).</p>' + self._add_link_panel(),
                HTTPStatus.BAD_REQUEST,
                script=COPY_SCRIPT,
            )
            return
        saved = self._saved_web_item(url)
        heading = escape(saved.title) if saved else "Add a link"
        status = (
            f"Already on {escape(FULL_FEED_TITLE)}; opening it…"
            if saved
            else f"Adding it to {escape(FULL_FEED_TITLE)}…"
        )
        body = f"""<section class="panel">
  <h2>{heading}</h2>
  <p class="meta">{escape(url)}</p>
  <p>{status}</p>
  <form id="add-form" method="post" action="/add">
    <input type="hidden" name="url" value="{escape(url)}">
    <input type="hidden" name="csrf" value="{self._add_link_token()}">
    <button type="submit">{escape(ADD_LABEL)}</button>
  </form>
  <p class="meta">{link(url, "Open the original", new_tab=True)}</p>
</section>"""
        self._send_page("Add a link", body, script=AUTO_SUBMIT_SCRIPT)

    def _add_link(self, form: dict[str, list[str]]) -> None:
        """POST /add: fetch, extract, sanitize, and store a web page; key points follow in the background."""
        if not self._post_allowed(form, "add-link", ""):
            return
        url = first_url(_value(form, "url"))
        if not url:
            self._add_link_error("That does not contain a web link (http or https).", "", HTTPStatus.BAD_REQUEST)
            return
        if not self.store.web_items_available():
            self._add_link_error(WEB_MIGRATION_HINT, url, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        saved = self._saved_web_item(url)
        if saved is not None:
            self._redirect(f"/item/{saved.gmail_id}?added=1")
            return
        try:
            fetched = self.server.fetch_page(url)
        except FetchError as exc:
            # The URL is not logged: it may carry a token.
            logging.info("Could not fetch a page to add (%s): %s", exc.reason, exc)
            self._add_link_error(fetch_error_message(exc), url, HTTPStatus.BAD_GATEWAY)
            return
        article = extract(decode_html(fetched.data, fetched.charset), fetched.url)
        item_id = article.web_id
        if self.store.get_full_item(item_id) is not None:
            # Another URL for a page already saved (same canonical URL).
            self._redirect(f"/item/{item_id}?added=1")
            return
        stored = self._store_web_content(item_id, article)
        try:
            self.store.add_full_item(
                FullItem(
                    gmail_id=item_id,
                    title=article.title,
                    url=article.canonical_url,
                    publication=article.site_name,
                    authors=[article.byline] if article.byline else [],
                    content_access="partial" if article.thin else "full",
                    source_gmail_url="",
                    bullets=[],
                    content_html="" if stored else article.content_html,
                    email_received_at=article.published,
                    added_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
                    source_kind="web",
                    source_url=normalized_url(url),
                )
            )
        except Exception:
            if stored:
                self._forget_web_content(item_id)
            raise
        logging.info("Added web page %s (%s words%s) to the reading list", item_id, article.words,
                     ", thin" if article.thin else "")
        self.server.background(self._summarize_later(item_id, article))
        self._redirect(f"/item/{item_id}?added=1")

    def _summarize_later(self, item_id: str, article: WebArticle) -> Callable[[], None]:
        """Key points for a saved page, then a FreshRSS refresh. A model failure leaves the item without them."""
        config = self.config
        summarize = self.server.summarize_web
        refresh = self._refresh_reader

        def job() -> None:
            try:
                summary = summarize(article)
                if summary is None:
                    logging.warning("The summarizer returned nothing for web page %s", item_id)
                else:
                    partial = article.thin or summary.content_access == "partial"
                    with Store(config.storage.sqlite_path) as store:
                        store.update_full_item_summary(
                            item_id,
                            bullets=summary.bullets,
                            content_access="partial" if partial else "full",
                            authors=None if article.byline else summary.authors,
                        )
            except Exception:
                logging.exception("Could not summarize web page %s; it stays on the reading list without key points", item_id)
            refresh()

        return job

    def _store_web_content(self, item_id: str, article: WebArticle) -> bool:
        """Keep the extracted article in email_content, so a sanitizer bump can re-derive it."""
        try:
            self.store.save_content(
                StoredContent(
                    gmail_id=item_id,
                    subject=article.title,
                    sender=article.site_name,
                    received_at=article.published,
                    raw_html=article.raw_html,
                    raw_text="",
                    content_html=article.content_html,
                    sanitizer_version=SANITIZER_VERSION,
                    word_count=article.words,
                )
            )
        except StoreFeatureUnavailable:
            return False
        return True

    def _forget_web_content(self, item_id: str) -> None:
        try:
            self.store.delete_content(item_id)
        except StoreFeatureUnavailable:
            pass
        except Exception:
            logging.exception("Could not delete the stored content of %s", item_id)

    def _saved_web_item(self, url: str) -> FullItem | None:
        source = normalized_url(url)
        try:
            return self.store.find_web_item(web_id(source), source)
        except StoreFeatureUnavailable:
            return None

    def _add_link_error(self, message: str, url: str, status: HTTPStatus) -> None:
        original = f'<p>{link(url, "Open the original", new_tab=True)}</p>' if url else ""
        self._send_page(
            "Could not add",
            f"""<p class="error">{escape(message)}</p>
<p class="meta">Nothing was saved.</p>
{original}
<p>{link("/full", "Back to the reading list")}</p>""",
            status,
        )

    def _status(self) -> None:
        now = datetime.now(UTC)
        status = gather_status(self.store, self.config, now=now, max_attempts=MAX_SUMMARY_ATTEMPTS)
        run = status.run
        warnings = (
            '<ul class="warnings">' + "".join(f"<li>{escape(warning)}</li>" for warning in status.warnings) + "</ul>"
            if status.warnings
            else '<p class="ok">No problems.</p>'
        )
        run_rows = [
            ("Last run", f"{run.get('status', 'never')} · started {self._local_time(str(run.get('started_at') or ''))}"
             if run else "never"),
            ("Finished", self._local_time(str(run.get("finished_at") or "")) or "none"),
            ("Last success", self._local_time(str(run.get("last_success") or "")) or "never"),
            ("Checkpoint", self._local_time(str(run.get("last_window_end") or "")) or "none"),
            ("Retrying from", self._local_time(str(run.get("retry_from") or "")) or "none"),
            ("Last published digest", status.last_published_day or "none"),
            ("Feed alert", status.alert.title if status.alert else "none"),
        ]
        refresh = status.refresh
        run_rows.append(
            (
                "FreshRSS refresh ping",
                (f"{'ok' if refresh.get('ok') else 'failed'} ({refresh.get('detail', '')}) at "
                 f"{self._local_time(str(refresh.get('at') or ''))}")
                if refresh
                else ("not configured" if self.server.refresh_reader is None else "not sent yet"),
            )
        )
        cache = ImageCache(self.config.images.cache_dir, 1)
        files, size = cache.stats() if self.config.images.proxy else (0, 0)
        run_rows.append(
            ("Image proxy", f"on · {files} cached, {size / 1_048_576:.1f} MB" if self.server.rewrite_image else "off")
        )
        table = "".join(f"<tr><th scope=row>{escape(name)}</th><td>{escape(value)}</td></tr>" for name, value in run_rows)
        problems = "".join(
            f"""<li><a href="/item/{escape(problem['gmail_id'])}">{escape(problem['subject'] or problem['gmail_id'])}</a>
  <span class="meta">{escape(sender_name(problem['sender']))} · {escape(STATUS_LABELS.get(problem['status'], problem['status']))}
  · {problem['attempts']} attempt(s){' · <strong>given up</strong>' if problem['gave_up'] else ''}
  · {escape(self._local_time(problem['updated_at']))}</span>
  <p class="meta">{escape(problem['detail'])}</p></li>"""
            for problem in status.problems
        )
        migrations = "".join(
            f"<tr><th scope=row>{escape(feature)}</th><td>{'installed' if installed else 'missing: apply ' + escape(migration)}</td></tr>"
            for feature, migration, installed in status.migrations
        )
        body = f"""{warnings}
<table class="facts">{table}</table>
<h2>Failures in the last 14 days</h2>
{f'<ul class="missed">{problems}</ul>' if problems else '<p class="meta">None.</p>'}
<h2>Database migrations</h2>
<table class="facts">{migrations}</table>"""
        self._send_page("Status", body, cacheable=True, active="/status")

    def _read_page(self, gmail_id: str) -> None:
        """GET never changes state; the page submits its own POST once visible."""
        if not self._known(gmail_id):
            return
        if gmail_id in self._full_ids()[0]:
            self._redirect(f"/item/{gmail_id}")
            return
        summary = self.store.get_summary_by_gmail_id(gmail_id)
        title = summary.title if summary else "this newsletter"
        body = f"""<section class="panel">
  <h2>{escape(title)}</h2>
  <p>Adding it to {escape(FULL_FEED_TITLE)}…</p>
  <form id="add-form" method="post" action="/read/{gmail_id}">
    <input type="hidden" name="csrf" value="{csrf_token(self.server.secret, self._email, "add", gmail_id)}">
    <button type="submit">{escape(ADD_LABEL)}</button>
  </form>
  <p class="meta">{link(f"/item/{gmail_id}", "Just read it")} · {link(gmail_link(gmail_id), "Open in Gmail", new_tab=True)}</p>
</section>"""
        self._send_page("Read in full", body, script=AUTO_SUBMIT_SCRIPT)

    def _add(self, gmail_id: str, form: dict[str, list[str]]) -> None:
        if not self._post_allowed(form, "add", gmail_id):
            return
        if not self._known(gmail_id):
            return
        if self.store.get_full_item(gmail_id):
            self._redirect(f"/item/{gmail_id}?added=1")
            return
        resolved = self._content(gmail_id)
        if resolved is None:
            self._send_page(
                "Could not add",
                f"""<p class="error">Gmail could not provide this email, so nothing was added.</p>
<p>{link(gmail_link(gmail_id), "Open it in Gmail", new_tab=True)}</p>""",
                HTTPStatus.BAD_GATEWAY,
            )
            return
        html, stored, subject, sender, received_at = resolved
        summary = self._summary(gmail_id)
        self.store.add_full_item(
            FullItem(
                gmail_id=gmail_id,
                title=(summary.title if summary else "") or subject,
                url=original_url(summary.url if summary else "", self.config.web.public_url),
                publication=(summary.publication if summary else "") or sender_name(sender),
                authors=summary.authors if summary else [],
                content_access=summary.content_access if summary else "full",
                source_gmail_url=gmail_link(gmail_id),
                bullets=summary.bullets if summary else [],
                # The content store holds the body; full_items keeps its own
                # copy only when that store is not installed yet.
                content_html="" if stored else html,
                email_received_at=received_at,
                added_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            )
        )
        logging.info("Added %s to the reading list", gmail_id)
        self._refresh_reader()
        self._redirect(f"/item/{gmail_id}?added=1")

    def _item(self, gmail_id: str, *, added: bool) -> None:
        """The reading view for any processed email; instant when its content is stored."""
        if is_web_id(gmail_id):
            self._web_item(gmail_id, added=added)
            return
        if not GMAIL_ID_PATTERN.match(gmail_id):
            self._send_text("Not found\n", HTTPStatus.NOT_FOUND)
            return
        full_item = self._full_item(gmail_id)
        # Anything that may reach Gmail passes the known-ID gate first; only a
        # reading list item's own stored copy is shown without it.
        if not (full_item is not None and full_item.content_html) and not self._known(gmail_id):
            return
        resolved = self._content(gmail_id, legacy=full_item)
        if resolved is None:
            self._send_page(
                "Could not open",
                f"""<p class="error">This email's content is not stored and Gmail could not provide it.</p>
<p>{link(gmail_link(gmail_id), "Open it in Gmail", new_tab=True)}</p>""",
                HTTPStatus.BAD_GATEWAY,
            )
            return
        html, _, subject, sender, received_at = resolved
        summary = self._summary(gmail_id)
        title = (summary.title if summary else "") or (full_item.title if full_item else "") or subject
        publication = (
            (summary.publication if summary else "") or (full_item.publication if full_item else "") or sender_name(sender)
        )
        authors = summary.authors if summary else full_item.authors if full_item else []
        bullets = summary.bullets if summary else full_item.bullets if full_item else []
        content_access = summary.content_access if summary else full_item.content_access if full_item else "full"
        original = original_url(summary.url if summary else full_item.url if full_item else "", self.config.web.public_url)
        notice = (
            f'<p class="notice">Added to your reading list. {"It will appear in FreshRSS in a moment." if self.server.refresh_reader else "FreshRSS shows it within about half an hour."}</p>'
            if added
            else ""
        )
        points = (
            '<details class="keypoints" open><summary>Key points</summary><ul>'
            + "".join(f"<li>{escape(bullet)}</li>" for bullet in bullets)
            + "</ul></details>"
            if bullets
            else ""
        )
        links = []
        if original:
            links.append(link(original, "Original", new_tab=True))
        links.append(link(gmail_link(gmail_id), "Gmail", new_tab=True))
        if full_item is not None:
            links.append(self._form(f"/item/{gmail_id}/remove", "remove", gmail_id, "Remove from reading list", danger=True))
        else:
            links.append(self._form(f"/read/{gmail_id}", "add", gmail_id, ADD_LABEL, quiet=True))
        words = summary.word_count if summary and summary.word_count else 0
        byline = " · ".join(
            escape(part)
            for part in (", ".join(authors), self._local_date(received_at), reading_time(words))
            if part
        )
        body = f"""{notice}
<div class="reader">
  <header class="post-head">
    {f'<p class="kicker">{escape(publication)}</p>' if publication else ""}
    <h1 class="post-title">{escape(title)}</h1>
    <p class="byline">{byline}</p>
    <div class="post-actions">{'<span aria-hidden="true">·</span>'.join(links)}</div>
  </header>
  {excerpt_note(content_access)}
  {points}
  <article class="post">{clean_html(drop_leading_title(html, title), self.server.rewrite_image)}</article>
</div>"""
        self._send_page(title, body, heading=False, cacheable=True)

    def _web_item(self, item_id: str, *, added: bool) -> None:
        """The reading view for a saved web page; only the store is read, never Gmail."""
        item = self._full_item(item_id)
        if item is None:
            self._send_text("Not found\n", HTTPStatus.NOT_FOUND)
            return
        content = current_content(self.store, item_id)
        html = content.content_html if content is not None else item.content_html
        notice = ""
        if added:
            later = " Key points will appear in a moment." if not item.bullets else ""
            notice = f'<p class="notice">Added to your reading list.{later}</p>'
        points = (
            '<details class="keypoints" open><summary>Key points</summary><ul>'
            + "".join(f"<li>{escape(bullet)}</li>" for bullet in item.bullets)
            + "</ul></details>"
            if item.bullets
            else ""
        )
        links = [
            link(item.url, "Original", new_tab=True),
            self._form(f"/item/{item_id}/remove", "remove", item_id, "Remove from reading list", danger=True),
        ]
        words = content.word_count if content is not None else 0
        byline = " · ".join(
            escape(part)
            for part in (", ".join(item.authors), self._local_date(item.email_received_at), reading_time(words))
            if part
        )
        body = f"""{notice}
<div class="reader">
  <header class="post-head">
    <p class="kicker"><span class="status">Web</span> {escape(item.publication)}</p>
    <h1 class="post-title">{escape(item.title)}</h1>
    <p class="byline">{byline}</p>
    <div class="post-actions">{'<span aria-hidden="true">·</span>'.join(links)}</div>
  </header>
  {excerpt_note(item.content_access, web=True)}
  {points}
  <article class="post">{clean_html(drop_leading_title(html, item.title), self.server.rewrite_image)}</article>
</div>"""
        self._send_page(item.title, body, heading=False, cacheable=True)

    def _remove(self, gmail_id: str, form: dict[str, list[str]]) -> None:
        web = is_web_id(gmail_id)
        if not (web or GMAIL_ID_PATTERN.match(gmail_id)):
            self._send_text("Not found\n", HTTPStatus.NOT_FOUND)
            return
        if not self._post_allowed(form, "remove", gmail_id):
            return
        self.store.delete_full_item(gmail_id)
        if web:
            # A web page's stored content exists only for the reading list.
            self._forget_web_content(gmail_id)
        self._refresh_reader()
        self._redirect("/full?removed=1")

    # Helpers

    def _content(
        self, gmail_id: str, *, legacy: FullItem | None = None
    ) -> tuple[str, bool, str, str, str] | None:
        """(sanitized html, stored in email_content, subject, sender, received_at), or None.

        Stored content first; then a reading list item's own copy (from before
        content was stored); then Gmail, storing what it returns. Callers must
        have checked that the ID is known.
        """
        content = current_content(self.store, gmail_id)
        if content is not None:
            return content.content_html, True, content.subject, content.sender, content.received_at
        if legacy is not None and legacy.content_html:
            return legacy.content_html, False, legacy.title, legacy.publication, legacy.email_received_at
        try:
            message = self.server.fetch(gmail_id)
            if message is None:
                raise ValueError("The email has no readable body")
        except Exception:
            logging.exception("Could not fetch Gmail message %s", gmail_id)
            return None
        content = content_from_message(message)
        stored = True
        try:
            self.store.save_content(content)
        except StoreFeatureUnavailable:
            stored = False
        except Exception:
            logging.exception("Could not store the content of %s", gmail_id)
            stored = False
        return content.content_html, stored, content.subject, content.sender, content.received_at

    def _summary(self, gmail_id: str) -> NewsletterSummary | None:
        """The email's summary, with its stable publication name."""
        return next(
            (summary for summary in load_summaries(self.store, self.config) if summary.source_gmail_id == gmail_id),
            None,
        )

    def _full_item(self, gmail_id: str) -> FullItem | None:
        try:
            return self.store.get_full_item(gmail_id)
        except StoreFeatureUnavailable:
            return None

    def _refresh_reader(self) -> None:
        if self.server.refresh_reader is None:
            return
        try:
            self.server.refresh_reader()
        except Exception:
            logging.exception("Could not ask FreshRSS to refresh the reading list feed")

    def _known(self, gmail_id: str) -> bool:
        """Only emails this service processed may be read: the Gmail credential sees the whole mailbox."""
        if GMAIL_ID_PATTERN.match(gmail_id) and self.store.is_known_gmail_id(gmail_id):
            return True
        self._send_text("Not found\n", HTTPStatus.NOT_FOUND)
        return False

    def _post_allowed(self, form: dict[str, list[str]], action: str, gmail_id: str) -> bool:
        origin = (self.headers.get("Origin") or "").lower()
        expected = csrf_token(self.server.secret, self._email, action, gmail_id)
        if origin != self.server.expected_origin or not hmac.compare_digest(_value(form, "csrf"), expected):
            self._send_text("Forbidden\n", HTTPStatus.FORBIDDEN)
            return False
        return True

    def _full_ids(self) -> tuple[set[str], str]:
        try:
            return self.store.full_item_ids(), ""
        except StoreFeatureUnavailable:
            return set(), f'<p class="error">{escape(MIGRATION_HINT)}</p>'

    def _form(
        self, action: str, csrf_action: str, gmail_id: str, label: str, *, danger: bool = False, quiet: bool = False
    ) -> str:
        token = csrf_token(self.server.secret, self._email, csrf_action, gmail_id)
        css = ' class="link danger"' if danger else ' class="quiet"' if quiet else ""
        return f"""<form method="post" action="{escape(action)}">
      <input type="hidden" name="csrf" value="{token}"><button type="submit"{css}>{escape(label)}</button></form>"""

    def _local_time(self, value: str) -> str:
        try:
            moment = datetime.fromisoformat(value).astimezone(self.config.gmail.tzinfo)
        except ValueError:
            return value
        return f"{moment:%a %b} {moment.day}, {moment.year} {moment.hour % 12 or 12}:{moment:%M %p}"

    def _local_date(self, value: str) -> str:
        try:
            moment = datetime.fromisoformat(value).astimezone(self.config.gmail.tzinfo)
        except ValueError:
            return value
        return f"{moment:%b} {moment.day}, {moment.year}"

    def _send_page(
        self,
        title: str,
        body: str,
        status: HTTPStatus = HTTPStatus.OK,
        script: str = "",
        *,
        heading: bool = True,
        cacheable: bool = False,
        active: str = "",
    ) -> None:
        self._cache_control = LIST_CACHE_CONTROL if cacheable else NO_STORE
        html = _layout(title, body, nonce=self._nonce, script=script, heading=heading, active=active)
        self._send_html(html, status)


def _digest_counts(digest: DailyDigest) -> str:
    count = digest.summary_count
    counts = f'{count} newsletter{"" if count == 1 else "s"}'
    if digest.not_summarized:
        counts += f", {len(digest.not_summarized)} not summarized"
    return counts


def _search_text(row: dict, summary: NewsletterSummary | None) -> str:
    """Subject, sender, and, for summarized emails, title, publication, authors, and key points."""
    parts = [str(row["subject"]), sender_name(str(row["sender"]))]
    if summary is not None:
        parts += [summary.title, summary.publication, *summary.authors, *summary.bullets, summary.summary]
    return " ".join(parts).casefold()


def _value(params: dict[str, list[str]], key: str) -> str:
    values = params.get(key)
    return values[0] if values else ""


def _page_number(query: dict[str, list[str]]) -> int:
    try:
        return max(1, min(int(_value(query, "page") or "1"), 10_000))
    except ValueError:
        return 1


def _pager(path: str, page: int, *, has_more: bool) -> str:
    links = []
    joiner = "&amp;" if "?" in path else "?"
    path = path.replace("&", "&amp;")
    if page > 1:
        links.append(f'<a class="tap" href="{path}{joiner}page={page - 1}">← Newer</a>')
    if has_more:
        links.append(f'<a class="tap" href="{path}{joiner}page={page + 1}">Older →</a>')
    return f'<nav class="pager" aria-label="Pages">{" ".join(links)}</nav>' if links else ""


def _copy_field(field_id: str, label: str, value: str) -> str:
    return f"""<label class="copy-label" for="{field_id}">{escape(label)}</label>
  <div class="copy-row"><input id="{field_id}" class="copy" readonly value="{escape(value)}">
  <button type="button" class="quiet" data-copy="{field_id}" aria-label="Copy {escape(label)}">Copy</button></div>"""


COPY_SCRIPT = """document.querySelectorAll("[data-copy]").forEach(function (button) {
  button.addEventListener("click", function () {
    var input = document.getElementById(button.dataset.copy);
    input.select();
    var done = function () { button.textContent = "Copied"; setTimeout(function () { button.textContent = "Copy"; }, 1500); };
    if (navigator.clipboard) navigator.clipboard.writeText(input.value).then(done, function () { document.execCommand("copy"); done(); });
    else { document.execCommand("copy"); done(); }
  });
});"""

# Submits the add form once the page is visible, so a link preview or a
# background tab cannot add anything.
AUTO_SUBMIT_SCRIPT = """(function () {
  var form = document.getElementById("add-form");
  function submitWhenVisible() {
    if (document.visibilityState !== "visible" || form.dataset.sent) return;
    form.dataset.sent = "1";
    form.submit();
  }
  if (document.visibilityState === "visible") submitWhenVisible();
  else document.addEventListener("visibilitychange", submitWhenVisible);
})();"""


def _nav(active: str) -> str:
    current = ' aria-current="page"'
    links = "".join(
        f'<a href="{href}"{current if href == active else ""}>{escape(label)}</a>' for href, label in NAV
    )
    return f'<nav aria-label="Main">{links}</nav>'


def _layout(title: str, body: str, *, nonce: str, script: str = "", heading: bool = True, active: str = "") -> str:
    script_tag = f'<script nonce="{nonce}">{script}</script>' if script else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} | Newsletters</title>
  <link rel="icon" type="image/png" href="/favicon.png">
  <link rel="apple-touch-icon" href="/favicon.png">
  <style>
    :root {{ color-scheme: light dark; --bg: #fbfbf9; --fg: #1d1e20; --text: #2b2c2e; --muted: #6b6f68; --line: #e3e3de; --accent: #245b4f; --card: #fff; --danger: #a23b34; --soft: #f1f1ec; --tap: 44px; }}
    @media (prefers-color-scheme: dark) {{ :root {{ --bg: #151615; --fg: #ececE7; --text: #d9dad4; --muted: #9a9e96; --line: #2e302b; --accent: #7cc4ad; --card: #1c1d1b; --danger: #e0817a; --soft: #20211f; }} }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; -webkit-text-size-adjust: 100%; }}
    body > header {{ border-bottom: 1px solid var(--line); background: var(--card); }}
    body > header div, main {{ max-width: 760px; margin: 0 auto; padding: 8px 16px; }}
    main {{ padding-top: 14px; padding-bottom: 14px; }}
    body > header div {{ display: flex; flex-wrap: wrap; gap: 0 12px; align-items: center; }}
    body > header strong {{ font-size: 17px; margin-right: auto; }}
    body > header nav {{ display: flex; flex-wrap: wrap; gap: 0 4px; }}
    body > header nav a {{ color: var(--muted); text-decoration: none; font-size: 15px; display: inline-flex; align-items: center; min-height: var(--tap); padding: 0 8px; border-bottom: 2px solid transparent; }}
    body > header nav a:hover {{ color: var(--fg); }}
    body > header nav a[aria-current="page"] {{ color: var(--fg); border-bottom-color: var(--accent); }}
    a {{ color: var(--accent); }}
    h1 {{ font-size: 26px; line-height: 1.25; margin: 16px 0 20px; }}
    h2 {{ font-size: 19px; margin: 28px 0 4px; }}
    h2 a {{ color: inherit; text-decoration: none; }}
    h3 {{ font-size: 17px; line-height: 1.35; margin: 0 0 2px; }}
    h3 a {{ color: var(--fg); text-decoration: none; }}
    h3 a:hover {{ text-decoration: underline; }}
    h3.section, h2.section {{ font-size: 15px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin: 24px 0 4px; }}
    p {{ margin: 4px 0; overflow-wrap: anywhere; }}
    .meta {{ color: var(--muted); font-size: 14px; }}
    .error, .warnings {{ color: var(--danger); }}
    .ok {{ color: var(--accent); }}
    .notice {{ background: var(--soft); border-radius: 8px; padding: 10px 14px; font-size: 15px; margin: 8px 0 20px; }}
    .today {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 4px 16px 8px; margin: 8px 0 16px; }}
    .today h2 {{ margin-top: 12px; }}
    .entry {{ border-bottom: 1px solid var(--line); padding: 14px 0; }}
    .entry:last-child {{ border-bottom: 0; }}
    .entry ul {{ margin: 8px 0; padding-left: 20px; }}
    .entry ul li {{ margin: 4px 0; }}
    .emails {{ padding: 0; list-style: none; margin: 0; }}
    .emails > li {{ border-bottom: 1px solid var(--line); padding: 12px 0; }}
    .title {{ font-weight: 650; font-size: 17px; }}
    .title a {{ color: var(--fg); text-decoration: none; }}
    .title a:hover {{ text-decoration: underline; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 0 12px; align-items: center; margin-top: 4px; font-size: 14px; }}
    .actions a, .post-actions a, a.tap {{ display: inline-flex; align-items: center; min-height: var(--tap); }}
    .actions form, .post-actions form {{ margin: 0; display: inline-flex; }}
    .panel {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 4px 16px 14px; margin: 16px 0; }}
    .status {{ display: inline-block; font-size: 12px; padding: 0 8px; border-radius: 10px; border: 1px solid var(--line); }}
    .status-summary_failed, .status-fetch_failed {{ color: var(--danger); }}
    button {{ font: inherit; font-size: 15px; min-height: var(--tap); padding: 0 16px; border-radius: 8px; border: 1px solid var(--accent); background: var(--accent); color: var(--card); cursor: pointer; }}
    button.quiet {{ background: transparent; color: var(--accent); border-color: var(--line); font-size: 14px; padding: 0 12px; border-radius: 999px; }}
    button.quiet:hover {{ border-color: var(--accent); }}
    button.link {{ background: none; border: 0; padding: 0; border-radius: 0; color: var(--muted); font-size: inherit; text-decoration: underline; text-decoration-color: var(--line); }}
    button.link.danger:hover {{ color: var(--danger); text-decoration-color: currentColor; }}
    .filters {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 8px; align-items: center; }}
    .filters input, .filters select {{ font: inherit; font-size: 16px; min-height: var(--tap); padding: 0 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--fg); }}
    .filters input {{ flex: 1 1 220px; min-width: 0; }}
    .filters select {{ flex: 0 1 260px; min-width: 0; }}
    .copy {{ flex: 1 1 auto; min-width: 0; width: 100%; min-height: var(--tap); font: 14px ui-monospace, monospace; padding: 0 8px; }}
    .copy-label {{ display: block; font-weight: 600; font-size: 14px; margin: 12px 0 4px; }}
    .copy-row {{ display: flex; gap: 8px; align-items: center; }}
    .missed {{ padding-left: 20px; }} .missed li {{ margin: 6px 0; }}
    .missed a {{ display: inline-block; min-height: 24px; }}
    .facts {{ border-collapse: collapse; width: 100%; font-size: 15px; margin: 12px 0; }}
    .facts th {{ text-align: left; font-weight: 600; padding: 6px 12px 6px 0; vertical-align: top; width: 40%; }}
    .facts td {{ padding: 6px 0; overflow-wrap: anywhere; }}
    .facts tr {{ border-bottom: 1px solid var(--line); }}
    .pager {{ display: flex; justify-content: space-between; margin: 24px 0; }}
    /* Reading view, modeled on Substack's post page. */
    .reader {{ max-width: 680px; margin: 0 auto; }}
    .post-head {{ margin: 24px 0 28px; padding-bottom: 12px; border-bottom: 1px solid var(--line); }}
    .kicker {{ color: var(--muted); font-size: 13px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; margin: 0 0 10px; }}
    .post-title {{ font-size: clamp(28px, 6vw, 38px); line-height: 1.15; letter-spacing: -0.01em; margin: 0 0 12px; }}
    .byline {{ color: var(--muted); font-size: 15px; margin: 0 0 4px; }}
    .post-actions {{ display: flex; flex-wrap: wrap; gap: 0 10px; align-items: center; color: var(--muted); font-size: 14px; }}
    .post-actions a {{ color: var(--muted); text-decoration-color: var(--line); }}
    .post-actions a:hover {{ color: var(--fg); }}
    .keypoints {{ background: var(--soft); border-radius: 10px; padding: 10px 16px; margin: 0 0 28px; font-size: 15px; }}
    .keypoints summary {{ cursor: pointer; color: var(--muted); font-weight: 600; min-height: var(--tap); display: flex; align-items: center; }}
    .keypoints ul {{ margin: 8px 0 4px; padding-left: 20px; }}
    article.post {{ font-family: Charter, "Bitstream Charter", "Sitka Text", Cambria, Georgia, serif; font-size: 19px; line-height: 1.65; color: var(--text); overflow-wrap: break-word; }}
    article.post p {{ margin: 0 0 1.25em; }}
    article.post h1, article.post h2, article.post h3, article.post h4 {{ font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--fg); line-height: 1.25; margin: 1.6em 0 .6em; }}
    article.post h1 {{ font-size: 1.5em; }} article.post h2 {{ font-size: 1.3em; }} article.post h3 {{ font-size: 1.12em; }}
    article.post a {{ color: inherit; text-decoration: underline; text-decoration-color: var(--accent); text-underline-offset: 2px; }}
    article.post img {{ display: block; max-width: 100%; height: auto; margin: 1.5em auto; border-radius: 4px; }}
    article.post figure {{ margin: 1.75em 0; }}
    article.post figure img {{ margin: 0 auto; }}
    article.post figcaption {{ font-family: ui-sans-serif, system-ui, sans-serif; color: var(--muted); font-size: 14px; text-align: center; margin-top: 8px; }}
    article.post ul, article.post ol {{ padding-left: 1.4em; margin: 0 0 1.25em; }}
    article.post li {{ margin: .35em 0; }}
    article.post blockquote {{ border-left: 3px solid var(--accent); margin: 1.5em 0; padding: 0 0 0 1em; color: var(--muted); font-style: italic; }}
    article.post hr {{ border: 0; border-top: 1px solid var(--line); margin: 2em 0; }}
    article.post pre {{ overflow-x: auto; font-size: 15px; background: var(--soft); padding: 12px; border-radius: 6px; }}
    /* Layout tables are flattened when stored; the tables left are data. A
       block-level table scrolls sideways instead of widening the page. */
    article.post table {{ display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; font-size: .85em; margin: 0 0 1.25em; }}
    article.post th, article.post td {{ border: 1px solid var(--line); padding: 4px 8px; vertical-align: top; }}
    @media (max-width: 600px) {{ article.post {{ font-size: 18px; }} }}
  </style>
</head>
<body>
<header><div><strong>Newsletters</strong>
  {_nav(active)}</div></header>
<main>
{f"<h1>{escape(title)}</h1>" if heading else ""}
{body}
</main>
{script_tag}
</body>
</html>"""
