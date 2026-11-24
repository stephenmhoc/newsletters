from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


@dataclass(frozen=True)
class GmailConfig:
    label: str
    user_id: str
    timezone: str

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class SummaryConfig:
    openrouter_api_key_env: str
    openrouter_keychain_service: str | None
    openrouter_keychain_account: str | None
    model: str
    max_input_chars: int
    site_url: str
    app_name: str
    prompt: str


@dataclass(frozen=True)
class RssConfig:
    title: str
    description: str
    # A URL on the public feed host; the three feed URLs are built next to it
    # (https://host/<digest|full|all>/<token>/feed.xml), and proxied images are
    # served from its origin.
    public_url: str
    max_items: int | None
    digest_hour: int = 19
    # Newest items in the "Newsletters: Everything" feed.
    all_feed_max_items: int = 50
    # Sender address (lowercase) -> publication name shown in digests. Without
    # an entry, a sender's most common prior name wins among its variants.
    publication_names: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int


@dataclass(frozen=True)
class StorageConfig:
    sqlite_path: Path
    log_path: Path


@dataclass(frozen=True)
class ImagesConfig:
    """The signed image proxy on the public listener (/img/<signature>/<url>)."""

    proxy: bool = True
    cache_dir: Path = Path("var/image-cache")
    cache_max_mb: int = 512
    max_image_mb: int = 10


@dataclass(frozen=True)
class WebConfig:
    """Private web UI (behind Cloudflare Access) and the full-text feed."""

    enabled: bool = False
    port: int = 18792
    bind: str = "127.0.0.1"
    # Public origin of the UI, e.g. https://newsletters.example.com. It is the
    # only accepted Host/Origin and the base of digest "Read in full" links.
    public_url: str = ""
    access_team_domain: str = ""
    access_aud: str = ""
    allowed_emails: tuple[str, ...] = ()
    secret_env: str = "WEB_SECRET"
    full_feed_max_items: int = 30
    # Env var holding FreshRSS's token-authenticated refresh URL for the
    # full-text feed; when set, adds and removals reach FreshRSS right away.
    reader_refresh_url_env: str = "FRESHRSS_REFRESH_URL"
    # Local development only: skips Cloudflare Access verification.
    dev_insecure_no_auth: bool = False


@dataclass(frozen=True)
class AppConfig:
    path: Path
    root: Path
    gmail: GmailConfig
    summary: SummaryConfig
    rss: RssConfig
    server: ServerConfig
    storage: StorageConfig
    web: WebConfig = field(default_factory=WebConfig)
    images: ImagesConfig = field(default_factory=ImagesConfig)


DEFAULT_MODEL = "anthropic/claude-opus-5.5"
# About 50k tokens. Bodies longer than this are cut with an explicit marker.
DEFAULT_MAX_INPUT_CHARS = 200_000


def _anthropic_model(model: str) -> str:
    """Summaries must come from a Claude model, routed through OpenRouter."""
    if not model.startswith("anthropic/claude-"):
        raise ValueError(
            f"summary.model must be an OpenRouter Anthropic model such as {DEFAULT_MODEL!r}; got {model!r}"
        )
    return model


def _path(root: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return root / candidate


def _require(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise ValueError(f"Missing required config key: {key}")
    return mapping[key]


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    root = config_path.parent
    _load_env_file(root / ".env")
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    gmail = raw.get("gmail") or {}
    summary = raw.get("summary") or {}
    rss = raw.get("rss") or {}
    server = raw.get("server") or {}
    storage = raw.get("storage") or {}
    web = raw.get("web") or {}
    images = raw.get("images") or {}

    digest_hour = int(rss.get("digest_hour", 19))
    if not 0 <= digest_hour <= 23:
        raise ValueError("rss.digest_hour must be an hour from 0 to 23")

    return AppConfig(
        path=config_path,
        root=root,
        gmail=GmailConfig(
            label=str(_require(gmail, "label")),
            user_id=str(gmail.get("user_id", "me")),
            timezone=str(gmail.get("timezone", "America/New_York")),
        ),
        summary=SummaryConfig(
            openrouter_api_key_env=str(summary.get("openrouter_api_key_env", "OPENROUTER_API_KEY")),
            openrouter_keychain_service=summary.get("openrouter_keychain_service"),
            openrouter_keychain_account=summary.get("openrouter_keychain_account"),
            model=_anthropic_model(str(summary.get("model", DEFAULT_MODEL))),
            max_input_chars=int(summary.get("max_input_chars", DEFAULT_MAX_INPUT_CHARS)),
            site_url=str(summary.get("site_url", "")),
            app_name=str(summary.get("app_name", "Newsletter Summary")),
            prompt=str(summary.get("prompt", "")),
        ),
        rss=RssConfig(
            title=str(rss.get("title", "Newsletters: Daily Digest")),
            description=str(rss.get("description", "Daily newsletter highlights.")),
            public_url=str(rss.get("public_url", "")),
            max_items=rss.get("max_items"),
            digest_hour=digest_hour,
            all_feed_max_items=_positive(rss, "all_feed_max_items", 50, "rss"),
            publication_names={
                str(address).strip().lower(): str(name).strip()
                for address, name in (rss.get("publication_names") or {}).items()
                if str(address).strip() and str(name).strip()
            },
        ),
        server=ServerConfig(
            host=str(server.get("host", "127.0.0.1")),
            port=int(server.get("port", 18791)),
        ),
        storage=StorageConfig(
            sqlite_path=_path(root, str(storage.get("sqlite_path", "var/newsletter-summary.sqlite3"))),
            log_path=_path(root, str(storage.get("log_path", "var/newsletter-summary.log"))),
        ),
        web=_web_config(web),
        images=ImagesConfig(
            proxy=bool(images.get("proxy", True)),
            cache_dir=_path(root, str(images.get("cache_dir", "var/image-cache"))),
            cache_max_mb=_positive(images, "cache_max_mb", 512, "images"),
            max_image_mb=_positive(images, "max_image_mb", 10, "images"),
        ),
    )


def _positive(mapping: dict[str, Any], key: str, default: int, section: str) -> int:
    value = int(mapping.get(key, default))
    if value < 1:
        raise ValueError(f"{section}.{key} must be at least 1")
    return value


def _web_config(web: dict[str, Any]) -> WebConfig:
    emails = web.get("allowed_emails") or []
    if isinstance(emails, str):
        emails = [emails]
    max_items = int(web.get("full_feed_max_items", 30))
    if max_items < 1:
        raise ValueError("web.full_feed_max_items must be at least 1")
    return WebConfig(
        enabled=bool(web.get("enabled", False)),
        port=int(web.get("port", 18792)),
        bind=str(web.get("bind", "127.0.0.1")),
        public_url=str(web.get("public_url", "")).rstrip("/"),
        access_team_domain=str(web.get("access_team_domain", "")).strip().removeprefix("https://").rstrip("/"),
        access_aud=str(web.get("access_aud", "")).strip(),
        allowed_emails=tuple(str(email).strip().lower() for email in emails if str(email).strip()),
        secret_env=str(web.get("secret_env", "WEB_SECRET")),
        full_feed_max_items=max_items,
        reader_refresh_url_env=str(web.get("reader_refresh_url_env", "FRESHRSS_REFRESH_URL")),
        dev_insecure_no_auth=bool(web.get("dev_insecure_no_auth", False)),
    )
