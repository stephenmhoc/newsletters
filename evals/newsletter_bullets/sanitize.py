"""Turn exported summarizer inputs into shareable eval fixtures.

Usage: python evals/newsletter_bullets/sanitize.py RAW_EXPORT.json

Unwraps redirect links to their canonical article URLs, strips query strings
(tracking and per-subscriber tokens), redacts opaque token paths, replaces the
recipient's identity, and refuses to write a fixture that still leaks it.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

CASES_DIR = Path(__file__).parent / "cases"
URL_RE = re.compile(r"https?://[^\s)\]\"'<>|]+")
TOKEN_RE = re.compile(r"[A-Za-z0-9_\-+/=.]{24,}")
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+/?")
# Identity markers to strip; checked again (including inside base64) before writing.
# Only the recipient's own address and full name; other people named Stephen are article content.
IDENTITY_RE = re.compile(r"[\w.+-]*@?merimerimeri\.com|\bstephen\s+meriwether\b|\bmeriwether\b", re.IGNORECASE)
LEAK_MARKERS = ("merimerimeri", "meriwether")
KEEP_QUERY = {"substack.com/app-link/post": ("publication_id", "post_id")}


def _b64decode(value: str) -> str | None:
    # Signed tokens look like <base64 payload>.<signature>; try the payload alone first.
    for candidate in dict.fromkeys((value.split(".")[0], value)):
        padded = candidate + "=" * (-len(candidate) % 4)
        altchars = b"-_" if re.fullmatch(r"[A-Za-z0-9_\-=]+", padded) else None
        try:
            return base64.b64decode(padded, altchars=altchars, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
    return None


def clean_url(url: str, redacted: dict[str, str]) -> str:
    parts = urlsplit(url)
    host_path = f"{parts.netloc}{parts.path}".rstrip("/")
    segments = [s for s in parts.path.split("/") if s]
    # Substack /redirect/2/<base64 JSON {"e": real_url, ...}>
    if parts.netloc.endswith("substack.com") and segments[:2] == ["redirect", "2"] and len(segments) > 2:
        decoded = _b64decode(segments[2])
        if decoded:
            try:
                target = json.loads(decoded).get("e")
            except json.JSONDecodeError:
                target = None
            if target:
                return clean_url(target, redacted)
    # Tracker paths whose last segment is a base64-encoded destination URL.
    if segments:
        decoded = _b64decode(segments[-1])
        if decoded and decoded.startswith("http"):
            return clean_url(decoded, redacted)
    for prefix, keep in KEEP_QUERY.items():
        if host_path == prefix:
            query = parse_qs(parts.query)
            kept = {key: query[key][0] for key in keep if key in query}
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))
    if any(_is_token(segment) for segment in segments):
        return redacted.setdefault(host_path, f"https://redacted.invalid/link-{len(redacted) + 1}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _is_token(segment: str) -> bool:
    """Opaque IDs/tokens, but not readable slugs such as detroit-pistons-trade-rumors."""
    if not TOKEN_RE.fullmatch(segment) or SLUG_RE.fullmatch(segment):
        return False
    return sum(c.isdigit() for c in segment) >= 3 or (segment.lower() != segment and segment.upper() != segment)


def leaks(text: str) -> list[str]:
    found = [m.group(0) for m in IDENTITY_RE.finditer(text)]
    for token in TOKEN_RE.findall(text):
        decoded = _b64decode(token) or ""
        if any(marker in decoded.lower() for marker in LEAK_MARKERS):
            found.append(f"base64:{token[:24]}...")
    return found


def sanitize(case_id: str, raw: dict) -> dict:
    redacted: dict[str, str] = {}

    def scrub(text: str) -> str:
        text = URL_RE.sub(lambda m: clean_url(m.group(0), redacted), text)
        return IDENTITY_RE.sub(lambda m: "reader@example.com" if "@" in m.group(0) else "Reader", text)

    return {
        "id": case_id,
        "subject": scrub(raw["subject"]),
        "sender": scrub(raw["sender"]),
        "received_at": raw["received_at"],
        "gmail_url": f"https://mail.google.com/mail/u/0/#inbox/eval-{case_id}",
        "body_markdown": scrub(raw["body_markdown"]),
        "links": [[clean_url(url, redacted), scrub(text)] for url, text in raw["links"]],
        "web_links": [[clean_url(url, redacted), scrub(text)] for url, text in raw["web_links"]],
    }


def main(path: str) -> int:
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    failed = False
    for case_id, raw in json.loads(Path(path).read_text()).items():
        fixture = sanitize(case_id, raw)
        problems = leaks(json.dumps(fixture, ensure_ascii=False))
        if problems:
            failed = True
            print(f"{case_id}: NOT written, identity still present: {problems[:5]}")
            continue
        (CASES_DIR / f"{case_id}.input.json").write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n")
        print(f"{case_id}: {len(fixture['body_markdown'])} chars, {len(fixture['links'])} links")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
