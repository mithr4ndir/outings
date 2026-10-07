"""History of suggested items, for dedupe and the prompt's already-suggested list.

A JSON file replaced by rename, not SQLite: the state lives on NFS, where
SQLite locking is unsafe (the lab's *arr databases corrupted that way).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

from .common import OutingsError, canonical_url, write_text_atomic

HISTORY_FILE = "history.json"
KEEP_DAYS = 365
PROMPT_DAYS = 28
PROMPT_MAX = 120
FIELDS = ("url", "title", "start", "run_date")


@dataclass
class History:
    items: list[dict[str, str]] = field(default_factory=list)

    def url_keys(self) -> set[str]:
        return {canonical_url(item["url"]) for item in self.items}

    def recent(self, today: dt.date, days: int = PROMPT_DAYS, limit: int = PROMPT_MAX) -> list[dict[str, str]]:
        cutoff = (today - dt.timedelta(days=days)).isoformat()
        kept = [item for item in self.items if item["run_date"] >= cutoff]
        kept.sort(key=lambda item: item["run_date"], reverse=True)
        return [{"title": item["title"], "url": item["url"]} for item in kept[:limit]]

    def record(self, url: str, title: str, start: str, run_date: str) -> None:
        key = (canonical_url(url), start[:10])
        for item in self.items:
            if (canonical_url(item["url"]), item["start"][:10]) == key and item["run_date"] == run_date:
                return
        self.items.append({"url": url, "title": title, "start": start, "run_date": run_date})

    def prune(self, today: dt.date) -> None:
        cutoff = (today - dt.timedelta(days=KEEP_DAYS)).isoformat()
        self.items = [item for item in self.items if item["run_date"] >= cutoff]


def load_history(state_dir: Path) -> History:
    path = state_dir / HISTORY_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return History()
    except (OSError, ValueError) as exc:
        # Refuse rather than start empty: an empty history would repeat old
        # suggestions. The file is left in place for inspection.
        raise OutingsError(f"history unreadable ({type(exc).__name__}); not running") from None
    raw = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise OutingsError("history has no items list; not running")
    items = [{k: entry[k] for k in FIELDS} for entry in raw
             if isinstance(entry, dict) and all(isinstance(entry.get(k), str) for k in FIELDS)]
    return History(items)


def save_history(state_dir: Path, history: History) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_text_atomic(state_dir / HISTORY_FILE, json.dumps({"version": 1, "items": history.items}, indent=1) + "\n",
                      mode=0o600)
