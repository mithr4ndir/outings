"""Shared helpers: logging that never leaks secrets, text cleaning, URL rules
and an HTTP GET that can only reach public hosts.

Much of this is ported from lab-scout (github.com/mithr4ndir/lab-scout), where
the same rules guard web-shaped model output and a Discord webhook.
"""

from __future__ import annotations

import datetime as dt
import http.client
import ipaddress
import logging
import os
import re
import socket
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("outings")

USER_AGENT = "Mozilla/5.0 (compatible; outings/0.1; +https://github.com/mithr4ndir/outings)"
URL_MAX_CHARS = 400
PAGE_TIMEOUT_SECS = 20
PAGE_MAX_BYTES = 4 << 20
MAX_REDIRECTS = 5

HOSTNAME_RE = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
PRIVATE_SUFFIXES = (".local", ".lan", ".internal", ".localhost", ".home.arpa", ".localdomain", ".home", ".corp")


class OutingsError(Exception):
    """An expected failure, with a message that is safe to log."""


class Rejected(Exception):
    """An item that is dropped. The message is safe to log (no URL)."""


# ---------------------------------------------------------------------------
# Logging that can never leak the webhook or the OAuth token
# ---------------------------------------------------------------------------

_SECRETS: set[str] = set()
WEBHOOK_ANY_RE = re.compile(r"https?://[^\s\"'<>]*/api/webhooks/[^\s\"'<>]*", re.IGNORECASE)


def register_secret(value: str) -> None:
    if value:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in _SECRETS:
        text = text.replace(secret, "[redacted]")
    return WEBHOOK_ANY_RE.sub("[redacted webhook]", text)


class RedactingFormatter(logging.Formatter):
    """Redacts after formatting, so arguments and tracebacks are covered too."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(stream: Any = None) -> None:
    """One line per record on stderr; the container runtime timestamps it."""
    log.handlers.clear()
    log.setLevel(logging.DEBUG if os.environ.get("OUTINGS_DEBUG") else logging.INFO)
    log.propagate = False
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
    log.addHandler(handler)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

WHITESPACE_RE = re.compile(r"\s+")
MENTION_FORMS_RE = re.compile("(?i)@​?(?:everyone|here)\\b|<[@#][!&]?[0-9]*>?")
IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
SECRETLIKE_RE = re.compile(r"(?i)\b(?:op://|ghp_|github_pat_|sk-ant-|xox[abp]-|password\s*[:=]|token\s*[:=])")


def clean_text(value: str, limit: int) -> str:
    """Strip control and invisible format characters, collapse whitespace, cap."""
    kept = []
    for char in value:
        category = unicodedata.category(char)
        kept.append(" " if category in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") else char)
    text = WHITESPACE_RE.sub(" ", "".join(kept)).strip()
    if len(text) > limit:
        text = text[: max(limit - 1, 0)].rstrip() + "…"
    return text


def strip_mentions(value: str) -> str:
    return WHITESPACE_RE.sub(" ", MENTION_FORMS_RE.sub("", value)).strip()


def checked_text(raw: Any, key: str, cap: int, required: bool = True) -> str:
    """Model text, cleaned and held to the rules, or Rejected. Never repaired
    beyond whitespace: text that breaks a rule drops the item."""
    if raw is None and not required:
        return ""
    if not isinstance(raw, str):
        raise Rejected(f"{key} is not a string")
    text = strip_mentions(clean_text(raw, 10 * cap))
    if not text:
        if required:
            raise Rejected(f"{key} is empty")
        return ""
    if len(text) > cap:
        raise Rejected(f"{key} is longer than {cap} characters")
    if IPV4_RE.search(text):
        raise Rejected(f"{key} contains an IP address")
    if SECRETLIKE_RE.search(text) or WEBHOOK_ANY_RE.search(text):
        raise Rejected(f"{key} contains secret-like text")
    if "://" in text or re.search(r"(?i)\bwww\.", text):
        raise Rejected(f"{key} contains a link")
    return text


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def validate_url(url: Any) -> str:
    """A plain https URL to a public host name, normalised, or Rejected."""
    if not isinstance(url, str):
        raise Rejected("url is not a string")
    url = url.strip()
    if not url or len(url) > URL_MAX_CHARS or not url.isprintable() or re.search(r"[\s<>\"'`\\]", url):
        raise Rejected("url has forbidden characters or is too long")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise Rejected("url does not parse") from None
    if parts.scheme != "https":
        raise Rejected("url is not https")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise Rejected("url carries userinfo")
    if port is not None or parts.netloc.endswith(":"):
        raise Rejected("url names a port")
    host = (parts.hostname or "").lower()
    if not host or is_ip_literal(host) or host.startswith("["):
        raise Rejected("url host is an IP literal or empty")
    if host == "localhost" or host.endswith(PRIVATE_SUFFIXES) or not HOSTNAME_RE.match(host):
        raise Rejected("url host is local or not a public host name")
    return urllib.parse.urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def canonical_url(url: str) -> str:
    """Dedup key: lowercased host and path, no query, fragment or trailing slash."""
    parts = urllib.parse.urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}".lower()


def public_host(host: str, resolver: Callable[..., Any] | None = None) -> bool:
    """True only when every address the host resolves to is globally routable."""
    resolver = resolver or socket.getaddrinfo
    try:
        infos = resolver(host, 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False
    try:
        return all(ipaddress.ip_address(addr.split("%", 1)[0]).is_global for addr in addresses)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Fetching a page the model named
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Page:
    status: int
    url: str
    text: str


Fetcher = Callable[[str], Page | None]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are followed by fetch_page itself, one checked hop at a time."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        return None


def fetch_page(url: str, opener: Callable[..., Any] | None = None,
               resolver: Callable[..., Any] | None = None) -> Page | None:
    """GET a model-named URL. None when it cannot be read.

    SECURITY: the URL came from the model, which read untrusted pages. Every
    hop, redirects included, must be a plain https URL to a host that resolves
    only to public addresses, so the job cannot be pointed at the lab LAN or a
    cluster service. The pod's egress NetworkPolicy is the second layer. The
    body is capped and only searched for a date, never executed or rendered.
    """
    opener = opener or urllib.request.build_opener(NoRedirect).open
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            current = validate_url(current)
        except Rejected:
            return None
        if not public_host(urllib.parse.urlsplit(current).hostname or "", resolver):
            return None
        request = urllib.request.Request(current, method="GET", headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.8",
        })
        try:
            with opener(request, timeout=PAGE_TIMEOUT_SECS) as response:
                status = getattr(response, "status", 200)
                body = response.read(PAGE_MAX_BYTES)
                charset = response.headers.get_content_charset() if getattr(response, "headers", None) else None
                return Page(status, current, body.decode(charset or "utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            code = exc.code
            exc.close()
            if 300 <= code < 400 and location:
                current = urllib.parse.urljoin(current, location)
                continue
            return Page(code, current, "")
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException, LookupError):
            return None
    return None


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def write_text_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    # A temp file in the same directory and a rename: atomic on NFS as well.
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
