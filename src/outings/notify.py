"""The weekly Discord message: the top three picks and a link to the page.

Ported from lab-scout. SECURITY: every model string goes through
discord_text(), the payload carries allowed_mentions {"parse": []}, and the
webhook URL is never logged or put in an exception.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .checks import Digest, Event
from .common import OutingsError, clean_text, log, register_secret, strip_mentions
from .render import when

WEBHOOK_ENV = "OUTINGS_WEBHOOK_URL"
WEBHOOK_RE = re.compile(
    r"^https://(?:discord\.com|discordapp\.com|ptb\.discord\.com|canary\.discord\.com)"
    r"/api/webhooks/[0-9]{1,30}/[A-Za-z0-9_-]{1,200}$"
)
USERNAME = "Weekly outings"
USER_AGENT = "DiscordBot (https://github.com/mithr4ndir/outings, 1.0)"
EMBED_COLOR = 0xB5452F
HTTP_TIMEOUT_SECS = 20
POST_MAX_ATTEMPTS = 5
POST_MAX_SLEEP_SECS = 60.0

MARKDOWN_CHARS_RE = re.compile(r"([\\\[\]()*_~`|>#<@-])")
URL_SCHEME_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,20})://")
MENTION_RE = re.compile(r"@(?=\S)")


def discord_text(value: str, limit: int) -> str:
    """Make untrusted text inert in Discord: clean and cap, defang URL
    schemes, then escape every markdown character in one pass."""
    text = clean_text(strip_mentions(value), limit)
    text = URL_SCHEME_RE.sub(r"\1[:]//", text)
    text = MARKDOWN_CHARS_RE.sub(r"\\\1", text)
    return MENTION_RE.sub("@​", text)


def pick_embed(event: Event) -> dict[str, Any]:
    return {
        "title": discord_text(event.title, 200),
        "url": event.url,
        "description": (
            f"**{discord_text(when(event), 80)}**\n{discord_text(f'{event.venue}, {event.city}', 200)}\n"
            f"{discord_text(event.why, 300)}"
        ),
        "color": EMBED_COLOR,
    }


def build_payload(digest: Digest, page_url: str) -> dict[str, Any]:
    count = len(digest.events())
    window = f"{digest.window_start:%b} {digest.window_start.day} to {digest.window_end:%b} {digest.window_end.day}"
    trip = f" Day trip idea: {discord_text(digest.day_trip.town, 60)}." if digest.day_trip else ""
    content = (
        f"🗓️ Weekly outings for {window}: {count} event{'' if count == 1 else 's'} with checked dates.{trip}\n"
        f"Full digest: {page_url}"
    )
    return {
        "username": USERNAME,
        "content": content[:2000],
        "embeds": [pick_embed(e) for e in digest.top_picks],
        # SECURITY: the text is shaped by web content. Nothing may ever ping.
        "allowed_mentions": {"parse": []},
    }


def resolve_webhook() -> str | None:
    """The webhook URL from OUTINGS_WEBHOOK_URL, or None with the reason logged.
    Registered for redaction before it is checked."""
    raw = os.environ.get(WEBHOOK_ENV)
    if raw is None or not raw.strip():
        log.warning("webhook: %s is not set; not posting", WEBHOOK_ENV)
        return None
    value = raw.strip()
    register_secret(raw)
    register_secret(value)
    if not WEBHOOK_RE.match(value):
        log.warning("webhook: %s is not a Discord webhook URL; not posting", WEBHOOK_ENV)
        return None
    return value


class PostError(OutingsError):
    pass


def retry_after_secs(error: urllib.error.HTTPError) -> float:
    try:
        value = float(json.loads(error.read() or b"{}").get("retry_after"))
    except (ValueError, TypeError, AttributeError, OSError):
        try:
            value = float(error.headers.get("Retry-After", "1"))
        except (ValueError, TypeError, AttributeError):
            value = 1.0
    return min(max(value, 0.0), POST_MAX_SLEEP_SECS)


def post_payload(webhook: str, payload: dict[str, Any], opener: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
    """POST one message. Raises PostError whose text never holds the URL."""
    opener = opener or urllib.request.urlopen
    data = json.dumps(payload).encode("utf-8")
    delay = 1.0
    for attempt in range(1, POST_MAX_ATTEMPTS + 1):
        request = urllib.request.Request(webhook + "?wait=true", data=data, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            # WEBHOOK_RE already pinned the scheme to https.
            with opener(request, timeout=HTTP_TIMEOUT_SECS) as response:  # noqa: S310
                status = getattr(response, "status", 200)
                if 200 <= status < 300:
                    return
                failure, retryable = f"HTTP {status}", status >= 500
        except urllib.error.HTTPError as exc:
            status = exc.code
            failure = f"HTTP {status}"
            if status == 429:
                wait = retry_after_secs(exc)
                exc.close()
                if attempt == POST_MAX_ATTEMPTS:
                    break
                log.info("discord rate limited; retrying in %.1fs", wait)
                sleep(wait)
                continue
            exc.close()
            retryable = status >= 500
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # These can carry the URL in their text. Keep only the type.
            failure, retryable = type(exc).__name__, True
        if not retryable or attempt == POST_MAX_ATTEMPTS:
            raise PostError(f"discord post failed: {failure}") from None
        sleep(min(delay, POST_MAX_SLEEP_SECS))
        delay *= 2
    raise PostError("discord post failed: still rate limited after retries") from None
