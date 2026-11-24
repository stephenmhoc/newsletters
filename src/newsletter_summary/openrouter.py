from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import requests

from .email_extract import ExtractedLink, normalize_url
from .gmail_client import NewsletterMessage, sender_name
from .secrets import get_secret

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
REQUEST_TIMEOUT_SECONDS = 90
MAX_ATTEMPTS = 3
RETRY_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
MAX_BULLETS = 3
TRUNCATION_MARKER = (
    "\n\n[Truncated: the email continues beyond this point. Summarize only the text above, "
    "and do not guess what the rest says.]"
)

ITEM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "publication": {"type": "string"},
        "authors": {"type": "array", "items": {"type": "string"}},
        "url": {"type": "string"},
        "content_access": {"type": "string", "enum": ["full", "partial"]},
        "bullets": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "publication", "authors", "url", "content_access", "bullets"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class SummaryItem:
    title: str
    url: str
    publication: str
    authors: list[str]
    bullets: list[str]
    content_access: str


def summarize_message(
    *,
    api_key_env: str,
    keychain_service: str | None,
    keychain_account: str | None,
    model: str,
    max_input_chars: int,
    app_name: str,
    site_url: str,
    base_prompt: str,
    message: NewsletterMessage,
    body_markdown: str,
    links: list[ExtractedLink],
    web_links: list[ExtractedLink] | None = None,
    fallback_url: str = "",
) -> SummaryItem | None:
    """Summarize one email. fallback_url (default: the Gmail link) replaces a URL not found in the email."""
    api_key = _api_key(api_key_env, keychain_service, keychain_account)
    web_links = web_links or []
    raw_item = _request_item(
        api_key=api_key,
        model=model,
        app_name=app_name,
        site_url=site_url,
        system=_system_prompt(base_prompt),
        user=_user_prompt(message, truncate_body(body_markdown, max_input_chars), links, web_links),
    )
    if raw_item is None:
        return None
    allowed_urls = {normalize_url(link.url) for link in [*links, *web_links]}
    return _coerce_item(raw_item, message, allowed_urls, fallback_url or message.gmail_url)


def summarize_article(
    *,
    api_key_env: str,
    keychain_service: str | None,
    keychain_account: str | None,
    model: str,
    max_input_chars: int,
    app_name: str,
    site_url: str,
    base_prompt: str,
    title: str,
    url: str,
    site_name: str,
    byline: str,
    body_markdown: str,
    links: list[ExtractedLink],
) -> SummaryItem | None:
    """Summarize a web page saved with "Add a link"; the same model, schema, and bullet rules as email.

    The returned url is always the page's own (canonical) URL.
    """
    api_key = _api_key(api_key_env, keychain_service, keychain_account)
    link_lines = "\n".join(f"- {link.text}: {link.url}" for link in links[:120])
    user = (
        f"Page title: {title}\n"
        f"Site: {site_name}\n"
        f"Byline: {byline or '(none)'}\n\n"
        f"SOURCE_URL: {url}\n\n"
        f"Links in the article:\n{link_lines or '(none)'}\n\n"
        f"Page text:\n{truncate_body(body_markdown, max_input_chars)}"
    )
    raw_item = _request_item(
        api_key=api_key,
        model=model,
        app_name=app_name,
        site_url=site_url,
        system=_article_system_prompt(base_prompt),
        user=user,
    )
    if raw_item is None:
        return None
    message = NewsletterMessage(
        gmail_id="", thread_id="", subject=title, sender=site_name, received_at=datetime.now(UTC), html="", text="",
        gmail_url=url,
    )
    return _coerce_item(raw_item, message, {normalize_url(url)}, url)


def _api_key(api_key_env: str, keychain_service: str | None, keychain_account: str | None) -> str:
    api_key = get_secret(api_key_env, keychain_service, keychain_account)
    if not api_key:
        keychain_hint = ""
        if keychain_service:
            keychain_hint = f" or add it to Keychain service {keychain_service!r}"
        raise RuntimeError(f"Missing OpenRouter API key. Set ${api_key_env}{keychain_hint}.")
    return api_key


def _request_item(*, api_key: str, model: str, app_name: str, site_url: str, system: str, user: str) -> dict | None:
    """One strict-JSON-schema request; the item object the model returned, or None."""
    payload = {
        "model": model,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "newsletter_item", "strict": True, "schema": ITEM_SCHEMA},
        },
        # Only route to providers that honor the JSON schema.
        "provider": {"require_parameters": True},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": site_url or "http://localhost",
        "X-Title": app_name,
    }
    data = _post_with_retries(headers, payload)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"OpenRouter returned an unexpected response shape: {str(data)[:1000]}") from exc
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenRouter returned non-JSON content: {content[:1000]}") from exc
    if not isinstance(parsed, dict):
        return None
    # Tolerate the older {"item": {...}} wrapper in case a model adds it.
    item = parsed.get("item")
    return item if isinstance(item, dict) else parsed


