"""The public listener (Cloudflare Tunnel at rss.<domain>): feeds, icons, and the image proxy.

Routes:
- /digest/<token>/feed.xml: the daily digest.
- /full/<token>/feed.xml: the reading list.
- /all/<token>/feed.xml: every summarized newsletter in full ("Newsletters: Everything").
- /img/<signature>/<base64url(url)>: the signed image proxy.
- /newsletters/<kind>/ and /newsletters/icons/<kind>.png: public feed home
  pages and icons.
- /favicon.ico.

Feed tokens derive from WEB_SECRET, are compared in constant time, and are
masked in logs, as are image URLs (they can carry subscriber tokens). Feeds
send ETag and Last-Modified and answer conditional requests with 304. Every
other path, including the retired /feed.xml, is 404: a redirect from
/feed.xml would hand out the secret URL.
"""
from __future__ import annotations

import hmac
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import AppConfig
from .digest import load_digests, load_summaries
from .email_content import current_content, with_stored_content
from .http_util import BaseHandler
from .image_proxy import ImageProxy, image_rewriter, verify_image_request
from .rss import (
    FEED_DESCRIPTIONS,
    FEED_KINDS,
    feed_icon_url,
    feed_page_title,
    feed_token,
    feed_url,
    latest,
    render_all_feed,
    render_digest_feed,
    render_full_text_feed,
    site_url,
    web_secret,
)
from .run_state import RunState, run_alert
from .store import Store, StoreFeatureUnavailable
from .web_ui import start_web_listener

STATIC_DIR = Path(__file__).with_name("static")
RSS_CONTENT_TYPE = "application/rss+xml; charset=utf-8"
# Proxied images never change; failures are retried after a short while.
IMAGE_CACHE_CONTROL = "public, max-age=31536000, immutable"
IMAGE_FAILURE_CACHE_CONTROL = "public, max-age=600"

# Feed URLs are bearer secrets and image URLs may carry subscriber tokens;
# never write one to a log.
_FEED_TOKEN = re.compile(r"/(full|digest|all)/[^/\s]*/")
_IMAGE_PATH = re.compile(r"/img/[^\s\"]*")


def mask_feed_tokens(text: str) -> str:
    return _IMAGE_PATH.sub("/img/<redacted>", _FEED_TOKEN.sub(r"/\1/<redacted>/", text))


# The name earlier versions used.
mask_full_feed_token = mask_feed_tokens


class PlatformHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], config: AppConfig, images: ImageProxy | None = None):
        super().__init__(server_address, PlatformHandler)
        self.config = config
        self.images = images or ImageProxy.from_config(config)


