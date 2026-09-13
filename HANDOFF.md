# DB_builder Handoff

## Project Overview

DB_builder is a local-first financial data and investment intelligence project.

Main responsibilities:

- Fetch U.S. equity raw prices from Eastmoney, with yfinance fallback for failed/missing session rows.
- Calculate and stage technical indicators for local PostgreSQL and selected Neon upload.
- Fetch macro/index/futures/rates/FX/commodity data from yfinance into `public.macro`, with intraday snapshots in `public.macro_live`.
- Fetch local news, classify headlines, build news/sector/regime/theme signals, and generate reports.
- Generate a deterministic rule-based market update report and publish the latest markdown report to the separate GitHub Pages dashboard repo.
- Build early positioning/flow ingestion infrastructure for CFTC COT and FINRA short-sale volume.

Main technologies:

- Python scripts and reusable modules under `src/db_builder`.
- PostgreSQL local database as source of truth.
- Neon PostgreSQL for selected deployed/API data.
- Render FastAPI app in nested repo `neon-api/`.
- GitHub Pages dashboard in separate local repo:
  `C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\market-dashboard`.
- Conda environment: `PostgreSQL_db`.

Important entry points:

- `scripts/auto_postgreSQL_db.bat`: scheduled equities workflow.
- `scripts/auto_macro_db.bat`: scheduled macro/economic workflow.
- `scripts/auto_news_intelligence.bat`: scheduled news/signals/report workflow.
- `scripts/macro_data_fetch.py`: macro close/live fetch.
- `scripts/pgSQL_equities_auto.py`: Eastmoney raw equity fetch.
- `scripts/indicator_staged_backfill.py`: staged indicator CSV/export/calculate/upsert workflow.
- `scripts/pgSQL_daily_bulk_sync_to_neon.py`: selected local-to-Neon sync.
- `scripts/rule_based_market_update.py`: deterministic report generation and dashboard publishing.

## Current Objective

The recent work first focused on making the daily pipelines more reliable and making the latest rule-based market update visible on the GitHub Pages market dashboard.

The latest work added a production-oriented ETF flow analytics layer. The goal was to move ETF flows from a descriptive 1D/5D table into structured analytics that can support market regime interpretation, flow confidence, price/flow contradictions, leadership rotation, and forward setup scoring.

Acceptance criteria from the current session:

- Daily Eastmoney raw fetch should not trust Eastmoney YTD percent change.
- YTD percent change should be locally calculated and written for daily raw equity rows.
- Partial Eastmoney fetches should be detectable, repairable, and covered by yfinance fallback for eligible tickers.
- Indicator generation should use a staged CSV-based process to survive interruptions.
- Macro live snapshots should be stored locally and uploaded to Neon.
- Economic indicators should remain local-only and not be uploaded to Neon.
- Latest rule-based report should be copied to `market-dashboard/data/latest-report.md`, committed, and pushed so the website shows it.
- ETF flow analytics should read existing issuer-derived ETF daily data, calculate flow features, persist analytical tables, and render a structured section in the rule-based report.
- Document current project structure and updates for the next session.

## Work Completed

### Equity Pipeline Hardening

Files:

- `src/db_builder/eastmoney.py`
- `src/db_builder/yfinance_equity_fallback.py`
- `src/db_builder/equity_security_status.py`
- `scripts/pgSQL_equities_auto.py`
- `scripts/equity_security_status.py`
- `scripts/equity_session_coverage_audit.py`
- `scripts/repair_historical_equity_gaps.py`
- `scripts/recalculate_core_ytd.py`
- `tests/test_eastmoney.py`
- `tests/test_yfinance_equity_fallback.py`
- `tests/test_equity_security_status.py`
- `tests/test_equity_session_coverage_audit.py`
- `tests/test_repair_historical_equity_gaps.py`
- `tests/test_recalculate_core_ytd.py`

Important details:

