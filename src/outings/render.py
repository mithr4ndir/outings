"""The digest as a static HTML page, plus a JSON copy.

SECURITY: every model string goes through html.escape, and hrefs come only
from URLs that passed validate_url (plain https to a public host) or are
built here. nginx serves the page with a CSP that forbids scripts, so even a
missed escape cannot run code.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import urllib.parse
from importlib import resources
from pathlib import Path
from typing import Any

from .checks import PACIFIC, DayTrip, Digest, Event
from .common import write_text_atomic

SECTION_TITLES = {
    "science_tech": "Science & tech",
    "japanese": "Japanese culture & language",
    "date_night": "Date night",
}
DIGEST_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.html$")
ARCHIVE_MAX = 26
GCAL = "https://calendar.google.com/calendar/render"


def esc(value: str) -> str:
    return html.escape(value, quote=True)


def when(event: Event) -> str:
    start = event.start
    text = f"{start:%a %b} {start.day}, {clock(start)}"
    if event.end is not None:
        end = event.end.astimezone(PACIFIC)
        text += f" to {clock(end)}" if end.date() == start.date() else f" to {end:%a %b} {end.day}"
    return text


def clock(moment: dt.datetime) -> str:
    hour = moment.hour % 12 or 12
    suffix = "am" if moment.hour < 12 else "pm"
    return f"{hour}{suffix}" if moment.minute == 0 else f"{hour}:{moment:%M}{suffix}"


def gcal_link(event: Event) -> str:
    end = event.end or event.start + dt.timedelta(hours=2)
    stamp = "%Y%m%dT%H%M%SZ"
    query = urllib.parse.urlencode({
        "action": "TEMPLATE",
        "text": event.title,
        "dates": f"{event.start.astimezone(dt.UTC):{stamp}}/{end.astimezone(dt.UTC):{stamp}}",
        "location": f"{event.venue}, {event.city}",
        "details": f"{event.why}\n{event.url}",
        "ctz": "America/Los_Angeles",
    })
    return f"{GCAL}?{query}"


def card(event: Event, pick: bool = False) -> str:
    badges = []
    if event.reminder:
        badges.append('<span class="badge">Reminder</span>')
    if event.evidence == "structured":
        badges.append('<span class="badge ok" title="The event page carries this date in its data">Date checked</span>')
    elif event.evidence == "text":
        badges.append('<span class="badge ok" title="This date appears on the event page">Date on page</span>')
    if "time" in event.checked:
        badges.append('<span class="badge ok" title="Start time from the event page\'s own data">Time checked</span>')
    if "price" in event.checked:
        badges.append('<span class="badge ok" title="Price from the event page\'s own data">Price checked</span>')
    price = f'<p class="price">{esc(event.price_note)}</p>' if event.price_note else ""
    return (
        f'<article class="card{" pick" if pick else ""}">'
        f'<h3><a href="{esc(event.url)}" rel="noopener noreferrer">{esc(event.title)}</a></h3>'
        f'<p class="when">{esc(when(event))}</p>'
        f'<p class="where">{esc(event.venue)}, {esc(event.city)}</p>'
        f'<p class="why">{esc(event.why)}</p>{price}'
        f'<p class="meta">{"".join(badges)}'
        f'<a class="cal" href="{esc(gcal_link(event))}" rel="noopener noreferrer">Add to calendar</a></p>'
        "</article>"
    )


def day_trip_html(trip: DayTrip | None) -> str:
    if trip is None:
        return ""
    todos = "".join(f"<li>{esc(t)}</li>" for t in trip.things_to_do)
    sources = " · ".join(f'<a href="{esc(u)}" rel="noopener noreferrer">source</a>' for u in trip.source_urls)
    return (
        '<section><h2>Day trip</h2><article class="card">'
        f"<h3>{esc(trip.town)}</h3><p class=\"when\">{esc(trip.drive_time)} from SF</p>"
        f'<p class="why">{esc(trip.why)}</p><ul>{todos}</ul><p class="meta">{sources}</p>'
        "</article></section>"
    )


def render_page(digest: Digest, run_date: dt.date, archive: list[str]) -> str:
    picks = "".join(card(e, pick=True) for e in digest.top_picks)
    sections = "".join(
        f"<section><h2>{SECTION_TITLES[name]}</h2>{''.join(card(e) for e in events)}</section>"
        for name, events in digest.sections.items() if events
    )
    past = "".join(f'<li><a href="{esc(name)}.html">{esc(name)}</a></li>' for name in archive if name != run_date.isoformat())
    window = f"{digest.window_start:%b} {digest.window_start.day} to {digest.window_end:%b} {digest.window_end.day}"
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>Weekly outings, {run_date.isoformat()}</title>"
        '<link rel="stylesheet" href="digest.css"></head><body><main>'
        f"<header><h1>Weekly outings</h1><p class=\"sub\">{esc(window)} · San Francisco and the Bay Area</p></header>"
        f'<section><h2>Top picks</h2><div class="picks">{picks}</div></section>'
        f"{sections}{day_trip_html(digest.day_trip)}"
        '<footer><p>Most events are collected by a script from venue calendars, listing sites and university '
        "feeds; a model picks among them, writes why each fits and researches what the collection lacks. "
        "Every event date was matched against the event\'s own page when this digest was built, and where the "
        "page publishes its event data the time, venue and price come from that data. "
        "Check the page before you go.</p>"
        f"{'<h2>Earlier digests</h2><ul>' + past + '</ul>' if past else ''}</footer>"
        "</main></body></html>\n"
    )


def digest_json(digest: Digest, run_date: dt.date) -> str:
    def event(e: Event) -> dict[str, Any]:
        return {
            "title": e.title, "start": e.start.isoformat(), "end": e.end.isoformat() if e.end else None,
            "venue": e.venue, "city": e.city, "url": e.url, "why": e.why, "price_note": e.price_note,
            "reminder": e.reminder, "source_urls": list(e.source_urls), "evidence": e.evidence,
            "evidence_url": e.evidence_url, "checked": list(e.checked), "origin": e.origin,
        }

    trip = digest.day_trip
    return json.dumps({
        "run_date": run_date.isoformat(),
        "window": [digest.window_start.isoformat(), digest.window_end.isoformat()],
        "top_picks": [e.url for e in digest.top_picks],
        "sections": {name: [event(e) for e in events] for name, events in digest.sections.items()},
        "day_trip": None if trip is None else {
            "town": trip.town, "why": trip.why, "things_to_do": list(trip.things_to_do),
            "drive_time_from_sf": trip.drive_time, "source_urls": list(trip.source_urls),
        },
        "dropped": digest.dropped,
    }, indent=1, ensure_ascii=False) + "\n"


def archive_names(site_dir: Path) -> list[str]:
    names = [m.group(1) for p in site_dir.glob("*.html") if (m := DIGEST_NAME_RE.match(p.name))]
    return sorted(names, reverse=True)[:ARCHIVE_MAX]


def write_site(site_dir: Path, digest: Digest, run_date: dt.date) -> Path:
    """Write <date>.html, <date>.json, index.html (a copy of the latest) and
    the stylesheet. Returns the dated page."""
    site_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    archive = sorted({*archive_names(site_dir), run_date.isoformat()}, reverse=True)[:ARCHIVE_MAX]
    page = render_page(digest, run_date, archive)
    dated = site_dir / f"{run_date.isoformat()}.html"
    write_text_atomic(dated, page)
    write_text_atomic(site_dir / f"{run_date.isoformat()}.json", digest_json(digest, run_date))
    write_text_atomic(site_dir / "digest.css", resources.files("outings").joinpath("static/digest.css").read_text())
    write_text_atomic(site_dir / "index.html", page)
    return dated
