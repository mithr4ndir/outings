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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from zoneinfo import ZoneInfo

from . import jsonld
from . import research as r
from .collect import Collected
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
    # Fields taken from the page's own event data instead of the model:
    # any of "time", "venue", "city", "price".
    checked: tuple[str, ...] = ()
    # "collected:<source>" for an event the script collected and the model
    # picked, "research" for one the model found itself.
    origin: str = "research"


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


NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def squash(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def amounts(text: str) -> set[str]:
    """Dollar figures as plain numbers: "$1,250.00" and "1250" are both "1250"."""
    found = set()
    for raw in NUMBER_RE.findall(text):
        number = raw.replace(",", "")
        found.add(number[:-3] if number.endswith(".00") else number)
    return found


def keep_model_venue(model: str, page: str) -> bool:
    """The model's venue says what the page says and more (a gallery name
    where the page only gives the street), so it is the better text."""
    return bool(squash(page)) and squash(page) in squash(model)


def keep_model_price(model: str, page: str) -> bool:
    """The model's note states the page's figures and adds detail (a drink
    minimum, what the price includes)."""
    if not model:
        return False
    if page == "Free":
        return "free" in model.lower()
    wanted = amounts(page)
    return bool(wanted) and wanted <= amounts(model)


def page_facts(url: str, urls: Iterable[str], day: dt.date, fetcher: Fetcher) -> jsonld.PageEvent | None:
    """The event's data from the first page that carries it, or None. Raises
    Rejected when that data contradicts the model (see jsonld.match)."""
    for candidate in list(urls)[:PAGES_PER_ITEM]:
        page = fetcher(candidate)
        if page is None or not 200 <= page.status < 300 or not page.text:
            continue
        found = jsonld.match(page, url, day)
        if found is not None:
            return found
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
    checked: list[str] = []
    if require_date_on_page:
        found = date_evidence(start.date(), (url, *sources), fetcher)
        if found is None:
            raise Rejected("date not found on the event page or its sources")
        evidence, evidence_url = found
        # The page's own data wins over the model for every field it states.
        facts = page_facts(url, (url, *sources), start.date(), fetcher)
        if facts is not None:
            if facts.start is not None:
                # The model's end survives only where the page gives none
                # and it still fits after the page's start.
                if facts.end is not None:
                    end = facts.end
                elif end is not None and not facts.start <= end <= facts.start + dt.timedelta(days=MAX_EVENT_DAYS):
                    end = None
                start = facts.start
                checked.append("time")
            if facts.venue:
                if not keep_model_venue(venue, facts.venue):
                    venue = facts.venue
                checked.append("venue")
            if facts.city:
                city = facts.city
                checked.append("city")
            if facts.price_note:
                if not keep_model_price(price, facts.price_note):
                    price = facts.price_note
                checked.append("price")
    else:
        evidence, evidence_url = "unchecked", ""
    # A repeat is allowed, but it is always labelled as one.
    reminder = raw["reminder"] or canonical_url(url) in seen_urls
    return Event(section, title, start, end, venue, city, url, why, price, reminder, sources, evidence, evidence_url,
                 tuple(checked))


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


def validate_pick(raw: Any, collected: Mapping[str, Collected], window: tuple[dt.date, dt.date],
                  seen_urls: set[str], fetcher: Fetcher, require_date_on_page: bool) -> Event:
    """A collected event the model picked. The model supplies only the id,
    the section, why and the reminder flag; everything else comes from the
    collection, and the event meets every rule a researched one does."""
    if not isinstance(raw, dict) or set(raw) != set(r.PICK_KEYS):
        raise Rejected("pick does not have exactly the schema keys")
    if raw["section"] not in r.SECTIONS:
        raise Rejected("pick section is unknown")
    if not isinstance(raw["id"], str) or raw["id"] not in collected:
        raise Rejected("pick id is not a collected event")
    if not isinstance(raw["reminder"], bool):
        raise Rejected("reminder is not a boolean")
    why = checked_text(raw["why"], "why", r.WHY_MAX_CHARS)
    found = collected[raw["id"]]
    event = validate_event(raw["section"], found.raw_item(why, raw["reminder"]), window, seen_urls, fetcher,
                           require_date_on_page)
    if found.start is None and "time" not in event.checked:
        raise Rejected("no start time on the event page")
    return replace(event, origin=f"collected:{found.source}")


def build_digest(raw: Any, window: tuple[dt.date, dt.date], seen_urls: set[str],
                 fetcher: Fetcher | None = None, require_date_on_page: bool = True,
                 collected: Mapping[str, Collected] | None = None, research: bool = True) -> Digest:
    """The validated digest. Raw output that is not an object is an error;
    a bad item only costs that item. Picks from the collection fill each
    section before researched items do."""
    if not isinstance(raw, dict):
        raise Rejected("digest is not an object")
    fetcher = fetcher or cached(fetch_page)
    digest = Digest(window[0], window[1], {name: [] for name in r.SECTIONS}, [], None)
    batch: set[str] = set()

    def drop(label: str, reason: str) -> None:
        digest.dropped += 1
        digest.drop_reasons[reason] = digest.drop_reasons.get(reason, 0) + 1
        log.info("item %r dropped: %s", label, reason)

    def add(event: Event) -> None:
        key = (canonical_url(event.url), event.start.date())
        if key in batch:
            raise Rejected("duplicate within this run")
        if len(digest.sections[event.section]) >= SECTION_MAX:
            raise Rejected("section is full")
        batch.add(key)
        digest.sections[event.section].append(event)

    collected = collected or {}
    picks = raw.get("picks")
    for pick in picks[: r.PICKS_MAX] if isinstance(picks, list) else []:
        label = pick.get("id") if isinstance(pick, dict) and isinstance(pick.get("id"), str) else "?"
        try:
            add(validate_pick(pick, collected, window, seen_urls, fetcher, require_date_on_page))
        except Rejected as exc:
            drop(clean_text(label, 20), str(exc))

    for section in r.SECTIONS:
        items = raw.get(section)
        if not isinstance(items, list):
            log.warning("section %s is missing", section)
            continue
        researched = 0
        for item in items[: r.SECTION_SCHEMA_MAX]:
            title = item.get("title") if isinstance(item, dict) else None
            label = clean_text(title, 60) if isinstance(title, str) else "?"
            try:
                if not research:
                    raise Rejected("research is off")
                if researched >= r.RESEARCHED_PER_SECTION:
                    raise Rejected("too many researched items in the section")
                add(validate_event(section, item, window, seen_urls, fetcher, require_date_on_page))
            except Rejected as exc:
                drop(label, str(exc))
                continue
            researched += 1
    for events in digest.sections.values():
        events.sort(key=lambda e: e.start)

    try:
        digest.day_trip = validate_day_trip(raw.get("day_trip"), fetcher)
    except Rejected as exc:
        drop("day trip", str(exc))

    digest.top_picks = choose_top_picks(raw.get("top_picks"), digest, collected)
    return digest


def choose_top_picks(raw: Any, digest: Digest, collected: Mapping[str, Collected] | None = None) -> list[Event]:
    """The model's top picks that survived, given as collected ids or urls,
    topped up from the sections in order. Reminders only fill in when
    nothing new is left."""
    events = digest.events()
    by_url = {canonical_url(e.url): e for e in events}
    collected = collected or {}
    picks: list[Event] = []
    for value in raw if isinstance(raw, list) else []:
        if not isinstance(value, str):
            continue
        url = collected[value].url if value in collected else value
        if (event := by_url.get(canonical_url(url))) and event not in picks:
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