- `eastmoney.replace_ytd_pct_chg_with_local_calculation()` replaces fetched `ytd_pct_chg` before raw upsert.
- The daily YTD formula compounds prior local `ytd_pct_chg` with today’s local daily return. It falls back to `close / prev_close - 1` if daily `pct_chg` is missing.
- yfinance fallback rows also pass through local YTD replacement before upsert.
- `equity_security_status` classifies raw instruments and flags coverage eligibility to avoid counting obvious non-core instruments/warrants/no-price rows as failed coverage.
- `equity_session_coverage_audit.py` can audit core or all coverage. Core coverage excludes non-core/no-valid-price rows.
- `repair_historical_equity_gaps.py` uses yfinance multi-ticker fallback to repair historical missing raw rows and recalculate affected indicators.
- `recalculate_core_ytd.py` calculates split-safe 2026 YTD CSVs and updates local/Neon directly by `(date, ticker)`.

Known run results:

- Local YTD CSV: `artifacts/core_ytd/core_ytd_2026_20260709_223924.csv`.
- Local YTD update verified: `1,419,982` matched rows, `0` mismatches.
- Neon YTD update verified against existing Neon rows: `517,058` matched rows, `0` mismatches, `902,924` CSV rows absent from Neon.
- Core session coverage audit for 2026-05-01 through 2026-07-08 showed no low raw-coverage sessions and no indicator gaps after repair/status filtering.

### Staged Indicator Workflow

Files:

- `scripts/indicator_staged_backfill.py`
- `scripts/auto_postgreSQL_db.bat`
- `tests/test_indicator_staged_backfill.py`

Important details:

- Daily scheduled workflow now runs raw fetch first, syncs raw equities to Neon, then runs staged indicator calculation/upsert.
- `indicator_staged_backfill.py` supports `export`, `calculate`, `upsert-local`, `upsert-neon`, and `all`.
- CSV staging is intended to allow resuming after interruption.

### Macro Live Data and Economic Sync Policy

Files:

- `scripts/macro_data_fetch.py`
- `scripts/economic_sync_to_neon.py`
- `scripts/auto_macro_db.bat`
- `tests/test_macro_data_fetch.py`
- `tests/test_economic_sync_to_neon.py`

Important details:

- `public.macro_live` stores only the latest intraday macro snapshot per run.
- `macro_data_fetch.py` refreshes live rows locally and in Neon when rows are not market-close data.
- `economic_sync_to_neon.py` is intentionally disabled. Economic indicators are local-only.
- `auto_macro_db.bat` no longer runs economic sync to Neon.

### Rule-Based Market Update and Dashboard Publish

Files:

- `scripts/rule_based_market_update.py`
- `scripts/auto_news_intelligence.bat`
- `src/db_builder/report_renderer.py`
- `src/db_builder/rule_based_market_data.py`
- `tests/test_rule_based_market_update_script.py`
- `tests/test_scoring_rules.py`

Important details:

- `rule_based_market_update.py --save --publish-dashboard --push-dashboard` now:
  - saves a timestamped report under `reports/`;
  - copies it to `C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\market-dashboard\data\latest-report.md`;
  - commits and pushes the dashboard repo if the report changed.
- `auto_news_intelligence.bat` now calls the publish/push flags, so scheduled daily rule-based reports update the website.
- Latest dashboard report was pushed to `wkyjim/market-dashboard` at commit `08a4488 Update latest market report`.

### Positioning and Flow Infrastructure

Files:

- `src/db_builder/flow_sources.py`
- `src/db_builder/cot_positions.py`
- `src/db_builder/finra_short_volume.py`
- `src/db_builder/positioning_flow_signals.py`
- `src/db_builder/sec_13f_holdings.py`
- `src/db_builder/ici_flows.py`
- `src/db_builder/etf_holdings.py`
- `scripts/flow_source_check.py`
- `scripts/cot_fetch.py`
- `scripts/finra_short_volume_fetch.py`
- `scripts/positioning_flow_signals.py`
- `scripts/sec_13f_fetch.py`
- `scripts/ici_flows_fetch.py`
- `scripts/etf_holdings_fetch.py`
- `tests/test_flow_sources.py`
- `tests/test_cot_positions.py`
- `tests/test_finra_short_volume.py`
- `tests/test_positioning_flow_signals.py`

