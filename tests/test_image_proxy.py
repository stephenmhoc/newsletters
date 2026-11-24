from __future__ import annotations

import base64
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from newsletter_summary import image_proxy
from newsletter_summary.image_proxy import (
    BlockedAddress,
    Image,
    ImageCache,
    ImageFetchError,
    ImageProxy,
    check_url,
    fetch_image,
    image_signature,
    is_public_address,
    proxied_image_url,
    sniff_image_type,
    verify_image_request,
)

SECRET = "image-secret"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
PUBLIC_IP = "93.184.216.34"


def public(_host: str, _port: int) -> list[str]:
    return [PUBLIC_IP]


def resolving(mapping: dict[str, list[str]]):
    def resolve(host: str, _port: int) -> list[str]:
        return mapping[host]

    return resolve


def _parts(url: str) -> tuple[str, str]:
    _, _, signature, encoded = url.rsplit("/", 3)
    return signature, encoded


class SignatureTests(unittest.TestCase):
    def test_round_trip_and_tampering(self) -> None:
        url = "https://cdn.example.com/a.png?x=1&token=abc"
        proxied = proxied_image_url("https://rss.example.com", SECRET, url)
        self.assertTrue(proxied.startswith("https://rss.example.com/img/"))
        signature, encoded = _parts(proxied)
        self.assertEqual(verify_image_request(SECRET, signature, encoded), url)
        # Wrong secret, altered signature, or a different URL with the old signature.
        self.assertIsNone(verify_image_request("other", signature, encoded))
        self.assertIsNone(verify_image_request(SECRET, "0" * 32, encoded))
        other = base64.urlsafe_b64encode(b"https://evil.example/x.png").decode().rstrip("=")
        self.assertIsNone(verify_image_request(SECRET, signature, other))
        self.assertIsNone(verify_image_request("", signature, encoded))
        self.assertIsNone(verify_image_request(SECRET, signature, "!!not base64!!"))

    def test_signed_urls_must_still_be_fetchable_http(self) -> None:
        for url in ("file:///etc/passwd", "gopher://x/", "https://user:pw@example.com/a.png", "http://example.com:8080/a.png"):
            with self.subTest(url=url):
                signature = image_signature(SECRET, url)
                encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
                self.assertIsNone(verify_image_request(SECRET, signature, encoded))


class AddressTests(unittest.TestCase):
    def test_private_loopback_link_local_and_special_addresses_are_blocked(self) -> None:
        for address in (
            "127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.8.21", "169.254.169.254", "100.98.128.109",
            "0.0.0.0", "224.0.0.1", "255.255.255.255", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1",
            "::ffff:192.168.1.1", "2002:c0a8:0101::1", "not-an-ip",
        ):  # fmt: skip
            with self.subTest(address=address):
                self.assertFalse(is_public_address(address))
        for address in (PUBLIC_IP, "1.1.1.1", "2606:4700:4700::1111"):
            with self.subTest(address=address):
                self.assertTrue(is_public_address(address))

    def test_check_url_resolves_and_requires_every_address_public(self) -> None:
        check_url("https://cdn.example.com/a.png", public)
        cases = {
            "https://internal.example/a.png": ["10.1.2.3"],
            "https://mixed.example/a.png": [PUBLIC_IP, "127.0.0.1"],
            "https://metadata.example/a.png": ["169.254.169.254"],
        }
        resolver = resolving({"internal.example": cases["https://internal.example/a.png"],
                              "mixed.example": cases["https://mixed.example/a.png"],
                              "metadata.example": cases["https://metadata.example/a.png"]})
        for url in cases:
            with self.subTest(url=url), self.assertRaises(BlockedAddress):
                check_url(url, resolver)
        for url in ("ftp://example.com/a.png", "https://example.com:22/a.png", "https://a:b@example.com/x.png", "http:///x"):
            with self.subTest(url=url), self.assertRaises(ImageFetchError):
                check_url(url, public)

        def broken(_host: str, _port: int) -> list[str]:
            raise OSError("no such host")

        with self.assertRaises(ImageFetchError):
            check_url("https://nowhere.example/a.png", broken)


class _Response:
    def __init__(self, status: int = 200, headers: dict[str, str] | None = None, body: bytes = PNG):
        self.status = status
        self.headers = headers if headers is not None else {"Content-Type": "image/png"}
        self.body = body
        self.released = False

    def stream(self, size: int, decode_content: bool = True):
        for index in range(0, len(self.body), size):
            yield self.body[index : index + size]

    def release_conn(self) -> None:
        self.released = True


