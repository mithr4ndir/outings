"""Tests for facts read from a page's own JSON-LD event data.

The fixtures under tests/fixtures are the ld+json script blocks of real pages,
captured 2026-10-18 with the job's User-Agent and trimmed to those blocks.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from outings import checks, jsonld, render
from outings.common import Page, Rejected

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PT = checks.PACIFIC
WINDOW = (dt.date(2026, 10, 18), dt.date(2026, 11, 1))
LUMA = "https://luma.com/592fcwx2"
EVENTBRITE = "https://www.eventbrite.com/e/boroboro-japanese-mending-with-dorothy-yuki-tickets-1998998484278"
MEETUP = "https://www.meetup.com/business-growth-lab-solve-problems-build-together-sf/events/317028197/"
CHABOT = "https://chabotspace.org/events/"


def fixture(name: str, url: str) -> Page:
    return Page(200, url, (FIXTURES / name).read_text())


def ld_page(url: str, *objects: dict[str, Any]) -> Page:
    blocks = "".join(f'<script type="application/ld+json">{json.dumps(o)}</script>' for o in objects)
    return Page(200, url, f"<html><body><h1>Event</h1>{blocks}</body></html>")


def event_obj(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "MusicEvent",
        "name": "Late Set",
        "url": "https://tickets.example.org/e/late-set",
        "startDate": "2026-10-23T19:30:00-07:00",
        "endDate": "2026-10-23T21:00:00-07:00",
        "eventStatus": "https://schema.org/EventScheduled",
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": {"@type": "Place", "name": "The Blue Room",
                     "address": {"@type": "PostalAddress", "addressLocality": "Oakland"}},
        "offers": [{"@type": "Offer", "price": "35", "priceCurrency": "USD",
                    "availability": "https://schema.org/InStock"}],
    }
    base.update(overrides)
    return base


def raw_item(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Late Set",
        "start": "2026-10-23T20:00:00-07:00",
        "end": "2026-10-23T23:00:00-07:00",
        "venue": "Some Jazz Club",
        "city": "San Francisco",
        "url": "https://tickets.example.org/e/late-set",
        "why": "A quiet set in a small room.",
        "price_note": "about twenty dollars",
        "reminder": False,
        "source_urls": [],
    }
    base.update(overrides)
    return base


def fetcher_of(*pages: Page) -> Any:
    by_url = {p.url: p for p in pages}
    return lambda url: by_url.get(url)


# ---------------------------------------------------------------------------
# Parsing real pages
# ---------------------------------------------------------------------------


def test_luma_page_gives_time_city_and_free_price_but_not_a_fake_venue() -> None:
    found = jsonld.match(fixture("luma_event.html", LUMA), LUMA, dt.date(2026, 10, 19))
    assert found is not None
    assert found.start == dt.datetime(2026, 10, 19, 17, 0, tzinfo=PT)
    assert found.end == dt.datetime(2026, 10, 19, 21, 0, tzinfo=PT)
    assert found.city == "San Francisco"
    # Luma names the place "San Francisco, CA": a city, not a venue.
    assert found.venue == ""
    assert found.price_note == "Free"


def test_eventbrite_sold_out_event_is_dropped() -> None:
    with pytest.raises(Rejected, match="sold out"):
        jsonld.match(fixture("eventbrite_event.html", EVENTBRITE), EVENTBRITE, dt.date(2026, 10, 24))


def test_meetup_online_event_is_dropped() -> None:
    with pytest.raises(Rejected, match="online only"):
        jsonld.match(fixture("meetup_online_event.html", MEETUP), MEETUP, dt.date(2026, 10, 29))


def test_chabot_listing_parses_many_events_and_matches_none_without_a_url() -> None:
    page = fixture("chabot_listing.html", CHABOT)
    events = jsonld.page_events(page)
    assert len(events) > 10
    assert all(e.start is not None and e.start.tzinfo is not None for e in events if e.start_date)
    # Entities in names are decoded (the page writes &#8211;).
    assert not any("&#" in e.name for e in events)
    # A listing with many events is not evidence for any one of them.
    assert jsonld.match(page, "https://chabotspace.org/events/some-other-event/", events[0].start_date) is None


# ---------------------------------------------------------------------------
# Matching rules
# ---------------------------------------------------------------------------


def test_single_event_on_its_own_page_with_another_date_is_rejected() -> None:
    page = ld_page("https://tickets.example.org/e/late-set", event_obj())
    with pytest.raises(Rejected, match="different date"):
        jsonld.match(page, "https://tickets.example.org/e/late-set", dt.date(2026, 10, 24))


def test_series_page_without_this_date_is_inconclusive_not_a_rejection() -> None:
    page = ld_page("https://tickets.example.org/e/late-set",
                   event_obj(), event_obj(startDate="2026-10-30T19:30:00-07:00"))
    assert jsonld.match(page, "https://tickets.example.org/e/late-set", dt.date(2026, 10, 24)) is None


def test_listing_page_matches_by_url() -> None:
    listing = ld_page("https://listings.example.org/sf",
                      event_obj(url="https://tickets.example.org/e/other", name="Other"),
                      event_obj())
    found = jsonld.match(listing, "https://tickets.example.org/e/late-set/", dt.date(2026, 10, 23))
    assert found is not None and found.name == "Late Set"


@pytest.mark.parametrize("status", ["EventCancelled", "https://schema.org/EventPostponed", "EventMovedOnline"])
def test_dead_statuses_are_dropped(status: str) -> None:
    page = ld_page("https://tickets.example.org/e/late-set", event_obj(eventStatus=status))
    with pytest.raises(Rejected, match="page data says"):
        jsonld.match(page, "https://tickets.example.org/e/late-set", dt.date(2026, 10, 23))


def test_naive_and_date_only_starts_give_no_time() -> None:
    naive = jsonld.parse_event(event_obj(startDate="2026-10-23T19:30:00"))
    assert naive.start is None and naive.start_date is None
    day_only = jsonld.parse_event(event_obj(startDate="2026-10-23"))
    assert day_only.start is None and day_only.start_date == dt.date(2026, 10, 23)


def test_utc_start_becomes_the_pacific_day() -> None:
    late = jsonld.parse_event(event_obj(startDate="2026-10-24T02:30:00.000Z"))
    assert late.start_date == dt.date(2026, 10, 23)
    assert late.start == dt.datetime(2026, 10, 23, 19, 30, tzinfo=PT)


@pytest.mark.parametrize(("offers", "note"), [
    ([{"price": "35", "priceCurrency": "USD"}], "$35"),
    ({"@type": "AggregateOffer", "lowPrice": "39.19", "highPrice": "60.54", "priceCurrency": "USD"}, "$39.19 to $60.54"),
    ([{"price": 0, "priceCurrency": "usd"}], "Free"),
    ([{"price": "20", "priceCurrency": "EUR"}], ""),
    ([{"price": "call us"}], ""),
    ([{"price": True}], ""),
])
def test_prices(offers: Any, note: str) -> None:
    assert jsonld.parse_event(event_obj(offers=offers)).price_note == note


def test_page_text_is_held_to_the_text_rules() -> None:
    evil = event_obj(location={"@type": "Place", "name": "Visit https://evil.example.org now",
                               "address": {"addressLocality": "10.0.0.5"}})
    parsed = jsonld.parse_event(evil)
    assert parsed.venue == "" and parsed.city == ""


def test_broken_and_hostile_blocks_are_ignored() -> None:
    deep: dict[str, Any] = event_obj()
    for _ in range(50):
        deep = {"@graph": [deep]}
    text = ('<script type="application/ld+json">{not json</script>'
            f'<script type="application/ld+json">{json.dumps(deep)}</script>')
    assert jsonld.page_events(Page(200, "https://a.example.org/", text)) == []


# ---------------------------------------------------------------------------
# Through the digest
# ---------------------------------------------------------------------------


def test_page_facts_replace_the_model_fields() -> None:
    page = ld_page("https://tickets.example.org/e/late-set", event_obj())
    event = checks.validate_event("date_night", raw_item(), WINDOW, set(), fetcher_of(page), True)
    assert event.start == dt.datetime(2026, 10, 23, 19, 30, tzinfo=PT)
    assert event.end == dt.datetime(2026, 10, 23, 21, 0, tzinfo=PT)
    assert event.venue == "The Blue Room"
    assert event.city == "Oakland"
    assert event.price_note == "$35"
    assert event.checked == ("time", "venue", "city", "price")


def test_without_page_data_the_model_fields_stay_and_nothing_is_marked_checked() -> None:
    page = Page(200, "https://tickets.example.org/e/late-set", "<p>Friday, October 23 at 8pm</p>")
    event = checks.validate_event("date_night", raw_item(), WINDOW, set(), fetcher_of(page), True)
    assert event.start == dt.datetime(2026, 10, 23, 20, 0, tzinfo=PT)
    assert event.venue == "Some Jazz Club" and event.price_note == "about twenty dollars"
    assert event.checked == ()


def test_a_contradicting_page_drops_the_event_and_counts_the_reason() -> None:
    page = ld_page("https://tickets.example.org/e/late-set", event_obj(eventStatus="EventCancelled"))
    raw = {"top_picks": [], "science_tech": [], "japanese": [], "date_night": [raw_item()], "day_trip": None}
    digest = checks.build_digest(raw, WINDOW, set(), fetcher_of(page))
    assert digest.events() == []
    assert digest.drop_reasons == {"page data says cancelled": 1, "day trip does not have exactly the schema keys": 1}


def test_source_page_listing_the_event_by_url_supplies_facts() -> None:
    own = Page(200, "https://tickets.example.org/e/late-set", "<p>Oct 23</p>")
    listing = ld_page("https://listings.example.org/sf", event_obj())
    item = raw_item(source_urls=["https://listings.example.org/sf"])
    event = checks.validate_event("date_night", item, WINDOW, set(), fetcher_of(own, listing), True)
    assert "time" in event.checked and event.venue == "The Blue Room"


def test_checked_fields_are_badged_and_exported() -> None:
    page = ld_page("https://tickets.example.org/e/late-set", event_obj())
    event = checks.validate_event("date_night", raw_item(), WINDOW, set(), fetcher_of(page), True)
    assert "Time checked" in render.card(event) and "Price checked" in render.card(event)
    plain = checks.validate_event("date_night", raw_item(), WINDOW, set(),
                                  fetcher_of(Page(200, "https://tickets.example.org/e/late-set", "<p>Oct 23</p>")), True)
    assert "Time checked" not in render.card(plain)
    digest = checks.Digest(WINDOW[0], WINDOW[1], {"science_tech": [], "japanese": [], "date_night": [event]}, [], None)
    exported = json.loads(render.digest_json(digest, WINDOW[0]))
    assert exported["sections"]["date_night"][0]["checked"] == ["time", "venue", "city", "price"]


# ---------------------------------------------------------------------------
# Regressions found replaying the 2026-10-18 digest against the live pages
# ---------------------------------------------------------------------------


def test_series_span_covering_the_day_is_not_a_different_date() -> None:
    # Eventbrite's recurring series: startDate is the first session, endDate
    # the last. The Oct 31 session was wrongly dropped before this rule.
    series = event_obj(startDate="2026-07-25T17:30:00-07:00", endDate="2026-10-31T20:00:00-07:00")
    page = ld_page("https://tickets.example.org/e/late-set", series)
    assert jsonld.match(page, "https://tickets.example.org/e/late-set", dt.date(2026, 10, 31)) is None
    with pytest.raises(Rejected, match="different date"):
        jsonld.match(page, "https://tickets.example.org/e/late-set", dt.date(2026, 11, 7))


@pytest.mark.parametrize(("location", "venue"), [
    ({"@type": "Place", "name": "SCRAP", "address": {"addressLocality": "San Francisco",
      "streetAddress": "141 Industrial Street, #1, San Francisco, CA 94124"}}, "SCRAP, 141 Industrial Street, #1"),
    ({"@type": "Place", "name": "1581 Webster Street, Suite 203",
      "address": {"addressLocality": "San Francisco", "streetAddress": "1581 Webster Street, Suite 203"}},
     "1581 Webster Street, Suite 203"),
    ({"@type": "Place", "name": "Public Works"}, "Public Works"),
    # Meetup: no street, only the city.
    ({"@type": "Place", "name": "Bimi Poke", "address": {"addressLocality": "San Francisco",
      "streetAddress": ", San Francisco, CA"}}, "Bimi Poke"),
    # Eventbrite: an address as the name and a different street.
    ({"@type": "Place", "name": "1581 Webster Street, Suite 203",
      "address": {"addressLocality": "San Francisco", "streetAddress": "1825 Post Street"}},
     "1581 Webster Street, Suite 203"),
])
def test_venue_joins_name_and_street_once(location: dict[str, Any], venue: str) -> None:
    assert jsonld.parse_event(event_obj(location=location)).venue == venue


@pytest.mark.parametrize(("model", "page", "kept"), [
    ("Tokaido Arts Gallery, 1581 Webster Street, Suite 203", "1581 Webster Street, Suite 203", True),
    ("Randall Museum, 199 Museum Way", "Randall Museum, 199 Museum Way", True),
    ("Some Jazz Club", "The Blue Room", False),
    ("Chotto Matte, 50 O'Farrell Street", "Chotto Matte San Francisco, 50 O'Farrell Street", False),
])
def test_model_venue_is_kept_only_when_it_contains_the_page_venue(model: str, page: str, kept: bool) -> None:
    assert checks.keep_model_venue(model, page) is kept


@pytest.mark.parametrize(("model", "page", "kept"), [
    ("Free entry, 12 dollar drink minimum", "Free", True),
    ("150 dollars per person, dinner and two drinks included", "$150", True),
    ("From $1,250.00 a couple", "$1,250", True),
    ("about twenty dollars", "$35", False),
    ("40 dollars", "$35", False),
    ("Tickets 39 to 61 dollars", "$39.19 to $60.54", False),
    ("", "Free", False),
])
def test_model_price_is_kept_only_when_it_states_the_page_figures(model: str, page: str, kept: bool) -> None:
    assert checks.keep_model_price(model, page) is kept


def test_model_end_survives_when_the_page_gives_only_a_start() -> None:
    page = ld_page("https://tickets.example.org/e/late-set", event_obj(endDate=None))
    event = checks.validate_event("date_night", raw_item(), WINDOW, set(), fetcher_of(page), True)
    assert event.start == dt.datetime(2026, 10, 23, 19, 30, tzinfo=PT)
    assert event.end == dt.datetime(2026, 10, 23, 23, 0, tzinfo=PT)
    early = raw_item(start="2026-10-23T17:00:00-07:00", end="2026-10-23T18:00:00-07:00")
    event = checks.validate_event("date_night", early, WINDOW, set(), fetcher_of(page), True)
    assert event.end is None
