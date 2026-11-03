"""Events gathered by script from venue calendars, listing sites and
university feeds, before the model runs.

The model then picks among these by id and writes why each fits; it never
supplies their title, date, time, venue or link. Every picked event still goes
through checks.validate_event, which fetches its own page again.

Every source is optional: one that cannot be fetched or parsed is logged and
skipped. Everything a source returns is untrusted page data and is held to
the same text and URL rules as model output.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from . import jsonld
from .common import Fetcher, Page, Rejected, canonical_url, checked_text, clean_text, log, validate_url

PACIFIC = jsonld.PACIFIC
TITLE_MAX_CHARS = 120
VENUE_MAX_CHARS = 120
CITY_MAX_CHARS = 60
PRICE_MAX_CHARS = 80
PER_SOURCE_MAX = 30
COLLECTED_MAX = 160
VENUE_UNKNOWN = "Venue on the event page"

# University feeds list hundreds of events (thesis defenses, deadlines,
# sports). Only these interests from profile.yaml are kept from them.
INTERESTS_RE = re.compile(
    r"(?i)\b(?:lectures?|talks?|panel|astronom\w*|planetari\w*|telescopes?|observatory|star party|space|"
    r"cosmo\w*|galax\w*|black holes?|fusion|plasma|AI|artificial intelligence|machine learning|robot\w*|"
    r"open source|japan\w*|anime|manga|jazz|concerts?|film|screenings?|cinema|cooking|tasting)\b"
)
MONTHS = {name: number for number, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"), start=1)}


@dataclass(frozen=True)
class Collected:
    id: str
    source: str
    title: str
    day: dt.date
    start: dt.datetime | None  # Pacific; None when the source gave a date only
    end: dt.datetime | None
    venue: str
    city: str
    url: str
    listing_url: str
    price_note: str

    def prompt_line(self) -> str:
        when = f"{self.start:%a %Y-%m-%d %H:%M}" if self.start else f"{self.day:%a %Y-%m-%d} (time on page)"
        # "|" separates the fields, so it never appears inside one.
        price = f" | {self.price_note.replace('|', '/')}" if self.price_note else ""
        return (f"{self.id} | {when} | {self.title.replace('|', '/')} | "
                f"{self.venue.replace('|', '/')}, {self.city.replace('|', '/')}{price} | {self.source}")

    def raw_item(self, why: str, reminder: bool) -> dict[str, Any]:
        """The event in the model's item shape, so checks.validate_event can
        hold it to every rule a researched item meets."""
        start = self.start or dt.datetime.combine(self.day, dt.time(0, 0), PACIFIC)
        item: dict[str, Any] = {
            "title": self.title, "start": start.isoformat(), "venue": self.venue, "city": self.city,
            "url": self.url, "why": why, "reminder": reminder,
            "source_urls": [self.listing_url] if self.listing_url and self.listing_url != self.url else [],
        }
        if self.end is not None and self.start is not None:
            item["end"] = self.end.isoformat()
        if self.price_note:
            item["price_note"] = self.price_note
        return item


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    kind: str  # "jsonld", "localist", "livewhale", "exploratorium" or "calacademy"
    venue: str = ""
    city: str = ""
    interests_only: bool = False
    pages: int = 1
    limit: int = PER_SOURCE_MAX


EB = "https://www.eventbrite.com/d/ca--san-francisco"
MEETUP = "https://www.meetup.com/find/?location=us--ca--san-francisco&source=EVENTS&keywords="
SOURCES: tuple[Source, ...] = (
    Source("chabot", "https://chabotspace.org/events/", "jsonld", "Chabot Space & Science Center", "Oakland"),
    Source("exploratorium", "https://www.exploratorium.edu/visit/calendar/after-dark", "exploratorium",
           "Exploratorium, Pier 15", "San Francisco"),
    Source("calacademy", "https://www.calacademy.org/nightlife", "calacademy",
           "California Academy of Sciences", "San Francisco"),
    Source("luma-sf", "https://luma.com/sf", "jsonld"),
    Source("luma-sf-tech", "https://luma.com/sf/tech", "jsonld"),
    Source("luma-ai-sf", "https://luma.com/ai-sf", "jsonld"),
    Source("meetup-japanese", MEETUP + "japanese", "jsonld"),
    Source("meetup-astronomy", MEETUP + "astronomy", "jsonld"),
    Source("eventbrite-japanese", f"{EB}/japanese/", "jsonld"),
    Source("eventbrite-science-tech", f"{EB}/science-and-tech--events/", "jsonld"),
    Source("eventbrite-astronomy", f"{EB}/astronomy/", "jsonld"),
    Source("eventbrite-jazz", f"{EB}/jazz/", "jsonld"),
    Source("eventbrite-cooking", f"{EB}/cooking-class/", "jsonld"),
    Source("stanford", "https://events.stanford.edu/api/2/events?start={start}&days={days}&pp=100&page={page}",
           "localist",
           "Stanford University", "Stanford", interests_only=True, pages=6, limit=20),
    # Without an explicit range the feed's first page was sometimes two months
    # old (seen 2026-10-18), so both ends are always given.
    Source("berkeley", "https://events.berkeley.edu/live/json/events/start_date/{start}/end_date/{end}"
           "/response_fields/location?page={page}", "livewhale", "UC Berkeley", "Berkeley",
           interests_only=True, pages=12, limit=20),
)


# San Francisco Bay Area and the near north, as a box. Listing sites leak
# events from elsewhere (Eventbrite's SF astronomy page listed Brooklyn).
BAY_AREA = ((36.8, 39.0), (-123.6, -121.0))
CALIFORNIA = ("ca", "california")


def in_area(region: str, geo: tuple[float, float] | None) -> bool:
    """False only on positive evidence of elsewhere: a missing location is
    left to the event page check. The region wins over geo: Eventbrite's SF
    listing gave an Ontario event (addressRegion "ON", and addressCountry
    "CA" for Canada) San Francisco coordinates."""
    if region and region.strip().lower() not in CALIFORNIA:
        return False
    if geo is not None:
        (lat_lo, lat_hi), (lon_lo, lon_hi) = BAY_AREA
        return lat_lo <= geo[0] <= lat_hi and lon_lo <= geo[1] <= lon_hi
    return True


@dataclass
class Found:
    """One event as a source states it, before the shared rules."""

    title: str
    day: dt.date | None
    start: dt.datetime | None
    end: dt.datetime | None
    venue: str
    city: str
    url: str
    price_note: str = ""


Parser = Callable[[Page, Source], list[Found]]


def _aware(value: Any) -> dt.datetime | None:
    day, moment = jsonld._moment(value)
    return moment


def parse_jsonld(page: Page, source: Source) -> list[Found]:
    found = []
    for event in jsonld.page_events(page):
        if event.status in jsonld.DEAD_STATUSES or event.online_only or event.sold_out or not event.link:
            continue
        if not in_area(event.region, event.geo):
            continue
        found.append(Found(event.name, event.start_date, event.start, event.end, event.venue or source.venue,
                           event.city or source.city, event.link, event.price_note))
    return found


def parse_localist(page: Page, source: Source) -> list[Found]:
    """Stanford's Localist API: {"events": [{"event": {...}}]}."""
    found = []
    for wrapper in _json(page).get("events", [])[:200]:
        event = wrapper.get("event") if isinstance(wrapper, dict) else None
        if not isinstance(event, dict) or event.get("experience") == "virtual" or event.get("status") != "live":
            continue
        instances = event.get("event_instances") or []
        instance = instances[0].get("event_instance") if instances and isinstance(instances[0], dict) else None
        if not isinstance(instance, dict) or instance.get("all_day"):
            continue
        start = _aware(instance.get("start"))
        price = "Free" if event.get("free") is True else ""
        found.append(Found(_s(event.get("title")), start.date() if start else None, start, _aware(instance.get("end")),
                           _s(event.get("location_name")) or source.venue, source.city,
                           _s(event.get("localist_url")), price))
    return found


