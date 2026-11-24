from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from urllib.parse import SplitResult, parse_qs, unquote, unquote_plus, urlparse, urlsplit, urlunsplit

import html2text
from bs4 import BeautifulSoup


@dataclass(frozen=True)
class ExtractedLink:
    url: str
    text: str


SKIP_URL_PREFIXES = ("mailto:", "tel:", "sms:", "#")

# Matched anywhere in the URL. These are distinctive enough that a substring hit
# is never a real article.
SKIP_URL_SUBSTRINGS = (
    "ad.doubleclick.net",
    "doubleclick.net",
    "list-manage.com/unsubscribe",
    "/unsubscribe",
    "view-in-browser",
    "viewonline",
)

# Matched against a whole path segment, so an article slug such as
# "the-terms-of-the-ai-deal" or "how-privacy-law-changed" is kept.
SKIP_PATH_SEGMENTS = frozenset(
    {
        "cookie-policy",
        "cookies",
        "email-preferences",
        "manage-preferences",
        "manage-subscription",
        "notification-preferences",
        "preferences",
        "privacy",
        "privacy-notice",
        "privacy-policy",
        "terms",
        "terms-and-conditions",
        "terms-of-service",
        "terms-of-use",
        "unsubscribe",
        "update-preferences",
    }
)

# Matched against the whole anchor text, so a headline that merely mentions one
# of these words is kept.
SKIP_LINK_LABELS = frozenset(
    {
        "cookie policy",
        "email preferences",
        "manage preferences",
        "manage your preferences",
        "privacy",
        "privacy notice",
        "privacy policy",
        "terms",
        "terms and conditions",
        "terms of service",
        "terms of use",
        "unsubscribe",
        "update preferences",
        "update your preferences",
        "view in browser",
        "view online",
        "view this email in your browser",
    }
)


# "View in browser" links are housekeeping for summarization but are often the
# best public URL for the issue itself.
WEB_VERSION_LABELS = frozenset(
    {
        "open in browser",
        "read in browser",
        "read in your browser",
        "read on the web",
        "read online",
        "view in browser",
        "view in your browser",
        "view online",
        "view on web",
        "view this email in your browser",
        "view this post on the web",
    }
)
WEB_VERSION_URL_SUBSTRINGS = ("view-in-browser", "viewonline")


def email_to_markdown(html: str, text: str) -> str:
    """The email body for the model prompt.

    Link URLs are left out (ignore_links): the prompt lists the extracted links
    separately, and inline tracking URLs were often half of the body.
    """
    if html:
        converter = html2text.HTML2Text()
        converter.ignore_images = True
        converter.ignore_links = True
        converter.ignore_emphasis = False
        converter.body_width = 0
        return converter.handle(html).strip()
    return text.strip()


def extract_links(html: str, text: str) -> list[ExtractedLink]:
    if html:
        return _links_from_html(html)
    return _links_from_markdownish_text(text)


def extract_web_version_links(html: str) -> list[ExtractedLink]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    links: list[ExtractedLink] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a"):
        href_value = anchor.get("href")
        if not isinstance(href_value, str):
            continue
        label = " ".join(anchor.get_text(" ", strip=True).split())
        url = normalize_url(href_value.strip())
        if not url.lower().startswith(("http://", "https://")) or url in seen:
            continue
        lowered = url.lower()
        if _normalized_label(label) in WEB_VERSION_LABELS or any(
            hint in lowered for hint in WEB_VERSION_URL_SUBSTRINGS
        ):
            seen.add(url)
            links.append(ExtractedLink(url=url, text=label or url))
    return links


def _links_from_html(html: str) -> list[ExtractedLink]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[ExtractedLink] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a"):
        href_value = anchor.get("href")
        if not isinstance(href_value, str):
            continue
        href = href_value.strip()
        label = " ".join(anchor.get_text(" ", strip=True).split())
        url = normalize_url(href)
        if _should_keep(url, label) and url not in seen:
            seen.add(url)
            links.append(ExtractedLink(url=url, text=label or url))
    return links


def _links_from_markdownish_text(text: str) -> list[ExtractedLink]:
    links: list[ExtractedLink] = []
    seen: set[str] = set()
    for token in text.split():
        candidate = token.strip("()[]<>.,;\"'")
        url = normalize_url(candidate)
        if _should_keep(url, "") and url not in seen:
            seen.add(url)
            links.append(ExtractedLink(url=url, text=url))
    return links


def _should_keep(url: str, label: str) -> bool:
    lowered = url.lower()
    if not lowered.startswith(("http://", "https://")):
        return False
    if lowered.startswith(SKIP_URL_PREFIXES):
        return False
    parsed = urlparse(url)
    if not parsed.netloc:
        return False
    if any(hint in lowered for hint in SKIP_URL_SUBSTRINGS):
        return False
    if _normalized_label(label) in SKIP_LINK_LABELS:
        return False
    return not any(_path_segment(segment) in SKIP_PATH_SEGMENTS for segment in parsed.path.split("/"))


def is_housekeeping_link(text: str, url: str) -> bool:
    """True for unsubscribe, preference, legal, and view-in-browser links.

    Unlike the article filter, this does not reject non-http links, so mailto:
    and relative links are kept unless they match a housekeeping pattern.
    """
    lowered = url.lower()
    if any(hint in lowered for hint in (*SKIP_URL_SUBSTRINGS, *WEB_VERSION_URL_SUBSTRINGS)):
        return True
    label = _normalized_label(text)
    if label in SKIP_LINK_LABELS or label in WEB_VERSION_LABELS:
        return True
    if lowered.startswith("mailto:"):
        return "unsubscribe" in lowered
    parsed = urlparse(url)
    return any(_path_segment(segment) in SKIP_PATH_SEGMENTS for segment in parsed.path.split("/"))


