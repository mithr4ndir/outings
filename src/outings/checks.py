"""Deterministic checks on the model's digest.

The model is never the only source for a date. Every event must:
- carry an ISO 8601 start with an explicit UTC offset that falls inside the
  window (America/Los_Angeles days);
- link to a public https page, and that page (or one of its source pages)
  must show the event's local date when the script fetches it.
An item that fails any rule is dropped, never repaired.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from . import research as r
from .common import Fetcher, Page, Rejected, canonical_url, checked_text, clean_text, fetch_page, log, validate_url

PACIFIC = ZoneInfo("America/Los_Angeles")
SECTION_MAX = 6
MAX_EVENT_DAYS = 7
DEFAULT_END_HOURS = 2
PAGES_PER_ITEM = 3

MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Event:
    section: str
    title: str
    start: dt.datetime  # America/Los_Angeles
    end: dt.datetime | None
    venue: str
    city: str
    url: str
    why: str
    price_note: str
    reminder: bool
    source_urls: tuple[str, ...]
    evidence: str  # "structured" (ISO date on the page) or "text" (written date)
    evidence_url: str


@dataclass(frozen=True)
class DayTrip:
    town: str
    why: str
    things_to_do: tuple[str, ...]
    drive_time: str
    source_urls: tuple[str, ...]


@dataclass
class Digest:
    window_start: dt.date
    window_end: dt.date
    sections: dict[str, list[Event]]
    top_picks: list[Event]
    day_trip: DayTrip | None
    dropped: int = 0
    drop_reasons: dict[str, int] = field(default_factory=dict)

    def events(self) -> list[Event]:
        return [event for name in r.SECTIONS for event in self.sections[name]]


def parse_start(raw: Any, key: str) -> dt.datetime:
    """An aware datetime in Pacific time, or Rejected. A naive value is
    refused: without an offset the hour is a guess."""
    if not isinstance(raw, str) or len(raw) > 40:
        raise Rejected(f"{key} is not a short string")
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        raise Rejected(f"{key} is not ISO 8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Rejected(f"{key} has no UTC offset")
    return parsed.astimezone(PACIFIC)


def page_text(page: Page) -> str:
    """The page as one line of text, tags removed and entities decoded. The raw
    HTML is kept too: JSON-LD startDate values live inside script tags."""
    stripped = html.unescape(TAG_RE.sub(" ", page.text))
    return SPACE_RE.sub(" ", f"{page.text} {stripped}")


def date_patterns(day: dt.date) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """(structured, written) patterns for one local date."""
    structured = re.compile(rf"\b{day.isoformat()}(?:T|\b)")
    month = MONTHS[day.month - 1]
    sept = r"|Sept\.?" if day.month == 9 else ""
    names = rf"(?:{month}|{month[:3]}\.?{sept})"
    d = rf"0?{day.day}(?:st|nd|rd|th)?(?!\d)"
    written = re.compile(
        rf"(?i)(?<![A-Za-z]){names}\s{{0,3}}{d}"  # October 8, Oct. 8th
        rf"|(?<!\d){d}\s{{0,3}}(?:of\s)?{names}(?![a-z])"  # 8 October
        rf"|(?<!\d)0?{day.month}/{d}(?:/(?:{day.year}|{day.year % 100}))?(?!\d)"  # 10/8, 10/8/2026
    )
    return structured, written


def date_evidence(day: dt.date, urls: Iterable[str], fetcher: Fetcher) -> tuple[str, str] | None:
    """(kind, url) for the first page that shows the date, or None."""
    structured, written = date_patterns(day)
    for url in list(urls)[:PAGES_PER_ITEM]:
        page = fetcher(url)
        if page is None or not 200 <= page.status < 300 or not page.text:
            continue
        text = page_text(page)
        if structured.search(text):
            return "structured", url
        if written.search(text):
            return "text", url
    return None


def clean_urls(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    kept: list[str] = []
    for value in raw[: r.SOURCE_URLS_MAX]:
        try:
            url = validate_url(value)
        except Rejected:
            continue
        if url not in kept:
            kept.append(url)
    return tuple(kept)


def validate_event(section: str, raw: Any, window: tuple[dt.date, dt.date], seen_urls: set[str],
                   fetcher: Fetcher, require_date_on_page: bool) -> Event:
    if not isinstance(raw, dict):
        raise Rejected("item is not an object")
    keys = set(raw)
    if not set(r.ITEM_KEYS_REQUIRED) <= keys or not keys <= {*r.ITEM_KEYS_REQUIRED, *r.ITEM_KEYS_OPTIONAL}:
        raise Rejected("item does not have the schema keys")
    title = checked_text(raw["title"], "title", r.TITLE_MAX_CHARS)
    venue = checked_text(raw["venue"], "venue", r.VENUE_MAX_CHARS)
    city = checked_text(raw["city"], "city", r.CITY_MAX_CHARS)
    why = checked_text(raw["why"], "why", r.WHY_MAX_CHARS)
    price = checked_text(raw.get("price_note"), "price_note", r.PRICE_MAX_CHARS, required=False)
    if not isinstance(raw["reminder"], bool):
        raise Rejected("reminder is not a boolean")
    url = validate_url(raw["url"])
    start = parse_start(raw["start"], "start")
    first, last = window
    if not first <= start.date() <= last:
        raise Rejected("start is outside the window")
    end = None
    if raw.get("end") is not None:
        end = parse_start(raw["end"], "end")
        if end < start or end - start > dt.timedelta(days=MAX_EVENT_DAYS):
            raise Rejected("end is before start or too far after it")
    sources = tuple(u for u in clean_urls(raw["source_urls"]) if u != url)
    if require_date_on_page:
        found = date_evidence(start.date(), (url, *sources), fetcher)
        if found is None:
            raise Rejected("date not found on the event page or its sources")
        evidence, evidence_url = found
    else:
        evidence, evidence_url = "unchecked", ""
    # A repeat is allowed, but it is always labelled as one.
    reminder = raw["reminder"] or canonical_url(url) in seen_urls
    return Event(section, title, start, end, venue, city, url, why, price, reminder, sources, evidence, evidence_url)


def validate_day_trip(raw: Any, fetcher: Fetcher) -> DayTrip:
    if not isinstance(raw, dict) or set(raw) != set(r.DAY_TRIP_KEYS):
        raise Rejected("day trip does not have exactly the schema keys")
    town = checked_text(raw["town"], "town", 60)
    why = checked_text(raw["why"], "day trip why", r.WHY_MAX_CHARS)
    drive = checked_text(raw["drive_time_from_sf"], "drive time", r.DRIVE_MAX_CHARS)
    todos_raw = raw["things_to_do"]
    if not isinstance(todos_raw, list) or not todos_raw:
        raise Rejected("things_to_do is empty")
    todos = []
    for item in todos_raw[: r.TODOS_MAX]:
        try:
            todos.append(checked_text(item, "thing to do", r.TODO_MAX_CHARS))
        except Rejected:
            continue
    if not todos:
        raise Rejected("no thing to do survived the text rules")
    # Undated, so the check is that its sources exist and answer.
    sources = tuple(u for u in clean_urls(raw["source_urls"]) if (p := fetcher(u)) is not None and 200 <= p.status < 400)
    if not sources:
        raise Rejected("no day trip source answered")
    return DayTrip(town, why, tuple(todos), drive, sources)


def build_digest(raw: Any, window: tuple[dt.date, dt.date], seen_urls: set[str],
                 fetcher: Fetcher | None = None, require_date_on_page: bool = True) -> Digest:
    """The validated digest. Raw output that is not an object is an error;
    a bad item only costs that item."""
    if not isinstance(raw, dict):
        raise Rejected("digest is not an object")
    fetcher = fetcher or cached(fetch_page)
    digest = Digest(window[0], window[1], {name: [] for name in r.SECTIONS}, [], None)
    batch: set[str] = set()

    def drop(label: str, reason: str) -> None:
        digest.dropped += 1
        digest.drop_reasons[reason] = digest.drop_reasons.get(reason, 0) + 1
        log.info("item %r dropped: %s", label, reason)

    for section in r.SECTIONS:
        items = raw.get(section)
        if not isinstance(items, list):
            log.warning("section %s is missing", section)
            continue
        for item in items[: r.SECTION_SCHEMA_MAX]:
            title = item.get("title") if isinstance(item, dict) else None
            label = clean_text(title, 60) if isinstance(title, str) else "?"
            try:
                event = validate_event(section, item, window, seen_urls, fetcher, require_date_on_page)
                key = (canonical_url(event.url), event.start.date())
                if key in batch:
                    raise Rejected("duplicate within this run")
                if len(digest.sections[section]) >= SECTION_MAX:
                    raise Rejected("section is full")
            except Rejected as exc:
                drop(label, str(exc))
                continue
            batch.add(key)
            digest.sections[section].append(event)
        digest.sections[section].sort(key=lambda e: e.start)

    try:
        digest.day_trip = validate_day_trip(raw.get("day_trip"), fetcher)
    except Rejected as exc:
        drop("day trip", str(exc))

    digest.top_picks = choose_top_picks(raw.get("top_picks"), digest)
    return digest


def choose_top_picks(raw: Any, digest: Digest) -> list[Event]:
    """The model's picks that survived, topped up from the sections in order.
    Reminders only fill in when nothing new is left."""
    events = digest.events()
    by_url = {canonical_url(e.url): e for e in events}
    picks: list[Event] = []
    for value in raw if isinstance(raw, list) else []:
        if isinstance(value, str) and (event := by_url.get(canonical_url(value))) and event not in picks:
            picks.append(event)
    for reminders in (False, True):
        queues = [[e for e in digest.sections[name] if e.reminder == reminders and e not in picks] for name in r.SECTIONS]
        # Round robin across sections, soonest first within each.
        while len(picks) < 3 and any(queues):
            for queue in queues:
                if queue and len(picks) < 3:
                    picks.append(queue.pop(0))
    return picks[:3]


def cached(fetcher: Callable[[str], Page | None]) -> Fetcher:
    """One fetch per URL per run: an item and its sources often share pages."""
    memo: dict[str, Page | None] = {}

    def fetch(url: str) -> Page | None:
        if url not in memo:
            memo[url] = fetcher(url)
        return memo[url]

    return fetch
