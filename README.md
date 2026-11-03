# outings

A weekly Discord digest of San Francisco and Bay Area outings: science and
tech nights, Japanese culture and language, date night picks and one day
trip, for the next two weeks. Runs as a Kubernetes CronJob on QuasarLab
(Sunday 08:50 America/Los_Angeles); the manifests live in `k8s-argocd` under
`apps/automation/outings`.

Forked from [lab-scout](https://github.com/mithr4ndir/lab-scout): stdlib-only
Python around a pinned `claude` CLI, run on a subscription OAuth token.

## How a run works

1. Load `state/history.json` (what was already suggested).
2. Collect events in the window by script (`collect.py`): JSON-LD listings
   (Chabot, Luma, Meetup and Eventbrite searches), the Stanford and Berkeley
   event feeds (kept only when the title matches a listed interest), and
   small parsers for Exploratorium After Dark and Cal Academy NightLife.
   Online, cancelled, sold-out and out-of-area events are dropped here. Each
   event gets a stable id. See `SOURCES.md`.
3. `claude -p` gets the collected events as data and returns JSON matching
   `research.DIGEST_SCHEMA`: `picks` (a collected id, a section, why, a
   reminder flag; never a title, date or link), plus, with research on,
   up to three researched events per section for what the collection lacks
   (web search and a WebFetch allowlist). With `OUTINGS_RESEARCH=0` the model
   gets no tools at all and only picks.
4. Every event, picked or researched, is checked in code (`checks.py`): ISO start with a UTC offset,
   inside the window, safe https URL, plain text fields, and its local date
   must appear on its own page or a source page, fetched by the job. Anything
   that fails is dropped, never repaired.
   Where the page publishes schema.org event data (JSON-LD, `jsonld.py`), the
   start and end time, venue, city and price come from that data instead of
   the model, and the page badges them ("Time checked", "Price checked"). An
   event the page data marks cancelled, postponed, online only or sold out is
   dropped, as is one whose own page shows a different date. The model's
   venue or price text is kept only where it states what the page states
   and adds detail (a gallery name, a drink minimum).
5. The page is written to `site/<date>.html` (and `index.html`), then the top
   three picks are posted to Discord with a link to it, then history is saved.
6. One summary log line: `event=digest_sent collected=... collected_by_source=...
   items=... picked=... researched=... dropped=... drop_reasons=... turns=...
   cost_usd=... duration_s=...`.

Exit code 0 means posted. Fewer than `OUTINGS_MIN_EVENTS` checked events fails
the run and posts nothing.

## Local use

```
uv sync --frozen
uv run ruff check
uv run pytest -q -rs

# A real research run that writes nothing under the data dir:
(cd claude && npm ci --ignore-scripts)
OUTINGS_DATA_DIR=$PWD/.data OUTINGS_PROFILE=$PWD/profile.yaml \
OUTINGS_CLAUDE_BIN=$PWD/claude/node_modules/@anthropic-ai/claude-code-linux-x64/claude \
PYTHONPATH=src uv run python -m outings weekly --dry-run --out ./out

# Re-check a saved model answer without calling the model:
PYTHONPATH=src uv run python -m outings weekly --from-json ./out/raw.json --out ./out
```

Configuration is documented at the top of `src/outings/cli.py`. Seed sources and
how each one renders are in `SOURCES.md`.
