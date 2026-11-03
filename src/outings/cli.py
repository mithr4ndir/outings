"""Weekly outings digest for San Francisco and the Bay Area.

Usage:
    python -m outings weekly [--dry-run] [--out DIR] [--from-json FILE]

weekly       Collect events in the next OUTINGS_WINDOW_DAYS days from venue
             calendars, listing sites and university feeds (collect.py), ask
             claude to pick among them and, with research on, to add what
             the collection lacks (web search, allowlisted WebFetch). Check
             every event's date against its own page, write the digest page
             under DATA/site, post the top three picks to Discord, then
             record them in DATA/state.
             Run by a Kubernetes CronJob; exit code 0 means posted.
--dry-run    No webhook read, no post, nothing written under the data
             directory. Prints the checked digest as JSON.
--out DIR    With --dry-run: also write the page, the raw model output
             and the collected events to DIR, to look at.
--from-json  Skip the model and check a saved raw output instead (the
             raw.json that --out writes). Implies --dry-run. Collection
             still runs: event ids are stable, so picks map again.

Configuration (environment, all optional unless noted):
    OUTINGS_WEBHOOK_URL         Discord webhook (required to post)
    CLAUDE_CODE_OAUTH_TOKEN     claude subscription token (passed to claude only)
    OUTINGS_PAGE_URL            where the page is served (required to post)
    OUTINGS_DATA_DIR            absolute; site/ and state/ live here (/data)
    OUTINGS_PROFILE             interests profile for the prompt (/app/profile.yaml)
    OUTINGS_CLAUDE_BIN          claude CLI (claude)
    OUTINGS_CLAUDE_MODEL        model alias (opus)
    OUTINGS_CLAUDE_TIMEOUT      seconds (1500)
    OUTINGS_WINDOW_DAYS         days after today in the window (14)
    OUTINGS_MIN_EVENTS          fewer checked events fails the run (3)
    OUTINGS_REQUIRE_DATE_ON_PAGE  0 turns the date-on-page check off (1)
    OUTINGS_COLLECT             0 skips the scripted collection (1)
    OUTINGS_RESEARCH            0 gives the model no tools: it only picks
                                from the collection (1)
    OUTINGS_DEBUG               any value enables debug logging
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import collect, research
from .checks import PACIFIC, Digest, build_digest, cached
from .common import Fetcher, OutingsError, Rejected, fetch_page, log, setup_logging, utcnow, write_text_atomic
from .notify import build_payload, post_payload, resolve_webhook
from .render import digest_json, write_site
from .state import load_history, save_history

DEFAULT_DATA_DIR = "/data"
DEFAULT_PROFILE = "/app/profile.yaml"
RAW_MAX_BYTES = 4 << 20


@dataclass
class Config:
    data_dir: Path
    profile: str
    page_url: str | None
    claude_bin: str
    claude_model: str
    claude_timeout: int
    window_days: int
    min_events: int
    require_date_on_page: bool
    collect: bool = True
    research: bool = True

    @classmethod
    def from_env(cls) -> Config:
        # OUTINGS_WEBHOOK_URL is deliberately not read here: only
        # resolve_webhook reads it, and only to post.
        env = os.environ

        def int_env(name: str, default: int, low: int, high: int) -> int:
            try:
                return min(max(int(env.get(name, default)), low), high)
            except ValueError:
                log.warning("%s is not an integer; using %d", name, default)
                return default

        data_dir = env.get("OUTINGS_DATA_DIR", DEFAULT_DATA_DIR)
        if not os.path.isabs(data_dir):
            log.warning("OUTINGS_DATA_DIR is not absolute; using the default")
            data_dir = DEFAULT_DATA_DIR
        model = env.get("OUTINGS_CLAUDE_MODEL", "opus")
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,99}$", model):
            log.warning("OUTINGS_CLAUDE_MODEL is malformed; using opus")
            model = "opus"
        page_url = (env.get("OUTINGS_PAGE_URL") or "").strip() or None
        if page_url is not None:
            parts = urllib.parse.urlsplit(page_url)
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or "@" in parts.netloc:
                log.warning("OUTINGS_PAGE_URL is not a plain http(s) URL; ignoring it")
                page_url = None
        return cls(
            data_dir=Path(data_dir),
            profile=env.get("OUTINGS_PROFILE", DEFAULT_PROFILE),
            page_url=page_url,
            claude_bin=env.get("OUTINGS_CLAUDE_BIN", "claude"),
            claude_model=model,
            claude_timeout=int_env("OUTINGS_CLAUDE_TIMEOUT", 1500, 60, 3600),
            window_days=int_env("OUTINGS_WINDOW_DAYS", 14, 1, 28),
            min_events=int_env("OUTINGS_MIN_EVENTS", 3, 1, 18),
            require_date_on_page=env.get("OUTINGS_REQUIRE_DATE_ON_PAGE", "1").strip() != "0",
            collect=env.get("OUTINGS_COLLECT", "1").strip() != "0",
            research=env.get("OUTINGS_RESEARCH", "1").strip() != "0",
        )


@dataclass
class Deps:
    """Seams for tests. None means the real implementation."""

    fetcher: Fetcher | None = None
    opener: Callable[..., Any] | None = None
    sleep: Callable[[float], None] = time.sleep
    out: Any = None
    clock: Callable[[], dt.datetime] = utcnow


@dataclass
class Summary:
    """The run's one structured log line, for Loki and a dashboard."""

    fields: dict[str, Any] = field(default_factory=dict)

    def line(self, event: str) -> str:
        parts = [f"event={event}"]
        for key, value in self.fields.items():
            if isinstance(value, dict):
                value = json.dumps(value, separators=(",", ":"), sort_keys=True)
            parts.append(f"{key}={value}")
        return " ".join(parts)


