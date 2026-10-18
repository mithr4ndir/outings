# Agent instructions: outings

## Purpose

A weekly Discord digest of SF and Bay Area outings. Stdlib-only Python that shells out to
a pinned `claude` CLI for research, checks every event date in code, writes a static page
and posts the top picks. Shipped as `ghcr.io/mithr4ndir/outings`, run as a CronJob from
`k8s-argocd/apps/automation/outings`.

## Boundaries

- Forked from `lab-scout`; keep the two aligned when fixing shared code (redaction,
  `discord_text`, `post_payload`, `claude_argv`).
- `[tool.uv] package = false`. The image copies `src/outings` and runs `python -m outings`.
- Secrets (`OUTINGS_WEBHOOK_URL`, `CLAUDE_CODE_OAUTH_TOKEN`) come from a Kubernetes
  Secret. None are in this repo.
- State is JSON replaced by rename. Do not move it to SQLite: it lives on NFS.

## Validation

```
uv sync --frozen
uv run ruff check
uv run pytest -q -rs
```

`__init__.py`'s `__version__` must match `pyproject.toml`; a test enforces it.

## Landmines

- **A skipped test fails the run** (`tests/conftest.py`).
- The suite refuses network: urllib and DNS are monkeypatched to raise.
- The model is never the only source for a date. Do not loosen `checks.date_evidence`
  or default `OUTINGS_REQUIRE_DATE_ON_PAGE` to off to get more items; measure the drop
  reasons in the summary line instead.
- Page facts (`jsonld.py`) override model fields only for an Event matched by url, or
  the single Event on the event's own page. A listing page with many Events is not
  evidence for any one of them. Eventbrite series give the first session as startDate
  and the last as endDate, so a day inside that span is not a different date (a replay
  of the 2026-10-18 digest caught that false drop). Replay a real digest with
  `--from-json` after changing match rules.
- `claude -p` needs `--allowedTools` for WebSearch and WebFetch, or every call is
  silently denied and the model answers from memory.
- Exploratorium 403s curl's default User-Agent; SFJAZZ and JCCCNC block all
  non-browser clients (see `SOURCES.md`).
