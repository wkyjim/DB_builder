# DB Builder — Agent Guide

## Project Summary

DB_builder is a local-first financial data pipeline and investment intelligence platform.

- **Ingests** U.S. equities, macro data, news, ETF flows, and short-positioning data
- **Stores** in local PostgreSQL (source of truth)
- **Syncs** selected data to Neon (cloud subset for API/deployment)
- **Publishes** reports via FastAPI API, Telegram bot, and GitHub Pages dashboard

Key components:
- **Local PostgreSQL** — source of truth for all raw and derived data
- **Neon** — selected cloud subset consumed by API/Telegram/dashboard
- **API** (`neon-api/`) — FastAPI app, reads from Neon only
- **Telegram** (`market-intelligence-telegram-bot/`) — command interface + scheduled reports
- **Dashboard** (`market-dashboard/`) — GitHub Pages static site

## Documentation

| Document | Purpose |
|----------|---------|
| `ARCHITECTURE.md` | Full current architecture |
| `TECHNICAL_DEBT.md` | Prioritized improvement backlog |
| `docs/PROJECT_MAP.md` | Annotated directory tree |
| `docs/DATA_FLOW.md` | Sources, pipelines, table relationships |
| `docs/DEPLOYMENT.md` | Infrastructure and deployment |

## Agent Quick Start

```bash
cd /workspace/DB_builder
git status
git log --oneline -10

# Run all tests
python -m pytest

# Run specific module tests
python -m pytest tests/test_indicators.py
python -m pytest tests/test_eastmoney.py
python -m pytest tests/test_etf_flow_analytics.py

# Run nested repo tests
python -m pytest neon-api/tests/test_api.py
python -m pytest market-intelligence-telegram-bot/tests/test_main.py

# Dry-run commands (safe, no writes)
python scripts/rule_based_market_update.py --dry-run --window-hours 24
python scripts/etf_flow_analytics.py --dry-run --start-date 2026-01-01
python scripts/pgSQL_equities_auto.py --dry-run --tickers AAPL
```

## Critical Safety Rules

- **Never** access or modify `DB_builder_env/` (external secret directory)
- **Never** print, log, or expose secrets in any output
- **Never** run destructive SQL (DROP, TRUNCATE, DELETE, ALTER DROP) without explicit human approval
- **Never** push, deploy, or change remote infrastructure without explicit approval
- **Always** inspect `git status` before starting work
- **Always** inspect `git diff` after making changes
- **Prefer** minimal, reversible changes
- **Keep** `DB_BUILDER_AUDIT_MODE=1` during audits (never unset it)

## Production-Sensitive Areas

Exercise extreme caution when modifying:

- `scripts/auto_*.bat` — scheduled production workflows
- `deploy/oracle/` — Oracle Cloud deployment configuration
- `neon-api/` — deployed FastAPI API (separate git repo)
- `market-intelligence-telegram-bot/` — deployed bot (separate git repo)
- `migrations/` — database schema changes
- `src/db_builder/neon_sync.py` — local-to-Neon sync logic
- `src/db_builder/rule_based_regime.py` — market regime scoring

## Architecture Invariants

- **Local PostgreSQL is source of truth** — Neon is a deployment subset
- **Eastmoney primary, yfinance fallback** for equity data
- **Rule-based reports are deterministic** — do not introduce LLM scoring unless explicitly requested
- **FINRA short volume ≠ short interest** — distinct datasets, distinct tables
- **Economic indicators are local-only** — not synced to Neon unless explicitly changed
- **Nested repos have independent deployment lifecycles** — do not merge without analysis
- **`*.md` and `*.sql` are gitignored** — use `git add -f` to track docs and migrations

## Where To Look

| Task | Location |
|------|----------|
| Equity ingestion | `scripts/pgSQL_equities_auto.py`, `src/db_builder/eastmoney.py` |
| Macro data | `scripts/macro_data_fetch.py`, `src/db_builder/config.py` |
| Technical indicators | `src/db_builder/indicators.py` |
| ETF flow analytics | `src/db_builder/etf_flow/`, `scripts/etf_flow_analytics.py` |
| Short analytics | `src/db_builder/finra_short_*.py`, `src/db_builder/short_pipeline.py` |
| Market regime | `src/db_builder/rule_based_regime.py` |
| Report generation | `src/db_builder/report_renderer.py`, `scripts/rule_based_market_update.py` |
| API endpoints | `neon-api/main.py` |
| Telegram commands | `market-intelligence-telegram-bot/main.py` |
| Deployment | `deploy/oracle/`, `neon-api/Dockerfile` |
| Tests | `tests/`, `neon-api/tests/`, `market-intelligence-telegram-bot/tests/` |

## Before Making Changes

1. Run `git status` and read relevant architecture docs
2. Inspect affected tests before changing code
3. Make bounded, minimal changes
4. Run focused tests: `python -m pytest tests/test_<module>.py`
5. Inspect `git diff` after changes
6. Do not commit or push unless explicitly authorized
7. Use `git add -f` for documentation (`*.md`) and migration (`*.sql`) files

## What NOT to Do

- Do not redesign the repository structure
- Do not rename `db_builder` or any existing modules
- Do not merge nested repositories
- Do not remove files merely because they look deprecated
- Do not make speculative refactors or "improvements"
- Do not change `.gitignore` without understanding its current behavior