class PlatformHandler(BaseHandler):
    server: PlatformHTTPServer

    @property
    def config(self) -> AppConfig:
        return self.server.config

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._head(self._dispatch)

    def log_message(self, format: str, *args: Any) -> None:
        logging.info("%s - %s", self.address_string(), mask_feed_tokens(format % args))

    def _dispatch(self) -> None:
        self._response_started = False
        path = urlparse(self.path).path
        try:
            if path.startswith("/digest/"):
                self._feed(path, "digest", self._digest_feed)
            elif path.startswith("/full/"):
                self._feed(path, "full", self._full_feed)
            elif path.startswith("/all/"):
                self._feed(path, "all", self._all_feed)
            elif path.startswith("/img/"):
                self._image(path)
            elif path.startswith("/newsletters/"):
                self._feed_home(path)
            elif path == "/favicon.ico":
                self._send_icon("digest")
            else:
                self._not_found()
        except Exception:
            # Log the detail locally; do not echo internals such as filesystem
            # paths or SQL back to the client.
            logging.exception("Unhandled error serving %s %s", self.command, mask_feed_tokens(path))
            if not self._response_started:
                self._send_text("Internal server error\n", HTTPStatus.INTERNAL_SERVER_ERROR)

    def _feed(self, path: str, kind: str, render: Callable[[str], None]) -> None:
        """/<kind>/<token>/feed.xml, checked before the database is opened."""
        parts = path.strip("/").split("/")
        secret = web_secret(self.config)
        if (
            not secret
            or len(parts) != 3
            or parts[2] != "feed.xml"
            or not hmac.compare_digest(parts[1].encode("utf-8"), feed_token(secret, kind).encode("utf-8"))
        ):
            self._not_found()
            return
        render(secret)

    def _send_feed(self, payload: bytes, last_modified: datetime | None) -> None:
        self._send_conditional(payload, RSS_CONTENT_TYPE, last_modified=last_modified)

    def _digest_feed(self, secret: str) -> None:
        config = self.config
        now = datetime.now(config.gmail.tzinfo)
        # The database is opened only for feed requests, so an outage cannot
        # take down the icons and home pages.
        with Store(config.storage.sqlite_path) as store:
            digests = load_digests(store, config, now=now, max_digests=config.rss.max_items)
        alert = run_alert(RunState(config).data, now=now, timezone=config.gmail.tzinfo)
        feed = render_digest_feed(
            digests,
            title=config.rss.title,
            description=config.rss.description,
            self_url=feed_url(config, secret, "digest"),
            web_public_url=site_url(config) or "",
            alert=alert,
        )
        self._send_feed(feed, latest([*(digest.end for digest in digests), alert.published if alert else None]))

    def _full_feed(self, secret: str) -> None:
        if not self.config.web.enabled:
            self._not_found()
            return
        try:
            with Store(self.config.storage.sqlite_path) as store:
                items = with_stored_content(store, store.list_full_items(self.config.web.full_feed_max_items))
        except StoreFeatureUnavailable:
            logging.warning("Reading list feed requested before its tables exist")
            self._send_text("Service unavailable\n", HTTPStatus.SERVICE_UNAVAILABLE)
            return
        feed = render_full_text_feed(
            items,
            self_url=feed_url(self.config, secret, "full"),
            site_url=self.config.web.public_url,
            rewrite_image=image_rewriter(self.config, secret),
        )
        self._send_feed(feed, latest(_parse(item.added_at) for item in items))

    def _all_feed(self, secret: str) -> None:
        config = self.config
        with Store(config.storage.sqlite_path) as store:
            summaries = load_summaries(store, config)
            newest = sorted(summaries, key=lambda summary: summary.email_received_at, reverse=True)
            entries = []
            for summary in newest[: config.rss.all_feed_max_items]:
                content = current_content(store, summary.source_gmail_id) if summary.source_gmail_id else None
                entries.append((summary, content.content_html if content else None))
        feed = render_all_feed(
            entries,
            self_url=feed_url(config, secret, "all"),
            site=site_url(config),
            rewrite_image=image_rewriter(config, secret),
        )
        self._send_feed(feed, latest(summary.email_received_at for summary, _ in entries))

    def _image(self, path: str) -> None:
        """/img/<signature>/<base64url(url)>: only URLs this app signed are fetched."""
        parts = path.strip("/").split("/")
        secret = web_secret(self.config)
        url = (
            verify_image_request(secret, parts[1], parts[2])
            if len(parts) == 3 and self.config.images.proxy
            else None
        )
        if url is None:
            self._not_found()
            return
        image = self.server.images.get(url)
        if image is None:
            self._send_bytes(
                b"Image unavailable\n",
                "text/plain; charset=utf-8",
                HTTPStatus.NOT_FOUND,
                {"Cache-Control": IMAGE_FAILURE_CACHE_CONTROL},
            )
            return
        self._send_bytes(
            image.data,
            image.content_type,
            headers={
                "Cache-Control": IMAGE_CACHE_CONTROL,
                "X-Content-Type-Options": "nosniff",
                # Opened directly, an image can never run anything here.
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Cross-Origin-Resource-Policy": "cross-origin",
            },
        )

    def _feed_home(self, path: str) -> None:
        """Public home page and icon for each feed, so RSS readers can show distinct icons."""
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[1] == "icons":
            kind = parts[2].removesuffix(".png")
            if kind in FEED_KINDS and parts[2] == f"{kind}.png":
                self._send_icon(kind)
            else:
                self._not_found()
            return
        if len(parts) != 2 or parts[1] not in FEED_KINDS or not path.endswith("/"):
            self._not_found()
            return
        kind = parts[1]
        title = feed_page_title(self.config, kind)
        icon = feed_icon_url(self.config.rss.public_url, kind) or f"/newsletters/icons/{kind}.png"
        site = self.config.web.public_url if self.config.web.enabled else ""
        site_link = f'<p><a href="{escape(site)}">Open the newsletters site</a></p>' if site else ""
        self._send_html(
            f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<link rel="icon" type="image/png" href="{escape(icon)}">
<link rel="apple-touch-icon" href="{escape(icon)}">
<style>body{{font:17px/1.5 system-ui,sans-serif;max-width:560px;margin:15vh auto;padding:0 16px;color:#1d1e20}}
img{{width:72px;height:72px}}a{{color:#245b4f}}</style></head>
<body><img src="{escape(icon)}" alt=""><h1>{escape(title)}</h1><p>{escape(FEED_DESCRIPTIONS[kind])}</p>{site_link}</body></html>"""
        )

    def _send_icon(self, kind: str) -> None:
        payload = (STATIC_DIR / f"{kind}.png").read_bytes()
        self._send_bytes(payload, "image/png", headers={"Cache-Control": "public, max-age=604800"})


def _parse(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def serve_platform(config: AppConfig, host: str | None, port: int | None) -> None:
    address = (host or config.server.host, port or config.server.port)
    server = PlatformHTTPServer(address, config)
    if not web_secret(config):
        logging.error("$%s is not set: the feeds and the image proxy are not served", config.web.secret_env)
    # The private UI gets its own listener so the public port never serves it.
    start_web_listener(config)
    logging.info("Serving the public feeds at http://%s:%s/", address[0], address[1])
    server.serve_forever()