CA_CITY_RE = re.compile(r"([A-Z][A-Za-z .]{1,40}?),?\s+CA\b")


def parse_livewhale(page: Page, source: Source) -> list[Found]:
    """Berkeley's LiveWhale feed: {"data": [{...}]}."""
    found = []
    for event in _json(page).get("data", [])[:200]:
        if not isinstance(event, dict) or event.get("is_all_day") or event.get("is_online") or event.get("is_canceled"):
            continue
        start = _aware(event.get("date_iso"))
        location = _s(event.get("location"))
        city = match.group(1).strip() if (match := CA_CITY_RE.search(location)) else source.city
        venue = location.split(",")[0].strip() if location else source.venue
        found.append(Found(html.unescape(_s(event.get("title"))), start.date() if start else None, start,
                           _aware(event.get("date2_iso")), venue, city, _s(event.get("url"))))
    return found


EXPLO_CARD_RE = re.compile(
    r'href="(?P<href>/visit/calendar/after-dark[a-z0-9-]*)".{0,6000}?card-title">(?P<title>[^<]{1,200})<'
    r'.{0,1500}?<time datetime="(?P<start>[^"]{10,40})"(?:.{0,200}?<time datetime="(?P<end>[^"]{10,40})")?',
    re.S,
)


def parse_exploratorium(page: Page, source: Source) -> list[Found]:
    found = []
    for match in EXPLO_CARD_RE.finditer(page.text):
        start = _aware(match.group("start"))
        found.append(Found(html.unescape(match.group("title")), start.date() if start else None, start,
                           _aware(match.group("end")), source.venue, source.city,
                           "https://www.exploratorium.edu" + match.group("href")))
    return found


