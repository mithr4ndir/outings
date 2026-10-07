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
- `claude -p` needs `--allowedTools` for WebSearch and WebFetch, or every call is
  silently denied and the model answers from memory.
- Exploratorium 403s curl's default User-Agent; SFJAZZ and JCCCNC block all
  non-browser clients (see `SOURCES.md`).
