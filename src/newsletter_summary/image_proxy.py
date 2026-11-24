"""Signed image proxy: newsletter images load through the app, not from senders.

Images in the reading view and in the reading list and everything feeds are
rewritten to /img/<signature>/<base64url(url)> on the public listener. Senders
then never see the reader's IP address or an open (most newsletter images are
tracked), and cached images survive link rot.

Only URLs the app emitted are fetched: the signature is
HMAC(WEB_SECRET, "image-proxy" NUL url). Fetching goes through safe_fetch and
is strict:

- http/https on ports 80/443 only, no credentials in the URL;
- every address the host resolves to must be public, and the socket's actual
  peer address is checked again after connecting, before any byte is sent, so
  DNS rebinding cannot reach private, loopback, or link-local addresses;
- redirects are followed by hand, at most MAX_REDIRECTS, each one re-checked;
- at most images.max_image_mb, within a total deadline;
- the response must say image/* and its bytes must be JPEG, PNG, GIF, WebP,
  or AVIF (never SVG, which can carry script).

Fetched images are cached on disk (images.cache_dir), bounded by
images.cache_max_mb with least-recently-used eviction.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import urllib3

from . import safe_fetch
from .config import AppConfig
from .safe_fetch import Resolver, resolve

SIGNATURE_HEX_CHARS = 32
MAX_URL_LENGTH = safe_fetch.MAX_URL_LENGTH
MAX_REDIRECTS = safe_fetch.MAX_REDIRECTS
TOTAL_DEADLINE_SECONDS = safe_fetch.TOTAL_DEADLINE_SECONDS
# Read at call time, so tests can point it at a local server.
ALLOWED_PORTS = safe_fetch.ALLOWED_PORTS
# Failed URLs are not retried for this long, so a dead host is not hammered.
FAILURE_TTL_SECONDS = 600
MAX_REMEMBERED_FAILURES = 2000
USER_AGENT = "newsletter-summary image proxy"
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "image/avif": "avif"}


# One error family for every guarded fetch; the old names stay for callers.
ImageFetchError = safe_fetch.FetchError
BlockedAddress = safe_fetch.BlockedAddress


# Signing


def image_signature(secret: str, url: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), b"image-proxy\0" + url.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:SIGNATURE_HEX_CHARS]


def proxied_image_url(base: str, secret: str, url: str) -> str:
    encoded = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{base}/img/{image_signature(secret, url)}/{encoded}"


def verify_image_request(secret: str, signature: str, encoded: str) -> str | None:
    """The image URL for a valid /img/<signature>/<encoded> request, else None."""
    if not secret or len(encoded) > MAX_URL_LENGTH * 2:
        return None
    try:
        url = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(signature.encode("utf-8"), image_signature(secret, url).encode("utf-8")):
        return None
    return url if _url_problem(url) is None else None


def image_rewriter(config: AppConfig, secret: str) -> Callable[[str], str] | None:
    """Rewrite image URLs to the proxy, or None when the proxy is off or has no public host."""
    from .rss import public_origin

    base = public_origin(config)
    if not config.images.proxy or not secret or not base:
        return None

    def rewrite(url: str) -> str:
        return proxied_image_url(base, secret, url) if _url_problem(url) is None else url

    return rewrite


# Fetching (the guards live in safe_fetch, shared with "Add a link")


def is_public_address(address: str) -> bool:
    return safe_fetch.is_public_address(address)


def _url_problem(url: str) -> str | None:
    return safe_fetch.url_problem(url, ALLOWED_PORTS)


def check_url(url: str, resolver: Resolver = resolve) -> None:
    """Raise unless url is http(s) to a host whose every address is public."""
    safe_fetch.check_url(url, resolver, ALLOWED_PORTS)


def sniff_image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    return None


def _is_image_type(media_type: str) -> bool:
    return media_type.startswith("image/") and "svg" not in media_type


@dataclass(frozen=True)
class Image:
    data: bytes
    content_type: str


def fetch_image(
    url: str,
    *,
    max_bytes: int,
    resolver: Resolver = resolve,
    manager: urllib3.PoolManager | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Image:
    fetched = safe_fetch.fetch(
        url,
        max_bytes=max_bytes,
        accept_type=_is_image_type,
        headers={"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif"},
        resolver=resolver,
        manager=manager,
        clock=clock,
        max_redirects=MAX_REDIRECTS,
        allowed_ports=ALLOWED_PORTS,
        deadline_seconds=TOTAL_DEADLINE_SECONDS,
    )
    content_type = sniff_image_type(fetched.data)
    if content_type is None:
        raise ImageFetchError("not an allowed image type")
    return Image(data=fetched.data, content_type=content_type)


# Cache


class ImageCache:
    """Images on disk, one file per URL, evicted least recently used beyond max_bytes."""

    def __init__(self, directory: Path, max_bytes: int):
        self.directory = directory
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def get(self, url: str) -> Image | None:
        key = self.key(url)
        for content_type, extension in EXTENSIONS.items():
            path = self.directory / f"{key}.{extension}"
            try:
                data = path.read_bytes()
            except OSError:
                continue
            try:
                # The access time that eviction orders by.
                os.utime(path)
            except OSError:
                pass
            return Image(data=data, content_type=content_type)
        return None

    def put(self, url: str, image: Image) -> None:
        extension = EXTENSIONS.get(image.content_type)
        if extension is None or len(image.data) > self.max_bytes:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.directory, prefix=".tmp-")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(image.data)
            os.replace(temporary, self.directory / f"{self.key(url)}.{extension}")
        except OSError:
            Path(temporary).unlink(missing_ok=True)
            raise
        self.evict()

    def evict(self) -> None:
        with self._lock:
            files = []
            for path in self.directory.iterdir():
                if path.name.startswith("."):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                files.append((stat.st_mtime, stat.st_size, path))
            total = sum(size for _, size, _ in files)
            if total <= self.max_bytes:
                return
            # Evict to 90% so the next few writes do not each rescan.
            target = int(self.max_bytes * 0.9)
            for _, size, path in sorted(files, key=lambda entry: entry[0]):
                if total <= target:
                    break
                path.unlink(missing_ok=True)
                total -= size

    def stats(self) -> tuple[int, int]:
        """(files, bytes) in the cache."""
        try:
            sizes = [path.stat().st_size for path in self.directory.iterdir() if not path.name.startswith(".")]
        except OSError:
            return 0, 0
        return len(sizes), sum(sizes)


class ImageProxy:
    """Cache first, then a guarded fetch; failures are remembered briefly."""

    def __init__(
        self,
        cache: ImageCache,
        *,
        max_bytes: int,
        fetch: Callable[[str], Image] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cache = cache
        self.max_bytes = max_bytes
        self._fetch = fetch or (lambda url: fetch_image(url, max_bytes=max_bytes))
        self._clock = clock
        self._failures: dict[str, float] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config: AppConfig) -> ImageProxy:
        return cls(
            ImageCache(config.images.cache_dir, config.images.cache_max_mb * 1024 * 1024),
            max_bytes=config.images.max_image_mb * 1024 * 1024,
        )

    def get(self, url: str) -> Image | None:
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        key = ImageCache.key(url)
        with self._lock:
            failed_until = self._failures.get(key)
            if failed_until is not None and failed_until > self._clock():
                return None
        try:
            image = self._fetch(url)
        except ImageFetchError as exc:
            # Never log the URL: image URLs often carry subscriber tokens.
            logging.info("Image proxy fetch failed: %s", exc)
            self._remember_failure(key)
            return None
        except Exception:
            logging.exception("Image proxy fetch failed unexpectedly")
            self._remember_failure(key)
            return None
        try:
            self.cache.put(url, image)
        except OSError:
            logging.exception("Could not cache a proxied image")
        return image

    def _remember_failure(self, key: str) -> None:
        with self._lock:
            if len(self._failures) >= MAX_REMEMBERED_FAILURES:
                now = self._clock()
                self._failures = {k: until for k, until in self._failures.items() if until > now}
                if len(self._failures) >= MAX_REMEMBERED_FAILURES:
                    self._failures.clear()
            self._failures[key] = self._clock() + FAILURE_TTL_SECONDS
