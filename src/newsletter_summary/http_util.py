"""Response helpers shared by the public listener and the private web UI."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs

MAX_REQUEST_BODY_BYTES = 16 * 1024


def etag_for(payload: bytes) -> str:
    return '"' + hashlib.sha256(payload).hexdigest()[:32] + '"'


def is_not_modified(headers, etag: str, last_modified: datetime | None) -> bool:
    """RFC 9110 conditional GET: If-None-Match wins; If-Modified-Since only without it.

    ETags are compared weakly: Cloudflare marks ETags weak (W/) when it
    compresses a response.
    """
    if_none_match = headers.get("If-None-Match")
    if if_none_match is not None:
        tags = [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
        return "*" in tags or etag in tags
    if_modified_since = headers.get("If-Modified-Since")
    if if_modified_since and last_modified is not None:
        try:
            since = parsedate_to_datetime(if_modified_since)
        except (TypeError, ValueError, IndexError):
            return False
        if since.tzinfo is None:
            return False
        return last_modified.replace(microsecond=0) <= since
    return False


class BaseHandler(BaseHTTPRequestHandler):
    """Byte, text, HTML, redirect, and conditional responses; HEAD sends headers only."""

    _send_response_body = True
    _response_started = False

    def extra_headers(self, status: HTTPStatus) -> dict[str, str]:
        """Headers added to every response (the web UI adds its security headers)."""
        return {}

    def _send_bytes(
        self,
        payload: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in {**self.extra_headers(status), **(headers or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        if self._send_response_body:
            self.wfile.write(payload)

    def _send_conditional(
        self,
        payload: bytes,
        content_type: str,
        *,
        last_modified: datetime | None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """200 with ETag and Last-Modified, or 304 when the client's copy is current."""
        etag = etag_for(payload)
        validators = {"ETag": etag, **(headers or {})}
        if last_modified is not None and last_modified.tzinfo is not None:
            validators["Last-Modified"] = format_datetime(last_modified.astimezone(UTC), usegmt=True)
        if is_not_modified(self.headers, etag, last_modified):
            self._response_started = True
            self.send_response(HTTPStatus.NOT_MODIFIED)
            for key, value in {**self.extra_headers(HTTPStatus.NOT_MODIFIED), **validators}.items():
                self.send_header(key, value)
            self.end_headers()
            return
        self._send_bytes(payload, content_type, HTTPStatus.OK, validators)

    def _send_html(self, html: str, status: HTTPStatus = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8", status, headers)

    def _send_text(self, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send_bytes(text.encode("utf-8"), "text/plain; charset=utf-8", status)

    def _not_found(self) -> None:
        self._send_text("Not found\n", HTTPStatus.NOT_FOUND)

    def _redirect(self, location: str) -> None:
        self._send_bytes(b"", "text/plain; charset=utf-8", HTTPStatus.SEE_OTHER, {"Location": location})

    def _read_form(self) -> dict[str, list[str]]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            raise ValueError("invalid Content-Length") from None
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            raise ValueError("invalid request body size")
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        return parse_qs(body)

    def _head(self, dispatch) -> None:
        self._send_response_body = False
        try:
            dispatch()
        finally:
            self._send_response_body = True
