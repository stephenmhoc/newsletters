"""Turn a newsletter email (or a saved web article) into safe, readable HTML.

The result is stored in email_content and shown by the reading view and the
Reading List and Everything feeds.

BeautifulSoup does the email-specific clean-up (hidden pre-headers, tracking
pixels, housekeeping links, lazy-loaded images). nh3 is the security boundary:
its allow-list decides what HTML survives, both when an item is stored and
again whenever it is rendered.
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from html import escape

import nh3
from bs4 import BeautifulSoup, Comment, Tag

from .email_extract import is_housekeeping_link, normalize_url

# Bump whenever _sanitize_html changes what it keeps. Stored email content
# records the version that produced it and is re-derived from the stored raw
# email when it is older (on read, or with `newsletter-summary resanitize`).
SANITIZER_VERSION = 1
# Average adult silent reading speed, for "N min read".
WORDS_PER_MINUTE = 238

ALLOWED_TAGS = frozenset(
    {
        "a", "abbr", "b", "blockquote", "br", "caption", "code", "col", "colgroup", "dd", "del", "div",
        "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img",
        "ins", "li", "mark", "ol", "p", "pre", "q", "s", "small", "span", "strike", "strong", "sub", "sup",
        "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
    }
)  # fmt: skip
_CELL_ATTRIBUTES = frozenset({"colspan", "rowspan", "align", "valign"})
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height", "style"},
    "table": {"align"},
    "tr": {"align", "valign"},
    "td": set(_CELL_ATTRIBUTES),
    "th": set(_CELL_ATTRIBUTES),
    "col": {"span"},
    "colgroup": {"span"},
}
# The only inline style kept: images scale down to the reader's width.
IMAGE_STYLE = "max-width:100%;height:auto"
ALLOWED_STYLE_PROPERTIES = frozenset({"max-width", "height"})
URL_SCHEMES = frozenset({"http", "https", "mailto"})
# Removed with their content, by nh3 as well as during clean-up.
CONTENT_TAGS = frozenset(
    {"script", "style", "head", "title", "meta", "link", "form", "input", "button", "iframe", "object",
     "embed", "svg", "noscript", "template", "textarea", "select", "math", "audio", "video", "canvas"}
)  # fmt: skip

_HIDDEN_STYLE = re.compile(
    r"display:none|visibility:hidden|mso-hide:all|max-height:0(?:px)?(?:;|!|$)|opacity:0(?:\.0+)?(?:;|!|$)"
)
_HOUSEKEEPING_TEXT = re.compile(
    r"\bunsubscribe\b|manage (?:your )?(?:email |subscription )?preferences|"
    r"update (?:your )?preferences|view (?:this (?:email|post) )?(?:in|on) (?:your |the )?(?:browser|web)|"
    r"read (?:it )?(?:online|in (?:your )?browser)"
)
_BLOCKS = ("p", "li", "td", "div", "table", "center")
# Newsletter-platform chrome that means nothing outside the email: Substack's
# like/comment/restack/share row, "READ IN APP", and the forwarding blurb.
_CHROME_LINK_TEXT = re.compile(
    r"^(?:read in app|open in app|like|comment|comments|restack|share|share this post|listen|listen now|"
    r"view in browser|get the app)$",
    re.I,
)
_CHROME_BLOCK_TEXT = re.compile(r"^(?:forwarded this email\?|this post is public so feel free to share it)", re.I)
_CHROME_IMAGE_ALT = re.compile(r"start your substack|get the app|download (?:the|our) app|app store|google play", re.I)
# Interface icons and byline avatars (Substack's are 18px and 40px); real
# content images are larger.
_ICON_MAX_PX = 40
_TABLE_SECTIONS = ("thead", "tbody", "tfoot")
_PLACEHOLDER_SRC = re.compile(r"^data:|^about:|placeholder|spacer|blank\.gif|transparent\.gif|pixel\.gif", re.I)
_LAZY_SRC_ATTRIBUTES = ("data-src", "data-original", "data-lazy-src", "data-url")
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(px)?\s*$", re.I)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""

_CLEANER_OPTIONS: dict = {
    "tags": set(ALLOWED_TAGS),
    "clean_content_tags": set(CONTENT_TAGS),
    "attributes": ALLOWED_ATTRIBUTES,
    "url_schemes": set(URL_SCHEMES),
    "link_rel": "noopener noreferrer",
    "strip_comments": True,
    "filter_style_properties": set(ALLOWED_STYLE_PROPERTIES),
}
_cleaner = nh3.Cleaner(**_CLEANER_OPTIONS)

# Rewrites an http(s) image URL, e.g. to the signed image proxy.
ImageRewriter = Callable[[str], str]


@dataclass(frozen=True)
class SanitizedEmail:
    html: str
    # Inline (cid:) attachments cannot be shown in a feed; they become alt text.
    cid_images: int = 0


def clean_html(html: str, rewrite_image: ImageRewriter | None = None) -> str:
    """The nh3 security boundary; safe to run repeatedly on stored content.

    rewrite_image, when given, maps each http(s) <img src> inside nh3 itself
    (its attribute filter), so nothing re-serializes the HTML after nh3.
    """
    if rewrite_image is None:
        return _cleaner.clean(html)

    def attribute_filter(tag: str, attribute: str, value: str) -> str | None:
        if tag == "img" and attribute == "src" and value.lower().startswith(("http://", "https://")):
            return rewrite_image(value)
        return value

    return nh3.Cleaner(**_CLEANER_OPTIONS, attribute_filter=attribute_filter).clean(html)


def word_count(html: str) -> int:
    """Words of visible text in sanitized HTML."""
    if not html:
        return 0
    return len(BeautifulSoup(html, "html.parser").get_text(" ").split())


def reading_minutes(words: int) -> int:
    return max(1, math.ceil(words / WORDS_PER_MINUTE)) if words > 0 else 0


def sanitize_email(html: str, text: str) -> SanitizedEmail:
    if html.strip():
        return _sanitize_html(html)
    return SanitizedEmail(html=clean_html(plain_text_to_html(text)))


def plain_text_to_html(text: str) -> str:
    """Escaped paragraphs with line breaks preserved and bare URLs linked."""
    paragraphs = [block.strip() for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if block.strip()]
    return "\n".join(f"<p>{'<br>'.join(_linkify(line) for line in block.splitlines())}</p>" for block in paragraphs)


def _linkify(line: str) -> str:
    parts = []
    position = 0
    for match in _URL.finditer(line):
        raw = match.group(0)
        stripped = raw.rstrip(_TRAILING_PUNCTUATION)
        parts.append(escape(line[position : match.start()]))
        parts.append(f'<a href="{escape(normalize_url(stripped))}">{escape(stripped)}</a>')
        position = match.start() + len(stripped)
    parts.append(escape(line[position:]))
    return "".join(parts)


def _sanitize_html(html: str) -> SanitizedEmail:
    soup = BeautifulSoup(html, "html.parser")
    root: Tag = soup.body or soup

    for comment in root.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for picture in root.find_all("picture"):
        _unwrap_picture(picture)
    for tag in root.find_all(list(CONTENT_TAGS)):
        tag.decompose()
    for tag in root.find_all(_is_hidden):
        tag.decompose()

    cid_images = 0
    for image in root.find_all("img"):
        cid_images += _clean_image(soup, image)

    for anchor in root.find_all("a"):
        _clean_link(anchor)
    _remove_housekeeping_blocks(root)
    _remove_email_chrome(root)

    _flatten_layout_tables(root)

    for tag in root.find_all(True):
        if tag.name == "img":
            continue
        for attribute in list(tag.attrs):
            if attribute.lower() not in ALLOWED_ATTRIBUTES.get(tag.name, ()):
                del tag.attrs[attribute]

    body = root.decode_contents() if isinstance(root, Tag) else str(root)
    return SanitizedEmail(html=clean_html(body).strip(), cid_images=cid_images)


def _is_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden"):
        return True
    style = _attribute(tag, "style")
    if style and _HIDDEN_STYLE.search(re.sub(r"\s+", "", style.lower())):
        return True
    classes = tag.get("class") or []
    return any("preheader" in name.lower() for name in classes)


def _unwrap_picture(picture: Tag) -> None:
    image = picture.find("img")
    if not isinstance(image, Tag):
        picture.decompose()
        return
    if not _usable_src(_attribute(image, "src")) and not _attribute(image, "srcset"):
        for source in picture.find_all("source"):
            srcset = _attribute(source, "srcset")
            if srcset:
                image["srcset"] = srcset
                break
    picture.replace_with(image.extract())


def _clean_image(soup: BeautifulSoup, image: Tag) -> int:
    """Normalize or remove one image; returns 1 when it was an inline cid: image."""
    src = _attribute(image, "src").strip()
    if not _usable_src(src):
        lazy = next((_attribute(image, name) for name in _LAZY_SRC_ATTRIBUTES if _attribute(image, name)), "")
        candidate = lazy.strip() or _largest_srcset_candidate(
            _attribute(image, "srcset") or _attribute(image, "data-srcset")
        )
        if candidate:
            src = candidate
    alt = _attribute(image, "alt").strip()
    if src.lower().startswith("cid:"):
        if alt:
            image.replace_with(soup.new_string(alt))
        else:
            image.decompose()
        return 1
    if (
        not src.lower().startswith(("http://", "https://"))
        or _is_tracking_pixel(image)
        or _is_icon(image)
        or _CHROME_IMAGE_ALT.search(alt)
    ):
        image.decompose()
        return 0

    width = _real_size(_attribute(image, "width"))
    height = _real_size(_attribute(image, "height"))
    title = _attribute(image, "title")
    image.attrs = {"src": src}
    if alt:
        image["alt"] = alt
    if title:
        image["title"] = title
    if width:
        image["width"] = width
    if height:
        image["height"] = height
    image["style"] = IMAGE_STYLE
    return 0


def _usable_src(src: str) -> bool:
    return bool(src.strip()) and not _PLACEHOLDER_SRC.search(src.strip())


def _largest_srcset_candidate(srcset: str) -> str:
    best_url, best_size = "", -1.0
    for candidate in srcset.split(","):
        pieces = candidate.strip().split()
        if not pieces:
            continue
        size = 1.0
        if len(pieces) > 1:
            match = re.match(r"^(\d+(?:\.\d+)?)[wx]$", pieces[1].lower())
            size = float(match.group(1)) if match else 1.0
        if size > best_size:
            best_url, best_size = pieces[0], size
    return best_url


def _is_tracking_pixel(image: Tag) -> bool:
    for name in ("width", "height"):
        size = _pixels(_attribute(image, name))
        if size is not None and size <= 2:
            return True
    style = re.sub(r"\s+", "", _attribute(image, "style").lower())
    for name in ("width", "height", "max-width", "max-height"):
        match = re.search(rf"(?:^|;){name}:(\d+(?:\.\d+)?)px", style)
        if match and float(match.group(1)) <= 2:
            return True
    return False


def _is_icon(image: Tag) -> bool:
    width = _pixels(_attribute(image, "width"))
    height = _pixels(_attribute(image, "height"))
    return width is not None and height is not None and width <= _ICON_MAX_PX and height <= _ICON_MAX_PX


def _remove_email_chrome(root: Tag) -> None:
    for anchor in root.find_all("a"):
        if anchor.decomposed:
            continue
        text = " ".join(anchor.get_text(" ", strip=True).split())
        if _CHROME_LINK_TEXT.match(text) or (not text and anchor.find("img") is None):
            _remove_with_empty_parent(anchor)
    for block in reversed(root.find_all(list(_BLOCKS))):
        if block.decomposed:
            continue
        text = " ".join(block.get_text(" ", strip=True).split())
        if text and len(text) < 160 and _CHROME_BLOCK_TEXT.match(text) and block.find("img") is None:
            block.decompose()


def _flatten_layout_tables(root: Tag) -> None:
    """Turn email layout tables into plain blocks.

    RSS readers ignore our CSS, so a layout table would show as columns:
    avatars beside bylines, icons spread across a row. Data tables stay.
    Innermost tables go first, so each pass only sees its own rows and cells.
    """
    for table in reversed(root.find_all("table")):
        if table.decomposed or _is_data_table(table):
            continue
        for section in table.find_all(_TABLE_SECTIONS, recursive=False):
            section.unwrap()
        for child in table.find_all(["colgroup", "col"], recursive=False):
            child.decompose()
        for caption in table.find_all("caption", recursive=False):
            caption.name = "p"
        for row in table.find_all("tr", recursive=False):
            for cell in row.find_all(["td", "th"], recursive=False):
                cell.name = "div"
                cell.attrs = {}
            row.unwrap()
        table.name = "div"
        table.attrs = {}
    for block in reversed(root.find_all("div")):
        if not block.decomposed and not block.get_text(strip=True) and block.find(["img", "hr"]) is None:
            block.decompose()


def _is_data_table(table: Tag) -> bool:
    """A real table of values: header cells, or several rows of short text in several columns."""
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    if any(row.find("th", recursive=False) for row in rows):
        return True
    if len(rows) < 2 or table.find(["img", "table"]) is not None:
        return False
    widths = [len(row.find_all("td", recursive=False)) for row in rows]
    cells = [cell for row in rows for cell in row.find_all("td", recursive=False)]
    return min(widths) >= 2 and all(len(cell.get_text(" ", strip=True)) <= 120 for cell in cells)


def drop_leading_title(html: str, title: str) -> str:
    """Remove the email's own headline when it repeats the title shown above it."""
    wanted = _normalized_title(title)
    if not wanted or not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for heading in soup.find_all(["h1", "h2", "h3"], limit=3):
        if _normalized_title(heading.get_text(" ", strip=True)) == wanted:
            _remove_with_empty_parent(heading)
            return str(soup)
    return html