def page_link(base: str, run_date: dt.date) -> str:
    return urllib.parse.urljoin(base if base.endswith("/") else base + "/", f"{run_date.isoformat()}.html")


def read_raw(path: str) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise OutingsError(f"--from-json file unreadable ({type(exc).__name__})") from None
    if len(raw) > RAW_MAX_BYTES:
        raise OutingsError("--from-json file is larger than the limit")
    try:
        data = json.loads(raw)
    except ValueError:
        raise OutingsError("--from-json file is not JSON") from None
    if not isinstance(data, dict):
        raise OutingsError("--from-json file is not a JSON object")
    return data


def run(cfg: Config, args: argparse.Namespace, deps: Deps, summary: Summary) -> None:
    """One digest. Raises OutingsError on any failure."""
    started = time.monotonic()
    dry_run = args.dry_run or bool(args.from_json)
    now = deps.clock().astimezone(PACIFIC)
    today = now.date()
    window = (today, today + dt.timedelta(days=cfg.window_days))
    state_dir, site_dir = cfg.data_dir / "state", cfg.data_dir / "site"
    history = load_history(state_dir)

    webhook = None
    if not dry_run:
        # Before the model call: a run whose output can never be posted
        # wastes usage and up to the claude timeout.
        if cfg.page_url is None:
            raise OutingsError("OUTINGS_PAGE_URL is not set; not calling claude")
        webhook = resolve_webhook()
        if webhook is None:
            raise OutingsError("no webhook; not calling claude")

    # One fetcher for the whole run: a picked event's page was often fetched
    # already by its listing, and checks fetch it again.
    fetcher = deps.fetcher or cached(fetch_page)
    counts: dict[str, int] = {}
    collected = collect.collect(window, fetcher, counts=counts) if cfg.collect else []
    summary.fields["collected"] = len(collected)
    summary.fields["collected_by_source"] = counts
    if not collected and not cfg.research:
        raise OutingsError("nothing was collected and research is off; not calling claude")

    usage: dict[str, Any] = {}
    if args.from_json:
        raw = read_raw(args.from_json)
    else:
        profile = research.read_profile(cfg.profile)
        prompt = research.build_prompt(today, window[1], profile, history.recent(today),
                                       [c.prompt_line() for c in collected], cfg.research)
        # The model call never runs in the data directory: it is shared storage.
        with tempfile.TemporaryDirectory(prefix="outings-claude-") as scratch:
            raw = research.call_claude(cfg.claude_bin, cfg.claude_model, cfg.claude_timeout, prompt,
                                       Path(scratch), usage=usage, research=cfg.research)
    summary.fields.update(usage)

    try:
        digest = build_digest(raw, window, history.url_keys(), fetcher, cfg.require_date_on_page,
                              {c.id: c for c in collected}, cfg.research)
    except Rejected as exc:
        raise OutingsError(f"model output rejected: {exc}") from None
    record_counts(summary, digest)
    count = len(digest.events())
    if count < cfg.min_events:
        raise OutingsError(f"only {count} checked event(s), fewer than {cfg.min_events}; posting nothing")

    if dry_run:
        print(digest_json(digest, today), end="", file=deps.out or sys.stdout)
        if args.out:
            out = Path(args.out)
            write_site(out, digest, today)
            write_text_atomic(out / "raw.json", json.dumps(raw, indent=1, ensure_ascii=False) + "\n")
            write_text_atomic(out / "collected.txt", "".join(c.prompt_line() + "\n" for c in collected))
            log.info("dry run: page written to %s", out / "index.html")
        summary.fields["duration_s"] = round(time.monotonic() - started, 1)
        log.info("%s", summary.line("digest_dry_run"))
        return

    assert webhook is not None and cfg.page_url is not None
    # The page first, so the link in the message works when it lands.
    write_site(site_dir, digest, today)
    post_payload(webhook, build_payload(digest, page_link(cfg.page_url, today)), opener=deps.opener, sleep=deps.sleep)
    for event in digest.events():
        history.record(event.url, event.title, event.start.isoformat(), today.isoformat())
    history.prune(today)
    try:
        save_history(state_dir, history)
    except OSError as exc:
        raise OutingsError(f"posted, but the history could not be saved ({type(exc).__name__})") from None
    summary.fields["duration_s"] = round(time.monotonic() - started, 1)
    log.info("%s", summary.line("digest_sent"))