def _path_segment(segment: str) -> str:
    return re.sub(r"\.(html?|php|aspx?)$", "", segment.strip().lower())


def _normalized_label(label: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", label.lower()).split())


def normalize_url(url: str, _depth: int = 0) -> str:
    """Unwrap known redirectors and drop credentials, leaving the URL's own encoding alone.

    The outer URL is never percent-decoded: "%26", "%25", "%2B", and "%23" mean
    something different from "&", "%", "+", and "#". Only a URL extracted from a
    redirector (a query value, or base64 in the path) is decoded, then
    normalized in turn. Opaque click trackers are left as they are and are
    never followed over the network.
    """
    current = url.strip().replace("\\u0026", "&")
    if _depth < 3:
        inner = _unwrap_base64_path_url(current) or _unwrap_query_url(current)
        if inner:
            return normalize_url(inner, _depth + 1)
    return strip_credentials(current)


# Query parameters that identify or log in the subscriber (Substack app-link
# "token"/"r", Stratechery "access_token") or only track clicks. These links
# reach the feeds and the model prompt, so they are dropped.
_CREDENTIAL_PARAMS = frozenset(
    {"token", "access_token", "auth", "auth_token", "r", "isfreemail", "triedredirect", "mc_eid", "mc_cid",
     "_hsenc", "_hsmi", "ck_subscriber_id", "subscriber_id", "uid", "user_id", "email"}
)  # fmt: skip


def strip_credentials(url: str) -> str:
    """Drop subscriber credentials and click tracking from a URL's query string.

    The remaining parameters keep their exact original encoding.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query or parts.scheme not in {"http", "https"}:
        return url
    pieces = parts.query.split("&")
    kept = [piece for piece in pieces if piece and not _is_credential_param(piece.split("=", 1)[0])]
    if len(kept) == len(pieces):
        return url
    return urlunsplit(parts._replace(query="&".join(kept)))


def _is_credential_param(raw_key: str) -> bool:
    key = unquote_plus(raw_key).lower()
    return key in _CREDENTIAL_PARAMS or key.startswith("utm_")


# Query keys that carry the destination on a redirector.
_REDIRECT_QUERY_KEYS = ("url", "u", "redirect", "redirect_url", "redirect_uri", "target", "to", "dest", "destination")
# Hosts that only redirect, and first host labels used by mail click trackers
# (click.example.net, links.example.com).
_REDIRECT_HOSTS = frozenset(
    {"l.facebook.com", "lm.facebook.com", "l.instagram.com", "out.reddit.com", "href.li", "t.umblr.com"}
)
_REDIRECT_HOST_LABELS = frozenset(
    {"click", "clicks", "link", "links", "lnk", "track", "tracking", "trk", "go", "redirect", "r", "email", "out"}
)
# Path segments of redirect endpoints (/r?url=, /redirect?to=, /click?u=).
_REDIRECT_PATH_SEGMENTS = frozenset(
    {"r", "redirect", "redir", "click", "track", "out", "url", "link", "l", "c", "away", "external"}
)


def _is_redirector(parts: SplitResult) -> bool:
    host = (parts.hostname or "").lower()
    if host in _REDIRECT_HOSTS or host.endswith(".safelinks.protection.outlook.com"):
        return True
    if host.split(".", 1)[0] in _REDIRECT_HOST_LABELS:
        return True
    return any(segment.lower() in _REDIRECT_PATH_SEGMENTS for segment in parts.path.split("/"))


def _unwrap_query_url(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not _is_redirector(parts):
        return None
    query = parse_qs(parts.query)
    # Google's redirector: https://www.google.com/url?q=<destination>
    keys = (*_REDIRECT_QUERY_KEYS, "q") if parts.path == "/url" else _REDIRECT_QUERY_KEYS
    for key in keys:
        for value in query.get(key, []):
            candidate = value.strip()
            # A destination encoded twice is still "https%3A%2F%2F..." after parse_qs.
            if re.match(r"https?%3a", candidate, re.IGNORECASE):
                candidate = unquote(candidate)
            if candidate.lower().startswith(("http://", "https://")):
                return candidate
    return None


def _unwrap_base64_path_url(url: str) -> str | None:
    """Destinations carried as base64 in the path.

    Substack's https://substack.com/redirect/2/<base64 JSON>.<signature> holds
    {"e": "<destination>", ...}; other platforms use a bare base64 URL.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"}:
        return None
    for segment in parts.path.split("/"):
        for candidate in dict.fromkeys((segment.strip(), segment.strip().split(".", 1)[0])):
            if len(candidate) < 16:
                continue
            decoded = _decode_possible_base64_url(candidate)
            if decoded:
                return decoded
    return None


def _decode_possible_base64_url(value: str) -> str | None:
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            padded = value + ("=" * (-len(value) % 4))
            decoded = decoder(padded.encode("ascii")).decode("utf-8", errors="ignore")
        except Exception:
            continue
        if decoded.startswith(("http://", "https://")):
            return decoded
        decoded_json_url = _url_from_json(decoded)
        if decoded_json_url:
            return decoded_json_url
    return None


_JSON_URL_KEYS = ("e", "href", "url", "target")


def _url_from_json(value: str) -> str | None:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r'"(?:e|href|url|target)"\s*:\s*"(https?:[^"]+)"', value)
        if match:
            return _decode_json_string(match.group(1))
        return None
    if not isinstance(parsed, dict):
        return None
    for key in _JSON_URL_KEYS:
        candidate = parsed.get(key)
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            return candidate
    return None


def _decode_json_string(value: str) -> str:
    try:
        parsed = json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value
    return parsed if isinstance(parsed, str) else value
