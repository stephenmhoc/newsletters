"""Guarded outbound HTTP fetches for URLs that come from outside the app.

Used by the image proxy (signed image URLs from newsletters) and by "Add a
link" (any web page the owner submits). Every fetch is strict:

- http/https on ports 80/443 only, no credentials in the URL;
- every address the host resolves to must be public, and the socket's actual
  peer address is checked again after connecting, before any byte is sent, so
  DNS rebinding cannot reach private, loopback, or link-local addresses;
- no proxy environment variables are honored: connections go straight to the
  checked address;
- redirects are followed by hand, at most max_redirects, each one re-checked;
- the declared Content-Type must pass the caller's check before the body is
  read, and the body is capped at max_bytes within a total deadline.

Errors are FetchError subclasses whose `reason` names what went wrong, so
callers can explain it without echoing the URL (which may carry tokens).
"""
from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import certifi
import urllib3
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

MAX_URL_LENGTH = 4096
MAX_REDIRECTS = 3
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 10
TOTAL_DEADLINE_SECONDS = 20
ALLOWED_PORTS = frozenset({80, 443})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

Resolver = Callable[[str, int], list[str]]
TypeCheck = Callable[[str], bool]


class FetchError(Exception):
    """The URL could not be fetched safely."""

    reason = "unreachable"


class BlockedAddress(FetchError):
    """The URL points at a non-public address."""

    reason = "blocked"


class InvalidURL(FetchError):
    """Not an http(s) URL on an allowed port."""

    reason = "invalid"


class TooLarge(FetchError):
    reason = "too_large"


class TimedOut(FetchError):
    reason = "timeout"


class UnsupportedType(FetchError):
    reason = "type"


class HTTPStatusError(FetchError):
    reason = "status"


@dataclass(frozen=True)
class Fetched:
    url: str  # after redirects
    content_type: str  # declared media type, lowercased, without parameters
    charset: str  # from the Content-Type header, else ""
    data: bytes


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour
        if mapped is not None:
            return is_public_address(str(mapped))
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local)


def resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def url_problem(url: str, allowed_ports: frozenset[int] = ALLOWED_PORTS) -> str | None:
    """Why url may never be fetched, judged from the URL alone; None when it may."""
    if len(url) > MAX_URL_LENGTH:
        return "URL is too long"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "invalid URL"
    if parts.scheme not in {"http", "https"}:
        return "only http and https are allowed"
    if not parts.hostname:
        return "no host"
    if parts.username or parts.password:
        return "credentials in the URL"
    if port is not None and port not in allowed_ports:
        return "only ports 80 and 443 are allowed"
    return None


def check_url(url: str, resolver: Resolver = resolve, allowed_ports: frozenset[int] = ALLOWED_PORTS) -> None:
    """Raise unless url is http(s) to a host whose every address is public."""
    problem = url_problem(url, allowed_ports)
    if problem:
        raise InvalidURL(problem)
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = resolver(host, port)
    except OSError as exc:
        raise FetchError(f"cannot resolve host: {type(exc).__name__}") from None
    if not addresses:
        raise FetchError("host has no addresses")
    if not all(is_public_address(address) for address in addresses):
        raise BlockedAddress("host resolves to a non-public address")


def _check_peer(sock: socket.socket) -> None:
    peer = sock.getpeername()[0]
    if not is_public_address(str(peer)):
        sock.close()
        raise BlockedAddress("connected to a non-public address")


class _GuardedHTTPConnection(HTTPConnection):
    def _new_conn(self) -> socket.socket:
        sock = super()._new_conn()
        _check_peer(sock)
        return sock


class _GuardedHTTPSConnection(HTTPSConnection):
    def _new_conn(self) -> socket.socket:
        sock = super()._new_conn()
        _check_peer(sock)
        return sock


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


def pool_manager() -> urllib3.PoolManager:
    # A PoolManager never reads proxy environment variables, so every
    # connection goes straight to the checked address.
    manager = urllib3.PoolManager(num_pools=4, maxsize=2, cert_reqs="CERT_REQUIRED", ca_certs=certifi.where())
    # A fresh dict: the default one is shared by every PoolManager.
    manager.pool_classes_by_scheme = {  # ty: ignore[invalid-assignment]
        "http": _GuardedHTTPPool,
        "https": _GuardedHTTPSPool,
    }
    return manager


def _content_type(header: str) -> tuple[str, str]:
    """(media type, charset) from a Content-Type header."""
    media, _, params = header.partition(";")
    charset = ""
    for param in params.split(";"):
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset":
            charset = value.strip().strip("\"'").lower()
    return media.strip().lower(), charset


def fetch(
    url: str,
    *,
    max_bytes: int,
    accept_type: TypeCheck,
    headers: dict[str, str] | None = None,
    resolver: Resolver = resolve,
    manager: urllib3.PoolManager | None = None,
    clock: Callable[[], float] = time.monotonic,
    max_redirects: int = MAX_REDIRECTS,
    allowed_ports: frozenset[int] = ALLOWED_PORTS,
    deadline_seconds: float = TOTAL_DEADLINE_SECONDS,
) -> Fetched:
    """GET url under every guard above; accept_type judges the declared media type."""
    manager = manager or pool_manager()
    deadline = clock() + deadline_seconds
    for _ in range(max_redirects + 1):
        check_url(url, resolver, allowed_ports)
        if clock() > deadline:
            raise TimedOut("took too long")
        try:
            response = manager.request(
                "GET",
                url,
                headers=headers or {},
                redirect=False,
                retries=False,
                preload_content=False,
                timeout=urllib3.Timeout(connect=CONNECT_TIMEOUT_SECONDS, read=READ_TIMEOUT_SECONDS),
            )
        except BlockedAddress:
            raise
        except urllib3.exceptions.HTTPError as exc:
            if isinstance(exc.__context__, BlockedAddress) or "non-public" in str(exc):
                raise BlockedAddress("connected to a non-public address") from None
            if isinstance(exc, urllib3.exceptions.TimeoutError):
                raise TimedOut("timed out") from None
            raise FetchError(f"request failed: {type(exc).__name__}") from None
        try:
            if response.status in REDIRECT_STATUSES:
                location = response.headers.get("Location", "")
                if not location:
                    raise FetchError("redirect without a location")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise HTTPStatusError(f"HTTP {response.status}")
            media_type, charset = _content_type(response.headers.get("Content-Type", ""))
            if not accept_type(media_type):
                raise UnsupportedType("not an allowed content type")
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise TooLarge("response is too large")
            chunks = []
            total = 0
            try:
                # decode_content=True: urllib3's stream() does not decompress
                # by default; the size cap then applies to decompressed bytes.
                for chunk in response.stream(64 * 1024, decode_content=True):
                    total += len(chunk)
                    if total > max_bytes:
                        raise TooLarge("response is too large")
                    if clock() > deadline:
                        raise TimedOut("took too long")
                    chunks.append(chunk)
            except urllib3.exceptions.TimeoutError:
                raise TimedOut("timed out") from None
            except urllib3.exceptions.HTTPError as exc:
                raise FetchError(f"reading failed: {type(exc).__name__}") from None
            return Fetched(url=url, content_type=media_type, charset=charset, data=b"".join(chunks))
        finally:
            response.release_conn()
    raise FetchError("too many redirects")
