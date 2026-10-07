"""Tests for outings.

Run from the repo root:
    uv run pytest -q -rs

SAFETY: nothing here reaches Anthropic, Discord or the web. claude is a fake
executable in a temporary directory, pages come from a fake fetcher, the
webhook is a fake URL, and conftest.no_network refuses every in-process
urllib request and DNS lookup.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import stat
import sys
import tomllib
import urllib.error
from pathlib import Path
from typing import Any

import pytest

import outings
from outings import checks, cli, common, notify, render, research, state
from outings.common import Page, Rejected

ROOT = Path(__file__).resolve().parents[1]
PT = checks.PACIFIC
FAKE_WEBHOOK = "https://discord.com/api/webhooks/987654321098765432/FAKE-outings_token-for-tests-only"
FAKE_OAUTH_TOKEN = "sk-ant-oat01-FAKE-oauth-canary"
# Wednesday 2026-10-07 13:00 Pacific; the window runs through 2026-10-21.
NOW = dt.datetime(2026, 10, 7, 20, 0, tzinfo=dt.UTC)
WINDOW = (dt.date(2026, 10, 7), dt.date(2026, 10, 21))


def page(text: str, status: int = 200, url: str = "https://example.org/") -> Page:
    return Page(status, url, text)


def fetcher_for(pages: dict[str, str]) -> Any:
    calls: list[str] = []

    def fetch(url: str) -> Page | None:
        calls.append(url)
        return page(pages[url], url=url) if url in pages else None

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def item(**overrides: Any) -> dict[str, Any]:
    base = {
        "title": "After Dark: Speak Easy",
        "start": "2026-10-15T18:00:00-07:00",
        "end": "2026-10-15T22:00:00-07:00",
        "venue": "Exploratorium",
        "city": "San Francisco",
        "url": "https://www.exploratorium.edu/visit/calendar/after-dark-speak-easy",
        "why": "Adults-only night of hands-on exhibits.",
        "reminder": False,
        "source_urls": [],
    }
    base.update(overrides)
    return base


def day_trip(**overrides: Any) -> dict[str, Any]:
    base = {
        "town": "Sonoma",
        "why": "Plaza, missions and wine country an hour north.",
        "things_to_do": ["Walk the Sonoma Plaza", "Visit Mission San Francisco Solano"],
        "drive_time_from_sf": "about an hour",
        "source_urls": ["https://www.sonomavalley.com/"],
    }
    base.update(overrides)
    return base


ON_PAGE = "<html><body><h1>Speak Easy</h1><p>Thu, Oct 15 2026 &bull; 6 - 10pm</p></body></html>"


# ---------------------------------------------------------------------------
# Date evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Thu, Oct 15 2026", "October 15 at 6:00 pm", "Oct. 15th", "15 October", "10/15/2026", "10/15",
    "Thursday,&nbsp;October&nbsp;15", '"startDate":"2026-10-15T18:00:00-07:00"',
])
def test_date_on_page_is_found(text: str) -> None:
    found = checks.date_evidence(dt.date(2026, 10, 15), ["https://a.example.org/"],
                                 fetcher_for({"https://a.example.org/": f"<p>{text}</p>"}))
    assert found is not None


@pytest.mark.parametrize("text", ["Oct 5", "Oct 150", "October 25", "10/5/2026", "10/155", "2026-10-05", "Nov 15", "110/15"])
def test_other_dates_are_not_evidence(text: str) -> None:
    found = checks.date_evidence(dt.date(2026, 10, 15), ["https://a.example.org/"],
                                 fetcher_for({"https://a.example.org/": f"<p>{text}</p>"}))
    assert found is None


def test_iso_date_is_structured_evidence() -> None:
    fetch = fetcher_for({"https://a.example.org/": '<script>{"startDate":"2026-10-15T18:00:00-07:00"}</script>'})
    assert checks.date_evidence(dt.date(2026, 10, 15), ["https://a.example.org/"], fetch) == ("structured", "https://a.example.org/")


def test_error_pages_are_not_evidence() -> None:
    def fetch(url: str) -> Page:
        return page("Oct 15", status=404)

    assert checks.date_evidence(dt.date(2026, 10, 15), ["https://a.example.org/"], fetch) is None


# ---------------------------------------------------------------------------
# Event validation
# ---------------------------------------------------------------------------


def validate(raw: dict[str, Any], pages: dict[str, str] | None = None, seen: set[str] | None = None,
             require: bool = True) -> checks.Event:
    fetch = fetcher_for(pages if pages is not None else {item()["url"]: ON_PAGE})
    return checks.validate_event("science_tech", raw, WINDOW, seen or set(), fetch, require)


def test_good_event_passes_with_pacific_times() -> None:
    event = validate(item())
    assert event.start == dt.datetime(2026, 10, 15, 18, 0, tzinfo=PT)
    assert event.evidence == "text"
    assert event.reminder is False


def test_utc_start_is_converted_and_checked_against_the_local_date() -> None:
    # 01:30 UTC on the 16th is 18:30 Pacific on the 15th, the date on the page.
    event = validate(item(start="2026-10-16T01:30:00Z", end=None))
    assert event.start.date() == dt.date(2026, 10, 15)


@pytest.mark.parametrize("start, reason", [
    ("2026-10-15T18:00:00", "no UTC offset"),
    ("next Thursday", "not ISO 8601"),
    ("2026-10-06T18:00:00-07:00", "outside the window"),
    ("2026-10-22T18:00:00-07:00", "outside the window"),
])
def test_bad_starts_are_dropped(start: str, reason: str) -> None:
    with pytest.raises(Rejected, match=reason):
        validate(item(start=start, end=None))


def test_date_missing_from_page_drops_the_event() -> None:
    with pytest.raises(Rejected, match="date not found"):
        validate(item(), pages={item()["url"]: "<p>Thursdays, 6 to 10pm</p>"})


def test_date_on_a_source_page_is_enough() -> None:
    src = "https://www.eventbrite.com/e/speak-easy-123"
    event = validate(item(source_urls=[src]), pages={item()["url"]: "<p>Thursdays</p>", src: ON_PAGE})
    assert event.evidence_url == src


def test_check_can_be_turned_off() -> None:
    event = validate(item(), pages={}, require=False)
    assert event.evidence == "unchecked"


@pytest.mark.parametrize("url", [
    "http://www.exploratorium.edu/x", "https://192.168.1.10/x", "https://nas.local/x", "https://a.example.org:8443/x",
    "https://user@a.example.org/x", "javascript:alert(1)",
])
def test_unsafe_urls_drop_the_event(url: str) -> None:
    with pytest.raises(Rejected):
        validate(item(url=url), pages={url: ON_PAGE})


@pytest.mark.parametrize("key, value", [
    ("title", "See https://evil.example.com"), ("why", "token: abc"), ("venue", "10.0.0.1"), ("title", ""),
    ("why", "x" * 300), ("reminder", "no"),
])
def test_bad_text_drops_the_event(key: str, value: Any) -> None:
    with pytest.raises(Rejected):
        validate(item(**{key: value}))


def test_unknown_keys_drop_the_event() -> None:
    with pytest.raises(Rejected, match="schema keys"):
        validate({**item(), "rating": 5})


def test_end_before_start_drops_the_event() -> None:
    with pytest.raises(Rejected, match="end"):
        validate(item(end="2026-10-15T17:00:00-07:00"))


def test_a_seen_url_is_always_labelled_a_reminder() -> None:
    event = validate(item(), seen={common.canonical_url(item()["url"] + "/")})
    assert event.reminder is True


# ---------------------------------------------------------------------------
# The whole digest
# ---------------------------------------------------------------------------


def numbered(n: int, section: str = "science_tech") -> tuple[dict[str, Any], str]:
    url = f"https://www.example.org/{section}/{n}"
    day = 8 + n
    return item(title=f"Event {n}", url=url, start=f"2026-10-{day:02d}T19:00:00-07:00", end=None), url


def raw_digest(**sections: list[dict[str, Any]]) -> dict[str, Any]:
    return {"top_picks": [], "science_tech": [], "japanese": [], "date_night": [], "day_trip": day_trip(), **sections}


def pages_for(*items: dict[str, Any]) -> dict[str, str]:
    pages = {"https://www.sonomavalley.com/": "<p>Sonoma</p>"}
    for raw in items:
        day = dt.datetime.fromisoformat(raw["start"]).date()
        pages[raw["url"]] = f"<p>{day:%B} {day.day}</p>"
    return pages


def test_digest_drops_bad_items_and_counts_why() -> None:
    good, _ = numbered(1)
    late = item(title="Late", url="https://www.example.org/late", start="2026-11-30T19:00:00-08:00", end=None)
    digest = checks.build_digest(raw_digest(science_tech=[good, late]), WINDOW, set(), fetcher_for(pages_for(good)))
    assert [e.title for e in digest.events()] == ["Event 1"]
    assert digest.dropped == 1
    assert digest.drop_reasons == {"start is outside the window": 1}


def test_digest_dedupes_and_caps_sections() -> None:
    items = [numbered(n)[0] for n in range(1, 9)]
    dup = dict(items[0])
    digest = checks.build_digest(raw_digest(science_tech=[*items[:7], dup]), WINDOW, set(),
                                 fetcher_for(pages_for(*items)))
    assert len(digest.sections["science_tech"]) == checks.SECTION_MAX
    assert digest.drop_reasons == {"section is full": 1, "duplicate within this run": 1}


def test_top_picks_keep_model_choices_that_survived_and_fill_the_rest() -> None:
    a, url_a = numbered(1, "science_tech")
    b, url_b = numbered(2, "japanese")
    c, _ = numbered(3, "date_night")
    raw = raw_digest(science_tech=[a], japanese=[b], date_night=[c],
                     top_picks=[url_b, "https://www.example.org/dropped"])
    digest = checks.build_digest(raw, WINDOW, set(), fetcher_for(pages_for(a, b, c)))
    assert [e.url for e in digest.top_picks][0] == url_b
    assert {e.url for e in digest.top_picks} == {e.url for e in digest.events()}
    assert url_a in {e.url for e in digest.top_picks}


def test_top_picks_prefer_new_items_over_reminders() -> None:
    a, url_a = numbered(1)
    b, url_b = numbered(2)
    c, url_c = numbered(3)
    d, url_d = numbered(4, "japanese")
    raw = raw_digest(science_tech=[a, b, c], japanese=[d])
    digest = checks.build_digest(raw, WINDOW, {common.canonical_url(url_d)}, fetcher_for(pages_for(a, b, c, d)))
    assert url_d not in [e.url for e in digest.top_picks]


def test_day_trip_needs_a_source_that_answers() -> None:
    good, _ = numbered(1)
    digest = checks.build_digest(raw_digest(science_tech=[good], day_trip=day_trip(source_urls=["https://gone.example.org/"])),
                                 WINDOW, set(), fetcher_for(pages_for(good)))
    assert digest.day_trip is None
    assert digest.drop_reasons == {"no day trip source answered": 1}


def test_non_object_output_is_rejected() -> None:
    with pytest.raises(Rejected):
        checks.build_digest(["not", "an", "object"], WINDOW, set(), fetcher_for({}))


# ---------------------------------------------------------------------------
# Fetching pages the model named
# ---------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status
        self.headers = _Headers()


class _Headers(dict):
    def get_content_charset(self) -> str:
        return "utf-8"


def public_resolver(host: str, *args: Any, **kwargs: Any) -> list[Any]:
    address = {"lan.example.org": "192.168.1.150", "meta.example.org": "169.254.169.254"}.get(host, "93.184.215.14")
    return [(None, None, None, None, (address, 443))]


def redirect(location: str, code: int = 301) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x.example.org/", code, "moved", {"Location": location}, None)  # type: ignore[arg-type]


def test_fetch_reads_a_public_page() -> None:
    seen: list[str] = []

    def opener(request: Any, timeout: float) -> FakeResponse:
        seen.append(request.full_url)
        assert request.get_header("User-agent") == common.USER_AGENT
        return FakeResponse(b"<p>Oct 15</p>")

    got = common.fetch_page("https://a.example.org/e", opener, public_resolver)
    assert got is not None and got.text == "<p>Oct 15</p>" and seen == ["https://a.example.org/e"]


@pytest.mark.parametrize("url", ["https://lan.example.org/", "https://meta.example.org/", "http://a.example.org/"])
def test_fetch_refuses_private_or_plain_http_targets(url: str) -> None:
    def opener(request: Any, timeout: float) -> FakeResponse:
        raise AssertionError("must not connect")

    assert common.fetch_page(url, opener, public_resolver) is None


@pytest.mark.parametrize("location", ["https://lan.example.org/admin", "http://b.example.org/", "https://10.0.0.5/"])
def test_fetch_never_follows_a_redirect_inward(location: str) -> None:
    calls: list[str] = []

    def opener(request: Any, timeout: float) -> FakeResponse:
        calls.append(request.full_url)
        raise redirect(location)

    assert common.fetch_page("https://a.example.org/", opener, public_resolver) is None
    assert calls == ["https://a.example.org/"]


def test_fetch_follows_public_redirects_and_gives_up_on_loops() -> None:
    hops: list[str] = []

    def opener(request: Any, timeout: float) -> FakeResponse:
        hops.append(request.full_url)
        if request.full_url == "https://a.example.org/":
            raise redirect("/event/1")
        if request.full_url == "https://a.example.org/event/1":
            return FakeResponse(b"ok")
        raise AssertionError(request.full_url)

    assert common.fetch_page("https://a.example.org/", opener, public_resolver).text == "ok"

    def looping(request: Any, timeout: float) -> FakeResponse:
        raise redirect("https://a.example.org/again")

    assert common.fetch_page("https://a.example.org/", looping, public_resolver) is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def one_event_digest(**overrides: Any) -> checks.Digest:
    raw = item(**overrides)
    event = checks.validate_event("science_tech", raw, WINDOW, set(), fetcher_for({raw["url"]: ON_PAGE}), False)
    return checks.Digest(WINDOW[0], WINDOW[1], {"science_tech": [event], "japanese": [], "date_night": []}, [event], None)


def test_page_escapes_model_text() -> None:
    digest = one_event_digest(title='<script>alert("x")</script>', why='<img src=x onerror=alert(1)> & "quotes"')
    html_text = render.render_page(digest, WINDOW[0], [])
    assert "<script>" not in html_text and "<img" not in html_text
    assert "&lt;script&gt;" in html_text and "&amp; &quot;quotes&quot;" in html_text


def test_calendar_link_is_in_utc_with_a_default_length() -> None:
    event = one_event_digest(end=None).events()[0]
    link = render.gcal_link(event)
    assert "dates=20261016T010000Z%2F20261016T030000Z" in link
    assert link.startswith("https://calendar.google.com/")


def test_times_read_naturally() -> None:
    event = one_event_digest().events()[0]
    assert render.when(event) == "Thu Oct 15, 6pm to 10pm"


def test_write_site_keeps_an_archive(tmp_path: Path) -> None:
    (tmp_path / "2026-09-30.html").write_text("old")
    (tmp_path / "notes.html").write_text("not a digest")
    render.write_site(tmp_path, one_event_digest(), WINDOW[0])
    index = (tmp_path / "index.html").read_text()
    assert index == (tmp_path / "2026-10-07.html").read_text()
    assert 'href="2026-09-30.html"' in index and "notes.html" not in index
    assert json.loads((tmp_path / "2026-10-07.json").read_text())["sections"]["science_tech"][0]["title"]
    assert (tmp_path / "digest.css").read_text().startswith(":root")


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def test_discord_text_is_inert() -> None:
    text = notify.discord_text("@everyone [click](https://evil.example) **bold** <@123>", 200)
    assert "@everyone" not in text and "](" not in text and "https://" not in text and "<@123>" not in text


def test_payload_never_pings_and_links_the_page() -> None:
    payload = notify.build_payload(one_event_digest(), "http://192.168.1.236/2026-10-07.html")
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["content"].endswith("Full digest: http://192.168.1.236/2026-10-07.html")
    assert len(payload["embeds"]) == 1 and payload["embeds"][0]["url"] == item()["url"]


def test_post_failure_never_carries_the_webhook(caplog: pytest.LogCaptureFixture) -> None:
    def opener(request: Any, timeout: float) -> Any:
        raise urllib.error.URLError(f"cannot reach {request.full_url}")

    with pytest.raises(notify.PostError) as info:
        notify.post_payload(FAKE_WEBHOOK, {"content": "x"}, opener=opener, sleep=lambda s: None)
    assert "webhooks" not in str(info.value)


def test_post_does_not_retry_a_client_error() -> None:
    calls: list[int] = []

    def opener(request: Any, timeout: float) -> Any:
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, None)  # type: ignore[arg-type]

    with pytest.raises(notify.PostError, match="HTTP 400"):
        notify.post_payload(FAKE_WEBHOOK, {"content": "x"}, opener=opener, sleep=lambda s: None)
    assert calls == [1]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_history_round_trips_and_feeds_the_prompt(tmp_path: Path) -> None:
    history = state.History()
    history.record("https://a.example.org/1", "Old", "2026-08-01T19:00:00-07:00", "2026-08-01")
    history.record("https://a.example.org/2", "Recent", "2026-10-01T19:00:00-07:00", "2026-10-01")
    state.save_history(tmp_path, history)
    loaded = state.load_history(tmp_path)
    assert loaded.items == history.items
    assert loaded.recent(dt.date(2026, 10, 7)) == [{"title": "Recent", "url": "https://a.example.org/2"}]
    assert stat.S_IMODE(os.stat(tmp_path / state.HISTORY_FILE).st_mode) == 0o600


def test_corrupt_history_refuses_to_run(tmp_path: Path) -> None:
    (tmp_path / state.HISTORY_FILE).write_text("{not json")
    with pytest.raises(common.OutingsError, match="history unreadable"):
        state.load_history(tmp_path)


def test_history_prunes_old_runs() -> None:
    history = state.History([{"url": "u", "title": "t", "start": "2025-01-01", "run_date": "2025-01-01"}])
    history.prune(dt.date(2026, 10, 7))
    assert history.items == []


# ---------------------------------------------------------------------------
# The model call
# ---------------------------------------------------------------------------


def test_claude_argv_allows_only_search_and_allowlisted_fetches() -> None:
    argv = research.claude_argv("/bin/claude", "opus")
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert allowed[0] == "WebSearch"
    assert all(a.startswith("WebFetch(domain:") for a in allowed[1:])
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    assert "--safe-mode" in argv and argv[argv.index("--permission-prompts") + 1] == "none"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == research.DIGEST_SCHEMA


def test_claude_child_env_is_an_allowlist() -> None:
    env = research.claude_child_env({"PATH": "/bin", "HOME": "/tmp/h", "CLAUDE_CODE_OAUTH_TOKEN": FAKE_OAUTH_TOKEN,
                                     "OUTINGS_WEBHOOK_URL": FAKE_WEBHOOK, "AWS_SECRET_ACCESS_KEY": "x"})
    assert set(env) == {"PATH", "HOME", "CLAUDE_CODE_OAUTH_TOKEN", "DISABLE_AUTOUPDATER",
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "OUTINGS_CHILD"}


def test_prompt_carries_window_seeds_and_history_as_data() -> None:
    prompt = research.build_prompt(WINDOW[0], WINDOW[1], "home: {braces}", [{"title": "Old", "url": "https://a.example.org/"}])
    assert "2026-10-07 through\n2026-10-21" in prompt.replace("@@", "") or "2026-10-21" in prompt
    assert "home: {braces}" in prompt and '"title": "Old"' in prompt
    assert all(seed in prompt for seed in research.SEED_URLS)
    assert all(host in prompt for host in research.BLOCKED_HOSTS)
    assert "@@" not in prompt


@pytest.mark.parametrize("envelope, message", [
    ("nope", "not JSON"),
    ({"type": "result", "subtype": "error_max_turns", "is_error": True}, "did not finish"),
    ({"type": "result", "subtype": "success", "is_error": False}, "no structured digest"),
])
def test_bad_envelopes_fail_the_run(envelope: Any, message: str) -> None:
    with pytest.raises(common.OutingsError, match=message):
        research.parse_envelope(envelope if isinstance(envelope, str) else json.dumps(envelope))


# ---------------------------------------------------------------------------
# End to end, with a fake claude
# ---------------------------------------------------------------------------


def full_raw() -> tuple[dict[str, Any], dict[str, str]]:
    a, _ = numbered(1, "science_tech")
    b, url_b = numbered(2, "japanese")
    c, _ = numbered(3, "date_night")
    return raw_digest(science_tech=[a], japanese=[b], date_night=[c], top_picks=[url_b]), pages_for(a, b, c)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    raw, pages = full_raw()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "claude-called"
    envelope = json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 9,
                           "total_cost_usd": 1.2345, "permission_denials": [], "structured_output": raw})
    fake = bin_dir / "claude"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "sys.stdin.read()\n"
        f"open({str(marker)!r}, 'w').write(json.dumps(sorted(os.environ)))\n"
        f"print({envelope!r})\n"
    )
    fake.chmod(0o755)
    data = tmp_path / "data"
    monkeypatch.setenv("OUTINGS_DATA_DIR", str(data))
    monkeypatch.setenv("OUTINGS_PROFILE", str(ROOT / "profile.yaml"))
    monkeypatch.setenv("OUTINGS_CLAUDE_BIN", str(fake))
    monkeypatch.setenv("OUTINGS_PAGE_URL", "http://192.168.1.236/")
    monkeypatch.setenv("OUTINGS_WEBHOOK_URL", FAKE_WEBHOOK)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_OAUTH_TOKEN)
    posts: list[dict[str, Any]] = []

    def opener(request: Any, timeout: float) -> FakeResponse:
        posts.append(json.loads(request.data))
        return FakeResponse(b"{}")

    deps = cli.Deps(fetcher=fetcher_for(pages), opener=opener, sleep=lambda s: None, out=io.StringIO(), clock=lambda: NOW)
    return {"data": data, "marker": marker, "posts": posts, "deps": deps, "tmp": tmp_path}


def test_live_run_writes_the_page_posts_and_records_history(env: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    assert cli.main(["weekly"], env["deps"]) == 0
    assert len(env["posts"]) == 1
    assert env["posts"][0]["content"].endswith("Full digest: http://192.168.1.236/2026-10-07.html")
    assert (env["data"] / "site" / "2026-10-07.html").exists()
    history = json.loads((env["data"] / "state" / "history.json").read_text())
    assert len(history["items"]) == 3 and {i["run_date"] for i in history["items"]} == {"2026-10-07"}
    child_env = json.loads(env["marker"].read_text())
    assert "OUTINGS_WEBHOOK_URL" not in child_env


def test_dry_run_writes_nothing_and_posts_nothing(env: dict[str, Any]) -> None:
    assert cli.main(["weekly", "--dry-run"], env["deps"]) == 0
    assert env["posts"] == []
    assert not env["data"].exists()
    printed = json.loads(env["deps"].out.getvalue())
    assert printed["top_picks"][0].endswith("/japanese/2")


def test_from_json_skips_the_model(env: dict[str, Any]) -> None:
    raw, _ = full_raw()
    saved = env["tmp"] / "raw.json"
    saved.write_text(json.dumps(raw))
    out = env["tmp"] / "out"
    assert cli.main(["weekly", "--from-json", str(saved), "--out", str(out)], env["deps"]) == 0
    assert not env["marker"].exists()
    assert (out / "index.html").exists() and env["posts"] == []


def test_no_webhook_means_no_model_call(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUTINGS_WEBHOOK_URL")
    assert cli.main(["weekly"], env["deps"]) == 1
    assert not env["marker"].exists()


def test_too_few_checked_events_posts_nothing(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUTINGS_MIN_EVENTS", "4")
    assert cli.main(["weekly"], env["deps"]) == 1
    assert env["posts"] == [] and not (env["data"] / "state" / "history.json").exists()


def test_webhook_and_token_never_reach_the_log(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(cli, "setup_logging", lambda: common.setup_logging(stream))

    def failing(request: Any, timeout: float) -> Any:
        raise urllib.error.URLError(f"{request.full_url} {FAKE_OAUTH_TOKEN}")

    env["deps"].opener = failing
    common.log.debug("token %s", FAKE_OAUTH_TOKEN)
    assert cli.main(["weekly"], env["deps"]) == 1
    common.log.warning("leak %s %s", FAKE_WEBHOOK, FAKE_OAUTH_TOKEN)
    text = stream.getvalue()
    assert "event=digest_failed" in text
    assert FAKE_WEBHOOK not in text and FAKE_OAUTH_TOKEN not in text


def test_summary_line_carries_usage(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(cli, "setup_logging", lambda: common.setup_logging(stream))
    assert cli.main(["weekly"], env["deps"]) == 0
    line = next(ln for ln in stream.getvalue().splitlines() if "event=digest_sent" in ln)
    assert "items=3" in line and "turns=9" in line and "cost_usd=1.2345" in line


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


def test_versions_agree() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == outings.__version__


def test_logger_does_not_propagate() -> None:
    common.setup_logging(io.StringIO())
    assert logging.getLogger("outings").propagate is False
