"""Tests for the scripted collection and the model's picks from it.

Fixtures under tests/fixtures were captured 2026-10-18 with the job's
User-Agent and trimmed (feeds to a few events, pages to the parsed markup).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from outings import checks, collect, research
from outings.collect import Collected, Found, Source
from outings.common import Page, Rejected

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PT = collect.PACIFIC
WINDOW = (dt.date(2026, 10, 18), dt.date(2026, 11, 1))


def fixture(name: str, url: str = "https://example.org/") -> Page:
    return Page(200, url, (FIXTURES / name).read_text())


def source(name: str) -> Source:
    return next(s for s in collect.SOURCES if s.name == name)


def collected(**overrides: Any) -> Collected:
    base: dict[str, Any] = {
        "id": "e000001", "source": "eventbrite-jazz", "title": "Late Set", "day": dt.date(2026, 10, 23),
        "start": dt.datetime(2026, 10, 23, 19, 30, tzinfo=PT), "end": None, "venue": "The Blue Room",
        "city": "Oakland", "url": "https://tickets.example.org/e/late-set",
        "listing_url": "https://www.eventbrite.com/d/ca--san-francisco/jazz/", "price_note": "$35",
    }
    base.update(overrides)
    return Collected(**base)


def pick(**overrides: Any) -> dict[str, Any]:
    base = {"id": "e000001", "section": "date_night", "why": "A quiet set in a small room.", "reminder": False}
    base.update(overrides)
    return base


def raw(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"top_picks": [], "picks": [], "science_tech": [], "japanese": [], "date_night": [],
                            "day_trip": None}
    base.update(overrides)
    return base


def fetcher_of(pages: dict[str, str]) -> Any:
    calls: list[str] = []

    def fetch(url: str) -> Page | None:
        calls.append(url)
        return Page(200, url, pages[url]) if url in pages else None

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


# ---------------------------------------------------------------------------
# Parsers, on real pages
# ---------------------------------------------------------------------------


def test_exploratorium_cards_give_iso_times_and_their_own_links() -> None:
    found = collect.parse_exploratorium(fixture("exploratorium.html"), source("exploratorium"))
    by_title = {f.title: f for f in found}
    creep = by_title["After Dark: Creepatorium"]
    assert creep.start == dt.datetime(2026, 10, 29, 18, 0, tzinfo=PT)
    assert creep.end == dt.datetime(2026, 10, 29, 22, 0, tzinfo=PT)
    assert creep.url == "https://www.exploratorium.edu/visit/calendar/after-dark-creepatorium"
    # Every card's link names its own theme, never a neighbour's.
    assert all(f.url.rsplit("/", 1)[-1].startswith("after-dark") for f in found)


def test_calacademy_rows_infer_the_year_and_the_pm_hour() -> None:
    found = collect.parse_calacademy(fixture("calacademy.html"), source("calacademy"), today=dt.date(2026, 10, 18))
    assert [(f.title, f.start) for f in found][:2] == [
        ("NightLife: Día de los Muertos", dt.datetime(2026, 10, 22, 18, 0, tzinfo=PT)),
        ("Fright NightLife", dt.datetime(2026, 10, 29, 18, 0, tzinfo=PT)),
    ]
    # Read in December, an "October" row means next year.
    late = collect.parse_calacademy(fixture("calacademy.html"), source("calacademy"), today=dt.date(2026, 12, 20))
    assert late[0].day == dt.date(2027, 10, 22)


def test_localist_keeps_timed_in_person_events() -> None:
    found = collect.parse_localist(fixture("stanford.json"), source("stanford"))
    # The fixture holds four timed in-person events, one all-day, one virtual.
    assert len(found) == 4 and all(f.start is not None and f.start.tzinfo is not None for f in found)
    assert all(f.url.startswith("https://events.stanford.edu/event/") for f in found)


def test_livewhale_drops_all_day_and_reads_venues() -> None:
    found = collect.parse_livewhale(fixture("berkeley.json"), source("berkeley"))
    titles = [f.title for f in found]
    assert not any("Golf" in t for t in titles)  # all-day
    lecture = next(f for f in found if f.title.startswith("G. N. Lewis"))
    assert lecture.venue == "120 Latimer Hall" and lecture.start == dt.datetime(2026, 10, 20, 16, 0, tzinfo=PT)


def test_eventbrite_listing_drops_events_outside_california() -> None:
    found = collect.parse_jsonld(fixture("eventbrite_listing.html"), source("eventbrite-astronomy"))
    cities = {f.city for f in found}
    assert found and "Brooklyn" not in cities and "Vaughan" not in cities


@pytest.mark.parametrize(("region", "geo", "inside"), [
    ("CA", (37.78, -122.39), True),
    ("California", None, True),
    ("ON", (37.78, -122.39), False),  # Eventbrite gave an Ontario event SF coordinates
    ("NY", None, False),
    ("", (40.6, -74.0), False),
    ("", None, True),
])
def test_area_rule(region: str, geo: tuple[float, float] | None, inside: bool) -> None:
    assert collect.in_area(region, geo) is inside


# ---------------------------------------------------------------------------
# The shared rules
# ---------------------------------------------------------------------------


def found(**overrides: Any) -> Found:
    base: dict[str, Any] = {"title": "Late Set", "day": dt.date(2026, 10, 23),
                            "start": dt.datetime(2026, 10, 23, 19, 30, tzinfo=PT), "end": None,
                            "venue": "The Blue Room", "city": "Oakland", "url": "https://tickets.example.org/e/late-set"}
    base.update(overrides)
    return Found(**base)


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"day": dt.date(2026, 11, 2)}, "outside the window"),
    ({"day": None}, "outside the window or undated"),
    ({"venue": "Online via Zoom"}, "online"),
    ({"url": "http://tickets.example.org/e/x"}, "not https"),
    ({"url": "https://10.0.0.5/e/x"}, "IP literal"),
    ({"title": "Click https://evil.example.org"}, "contains a link"),
])
def test_accept_rejects(overrides: dict[str, Any], reason: str) -> None:
    with pytest.raises(Rejected, match=reason):
        collect.accept(found(**overrides), source("eventbrite-jazz"), WINDOW)


def test_university_feeds_keep_only_listed_interests() -> None:
    with pytest.raises(Rejected, match="interest"):
        collect.accept(found(title="Joint Seminar 237 - Macroeconomics"), source("berkeley"), WINDOW)
    assert collect.accept(found(title="Space Physics Seminars"), source("berkeley"), WINDOW).title


def test_entities_are_decoded_and_ids_are_stable() -> None:
    a = collect.accept(found(venue="Genetics &amp; Plant Biology"), source("eventbrite-jazz"), WINDOW)
    b = collect.accept(found(venue="Genetics &amp; Plant Biology"), source("eventbrite-jazz"), WINDOW)
    assert a.venue == "Genetics & Plant Biology"
    assert a.id == b.id == collect.event_id("https://tickets.example.org/e/late-set/", dt.date(2026, 10, 23))


def test_prompt_line_never_carries_a_separator_inside_a_field() -> None:
    line = collected(title="Curator Led Talk | surfacing").prompt_line()
    assert line.count(" | ") == 5 and "Talk / surfacing" in line


def test_paged_api_is_never_a_listing_source() -> None:
    assert collect.listing_url(source("stanford")) == ""
    assert collect.listing_url(source("chabot")) == "https://chabotspace.org/events/"


def test_feed_urls_carry_the_window() -> None:
    urls = list(collect.source_urls(source("berkeley"), WINDOW))
    assert len(urls) == 12 and "start_date/2026-10-18/end_date/2026-11-01" in urls[0] and urls[1].endswith("page=2")
    assert list(collect.source_urls(source("chabot"), WINDOW)) == ["https://chabotspace.org/events/"]


def test_broken_source_is_skipped_and_duplicates_collapse() -> None:
    chabot = fixture("chabot_listing.html", "https://chabotspace.org/events/").text

    def fetch(url: str) -> Page | None:
        if url == "https://chabotspace.org/events/":
            # The real page carries every event in two ld+json blocks.
            return Page(200, url, chabot + chabot)
        raise RuntimeError("boom")

    counts: dict[str, int] = {}
    events = collect.collect(WINDOW, fetch, counts=counts)
    assert counts["chabot"] == len(events) > 0
    assert len({e.id for e in events}) == len(events)
    assert all(v == 0 for k, v in counts.items() if k != "chabot")


# ---------------------------------------------------------------------------
# Picks
# ---------------------------------------------------------------------------


EVENT_PAGE = '<p>Friday, October 23, 7:30 pm</p>'


def test_pick_takes_every_detail_from_the_collection() -> None:
    event = checks.validate_pick(pick(), {"e000001": collected()}, WINDOW, set(),
                                 fetcher_of({"https://tickets.example.org/e/late-set": EVENT_PAGE}), True)
    assert (event.title, event.venue, event.city, event.price_note) == ("Late Set", "The Blue Room", "Oakland", "$35")
    assert event.start == dt.datetime(2026, 10, 23, 19, 30, tzinfo=PT)
    assert event.why == "A quiet set in a small room." and event.origin == "collected:eventbrite-jazz"


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"id": "e999999"}, "not a collected event"),
    ({"section": "brunch"}, "section is unknown"),
    ({"why": "See https://evil.example.org"}, "contains a link"),
    ({"reminder": "yes"}, "not a boolean"),
])
def test_bad_picks_are_dropped(overrides: dict[str, Any], reason: str) -> None:
    with pytest.raises(Rejected, match=reason):
        checks.validate_pick(pick(**overrides), {"e000001": collected()}, WINDOW, set(),
                             fetcher_of({"https://tickets.example.org/e/late-set": EVENT_PAGE}), True)


def test_a_pick_cannot_carry_its_own_details() -> None:
    with pytest.raises(Rejected, match="schema keys"):
        checks.validate_pick(pick(url="https://evil.example.org/"), {"e000001": collected()}, WINDOW, set(),
                             fetcher_of({}), True)


def test_a_pick_still_needs_its_date_on_its_own_page() -> None:
    with pytest.raises(Rejected, match="date not found"):
        checks.validate_pick(pick(), {"e000001": collected(listing_url="")}, WINDOW, set(),
                             fetcher_of({"https://tickets.example.org/e/late-set": "<p>Nov 5</p>"}), True)


def test_a_date_only_pick_needs_a_time_from_its_page() -> None:
    dated = collected(start=None)
    page_ld = {"@type": "Event", "url": "https://tickets.example.org/e/late-set",
               "startDate": "2026-10-23T20:00:00-07:00", "location": {"@type": "Place", "name": "The Blue Room"}}
    with_time = f'<p>Oct 23</p><script type="application/ld+json">{json.dumps(page_ld)}</script>'
    event = checks.validate_pick(pick(), {"e000001": dated}, WINDOW, set(),
                                 fetcher_of({"https://tickets.example.org/e/late-set": with_time}), True)
    assert event.start == dt.datetime(2026, 10, 23, 20, 0, tzinfo=PT)
    with pytest.raises(Rejected, match="no start time"):
        checks.validate_pick(pick(), {"e000001": dated}, WINDOW, set(),
                             fetcher_of({"https://tickets.example.org/e/late-set": "<p>Oct 23</p>"}), True)


def researched(n: int) -> dict[str, Any]:
    return {"title": f"Researched {n}", "start": f"2026-10-{20 + n}T19:00:00-07:00", "venue": "Hall", "city": "SF",
            "url": f"https://www.example.org/r/{n}", "why": "Fits.", "reminder": False, "source_urls": []}


def test_picks_fill_sections_first_and_top_picks_accept_ids() -> None:
    pages = {"https://tickets.example.org/e/late-set": EVENT_PAGE,
             **{f"https://www.example.org/r/{n}": f"<p>October {20 + n}</p>" for n in range(1, 4)}}
    digest = checks.build_digest(
        raw(picks=[pick(), pick()], date_night=[researched(1), researched(2), researched(3)], top_picks=["e000001"]),
        WINDOW, set(), fetcher_of(pages), collected={"e000001": collected()})
    assert [e.origin for e in digest.sections["date_night"]].count("collected:eventbrite-jazz") == 1
    assert len(digest.sections["date_night"]) == 4
    assert digest.drop_reasons["duplicate within this run"] == 1
    assert digest.top_picks[0].title == "Late Set"


def test_research_off_drops_researched_items() -> None:
    pages = {"https://tickets.example.org/e/late-set": EVENT_PAGE, "https://www.example.org/r/1": "<p>October 21</p>"}
    digest = checks.build_digest(raw(picks=[pick()], date_night=[researched(1)]), WINDOW, set(), fetcher_of(pages),
                                 collected={"e000001": collected()}, research=False)
    assert [e.title for e in digest.events()] == ["Late Set"]
    assert digest.drop_reasons["research is off"] == 1


# ---------------------------------------------------------------------------
# The model call
# ---------------------------------------------------------------------------


def test_research_off_gives_the_model_no_tools() -> None:
    argv = research.claude_argv("/bin/claude", "opus", research=False)
    assert argv[argv.index("--tools") + 1] == ""
    assert "--allowedTools" not in argv and not any("WebSearch" in a or "WebFetch" in a for a in argv)
    on = research.claude_argv("/bin/claude", "opus")
    assert on[on.index("--tools") + 1] == "WebSearch,WebFetch"


def test_prompt_carries_the_collection_as_data_and_the_research_rule() -> None:
    line = collected().prompt_line()
    on = research.build_prompt(dt.date(2026, 10, 18), dt.date(2026, 11, 1), "home: SF", [], [line])
    off = research.build_prompt(dt.date(2026, 10, 18), dt.date(2026, 11, 1), "home: SF", [], [line], research=False)
    for prompt in (on, off):
        start = prompt.index("BEGIN COLLECTED EVENTS")
        assert start < prompt.index(line) < prompt.index("END COLLECTED EVENTS")
    assert "Research on the web only for what the collected list lacks" in on
    assert "Web research is not available in this run" in off and "Research on the web" not in off
    empty = research.build_prompt(dt.date(2026, 10, 18), dt.date(2026, 11, 1), "home: SF", [])
    assert "(nothing was collected this run)" in empty


def test_schema_requires_picks_and_limits_them() -> None:
    schema = research.DIGEST_SCHEMA
    assert "picks" in schema["required"]
    assert schema["properties"]["picks"]["items"]["required"] == list(research.PICK_KEYS)
    assert set(schema["properties"]["picks"]["items"]["properties"]) == set(research.PICK_KEYS)