def record_counts(summary: Summary, digest: Digest) -> None:
    summary.fields["items"] = len(digest.events())
    summary.fields["picked"] = sum(1 for e in digest.events() if e.origin.startswith("collected:"))
    summary.fields["researched"] = sum(1 for e in digest.events() if e.origin == "research")
    summary.fields["sections"] = {name: len(events) for name, events in digest.sections.items()}
    summary.fields["day_trip"] = int(digest.day_trip is not None)
    summary.fields["dropped"] = digest.dropped
    summary.fields["drop_reasons"] = digest.drop_reasons


def ensure_home() -> None:
    """Create HOME when it is missing: /tmp is an empty volume at pod start,
    and claude writes ~/.claude.json and ~/.claude/."""
    home = os.environ.get("HOME")
    if not home or not os.path.isabs(home) or os.path.isdir(home):
        return
    try:
        os.makedirs(home, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise OutingsError(f"HOME cannot be created ({type(exc).__name__})") from None


class QuietParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise OutingsError(f"usage: {message}")


def build_parser() -> argparse.ArgumentParser:
    parser = QuietParser(prog="outings")
    sub = parser.add_subparsers(dest="command", required=True)
    weekly = sub.add_parser("weekly")
    weekly.add_argument("--dry-run", action="store_true", help="print the checked digest, post nothing")
    weekly.add_argument("--out", help="with --dry-run, also write the page and raw output here")
    weekly.add_argument("--from-json", help="check a saved raw model output instead of calling claude")
    return parser


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_logging()
    summary = Summary()
    try:
        args = build_parser().parse_args(argv)
        if args.out and not (args.dry_run or args.from_json):
            raise OutingsError("--out needs --dry-run")
        ensure_home()
        run(Config.from_env(), args, deps or Deps(), summary)
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    except OutingsError as exc:
        log.warning("%s", exc)
        log.info("%s", summary.line("digest_failed"))
        return 1
    except BaseException:  # noqa: BLE001  the log and the exit code are the only report
        log.exception("unexpected failure")
        log.info("%s", summary.line("digest_failed"))
        return 1
