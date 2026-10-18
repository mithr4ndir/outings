"""Event facts from a page's own schema.org data (JSON-LD).

Event sites embed a machine-readable copy of each event in
<script type="application/ld+json">: start and end with a UTC offset, the
venue, ticket offers and a status. checks.py already fetches the event's page
to find its date; this reads the same page so the start time, venue, city and
price come from the venue's data instead of from the model.

SECURITY: the page is untrusted. Only the fields below are read, every string
goes through the same text rules as model output, sizes and nesting are
capped, and nothing is ever executed or rendered as HTML.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from .common import Page, Rejected, canonical_url, checked_text

PACIFIC = ZoneInfo("America/Los_Angeles")
LD_BLOCK_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
BLOCKS_MAX = 20
BLOCK_MAX_CHARS = 1 << 20
DEPTH_MAX = 8
EVENTS_MAX = 200
NESTED_KEYS = ("@graph", "itemListElement", "item", "subEvent", "mainEntity")
SCHEMA = "https://schema.org/"

# Statuses that mean the event will not happen as listed.
DEAD_STATUSES = ("EventCancelled", "EventPostponed", "EventMovedOnline")


@dataclass(frozen=True)
class PageEvent:
    """One Event object as the page states it. Times are aware, Pacific."""

    name: str
    url: str  # canonical, or "" when the object has none
    start_date: dt.date | None
    # The local date of endDate, however far away. A series page often gives
    # its first session as startDate and its last as endDate.
    last_date: dt.date | None
    start: dt.datetime | None  # None when the page gives a date only
    end: dt.datetime | None  # None when absent or longer than a week
    venue: str  # the place name and street, as far as the page states them
    city: str
    price_note: str
    status: str  # the schema.org status name, e.g. "EventScheduled", or ""
    online_only: bool
    sold_out: bool


def _schema_name(value: Any) -> str:
    """'https://schema.org/EventCancelled' or 'EventCancelled' -> 'EventCancelled'."""
    if not isinstance(value, str):
        return ""
    return value.rsplit("/", 1)[-1].strip()


def _is_event(obj: dict[str, Any]) -> bool:
    kinds = obj.get("@type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    return any(isinstance(k, str) and k.endswith("Event") for k in kinds)


def _walk(obj: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    if depth > DEPTH_MAX:
        return
    if isinstance(obj, list):
        for value in obj[:EVENTS_MAX]:
            yield from _walk(value, depth + 1)
    elif isinstance(obj, dict):
        if _is_event(obj):
            yield obj
        for key in NESTED_KEYS:
            if key in obj:
                yield from _walk(obj[key], depth + 1)


def _blocks(text: str) -> Iterator[Any]:
    for raw in LD_BLOCK_RE.findall(text)[:BLOCKS_MAX]:
        if len(raw) > BLOCK_MAX_CHARS:
            continue
        for candidate in (raw, html.unescape(raw)):
            try:
                yield json.loads(candidate)
                break
            except ValueError:
                continue


def _text(value: Any, key: str, cap: int) -> str:
    """A page string held to the model text rules, or "" when it breaks one."""
    if not isinstance(value, str):
        return ""
    try:
        return checked_text(html.unescape(value), key, cap)
    except Rejected:
        return ""


def _moment(value: Any) -> tuple[dt.date | None, dt.datetime | None]:
    """(local date, aware local datetime or None). A date-only value gives a
    date and no time; a datetime without an offset gives neither, because its
    hour would be a guess."""
    if not isinstance(value, str) or len(value) > 40:
        return None, None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return dt.date.fromisoformat(text), None
        except ValueError:
            return None, None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None, None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, None
    local = parsed.astimezone(PACIFIC)
    return local.date(), local


def _street(address: dict[str, Any], city: str) -> str:
    street = _text(address.get("streetAddress"), "page street", 160)
    # Eventbrite repeats the city, state and zip inside streetAddress, and
    # Meetup sends ", San Francisco, CA" with no street at all.
    if street and city:
        cut = street.lower().find(city.lower())
        if cut >= 0:
            street = street[:cut]
    return street.strip(" ,")


def _place(location: Any) -> tuple[str, str, bool]:
    """(venue, city, online_only) from a location value. The venue joins the
    place name and its street, without repeating one inside the other."""
    places = location if isinstance(location, list) else [location]
    venue = city = ""
    physical = virtual = False
    for place in places[:4]:
        if not isinstance(place, dict):
            continue
        kind = place.get("@type")
        if kind == "VirtualLocation":
            virtual = True
            continue
        physical = True
        if venue:
            continue
        address = place.get("address")
        street = ""
        if isinstance(address, dict):
            city = city or _text(address.get("addressLocality"), "page city", 60)
            street = _street(address, city)
        name = _text(place.get("name"), "page venue", 120)
        # Some sites (Luma) hide the address and name the place after the
        # city, e.g. "San Francisco, CA". That is not a venue.
        if name and city and name.lower().startswith(city.lower()):
            name = ""
        # A name that is itself an address (Eventbrite does this) stands
        # alone: a second, different street would only add a contradiction.
        if name and street and not name[0].isdigit() and street.lower() not in name.lower() \
                and name.lower() not in street.lower():
            venue = f"{name}, {street}"
        else:
            venue = name or street
    return venue[:120], city, virtual and not physical


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and re.fullmatch(r"\s*\d{1,6}(?:\.\d{1,2})?\s*", value):
        return float(value)
    return None


def _money(amount: float) -> str:
    return f"${amount:,.0f}" if amount == int(amount) else f"${amount:,.2f}"


def _offers(offers: Any) -> tuple[str, bool]:
    """(price note, sold out). Only US dollar prices are stated."""
    items = offers if isinstance(offers, list) else [offers]
    prices: list[float] = []
    availability: list[str] = []
    for offer in items[:20]:
        if not isinstance(offer, dict):
            continue
        availability.append(_schema_name(offer.get("availability")))
        currency = offer.get("priceCurrency")
        if currency is not None and (not isinstance(currency, str) or currency.upper() != "USD"):
            continue
        for key in ("price", "lowPrice", "highPrice"):
            amount = _number(offer.get(key))
            if amount is not None:
                prices.append(amount)
    sold_out = bool(availability) and all(a == "SoldOut" for a in availability)
    if not prices:
        return "", sold_out
    low, high = min(prices), max(prices)
    if high == 0:
        return "Free", sold_out
    return (_money(low) if low == high else f"{_money(low)} to {_money(high)}"), sold_out


def parse_event(obj: dict[str, Any]) -> PageEvent:
    start_date, start = _moment(obj.get("startDate"))
    last_date, end = _moment(obj.get("endDate"))
    if start is None or end is None or end < start or end - start > dt.timedelta(days=7):
        end = None
    venue, city, online_only = _place(obj.get("location"))
    attendance = _schema_name(obj.get("eventAttendanceMode"))
    price_note, sold_out = _offers(obj.get("offers"))
    url = obj.get("url")
    return PageEvent(
        name=_text(obj.get("name"), "page title", 200),
        url=canonical_url(url) if isinstance(url, str) and url.startswith("https://") else "",
        start_date=start_date,
        last_date=last_date if last_date is not None and start_date is not None and last_date >= start_date else None,
        start=start,
        end=end,
        venue=venue,
        city=city,
        price_note=price_note,
        status=_schema_name(obj.get("eventStatus")),
        online_only=online_only or attendance == "OnlineEventAttendanceMode",
        sold_out=sold_out,
    )


def page_events(page: Page) -> list[PageEvent]:
    """Every Event object on the page, in page order."""
    events: list[PageEvent] = []
    for block in _blocks(page.text):
        for obj in _walk(block):
            events.append(parse_event(obj))
            if len(events) >= EVENTS_MAX:
                return events
    return events


def match(page: Page, event_url: str, day: dt.date) -> PageEvent | None:
    """The page's Event for this event on this local date, or None.

    An Event is this event when its url is the event's url, or when it is the
    only Event on a page fetched from the event's own url. A listing page with
    many Events and no url match says nothing about this event.

    Raises Rejected when the event's own page data contradicts the model:
    a different date, or a cancelled, postponed or online-only event.
    """
    wanted = canonical_url(event_url)
    events = page_events(page)
    own_page = canonical_url(page.url) == wanted
    mine = [e for e in events if e.url == wanted]
    if not mine and own_page and len(events) == 1 and events[0].url in ("", wanted):
        mine = events
    if not mine:
        return None
    on_day = [e for e in mine if e.start_date == day]
    if not on_day:
        # Only an unambiguous single Event can contradict the model's date,
        # and only when the day is outside its whole span: a series page
        # gives its first session and its last, with the rest in between.
        only = mine[0] if len(mine) == 1 else None
        if only is not None and only.start_date is not None and not only.start_date <= day <= (only.last_date or only.start_date):
            raise Rejected("page data shows a different date")
        return None
    found = on_day[0]
    if found.status in DEAD_STATUSES:
        raise Rejected(f"page data says {found.status[len('Event'):].lower()}")
    if found.online_only:
        raise Rejected("page data says online only")
    if found.sold_out:
        raise Rejected("page data says sold out")
    return found