class FetchTests(unittest.TestCase):
    def manager(self, *responses: _Response) -> Mock:
        manager = Mock()
        manager.request.side_effect = list(responses)
        return manager

    def test_png_is_fetched_without_following_redirects_automatically(self) -> None:
        manager = self.manager(_Response())
        image = fetch_image("https://cdn.example.com/a.png", max_bytes=1000, resolver=public, manager=manager)
        self.assertEqual(image, Image(PNG, "image/png"))
        kwargs = manager.request.call_args.kwargs
        self.assertFalse(kwargs["redirect"])
        self.assertFalse(kwargs["retries"])

    def test_redirects_are_rechecked_and_capped(self) -> None:
        resolver = resolving({"cdn.example.com": [PUBLIC_IP], "internal.example": ["10.0.0.1"]})
        manager = self.manager(_Response(302, {"Location": "https://internal.example/secret.png"}))
        with self.assertRaises(BlockedAddress):
            fetch_image("https://cdn.example.com/a.png", max_bytes=1000, resolver=resolver, manager=manager)
        self.assertEqual(manager.request.call_count, 1)
        # A redirect to another scheme is refused too.
        manager = self.manager(_Response(301, {"Location": "file:///etc/passwd"}))
        with self.assertRaises(ImageFetchError):
            fetch_image("https://cdn.example.com/a.png", max_bytes=1000, resolver=public, manager=manager)
        loop = [_Response(302, {"Location": f"/hop{index}"}) for index in range(image_proxy.MAX_REDIRECTS + 1)]
        manager = self.manager(*loop)
        with self.assertRaisesRegex(ImageFetchError, "too many redirects"):
            fetch_image("https://cdn.example.com/a.png", max_bytes=1000, resolver=public, manager=manager)
        self.assertEqual(manager.request.call_count, image_proxy.MAX_REDIRECTS + 1)
        # Relative redirects resolve against the current URL, and are followed.
        manager = self.manager(_Response(302, {"Location": "/b.png"}), _Response())
        fetch_image("https://cdn.example.com/a.png", max_bytes=1000, resolver=public, manager=manager)
        self.assertEqual(manager.request.call_args.args[1], "https://cdn.example.com/b.png")

    def test_size_type_and_status_limits(self) -> None:
        cases = [
            _Response(headers={"Content-Type": "image/png", "Content-Length": "5000"}),
            _Response(body=PNG + b"\0" * 2000),
            _Response(headers={"Content-Type": "image/svg+xml"}, body=b"<svg onload=alert(1)>"),
            _Response(headers={"Content-Type": "text/html"}),
            _Response(headers={"Content-Type": "image/png"}, body=b"<html>not an image</html>"),
            _Response(404),
        ]
        for response in cases:
            with self.subTest(headers=response.headers, status=response.status), self.assertRaises(ImageFetchError):
                fetch_image("https://cdn.example.com/a.png", max_bytes=1000, resolver=public, manager=self.manager(response))
            self.assertTrue(response.released)

    def test_sniffing(self) -> None:
        self.assertEqual(sniff_image_type(b"\xff\xd8\xff\xe0rest"), "image/jpeg")
        self.assertEqual(sniff_image_type(b"GIF89a..."), "image/gif")
        self.assertEqual(sniff_image_type(b"RIFF\0\0\0\0WEBPVP8 "), "image/webp")
        self.assertEqual(sniff_image_type(b"\0\0\0\x1cftypavif"), "image/avif")
        self.assertIsNone(sniff_image_type(b"<?xml version='1.0'?><svg/>"))


class _Local(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG)))
        self.end_headers()
        self.wfile.write(PNG)
        type(self).hits += 1

    hits = 0

    def log_message(self, format: str, *args: Any) -> None:
        pass


class RebindingTests(unittest.TestCase):
    def test_connection_to_a_private_peer_is_refused_even_when_dns_said_public(self) -> None:
        """DNS rebinding: the check before connecting passes, the socket's real peer is loopback."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Local)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            # Port 80/443 only, so point the allowed port at the local server.
            original = image_proxy.ALLOWED_PORTS
            image_proxy.ALLOWED_PORTS = frozenset({port})
            try:
                with self.assertRaises(BlockedAddress):
                    fetch_image(f"http://127.0.0.1:{port}/a.png", max_bytes=1000, resolver=public)
            finally:
                image_proxy.ALLOWED_PORTS = original
            self.assertEqual(_Local.hits, 0)
        finally:
            server.shutdown()
            server.server_close()


class CacheTests(unittest.TestCase):
    def test_get_put_and_lru_eviction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = ImageCache(Path(directory) / "images", max_bytes=100)
            self.assertIsNone(cache.get("https://a/1.png"))
            for index in range(3):
                cache.put(f"https://a/{index}.png", Image(b"x" * 40, "image/png"))
                path = Path(directory) / "images" / f"{ImageCache.key(f'https://a/{index}.png')}.png"
                # Distinct, increasing access times.
                os.utime(path, (time.time() - 100 + index, time.time() - 100 + index))
                if index == 1:
                    # Reading 0 makes it more recent than 1.
                    self.assertIsNotNone(cache.get("https://a/0.png"))
            files, size = cache.stats()
            self.assertLessEqual(size, 100)
            self.assertIsNotNone(cache.get("https://a/2.png"))
            self.assertIsNotNone(cache.get("https://a/0.png"))
            self.assertIsNone(cache.get("https://a/1.png"))
            self.assertEqual(files, 2)
            # Larger than the whole cache: not stored.
            cache.put("https://a/big.png", Image(b"x" * 200, "image/png"))
            self.assertIsNone(cache.get("https://a/big.png"))

    def test_proxy_uses_the_cache_and_remembers_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls: list[str] = []

            def fetch(url: str) -> Image:
                calls.append(url)
                if "bad" in url:
                    raise ImageFetchError("HTTP 404")
                return Image(PNG, "image/png")

            proxy = ImageProxy(ImageCache(Path(directory), 10_000), max_bytes=1000, fetch=fetch)
            self.assertEqual(proxy.get("https://a/ok.png"), Image(PNG, "image/png"))
            self.assertEqual(proxy.get("https://a/ok.png"), Image(PNG, "image/png"))
            self.assertIsNone(proxy.get("https://a/bad.png?token=sekret"))
            with self.assertNoLogs(level="WARNING"):
                self.assertIsNone(proxy.get("https://a/bad.png?token=sekret"))
            self.assertEqual(calls, ["https://a/ok.png", "https://a/bad.png?token=sekret"])

    def test_failure_log_never_contains_the_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def fetch(url: str) -> Image:
                raise ImageFetchError("HTTP 500")

            proxy = ImageProxy(ImageCache(Path(directory), 10_000), max_bytes=1000, fetch=fetch)
            with self.assertLogs(level="INFO") as logs:
                proxy.get("https://a/x.png?token=sekret")
            self.assertNotIn("sekret", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