CALACADEMY_ROW_RE = re.compile(
    r'<a href="(?P<href>/nightlife/[^"]{1,200})">(?P<title>[^<]{1,200})</a>.{0,1500}?'
    r'date-display-single">(?P<month>[A-Z][a-z]+) (?P<day>\d{1,2}) at (?P<hour>\d{1,2}):(?P<minute>\d{2}) (?P<ampm>[ap]m)<',
    re.S,
)


def parse_calacademy(page: Page, source: Source, today: dt.date | None = None) -> list[Found]:
    """Rows give "October 22 at 6:00 pm" with no year: the year is the one
    that puts the date nearest after yesterday."""
    today = today or dt.datetime.now(PACIFIC).date()
    found = []
    for match in CALACADEMY_ROW_RE.finditer(page.text):
        month = MONTHS.get(match.group("month").lower())
        if month is None:
            continue
        hour = int(match.group("hour")) % 12 + (12 if match.group("ampm") == "pm" else 0)
        try:
            day = dt.date(today.year, month, int(match.group("day")))
            if day < today - dt.timedelta(days=1):
                day = day.replace(year=today.year + 1)
            start = dt.datetime.combine(day, dt.time(hour, int(match.group("minute"))), PACIFIC)
        except ValueError:
            continue
        found.append(Found(html.unescape(match.group("title")), day, start, None, source.venue, source.city,
                           "https://www.calacademy.org" + match.group("href")))
    return found


PARSERS: dict[str, Parser] = {
    "jsonld": parse_jsonld,
    "localist": parse_localist,
    "livewhale": parse_livewhale,
    "exploratorium": parse_exploratorium,
    "calacademy": parse_calacademy,
}