Important details:

- CFTC COT and FINRA short-sale volume are the first production-grade flow sources.
- SEC 13F, ICI, and ETF holdings currently have table/schema scaffolds but are not full production pipelines yet.
- `public.flow_source_health` mirrors RSS-style health tracking for fragile external sources.
- `public.positioning_flow_signals` unifies COT and FINRA-derived signals for reporting.

### ETF Flow Analytics Layer

Files:

- `migrations/20260711_etf_flow_analytics.sql`
- `config/etf_flow_analytics.yaml`
- `scripts/etf_flow_analytics.py`
- `src/db_builder/etf_flow/__init__.py`
- `src/db_builder/etf_flow/aggregation.py`
- `src/db_builder/etf_flow/backtest.py`
- `src/db_builder/etf_flow/breadth.py`
- `src/db_builder/etf_flow/confidence.py`
- `src/db_builder/etf_flow/config.py`
- `src/db_builder/etf_flow/consensus.py`
- `src/db_builder/etf_flow/feature_engineering.py`
- `src/db_builder/etf_flow/forward_signal.py`
- `src/db_builder/etf_flow/models.py`
- `src/db_builder/etf_flow/momentum.py`
- `src/db_builder/etf_flow/normalization.py`
- `src/db_builder/etf_flow/price_flow.py`
- `src/db_builder/etf_flow/regime.py`
- `src/db_builder/etf_flow/report_adapter.py`
- `src/db_builder/etf_flow/repository.py`
- `src/db_builder/etf_flow/run.py`
- `src/db_builder/etf_flow/validation.py`
- `docs/etf_flow_analytics.md`
- `tests/test_etf_flow_analytics.py`
- Updated `src/db_builder/rule_based_market_data.py`
- Updated `src/db_builder/report_renderer.py`

Important details:

- The canonical raw issuer source remains `public.etf_daily_data`.
- New idempotent schema objects include:
  - `public.etf_daily_raw`
  - `public.etf_master`
  - `public.etf_flow_daily`
  - `public.etf_flow_features`
  - `public.etf_flow_segment_daily`
  - `public.etf_flow_consensus_daily`
  - `public.etf_flow_rotation_daily`
  - `public.etf_flow_regime_daily`
  - `public.etf_flow_forward_signals`
  - `public.etf_flow_audit_flags`
- Preferred production flow estimate is:
  - `(shares_outstanding_t - shares_outstanding_t-1) * nav_t`
- Lag-NAV audit estimate is also calculated:
  - `(shares_outstanding_t - shares_outstanding_t-1) * nav_t-1`
- ETF trading volume is explicitly not treated as fund flow.
- The analytics package implements:
  - daily clean flow calculation;
  - normalized flow/AUM and winsorized observations;
  - 5D/20D/60D rolling features;
  - EMA, slope, acceleration, persistence;
  - cross-issuer consensus;
  - flow breadth and concentration;
  - price x flow state matrix;
  - segment flow scores;
  - ETF flow regime;
  - confidence adjustment utilities;
  - heuristic forward setup scoring;
  - audit flags;
  - backtest scaffold with no predictive claim.
- `scripts/etf_flow_analytics.py` supports:
  - `--dry-run`
  - `--upsert-local`
  - `--start-date`
  - `--as-of-date`
  - `--existing-regime-score`
  - `--json`
- `rule_based_market_data.collect_rule_based_inputs()` now loads latest persisted ETF flow analytics.
- `report_renderer.render_rule_based_report()` now renders:
  - ETF Flow Executive Summary
  - Market Flow Dashboard
  - Flow-Confirmed Forward Setups
  - ETF Flow Contradiction Flags

Recent ETF flow analytics run:

```text
python scripts/etf_flow_analytics.py --upsert-local --start-date 2026-01-01 --write-report-output

as_of=2026-07-11
raw=7,590
daily=7,590
features=7,590
segments=5,861
consensus=5,861
rotation=5,861
forward=5,861
audits=25
regime=1
flow_regime=moderate risk-off
score=32.70
confidence=34.90
```

Recent ETF flow report output:

- Report generated: `reports/rule_based_market_update_20260711_113336.md`
- Dashboard pushed in separate repo:
  - `market-dashboard` commit `47c2383 Add ETF flow analytics report section`

### Neon API and Dashboard

Nested repo:

- `C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\neon-api`

Dashboard repo:

- `C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\market-dashboard`

Recent dashboard work:

- `market-dashboard` was cloned locally.
- Website files exist locally: `index.html`, `app.js`, `styles.css`, `data/latest-report.md`.
- Pushed dashboard commit `5a6f306 Add OHLCV chart and expanded market tape`.
- Pushed report update commit `08a4488 Update latest market report`.

Recent API work:

- `neon-api/main.py` has local changes adding `/market-tape`, but that nested repo has not been committed/pushed in this DB_builder commit.
- The dashboard currently uses existing `/macro/batch/latest`, so it does not depend on `/market-tape` being deployed.

## Current State

Works:

- Local DB_builder has code to publish latest rule-based reports to the dashboard repo.
- Dashboard repo is clean and pushed to GitHub.
- Targeted tests for report publishing and scoring passed.
- Eastmoney daily dry-run showed calculated `ytd_pct_chg` in output.
- YTD local/Neon direct updates completed for existing rows.
- ETF flow analytics tables have been materialized in local PostgreSQL from existing issuer ETF daily data.
- Rule-based report renders the new ETF Flow Analytics section from persisted analytical tables.

Partially working:

- Full flow source ingestion is only complete for CFTC/FINRA initial pipelines. SEC 13F, ICI, and ETF holdings need real parsers/adapters.
- Neon has fewer raw equity rows than local. Existing Neon rows match corrected YTD, but missing Neon rows remain absent by design unless backfilled.
- `/market-tape` in `neon-api` is local-only until nested repo commit/push/deploy.

Not verified:

- Full `pytest` after all pending changes was not run in this handoff step.
- Full scheduled `.bat` workflows were not run after the latest commit preparation.
- GitHub Pages deployment status was not checked after the latest report push.
- ETF flow analytics has not been wired into the scheduled macro batch yet as a separate explicit step. Macro ETF flow refresh exists, but analytical table refresh should be scheduled deliberately if desired.
- ETF flow forward setup scores are heuristic only; no historical calibration or predictive validation has been completed.

Uncommitted/generated local artifacts:

- `artifacts/` contains CSV/JSON repair and YTD export outputs. These are generated local files and should not be committed.
- `reports/` and `logs/` are ignored generated outputs.
- `docs/` and `migrations/` are ignored by the current `.gitignore` because it has global `*.md` and `*.sql` ignores. ETF flow documentation and migration need `git add -f` when committing.

## Remaining Work

Priority checklist:

1. Verify and, if desired, commit/push nested `neon-api` changes.
   - Files: `neon-api/main.py`, `neon-api/tests/test_api.py`.
   - Run: `cd neon-api && pytest tests/test_api.py`.
   - Commit message suggestion: `Add market tape API endpoint`.

2. Run one full daily scheduled workflow.
   - File: `scripts/auto_news_intelligence.bat`.
   - Confirm it updates `market-dashboard/data/latest-report.md` and pushes if changed.

3. Run staged indicator workflow on the next market date.
   - Files: `scripts/auto_postgreSQL_db.bat`, `scripts/indicator_staged_backfill.py`.
   - Confirm raw equities sync happens before indicator calculation and indicator Neon upsert.