def truncate_body(body: str, max_chars: int) -> str:
    """Cut an over-long body, telling the model that it was cut."""
    if len(body) <= max_chars:
        return body
    logging.info("Email body is %s characters; sending the first %s", len(body), max_chars)
    return body[:max_chars] + TRUNCATION_MARKER


def _post_with_retries(headers: dict[str, str], payload: dict) -> dict:
    """POST to OpenRouter, retrying rate limits and transient server errors with backoff."""
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                OPENROUTER_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            last_error = f"OpenRouter request failed: {exc}"
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(last_error) from exc
        else:
            if response.status_code < 400:
                data = response.json()
                error = _error_body(data)
                if error is None:
                    return data
                # OpenRouter can answer 200 with {"error": {...}} when the
                # upstream provider fails mid-request.
                code, message = error
                last_error = f"OpenRouter error {code or 'in a 200 response'}: {message[:1000]}"
                if (code is not None and code not in RETRY_STATUS_CODES) or attempt == MAX_ATTEMPTS:
                    raise RuntimeError(last_error)
                delay = _retry_delay(attempt)
                logging.warning("%s; retrying in %ss (attempt %s/%s)", last_error, delay, attempt, MAX_ATTEMPTS)
                time.sleep(delay)
                continue
            last_error = f"OpenRouter error {response.status_code}: {response.text[:1000]}"
            if response.status_code not in RETRY_STATUS_CODES or attempt == MAX_ATTEMPTS:
                raise RuntimeError(last_error)
        delay = _retry_delay(attempt)
        logging.warning("%s; retrying in %ss (attempt %s/%s)", last_error, delay, attempt, MAX_ATTEMPTS)
        time.sleep(delay)
    raise RuntimeError(last_error or "OpenRouter request failed")


def _error_body(data: object) -> tuple[int | None, str] | None:
    """(code, message) for a response body that reports an error instead of choices."""
    if not isinstance(data, dict) or "error" not in data or data.get("choices"):
        return None
    error = data.get("error")
    if not isinstance(error, dict):
        return None, str(error)
    code = error.get("code")
    return (code if isinstance(code, int) else None), str(error.get("message") or error)


def _retry_delay(attempt: int) -> int:
    return 2**attempt


def _system_prompt(base_prompt: str) -> str:
    return (
        f"{base_prompt.strip()}\n\n"
        "Return one JSON object for the newsletter email with these fields:\n"
        "- title: the article's own headline. Drop subject-line clutter such as the newsletter name in "
        "brackets, issue numbers, or emoji when the body shows the real headline.\n"
        "- publication: the newsletter or publication name, not an email address.\n"
        "- authors: the people credited as writing this issue, from bylines or sign-offs in the body. Use the "
        "sender's display name only when it is clearly a person. Use an empty list when no author is "
        "identifiable; never guess.\n"
        "- url: the supplied link that opens this exact article or issue on the web, preferring a web version "
        "link or the headline link, copied exactly. When no such link exists, use SOURCE_EMAIL_URL exactly.\n"
        "- content_access: partial when the email contains only an excerpt or preview of the article and the "
        "rest is unavailable, subscriber-only, or behind a paywall; otherwise full. Judge this by the main piece: "
        "a promotion whose only gated part is a perk such as a discount code is full.\n"
        f"- bullets: one to {MAX_BULLETS} bullets that carry the substance of the piece; most need one or two.\n\n"
        + _bullet_rules(document="email", piece="newsletter")
        + "Produce exactly one object for the entire email, no matter how many links it contains. Use only the "
        "email body and links supplied by the user. Ignore housekeeping such as unsubscribe links, ads, "
        "preferences, social profiles, and sponsor sections.\n"
    )


def _article_system_prompt(base_prompt: str) -> str:
    """The email prompt's rules, for a web page the reader saved."""
    return (
        f"{base_prompt.strip()}\n\n"
        "Return one JSON object for the web article with these fields:\n"
        "- title: the article's own headline, without the site name or section labels.\n"
        "- publication: the site or publication name.\n"
        "- authors: the people credited as writing the article, from its byline. Use an empty list when no "
        "author is identifiable; never guess.\n"
        "- url: SOURCE_URL exactly.\n"
        "- content_access: partial when the page text contains only an excerpt or preview of the article and "
        "the rest is unavailable, subscriber-only, or behind a paywall; otherwise full.\n"
        f"- bullets: one to {MAX_BULLETS} bullets that carry the substance of the piece; most need one or two.\n\n"
        + _bullet_rules(document="page", piece="article")
        + "Produce exactly one object for the whole article. Use only the page text and links supplied by the "
        "user. Ignore site chrome such as navigation, related stories, comments, ads, and sign-up prompts.\n"
    )


