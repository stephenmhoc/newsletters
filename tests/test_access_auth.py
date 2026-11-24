from __future__ import annotations

import base64
import json
import time
import unittest
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.auth import crypt, jwt

from newsletter_summary.access_auth import AccessDenied, AccessVerifier, certs_url

TEAM = "team-example.cloudflareaccess.com"
AUD = "aud-1234567890"
EMAIL = "reader@example.com"


def _key_pair() -> tuple[str, str, rsa.RSAPublicNumbers]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode("ascii")
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return private_pem, cert.public_bytes(serialization.Encoding.PEM).decode("ascii"), key.public_key().public_numbers()


def _b64(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big")).decode().rstrip("=")


def _token(private_pem: str, kid: str, **overrides) -> str:
    now = int(time.time())
    claims = {"aud": [AUD], "iss": f"https://{TEAM}", "email": EMAIL, "iat": now, "exp": now + 300, "sub": "u1"}
    claims.update(overrides)
    claims = {key: value for key, value in claims.items() if value is not None}
    return jwt.encode(crypt.RSASigner.from_string(private_pem, kid), claims).decode("ascii")


class AccessVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private_pem, self.cert_pem, self.numbers = _key_pair()
        self.fetches: list[str] = []
        self.document = {"public_certs": [{"kid": "kid-1", "cert": self.cert_pem}], "keys": []}
        self.now = 1000.0

        def fetch(url: str) -> dict:
            self.fetches.append(url)
            return self.document

        self.verifier = AccessVerifier(
            team_domain=TEAM, audience=AUD, allowed_emails=["Reader@Example.com"], fetcher=fetch, clock=lambda: self.now
        )

    def test_valid_token_returns_the_email(self) -> None:
        self.assertEqual(self.verifier.verify(_token(self.private_pem, "kid-1")), EMAIL)
        self.assertEqual(self.fetches, [certs_url(TEAM)])
        self.assertEqual(self.fetches[0], f"https://{TEAM}/cdn-cgi/access/certs")
        # Keys are cached.
        self.verifier.verify(_token(self.private_pem, "kid-1"))
        self.assertEqual(len(self.fetches), 1)

    def test_jwk_keys_are_supported(self) -> None:
        self.document = {"keys": [{"kid": "kid-1", "kty": "RSA", "alg": "RS256",
                                   "n": _b64(self.numbers.n), "e": _b64(self.numbers.e)}]}
        self.assertEqual(self.verifier.verify(_token(self.private_pem, "kid-1")), EMAIL)

    def test_rejects_bad_claims(self) -> None:
        cases = {
            "wrong audience": _token(self.private_pem, "kid-1", aud=["someone-else"]),
            "wrong issuer": _token(self.private_pem, "kid-1", iss="https://evil.cloudflareaccess.com"),
            "expired": _token(self.private_pem, "kid-1", iat=int(time.time()) - 7200, exp=int(time.time()) - 3600),
            "not allowed": _token(self.private_pem, "kid-1", email="stranger@example.com"),
            "no email": _token(self.private_pem, "kid-1", email=None),
        }
        for name, token in cases.items():
            with self.subTest(name), self.assertRaises(AccessDenied):
                self.verifier.verify(token)

    def test_rejects_forged_and_malformed_tokens(self) -> None:
        other_private, _, _ = _key_pair()
        with self.assertRaises(AccessDenied):
            self.verifier.verify(_token(other_private, "kid-1"))
        header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": "kid-1"}).encode()).decode().rstrip("=")
        unsigned = _token(self.private_pem, "kid-1").split(".")
        for token in ("", "not-a-jwt", f"{header}.{unsigned[1]}.", f"{header}.{unsigned[1]}.{unsigned[2]}"):
            with self.subTest(token=token[:20]), self.assertRaises(AccessDenied):
                self.verifier.verify(token)

    def test_unknown_kid_refreshes_keys_with_a_rate_limit(self) -> None:
        self.verifier.verify(_token(self.private_pem, "kid-1"))
        # Key rotation: a new kid triggers one refresh once the interval passes.
        new_private, new_cert, _ = _key_pair()
        self.document = {"public_certs": [{"kid": "kid-2", "cert": new_cert}]}
        with self.assertRaises(AccessDenied):
            self.verifier.verify(_token(new_private, "kid-2"))  # within the refresh interval
        self.assertEqual(len(self.fetches), 1)
        self.now += 61
        self.assertEqual(self.verifier.verify(_token(new_private, "kid-2")), EMAIL)
        self.assertEqual(len(self.fetches), 2)

    def test_requires_team_domain_and_audience(self) -> None:
        with self.assertRaises(ValueError):
            AccessVerifier(team_domain="", audience=AUD, allowed_emails=[EMAIL])
        with self.assertRaises(ValueError):
            AccessVerifier(team_domain=TEAM, audience="", allowed_emails=[EMAIL])


if __name__ == "__main__":
    unittest.main()
