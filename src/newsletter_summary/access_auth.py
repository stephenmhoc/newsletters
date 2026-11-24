"""Verify Cloudflare Access JWTs (the Cf-Access-Jwt-Assertion header).

Cloudflare publishes the team's signing keys at
https://<team_domain>/cdn-cgi/access/certs, both as PEM certificates
(``public_certs``) and as JWKs (``keys``). Certificates are verified with
google-auth; JWKs are converted to PEM public keys with cryptography.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

import requests
from google.auth import jwt

CertsFetcher = Callable[[str], dict[str, Any]]

REQUEST_TIMEOUT_SECONDS = 5
# An unknown kid triggers a refresh at most this often, so forged tokens
# cannot make every request call Cloudflare.
MIN_REFRESH_INTERVAL_SECONDS = 60
CLOCK_SKEW_SECONDS = 60


class AccessDenied(Exception):
    """The request did not carry a valid Access token for an allowed user."""


def certs_url(team_domain: str) -> str:
    return f"https://{team_domain}/cdn-cgi/access/certs"


def fetch_certs(url: str) -> dict[str, Any]:
    response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def keys_from_certs_document(document: dict[str, Any]) -> dict[str, str]:
    """Map kid -> PEM certificate or public key."""
    keys: dict[str, str] = {}
    for entry in document.get("public_certs") or []:
        if isinstance(entry, dict) and entry.get("kid") and entry.get("cert"):
            keys[str(entry["kid"])] = str(entry["cert"])
    for jwk in document.get("keys") or []:
        if isinstance(jwk, dict) and jwk.get("kid") and str(jwk["kid"]) not in keys and jwk.get("kty") == "RSA":
            keys[str(jwk["kid"])] = _jwk_to_pem(jwk)
    return keys


def _jwk_to_pem(jwk: dict[str, Any]) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

    numbers = RSAPublicNumbers(e=_b64_int(jwk["e"]), n=_b64_int(jwk["n"]))
    return (
        numbers.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode("ascii")
    )


def _b64_int(value: str) -> int:
    return int.from_bytes(_b64decode(value), "big")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class AccessVerifier:
    def __init__(
        self,
        *,
        team_domain: str,
        audience: str,
        allowed_emails: Iterable[str],
        fetcher: CertsFetcher = fetch_certs,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not team_domain or not audience:
            raise ValueError("Cloudflare Access team domain and audience are required")
        self.team_domain = team_domain
        self.audience = audience
        self.issuer = f"https://{team_domain}"
        self.allowed_emails = frozenset(email.strip().lower() for email in allowed_emails if email.strip())
        self._fetcher = fetcher
        self._clock = clock
        self._keys: dict[str, str] = {}
        self._fetched_at: float | None = None
        self._lock = threading.Lock()

    def verify(self, token: str) -> str:
        """Return the verified, allowed email address or raise AccessDenied."""
        if not token:
            raise AccessDenied("missing Access token")
        header = _unverified_header(token)
        if header.get("alg") != "RS256":
            raise AccessDenied("unexpected token algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AccessDenied("token has no key id")
        key = self._key(kid)
        try:
            # Signature, iat, and exp. The audience is checked below because
            # Cloudflare sends aud as a list, which google-auth rejects.
            claims = jwt.decode(token, certs={kid: key}, clock_skew_in_seconds=CLOCK_SKEW_SECONDS)
        except Exception as exc:
            raise AccessDenied(f"invalid Access token: {exc}") from None
        audiences = claims.get("aud")
        if isinstance(audiences, str):
            audiences = [audiences]
        if not isinstance(audiences, list) or self.audience not in audiences:
            raise AccessDenied("unexpected token audience")
        if claims.get("iss") != self.issuer:
            raise AccessDenied("unexpected token issuer")
        email = claims.get("email")
        if not isinstance(email, str) or email.strip().lower() not in self.allowed_emails:
            raise AccessDenied("user is not allowed")
        return email.strip().lower()

    def _key(self, kid: str) -> str:
        with self._lock:
            if kid not in self._keys and self._may_refresh():
                self._fetched_at = self._clock()
                try:
                    self._keys = keys_from_certs_document(self._fetcher(certs_url(self.team_domain)))
                except Exception as exc:
                    raise AccessDenied(f"could not load Access signing keys: {exc}") from None
            key = self._keys.get(kid)
        if key is None:
            raise AccessDenied("unknown token key id")
        return key

    def _may_refresh(self) -> bool:
        return self._fetched_at is None or self._clock() - self._fetched_at >= MIN_REFRESH_INTERVAL_SECONDS


def _unverified_header(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise AccessDenied("malformed Access token")
    try:
        header = json.loads(_b64decode(parts[0]))
    except (ValueError, UnicodeDecodeError):
        raise AccessDenied("malformed Access token") from None
    if not isinstance(header, dict):
        raise AccessDenied("malformed Access token")
    return header
