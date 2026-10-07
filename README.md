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
2. `claude -p` with web search and a WebFetch allowlist researches the window
   and returns JSON matching `research.DIGEST_SCHEMA`.
3. Every event is checked in code (`checks.py`): ISO start with a UTC offset,
   inside the window, safe https URL, plain text fields, and its local date
   must appear on its own page or a source page, fetched by the job. Anything
   that fails is dropped, never repaired.
4. The page is written to `site/<date>.html` (and `index.html`), then the top
   three picks are posted to Discord with a link to it, then history is saved.
5. One summary log line: `event=digest_sent items=... dropped=...
   drop_reasons=... turns=... cost_usd=... duration_s=...`.

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
