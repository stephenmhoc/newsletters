from __future__ import annotations

import base64
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr
from typing import Any

import google.auth
from google.auth.exceptions import DefaultCredentialsError
from googleapiclient.discovery import build

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
# Gmail message IDs are hex. Anything else never becomes a link or a lookup.
GMAIL_ID_PATTERN = re.compile(r"^[0-9a-f]{8,32}$")


def gmail_link(gmail_id: str) -> str:
    """Open a message in Gmail. #all/ also works for label-only mail, unlike #inbox/."""
    return f"https://mail.google.com/mail/u/0/#all/{gmail_id}"


def unify_gmail_link(url: str) -> str:
    """Rewrite stored #inbox/ links from older versions to #all/."""
    if url.startswith("https://mail.google.com/") and "#inbox/" in url:
        return url.replace("#inbox/", "#all/", 1)
    return url


def sender_name(sender: str) -> str:
    """The display name from a From header, else its address."""
    name, address = parseaddr(sender)
    return name or address or sender.strip()


@dataclass(frozen=True)
class NewsletterMessage:
    gmail_id: str
    thread_id: str
    subject: str
    sender: str
    received_at: datetime
    html: str
    text: str
    gmail_url: str

    @property
    def has_body(self) -> bool:
        return bool(self.html or self.text)


def build_gmail_service():
    try:
        credentials, _ = google.auth.default(scopes=[GMAIL_READONLY_SCOPE])
    except DefaultCredentialsError as exc:
        raise RuntimeError(
            "Gmail credentials were not found. Run: "
            "gcloud auth application-default login "
            f"--scopes={GMAIL_READONLY_SCOPE},https://www.googleapis.com/auth/cloud-platform"
        ) from exc
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def find_label_id(service, user_id: str, label_name: str) -> str:
    response = service.users().labels().list(userId=user_id).execute()
    labels = response.get("labels", [])
    for label in labels:
        if label.get("name") == label_name or label.get("id") == label_name:
            return label["id"]
    known = ", ".join(sorted(label.get("name", "") for label in labels if label.get("name")))
    raise ValueError(f'Gmail label "{label_name}" was not found. Known labels include: {known}')


def list_messages_for_window(service, user_id: str, label_id: str, start: datetime, end: datetime) -> list[str]:
    query = f"after:{int(start.timestamp())} before:{int(end.timestamp())}"
    ids: list[str] = []
    request = service.users().messages().list(
        userId=user_id,
        labelIds=[label_id],
        q=query,
        includeSpamTrash=False,
        maxResults=100,
    )
    while request is not None:
        response = request.execute()
        ids.extend(message["id"] for message in response.get("messages", []))
        request = service.users().messages().list_next(request, response)
    return ids


def fetch_message(service, user_id: str, message_id: str, timezone) -> NewsletterMessage:
    """Fetch one message. One without an HTML or text body has has_body False."""
    response = service.users().messages().get(userId=user_id, id=message_id, format="full").execute()
    received_at = datetime.fromtimestamp(int(response["internalDate"]) / 1000, tz=timezone)
    payload = response.get("payload", {})
    headers = {item.get("name", "").lower(): item.get("value", "") for item in payload.get("headers", [])}
    subject = headers.get("subject", "(no subject)")
    sender = headers.get("from", "")
    html_parts: list[str] = []
    text_parts: list[str] = []
    for part in _walk_parts(payload):
        mime_type = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if not data:
            continue
        decoded = _decode_part(data)
        if mime_type == "text/html":
            html_parts.append(decoded)
        elif mime_type == "text/plain":
            text_parts.append(decoded)

    return NewsletterMessage(
        gmail_id=response["id"],
        thread_id=response.get("threadId", ""),
        subject=subject,
        sender=sender,
        received_at=received_at,
        html="\n".join(html_parts),
        text="\n".join(text_parts),
        gmail_url=gmail_link(response["id"]),
    )


def _walk_parts(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    parts = payload.get("parts")
    if not parts:
        yield payload
        return
    for part in parts:
        yield from _walk_parts(part)


def _decode_part(data: str) -> str:
    padded = data + ("=" * (-len(data) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")