def _normalized_title(value: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", value.lower()).split())


def _pixels(value: str) -> float | None:
    match = _SIZE.match(value or "")
    return float(match.group(1)) if match else None


def _real_size(value: str) -> str:
    size = _pixels(value)
    return str(int(size)) if size is not None and size > 2 else ""


def _clean_link(anchor: Tag) -> None:
    href = _attribute(anchor, "href").strip()
    text = " ".join(anchor.get_text(" ", strip=True).split())
    url = normalize_url(href) if href else ""
    if url and is_housekeeping_link(text, url):
        _remove_with_empty_parent(anchor)
        return
    if url.lower().startswith(("http://", "https://", "mailto:")):
        anchor["href"] = url
    elif "href" in anchor.attrs:
        del anchor.attrs["href"]
    if not text and anchor.find("img") is None:
        anchor.decompose()


def _remove_with_empty_parent(tag: Tag) -> None:
    parent = tag.parent
    tag.decompose()
    # "Unsubscribe | Preferences" leaves only separators behind.
    while isinstance(parent, Tag) and parent.name in _BLOCKS:
        if re.sub(r"[\s|\u00b7\u2022\u2013\u2014-]+", "", parent.get_text()) or parent.find("img") is not None:
            break
        grandparent = parent.parent
        parent.decompose()
        parent = grandparent


def _remove_housekeeping_blocks(root: Tag) -> None:
    """Drop short footer blocks such as "You can unsubscribe or manage preferences"."""
    for block in reversed(root.find_all(list(_BLOCKS))):
        if block.decomposed:
            continue
        text = " ".join(block.get_text(" ", strip=True).split())
        if (
            text
            and len(text) < 300
            and _HOUSEKEEPING_TEXT.search(text.lower())
            and block.find(["img", "h1", "h2", "h3"]) is None
        ):
            block.decompose()


def _attribute(tag: Tag, name: str) -> str:
    value = tag.get(name)
    if isinstance(value, list):
        return " ".join(value)
    return value or ""