def _json(page: Page) -> dict[str, Any]:
    try:
        data = json.loads(page.text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _s(value: Any) -> str:
    return value if isinstance(value, str) else ""


def event_id(url: str, day: dt.date) -> str:
    """Stable across runs, so a saved model answer can be checked again
    against a fresh collection."""
    return "e" + hashlib.sha256(f"{canonical_url(url)}|{day.isoformat()}".encode()).hexdigest()[:6]


def _plain(value: str, cap: int) -> str:
    """Feeds send entities ("&amp;") in some fields and not others."""
    return clean_text(html.unescape(value), cap)


# Feeds do not always flag online events: Berkeley's is_online was empty for
# "Online via Zoom" and "Virtual Classroom".
ONLINE_RE = re.compile(r"(?i)\b(?:zoom|online|virtual|webinar|livestream)\b")


def accept(found: Found, source: Source, window: tuple[dt.date, dt.date]) -> Collected:
    """The shared rules every source's event meets, or Rejected."""
    if found.day is None or not window[0] <= found.day <= window[1]:
        raise Rejected("outside the window or undated")
    if ONLINE_RE.search(found.venue):
        raise Rejected("online")
    title = checked_text(_plain(found.title, 10 * TITLE_MAX_CHARS), "title", TITLE_MAX_CHARS)
    if source.interests_only and not INTERESTS_RE.search(title):
        raise Rejected("not a listed interest")
    url = validate_url(found.url)
    venue = checked_text(_plain(found.venue, VENUE_MAX_CHARS), "venue", VENUE_MAX_CHARS, required=False)
    city = checked_text(_plain(found.city, CITY_MAX_CHARS), "city", CITY_MAX_CHARS, required=False)
    price = checked_text(_plain(found.price_note, PRICE_MAX_CHARS), "price", PRICE_MAX_CHARS, required=False)
    end = found.end if found.start is not None and found.end is not None and found.end >= found.start else None
    return Collected(event_id(url, found.day), source.name, title, found.day, found.start, end,
                     venue or VENUE_UNKNOWN, city or source.city or "Bay Area", url, listing_url(source), price)


def listing_url(source: Source) -> str:
    """The page a human can read the event on besides its own: a listing,
    never a paged API (its JSON is not evidence for one event)."""
    return "" if "{" in source.url else source.url


def source_urls(source: Source, window: tuple[dt.date, dt.date]) -> Iterable[str]:
    days = (window[1] - window[0]).days + 1
    for page in range(1, source.pages + 1):
        if "{" not in source.url:
            yield source.url
            return
        yield source.url.format(start=window[0].isoformat(), end=window[1].isoformat(), days=days, page=page)


def collect_source(source: Source, window: tuple[dt.date, dt.date], fetcher: Fetcher) -> list[Collected]:
    parser = PARSERS[source.kind]
    kept: dict[str, Collected] = {}
    for url in source_urls(source, window):
        page = fetcher(url)
        if page is None or not 200 <= page.status < 300 or not page.text:
            log.warning("collect: %s answered %s; skipping the rest of it", source.name,
                        "nothing" if page is None else f"HTTP {page.status}")
            break
        found = parser(page, source)
        for item in found:
            try:
                event = accept(item, source, window)
            except Rejected:
                continue
            # Some pages carry each event twice (Chabot: two ld+json blocks).
            kept.setdefault(event.id, event)
        # Paged feeds are in date order: stop once a page passes the window.
        dated = [f.day for f in found if f.day is not None]
        if not dated or max(dated) > window[1]:
            break
    ordered = sorted(kept.values(), key=lambda c: (c.day, c.start or dt.datetime.combine(c.day, dt.time(0), PACIFIC)))
    return ordered[:source.limit]


def collect(window: tuple[dt.date, dt.date], fetcher: Fetcher, sources: Iterable[Source] = SOURCES,
            counts: dict[str, int] | None = None) -> list[Collected]:
    """Every source's events in the window, deduplicated by link and day."""
    seen: set[str] = set()
    out: list[Collected] = []
    for source in sources:
        try:
            events = collect_source(source, window, fetcher)
        except Exception as exc:  # noqa: BLE001  one broken source must not stop the run
            log.warning("collect: %s failed (%s); skipping it", source.name, type(exc).__name__)
            events = []
        fresh = [e for e in events if e.id not in seen]
        seen.update(e.id for e in fresh)
        out.extend(fresh)
        if counts is not None:
            counts[source.name] = len(fresh)
    return out[:COLLECTED_MAX]