4. Decide whether to backfill missing Neon equity rows.
   - Current verified fact: Neon matched YTD values on existing rows but had fewer rows than local.
   - Likely files: `scripts/pgSQL_daily_bulk_sync_to_neon.py`, `scripts/recalculate_core_ytd.py`.

5. Finish flow source Phase 1 validation.
   - Run:
     - `python scripts/flow_source_check.py`
     - `python scripts/cot_fetch.py --dry-run`
     - `python scripts/finra_short_volume_fetch.py --dry-run`
     - `python scripts/positioning_flow_signals.py --dry-run`
   - Then decide whether to upsert COT/FINRA and include flow dashboard in report.

6. Decide how to schedule ETF flow analytics refresh.
   - Likely files:
     - `scripts/macro_data_fetch.py`
     - `scripts/auto_macro_db.bat`
     - `scripts/etf_flow_analytics.py`
   - Suggested daily sequence:
     - refresh ETF issuer data;
     - run `scripts/etf_flow_analytics.py --upsert-local --start-date 2026-01-01 --write-report-output`;
     - generate/publish rule-based market report.

7. Expand market dashboard frontend only after API deployment state is confirmed.
   - Repo: `C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\market-dashboard`.
   - Existing frontend now supports OHLCV chart and grouped tape using current API.

8. Backtest ETF flow forward setup features before treating them as probabilities.
   - Likely file: `src/db_builder/etf_flow/backtest.py`.
   - Needed metrics:
     - rank IC;
     - hit rate;
     - top-minus-bottom quintile spread;
     - signal decay;
     - regime-conditioned behavior.

## Known Issues and Risks

- `artifacts/` is untracked and can be large. Keep generated CSV/JSON out of git.
- `.gitignore` ignores `*.md`; `HANDOFF.md` must be force-added if it should be committed.
- `economic_sync_to_neon.py` is intentionally disabled. Do not assume economic indicators are available in Neon.
- `public.macro_live` is designed as a replace-only latest snapshot table, not history.
- YTD calculation now relies on prior local `ytd_pct_chg`. Keep prior-year/YTD repair scripts available if future historical corrections are needed.
- Eastmoney coverage logic can still fail if the endpoint returns incomplete pages. yfinance fallback mitigates missing eligible tickers but cannot recover unavailable/delisted instruments.
- FINRA short-sale volume is not short interest. Treat it as short-sale trading pressure only.
- CFTC COT is weekly and delayed. Treat it as positioning context, not intraday signal.
- Nested git repos are separate:
  - `DB_builder` root
  - `DB_builder/neon-api`
  - `C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\market-dashboard`
- ETF flow analytics scores can be distorted by limited issuer coverage. The report now surfaces concentration/audit flags; do not hide those.
- Current ETF flow forward setup labels are descriptive heuristic buckets, not calibrated probabilities.

## Important Decisions and Constraints

- Local PostgreSQL remains the primary source database.
- Neon is a selected deployment/API subset, not a full mirror of local.
- Economic indicators are local-only to save Neon storage.
- Latest rule-based report is published as static markdown in `market-dashboard/data/latest-report.md`.
- Rule-based market update remains deterministic and does not use LLMs for scoring.
- ETF flow analytics is deterministic and PostgreSQL-backed. It uses issuer-derived shares outstanding and NAV, not secondary-market trading volume.
- ETF flow regime is a separate input. It does not overwrite the existing market regime score.
- Daily YTD percent change from Eastmoney is not trusted. It is replaced locally before upsert.
- Indicator generation uses staged CSV to make interruptions recoverable.
- Non-core/no-price instruments are excluded from core coverage calculations.

Rejected or deferred approaches:

- Do not upload all economic indicators to Neon. Storage concern.
- Do not treat all unresolved raw tickers as failures; many are warrants, units, no-price records, or inactive candidates.
- Do not rely on Eastmoney YTD percent values.
- Do not build SEC 13F/ICI/ETF issuer ingestion fully until CFTC/FINRA are stable.

## Development Instructions

Setup:

```powershell
cd C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder
conda activate PostgreSQL_db
```

Run tests:

```powershell
python -m pytest
```

Focused tests used recently:

```powershell
python -m pytest tests\test_eastmoney.py tests\test_recalculate_core_ytd.py
python -m pytest tests\test_rule_based_market_update_script.py tests\test_scoring_rules.py
python -m pytest neon-api\tests\test_api.py
```

Generate rule-based report:

```powershell
python scripts\rule_based_market_update.py --save --window-hours 24
```

Generate and publish dashboard report:

```powershell
python scripts\rule_based_market_update.py --save --window-hours 24 --publish-dashboard --push-dashboard
```

Run ETF flow analytics:

```powershell
python scripts\etf_flow_analytics.py --dry-run --start-date 2026-01-01
python scripts\etf_flow_analytics.py --upsert-local --start-date 2026-01-01 --write-report-output
python scripts\etf_flow_analytics.py --dry-run --start-date 2026-01-01 --json
```

Daily scheduled report workflow:

```powershell
scripts\auto_news_intelligence.bat
```

Equity daily workflow:

```powershell
scripts\auto_postgreSQL_db.bat
```

Macro workflow:

```powershell
scripts\auto_macro_db.bat
```

Environment variables:

- Local private files live outside the repository under the sibling `DB_builder_env` directory.
- DB_builder uses `DB_builder_env/.env`; nested services use mirrored subdirectories.
- `DB_BUILDER_ENV_DIR` may override the external location when needed.
- Do not read, print, archive, or commit secrets. See `docs/SECRET_STORAGE.md`.

External dependencies:

- Local PostgreSQL.
- Neon PostgreSQL.
- yfinance.
- pandas / SQLAlchemy / psycopg2.
- Ollama only for optional LLM workflows.

## Suggested Starting Point For Next Session

Start with:

```powershell
cd C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder
git status --short
python -m pytest tests\test_rule_based_market_update_script.py tests\test_scoring_rules.py
python -m pytest tests\test_etf_flow_analytics.py tests\test_etf_flows.py tests\test_positioning_flow_signals.py tests\test_scoring_rules.py
python scripts\rule_based_market_update.py --save --window-hours 24 --publish-dashboard --push-dashboard
```

Then inspect:

- `scripts/rule_based_market_update.py`
- `scripts/auto_news_intelligence.bat`
- `src/db_builder/report_renderer.py`
- `src/db_builder/etf_flow/aggregation.py`
- `src/db_builder/etf_flow/repository.py`
- `scripts/etf_flow_analytics.py`
- `scripts/indicator_staged_backfill.py`
- `scripts/pgSQL_daily_bulk_sync_to_neon.py`
- `src/db_builder/eastmoney.py`
- `src/db_builder/yfinance_equity_fallback.py`
- `src/db_builder/equity_security_status.py`

If the task is dashboard/API related, inspect:

- `C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\market-dashboard\app.js`
- `C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\market-dashboard\data\latest-report.md`
- `C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\neon-api\main.py`

## Reference Map

- `scripts/`: CLI entry points and scheduled `.bat` jobs.
- `src/db_builder/`: reusable Python modules.
- `tests/`: pytest coverage.
- `reports/`: generated markdown reports, ignored.
- `logs/`: scheduled run logs, ignored.
- `artifacts/`: generated CSV/JSON repair/staging outputs, ignored by policy.
- `knowledge/`: local markdown knowledge files for LLM workflows.
- `neon-api/`: separate FastAPI repo for Render/Neon API.
- `notebooks/`: older exploratory notebooks.
- `config/`: scoring/config files.
- `migrations/`: idempotent PostgreSQL schema migrations. Currently ignored by `*.sql`; use `git add -f` for committed migrations.
- `docs/`: implementation documentation. Currently ignored by `*.md`; use `git add -f` for committed docs.
- `src/db_builder/etf_flow/`: ETF flow analytics package.
- `C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\market-dashboard`: separate GitHub Pages repo.