def _bullet_rules(*, document: str, piece: str) -> str:
    return (
        f"The bullets replace reading the {piece}. The reader will not open the article, so each bullet "
        "must teach them something on its own:\n"
        "- State the actual idea, finding, argument, or news, with the specifics that make it useful: names, "
        "numbers, dates, mechanisms, and the reason it matters. Prefer \"X cut costs 40% by doing Y\" over "
        "\"discusses how X cut costs\".\n"
        "- Never describe the article (\"the author explores\", \"this issue covers\", \"a look at\"); deliver "
        "its content directly.\n"
        "- Choose the most important or surprising points, not the first ones. Use as few bullets as the "
        f"{piece} needs: one for a single argument or a promotion, two for most, and three only when there are "
        "three separate ideas or stories that each matter on their own. Never pad, and never spread one idea "
        "across several bullets.\n"
        "- Keep each bullet to one sentence of about 20 words, never more than 30, in plain text with no "
        "leading bullet characters or markdown. Keep the one or two specifics that carry the point (a name, a "
        "number) and cut the rest: background, qualifiers, and secondary examples.\n"
        f"- Every name, number, and attribution must come from the {document}. When compressing, do not merge "
        f"separate facts, add names to a list, or say who did something unless the {document} says so. Do not "
        "link points with \"so\", \"proving\", or \"because\" unless the "
        f"{document} draws that connection, and keep the {document}'s hedges (\"may\", \"likely\", \"I believe\").\n"
        "- For recap, roundup, and index-style editions that briefly point to many stories, start the first "
        "bullet by saying it is a roundup of pieces published elsewhere (for example \"Weekly roundup: ...\"), "
        f"then give the key fact from the most significant items. Do not invent detail the {document} does not "
        "contain.\n"
        "- For subscriber-only previews, truncated editions, and excerpts that end at a paywall, use only the "
        "visible content; never reconstruct what the unavailable portion says.\n"
        f"- When the {document} is mainly an ad or promotion rather than an article (an event, sale, product, "
        "course, or subscription offer), write exactly one short bullet saying what is offered and its key "
        "details (what, when, where, price or discount). Do not add context, background, or marketing claims.\n"
        "- Do not rate the piece or advise whether to read it.\n\n"
    )


def _user_prompt(
    message: NewsletterMessage,
    body_markdown: str,
    links: list[ExtractedLink],
    web_links: list[ExtractedLink],
) -> str:
    link_lines = "\n".join(f"- {link.text}: {link.url}" for link in links[:120])
    web_link_lines = "\n".join(f"- {link.text}: {link.url}" for link in web_links[:5])
    return (
        f"Newsletter subject: {message.subject}\n"
        f"Newsletter sender: {message.sender}\n"
        f"Received at: {message.received_at.isoformat()}\n\n"
        f"SOURCE_EMAIL_URL: {message.gmail_url}\n\n"
        f"Web version links:\n{web_link_lines or '(none)'}\n\n"
        f"Extracted links:\n{link_lines or '(none)'}\n\n"
        f"Email body:\n{body_markdown}"
    )


def _coerce_item(
    item: dict, message: NewsletterMessage, allowed_urls: set[str], fallback_url: str
) -> SummaryItem | None:
    bullets = item.get("bullets")
    if isinstance(bullets, str):
        bullets = [bullets]
    if not isinstance(bullets, list):
        bullets = []
    bullets = [text for text in (_clean_bullet(bullet) for bullet in bullets) if text][:MAX_BULLETS]
    if not bullets:
        return None
    authors = item.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    if not isinstance(authors, list):
        authors = []
    content_access = str(item.get("content_access", "full")).strip().lower()
    if content_access not in {"full", "partial"}:
        content_access = "full"
    # Only accept a link that actually appeared in the email, so a hallucinated
    # URL can never reach the feed. Both sides are normalized: the model may copy
    # a raw redirect or tracking URL while the allow-list holds clean ones.
    raw_url = str(item.get("url") or "").strip()
    url = normalize_url(raw_url) if raw_url else ""
    if url not in allowed_urls:
        # The prompt names the fallback SOURCE_EMAIL_URL; some replies return that name.
        if raw_url and raw_url not in {message.gmail_url, "SOURCE_EMAIL_URL", "SOURCE_URL"}:
            logging.info("Model URL %s is not a link from %r; using the fallback", _without_query(url), message.subject)
        url = fallback_url
    return SummaryItem(
        title=_clean(item.get("title")) or message.subject or "Untitled newsletter",
        url=url,
        publication=_clean(item.get("publication")) or sender_name(message.sender),
        authors=[name for name in (_clean(author) for author in authors) if name],
        bullets=bullets,
        content_access=content_access,
    )


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _clean_bullet(value: object) -> str:
    return _clean(value).lstrip("-*•· ").strip()


def _without_query(url: str) -> str:
    """A URL safe to log: no query string or fragment, which may hold tokens."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(invalid URL)"
    return urlunsplit(parts._replace(query="", fragment=""))
