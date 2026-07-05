# DB_builder Codex Handoff

Generated: 2026-07-05  
Repository inspected: `C:\Users\User\OneDrive\Coding\DB_builder`

This handoff is for the next Codex session. It is based on direct repository inspection plus recent session context where noted. Anything not verified directly is marked as unverified.

## 1. Project Overview

`DB_builder` is a local-first market data and investment intelligence project.

It currently has three major parts:

1. Local data pipelines:
   - Fetch U.S. equities, ETFs, indices, macro assets, economic indicators, RSS/news, classifications, and derived investment signals.
   - Store data in local PostgreSQL.
   - Calculate technical indicators and market/news intelligence.

2. Neon synchronization:
   - Selected local data is synced to Neon.
   - Neon/API deployment is separate under `neon-api/`, which is ignored by this root repo.

3. Report generation:
   - Rule-based market update reports.
   - Daily/pulse house-view reports.
   - Optional local Ollama/Qwen/DeepSeek analyst reports.

Main technologies:

- Python
- PostgreSQL / SQLAlchemy / psycopg2
- pandas / numpy
- yfinance
- requests
- feedparser / BeautifulSoup
- pytest
- Windows batch + PowerShell for scheduled jobs
- Optional Ollama local models: Qwen and DeepSeek

Important entry points:

- Equity pipeline:
  - `scripts/pgSQL_equities_auto.py`
  - `scripts/pgSQL_equities_indicators.py`
  - `scripts/pgSQL_daily_bulk_sync_to_neon.py`

- Macro/economic pipeline:
  - `scripts/macro_data_fetch.py`
  - `scripts/economic_data_fetch.py`
  - `scripts/global_economic_data_fetch.py`
  - `scripts/economic_sync_to_neon.py`

- News pipeline:
  - `scripts/news_fetch.py`
  - `scripts/news_classify.py`
  - `scripts/news_signals.py`
  - `scripts/news_source_health.py`

- Investment intelligence:
  - `scripts/market_regime_v2.py`
  - `scripts/sector_regime.py`
  - `scripts/sector_rotation.py`
  - `scripts/secular_themes.py`
  - `scripts/rule_based_market_update.py`
  - `scripts/investment_report.py`
  - `scripts/daily_house_view_report.py`
  - `scripts/market_pulse_report.py`
  - `scripts/deepseek_multistage_report.py`

- Scheduled runners:
  - `scripts/auto_macro_db.bat`
  - `scripts/auto_news_intelligence.bat`
  - `scripts/auto_news_intelligence_dry_run.bat`
  - `scripts/auto_market_pulse_3h.bat`

Project rules:

- `AGENTS.md` exists and was read.
- Never run destructive SQL without asking.
- Never expose secrets or print `.env`.
- Prefer bulk upserts, symbol/date deduplication, minimal Neon reads, explicit logging, and small test runs before full runs.
- Before committing: run relevant tests, run affected scripts with small inputs, show git diff, and explain changes.

## 2. Current Objective

Recent objective in this session:

Build out a more deterministic, rule-based institutional market update/reporting layer, improve macro/economic data coverage, add readable economic metadata, and prepare a next-step plan for comprehensive positioning/flows/holdings analysis.

Intended behavior and acceptance criteria:

- Rule-based report should be deterministic and use PostgreSQL facts, not LLM inference.
- Daily scheduled news runner should generate the rule-based market update instead of the older house-view report.
- Rule-based report should include:
  - Macro snapshot
  - Economic data
  - Market regime score
  - Market strength
  - Evidence quality / confidence
  - Cross-asset confirmation
  - Consolidated sector/theme leadership
  - Breadth and participation
  - Market dispersion
  - Sector constituent dispersion
  - News analytics with readable headline formatting
  - Contradiction/audit flags
  - Data quality notes
- Economic indicator fetch should include:
  - FRED headline/core CPI, PPI, PCE
  - Derived MoM and YoY inflation rates
  - Release calendar gating so fetches can skip until after expected release time unless forced
  - Global economic data from free sources where available: World Bank, ECB, ABS
- ABS series should be decoded into readable names using local ABS metadata.
- S&P 500 sector/industry classification should be regularly updated and used for sector constituent dispersion.
- Future flow/positioning system should be built as staged ingestion jobs, not mixed into the existing equity pipeline.

## 3. Work Completed

### Rule-Based Market Update

Created untracked modules:

- `src/db_builder/rule_based_regime.py`
  - `compute_regime`
  - `compute_confidence`
  - `regime_label`
  - helper scoring functions for volatility, rates, commodities, dollar.

- `src/db_builder/market_strength.py`
  - `compute_market_strength`
  - `market_breadth`
  - technical score helpers for MA, returns, RSI, MACD, volume.

- `src/db_builder/sector_strength.py`
  - `rank_sectors`
  - `score_sector`

- `src/db_builder/theme_strength.py`
  - `rank_themes`
  - `score_theme`
  - `setup_label`

- `src/db_builder/news_scoring.py`
  - `score_news`
  - `score_headline`
  - `classify_news_relevance`

- `src/db_builder/contradiction_audit.py`
  - `audit_report_scores`

- `src/db_builder/report_renderer.py`
  - `score_all`
  - `render_rule_based_market_update`
  - `save_rule_based_report`
  - `scores_to_json`
  - Includes readable headline blocks, economic data section, market dispersion, sector constituent dispersion, and sector/theme leadership consolidation.

- `src/db_builder/rule_based_market_data.py`
  - `collect_rule_based_inputs`
  - Fetches technicals, macro snapshot, recent news, news signals, economic snapshot, and S&P 500 constituent technicals.

- `src/db_builder/rule_based_config.py`
  - Contains tickers, sectors, themes, economic series lists, and related mappings.

- `config/scoring_weights.yaml`
  - Weights for market regime, sector strength, theme strength, and outperformance setup.

Created untracked script:

- `scripts/rule_based_market_update.py`
  - CLI flags:
    - `--window-hours`
    - `--dry-run`
    - `--save`
    - `--json-output`

Created/updated tests:

- `tests/test_scoring_rules.py`
- `tests/test_market_dispersion.py`
- `tests/test_sector_theme_alignment.py`
- `tests/test_market_intelligence_report.py`

Important implementation details:

- News is intentionally limited in weight relative to price/technical/breadth signals.
- The report avoids subjective recommendations like "buy/sell/overweight" and instead uses labels such as improving, deteriorating, strong setup, weak confirmation, mixed, extended, and unconfirmed.
- The Macro Snapshot no longer shows `DXY` if unavailable; `DX-Y.NYB` remains available as a U.S. Dollar Index proxy in macro data.

### Market Dispersion and Sector/Theme Consolidation

Created untracked module:

- `src/db_builder/market_dispersion.py`
  - `compute_broad_market_dispersion`
  - `compute_sector_constituent_dispersion`

Created untracked module:

- `src/db_builder/sector_theme_alignment.py`
  - `align_sector_themes`
  - `signal_label`

Report behavior:

- Broad dispersion uses sector ETF 20D/60D return spreads and size/style spreads:
  - `RSP` vs `SPY`
  - `IWM` vs `SPY`
  - `IWF` vs `IWD`
  - `QQQ` vs `SPY`
- Sector constituent dispersion uses active S&P 500 constituents and latest `us_equities` / `us_equities_indicators`.
- Sector constituent fields:
  - Breadth 50D / 200D = percentage of available constituents closing above MA50 / MA200.
  - Positive 20D = percentage of available constituents with positive 20D return.
  - Std 20D = standard deviation of constituent 20D returns within the sector.
- Official sectors and themes are now consolidated under `## Sector and Theme Leadership`.
- Supporting/detracting columns use S&P 500 constituent leaders/laggards by 20D return when available, with ETF fallback.

Known detail:

- Current sector constituent dispersion can produce very large `Std 20D` values due to outliers. Winsorization or outlier filtering is not implemented yet.

### Economic Data and Release Calendar

Created untracked module:

- `src/db_builder/economic_data.py`
  - `EconomicSeries`
  - `series_registry`
  - `create_economic_indicators_table`
  - `default_release_calendar`
  - `replace_upcoming_release_calendar`
  - `should_fetch_series`
  - `fetch_fred_csv`
  - `upsert_economic_indicators`
  - `build_derived_inflation_rows`
  - `build_derived_inflation_rows_from_base_rows`
  - `latest_economic_summary`
  - `upcoming_release_summary`

Created untracked script:

- `scripts/economic_data_fetch.py`
  - Supports:
    - `--start-date`
    - `--series`
    - `--dry-run`
    - `--upsert-local`
    - `--summary`
    - `--release-calendar`
    - `--force-refresh`

Created/updated tests:

- `tests/test_economic_data.py`

Derived inflation metrics implemented:

- Headline CPI:
  - `DERIVED:CPI_HEADLINE_SA:MOM`
  - `DERIVED:CPI_HEADLINE_SA:YOY`
  - `DERIVED:CPI_HEADLINE_NSA:MOM`
  - `DERIVED:CPI_HEADLINE_NSA:YOY`

- Core CPI:
  - `DERIVED:CPI_CORE_SA:MOM`
  - `DERIVED:CPI_CORE_SA:YOY`
  - `DERIVED:CPI_CORE_NSA:MOM`
  - `DERIVED:CPI_CORE_NSA:YOY`

- PPI:
  - `DERIVED:PPI_HEADLINE:MOM`
  - `DERIVED:PPI_HEADLINE:YOY`
  - `DERIVED:PPI_CORE:MOM`
  - `DERIVED:PPI_CORE:YOY`

- PCE:
  - `DERIVED:PCE_HEADLINE:MOM`
  - `DERIVED:PCE_HEADLINE:YOY`
  - `DERIVED:PCE_CORE:MOM`
  - `DERIVED:PCE_CORE:YOY`

Formula:

- MoM `% = 100 * (current_index / previous_month_index - 1)`
- YoY `% = 100 * (current_index / index_12_months_ago - 1)`

Important implementation detail:

- `rule_based_market_data.fetch_economic_snapshot` was updated to deduplicate by `(series_id, date)` and use the latest `realtime_fetched_at` row, then compare latest two distinct dates. This was intended to fix false `0.0 vs prior` changes in the report.

### Global Economic Data

Created untracked module:

- `src/db_builder/global_economic_data.py`
  - `fetch_world_bank_rows`
  - `fetch_ecb_rows`
  - `fetch_abs_rows`
  - `fetch_global_economic_rows`
  - `rows_to_json_preview`

Created untracked script:

- `scripts/global_economic_data_fetch.py`
  - Supports:
    - `--start-date`
    - `--providers`
    - `--dry-run`
    - `--upsert-local`
    - `--preview`

Created/updated tests:

- `tests/test_global_economic_data.py`

Sources:

- World Bank
- ECB
- ABS

Unverified:

- Current production reliability of all free endpoints was not re-tested during this handoff step.

### ABS Metadata Decoding

Created untracked module:

- `src/db_builder/abs_metadata.py`
  - `create_abs_metadata_tables`
  - `fetch_abs_structure`
  - `parse_abs_structure`
  - `upsert_abs_metadata`
  - `fetch_and_store_abs_metadata`
  - `decode_abs_series_id`
  - `decoded_abs_name`
  - `build_abs_series_mappings`
  - `upsert_abs_series_mappings`

Created untracked script:

- `scripts/abs_metadata_fetch.py`

Created/updated tests:

- `tests/test_abs_metadata.py`

Session context, not re-run during handoff:

- ABS metadata had decoded about `22,295` ABS series and updated `economic_indicators.series_name`.
- Old ABS rows may still contain legacy series IDs that encoded `BASE_PERIOD` instead of `FREQ`; new fetch generation was fixed, but old duplicate rows were not deleted.

### S&P 500 Classification Mapping

Created untracked module:

- `src/db_builder/security_classification.py`
  - `normalize_ticker`
  - `create_security_classification_table`
  - `fetch_sp500_constituents`
  - `upsert_sp500_constituents`
  - `sp500_classification_summary`

Created untracked script:

- `scripts/security_classification_fetch.py`

Created/updated tests:

- `tests/test_security_classification.py`

Important implementation details:

- Fetches S&P 500 constituents from Wikipedia using `requests` with a User-Agent and `pandas.read_html`.
- Normalizes tickers such as `BRK.B` to `BRK-B`.
- Upserts current constituents and marks removed constituents inactive; no delete.
- Added as a scheduled step in `scripts/auto_macro_db.bat`.

Session context, not re-run during handoff:

- A previous dry run fetched 503 S&P 500 rows.
- A previous upsert reported 503 rows and `marked_inactive=0`.

### Macro Data Enhancements

Modified tracked file:

- `scripts/macro_data_fetch.py`

Key changes:

- Adds CLI flags:
  - `--symbols`
  - `--start-date`
  - `--local-only`
  - `--dry-run`
- Adds symbols:
  - `^MOVE`
  - `DX-Y.NYB`
  - `HYG`
  - `LQD`
  - `JNK`
  - `RSP`
  - `IWF`
  - `IWD`
  - `TLT`
  - `IEF`
  - `SHY`
- Allows local-only and dry-run operation.
- Allows start-date override for backfills.

Session context:

- User asked whether Neon should remove old macro rows; decision was "No need to remove anything."

### Economic Sync to Neon

Created untracked script:

- `scripts/economic_sync_to_neon.py`
  - `fetch_local_rows`
  - `bulk_upsert_neon`
  - `print_summary`

Created/updated tests:

- `tests/test_economic_sync_to_neon.py`

Current scheduled behavior:

- `scripts/auto_macro_db.bat` now includes:
  - `economic_sync_to_neon.py --start-date 2026-01-01`

Unverified:

- This handoff did not re-run Neon sync.
- Storage usage and Neon row count were not checked during handoff.

### News Source and Importance Enhancements

Modified tracked files:

- `src/db_builder/news_sources.py`
- `src/db_builder/news_importance.py`
- `src/db_builder/news_taxonomy.py`
- `src/db_builder/news_source_health.py`

News sources added:

- Investing.com Markets: `https://www.investing.com/rss/news_356.rss`, category `markets`, priority `85`
- Investing.com Economy: `https://www.investing.com/rss/news_357.rss`, category `economy`, priority `92`
- Investing.com Commodities: `https://www.investing.com/rss/news_1062.rss`, category `commodities`, priority `80`
- Investing.com Forex: `https://www.investing.com/rss/news_1063.rss`, category `forex`, priority `80`
- Investing.com Stock Market: `https://www.investing.com/rss/news_11.rss`, category `stock_market`, priority `90`
- Investing.com World News: `https://www.investing.com/rss/news_25.rss`, category `world_news`, priority `90`
- Investing.com Technology: `https://www.investing.com/rss/news_95.rss`, category `technology`, priority `85`
- Investing.com Economic Indicators: `https://www.investing.com/rss/news_14.rss`, category `economic_indicators`, priority `95`

Importance scoring added:

- `score_market_relevance_multiplier`
- `classification_priority` now multiplies:
  - `importance_score`
  - `event_type_priority`
  - `source_priority`
  - `market_relevance_multiplier`

Market relevance multipliers:

- Top macro / central bank event: `1.50`
- Policy / geopolitical shock: `1.40`
- Corporate / regulatory event: `1.25`
- Generic market recap: `0.50`

Taxonomy mappings expanded:

- Central banks: BOE, BOJ, ECB, Lagarde, Ueda, Bank of England
- Inflation: CPI, PPI, core inflation
- Rates: real rates, yield curve
- Labor: employment, NFP, payrolls, unemployment
- Trade/policy: tariffs, export controls, sanctions
- Oil: Brent, WTI, OPEC, oil supply

Source health table additions:

- `avg_response_time`
- `latest_status`

Tests updated:

- `tests/test_news_sources.py`
- `tests/test_news_importance.py`
- `tests/test_news_source_health.py`
- `tests/test_news_fetcher.py`
- `tests/test_news_taxonomy.py`

### Investment Report Refactor / House View

Modified tracked files:

- `scripts/investment_report.py`
- `src/db_builder/investment_report.py`
- `tests/test_investment_report.py`

Created untracked modules:

- `src/db_builder/market_intelligence_report.py`
- `src/db_builder/report_sections.py`

Created untracked scripts:

- `scripts/daily_house_view_report.py`
- `scripts/market_pulse_report.py`
- `scripts/auto_market_pulse_3h.bat`

Behavior:

- `scripts/investment_report.py` now accepts `--report-type` with:
  - `pulse`
  - `daily`
  - `house_view`
  - `legacy`
- Default report type is now `daily`.
- Saves with prefix:
  - `market_pulse_...` for pulse
  - `daily_house_view_...` for daily/house view
  - `investment_report_...` for legacy
- Optional Qwen overlay remains available but is not required.

Current scheduled news runner:

- `scripts/auto_news_intelligence.bat` now runs:
  - `rule_based_market_update.py --save --window-hours 24`
  instead of:
  - `investment_report.py --save --window-hours 24 --no-quality-summary`

### DeepSeek/Qwen Offline Reporting

Created untracked modules/scripts/tests:

- `src/db_builder/deepseek_cio_engine.py`
- `src/db_builder/deepseek_offline_agent.py`
- `src/db_builder/deepseek_snapshot_builder.py`
- `src/db_builder/knowledge_retriever.py`
- `src/db_builder/deepseek_report_validator.py`
- `src/db_builder/deepseek_section_generator.py`
- `src/db_builder/deepseek_multistage_agent.py`
- `src/db_builder/deepseek_stage_prompts.py`
- `src/db_builder/deepseek_stage_validator.py`
- `src/db_builder/deepseek_stage_outputs.py`
- `scripts/deepseek_house_view.py`
- `scripts/deepseek_offline_report.py`
- `scripts/deepseek_multistage_report.py`
- Matching tests under `tests/test_deepseek_*`

Knowledge files present:

- `knowledge/commodity_framework.md`
- `knowledge/confidence_scoring_framework.md`
- `knowledge/cross_asset_relationships.md`
- `knowledge/deepseek_agent_system_prompt.md`
- `knowledge/deepseek_cio_framework.md`
- `knowledge/deepseek_report_generation_playbook.md`
- `knowledge/equity_playbook.md`
- `knowledge/fixed_income_playbook.md`
- `knowledge/historical_regimes.md`
- `knowledge/institutional_output_templates.md`
- `knowledge/macro_regime_framework.md`
- `knowledge/market_state_schema.md`
- `knowledge/portfolio_construction_framework.md`
- `knowledge/quant_factor_models.md`
- `knowledge/risk_management_framework.md`

Current script label:

- `scripts/deepseek_multistage_report.py` prints "Qwen multistage report status" and uses `DEFAULT_MULTISTAGE_MODEL` from `deepseek_multistage_agent.py`.

Unverified:

- The current default model value was not re-opened during this handoff. Inspect `src/db_builder/deepseek_multistage_agent.py` before running.
- LLM output quality and full report generation were not re-tested during this handoff.

## 4. Current State

### Git State

Latest commits:

- `110955b Add critical PM house view layer`
- `dd91d66 Add local LLM analyst overlay`
- `168ce4f Add secular theme engine`
- `b422f16 Add market regime v2 and sector rotation engines`
- `501be49 Add sector intelligence to investment report`
- `92573fd Add sector intelligence layer`
- `1f36b9f Add event priority to news classification queue`
- `7df05a9 Add premium source classification backfill`
- `01c5a0b Improve signal article ranking by source quality`
- `a864900 Normalize news taxonomy variants`
- `28598dc Prioritize market-moving news classification queue`
- `bf2b35b Tighten news taxonomy mappings`

Tracked modified files:

- `scripts/auto_macro_db.bat`
- `scripts/auto_news_intelligence.bat`
- `scripts/investment_report.py`
- `scripts/macro_data_fetch.py`
- `src/db_builder/investment_report.py`
- `src/db_builder/market_regime_v2.py`
- `src/db_builder/news_importance.py`
- `src/db_builder/news_source_health.py`
- `src/db_builder/news_sources.py`
- `src/db_builder/news_taxonomy.py`
- `tests/test_investment_report.py`
- `tests/test_news_fetcher.py`
- `tests/test_news_importance.py`
- `tests/test_news_source_health.py`
- `tests/test_news_sources.py`

Important untracked directories/files:

- `config/`
- `knowledge/`
- Many new scripts under `scripts/`
- Many new modules under `src/db_builder/`
- Many new tests under `tests/`

`README.md` is not present in the repository root. `AGENTS.md` is present.

### What Works

Verified earlier in-session, but not all re-run during handoff:

- Focused tests for economic data and scoring rules previously passed.
- Focused tests for market dispersion/scoring rules previously passed.
- Focused tests for sector/theme alignment/scoring rules previously passed.
- Rule-based market update dry-run previously generated output successfully.

Verified by repository inspection during handoff:

- Entry scripts and modules exist for rule-based report, economic/global data fetch, ABS metadata, S&P 500 classification, and multistage Qwen/DeepSeek report.
- `AGENTS.md` safety constraints are intact.
- `.gitignore` excludes `.env`, logs, reports, caches, notebooks checkpoints, dumps, and `neon-api/`.

### Partially Working / Unverified

- Full `pytest` status is not known until the handoff tests below are run.
- Full scheduled path via `scripts/auto_macro_db.bat` is not re-tested in this handoff.
- Full scheduled path via `scripts/auto_news_intelligence.bat` is not re-tested in this handoff.
- Neon economic sync is not re-tested in this handoff.
- External endpoint reliability is unverified:
  - yfinance
  - FRED CSV
  - World Bank
  - ECB
  - ABS
  - Wikipedia S&P 500 constituent table
  - Investing.com RSS
- Qwen/DeepSeek report quality is unverified in this handoff.

## 5. Remaining Work

Prioritized checklist:

1. Verify current uncommitted code before new features.
   - Run:
     - `python -m pytest`
     - `python scripts/rule_based_market_update.py --dry-run --window-hours 24`
     - `python scripts/economic_data_fetch.py --summary`
     - `python scripts/security_classification_fetch.py --summary`
   - Likely files involved:
     - `src/db_builder/report_renderer.py`
     - `src/db_builder/rule_based_market_data.py`
     - `src/db_builder/economic_data.py`
     - `src/db_builder/security_classification.py`

2. Decide whether to commit the current large untracked/modified report/economic changes.
   - Before committing, inspect:
     - `git status --short`
     - `git diff --stat`
     - `git diff`
   - Likely command:
     - `git add` only the intended files; avoid sweeping unrelated generated files.

3. Fix sector constituent dispersion outliers.
   - Current `Std 20D` can be too large.
   - Add winsorization or outlier clipping.
   - Likely files:
     - `src/db_builder/market_dispersion.py`
     - `src/db_builder/report_renderer.py`
     - `tests/test_market_dispersion.py`

4. Verify economic report values with live database.
   - User reported `0.0 vs prior` changes; dedupe logic was added, but live output should be checked.
   - Likely files:
     - `src/db_builder/rule_based_market_data.py`
     - `src/db_builder/report_renderer.py`
     - `tests/test_scoring_rules.py`

5. Implement positioning and flow source probe as the next feature only after stabilization.
   - Suggested first files:
     - `src/db_builder/flow_sources.py`
     - `scripts/flow_source_check.py`
     - `tests/test_flow_sources.py`
   - Probe these sources:
     - CFTC COT
     - FINRA short-sale volume
     - SEC 13F datasets
     - ICI flows
     - ETF issuer holdings

6. Build Phase 1 flow ingestion.
   - Start with CFTC COT and FINRA short-sale volume only.
   - Likely files:
     - `src/db_builder/cot_positions.py`
     - `src/db_builder/finra_short_volume.py`
     - `src/db_builder/positioning_flow_signals.py`
     - `scripts/cot_fetch.py`
     - `scripts/finra_short_volume_fetch.py`
     - `scripts/positioning_flow_signals.py`
     - `tests/test_cot_positions.py`
     - `tests/test_finra_short_volume.py`
     - `tests/test_positioning_flow_signals.py`

7. Add `Positioning & Flow Dashboard` to rule-based report after data exists.
   - Likely files:
     - `src/db_builder/report_renderer.py`
     - `src/db_builder/rule_based_market_data.py`
     - `tests/test_scoring_rules.py`

## 6. Known Issues and Risks

Known issues:

- Large uncommitted working tree. There are many untracked modules/tests and modified tracked files.
- `README.md` is absent.
- `reports/`, `logs/`, `.env`, caches, and dumps are ignored; do not rely on git for generated report history.
- `src/db_builder/config.py` contains default Neon host/user constants but no secrets; passwords are environment-driven.
- `AGENTS.md` says never expose secrets and never hardcode passwords; keep this intact.
- `auto_macro_db.bat` currently schedules a Qwen multistage report step after macro/economic sync. This can make macro scheduled runs long and fragile.
- `deepseek_multistage_report.py` command says DeepSeek in filename but prints Qwen status. This naming can confuse future maintenance.
- LLM modules are large and complex compared with the deterministic report. Treat them as optional/manual unless explicitly needed.
- External data sources can fail or change formats. Source-specific error handling should be defensive.
- FINRA short-sale volume is not short interest. Future reports must phrase it as short-sale volume pressure only.
- SEC 13F is quarterly and delayed; it should not drive short-term signals.

Risks:

- Full `pytest` may uncover failures in uncommitted tests not yet re-run.
- Network-dependent scripts can fail under restricted network/sandbox or due upstream endpoint changes.
- Neon sync may increase storage usage if broad economic history is uploaded. User previously decided no deletion was needed, but future changes should still check scope before syncing.
- ABS duplicated old/new series IDs may still exist because old rows were not deleted.
- Sector/theme ranking can be duplicated conceptually; it was consolidated in the report, but underlying scoring modules remain separate by design.

## 7. Important Decisions and Constraints

Decisions made:

- Keep equity ingestion, indicator calculations, and Neon sync separate unless explicitly requested.
- Use local PostgreSQL as the main data source.
- Use deterministic rule-based scoring for the market update; do not use LLMs for scoring.
- Use Qwen/DeepSeek only as optional interpretation/reporting layers.
- Prefer official/structured sources for economic data:
  - FRED CSV for U.S. economics
  - World Bank for global annual macro
  - ECB for Euro-area data
  - ABS for Australia
- Use S&P 500 constituent sector mapping from Wikipedia for sector constituent dispersion.
- Use no destructive SQL; schema evolution uses `CREATE TABLE IF NOT EXISTS` and `ADD COLUMN IF NOT EXISTS`.
- Keep generated reports and logs ignored by git.

Approaches tried/rejected:

- Single-pass DeepSeek institutional report generation was unreliable due unsupported claims and validation failures.
- Section-by-section and multistage report generation improved structure but still needs careful validation and may be slower than useful for scheduled runs.
- The old investment house view became too long/duplicative; daily scheduled news runner was switched to deterministic rule-based market update.

## 8. Development Instructions

Environment:

- Windows
- Conda env: `PostgreSQL_db`
- Python executable used in prior commands:
  - `C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe`

Install dependencies:

```powershell
cd C:\Users\User\OneDrive\Coding\DB_builder
conda activate PostgreSQL_db
python -m pip install -r requirements-dev.txt
```

Runtime dependencies from `requirements.txt`:

- pandas
- numpy
- requests
- yfinance
- sqlalchemy
- psycopg2-binary
- python-dotenv
- pandas_market_calendars
- feedparser
- beautifulsoup4

Environment variables:

- `LOCAL_DB_PASSWORD` or alias `local_password`
- Optional local DB overrides:
  - `LOCAL_DATABASE_URL`
  - `LOCAL_DB_USER`
  - `LOCAL_DB_HOST`
  - `LOCAL_DB_PORT`
  - `LOCAL_DB_NAME`
- `NEON_DB_PASSWORD` or alias `neon_password`
- Optional Neon overrides:
  - `NEON_DATABASE_URL`
  - `NEON_DB_USER`
  - `NEON_DB_HOST`
  - `NEON_DB_NAME`
- Optional Ollama:
  - `OLLAMA_URL`
  - `OLLAMA_MODEL`
  - `OLLAMA_FAST_MODEL`
  - `OLLAMA_DEEP_MODEL`

Do not print or commit `.env`.

Run tests:

```powershell
cd C:\Users\User\OneDrive\Coding\DB_builder
python -m pytest
```

Focused tests used often:

```powershell
python -m pytest tests\test_economic_data.py tests\test_scoring_rules.py
python -m pytest tests\test_market_dispersion.py tests\test_scoring_rules.py
python -m pytest tests\test_sector_theme_alignment.py tests\test_scoring_rules.py
```

Generate deterministic market update:

```powershell
python scripts\rule_based_market_update.py --dry-run --window-hours 24
python scripts\rule_based_market_update.py --save --window-hours 24
```

Fetch macro data:

```powershell
python scripts\macro_data_fetch.py --dry-run --symbols ^GSPC,^IXIC,^RUT,^VIX,DX-Y.NYB
python scripts\macro_data_fetch.py --symbols HYG,LQD,JNK,RSP,IWF,IWD,TLT,IEF,SHY --start-date 2025-01-01
```

Fetch economic data:

```powershell
python scripts\economic_data_fetch.py --summary
python scripts\economic_data_fetch.py --release-calendar
python scripts\economic_data_fetch.py --upsert-local
python scripts\economic_data_fetch.py --force-refresh --upsert-local
```

Fetch global economic data:

```powershell
python scripts\global_economic_data_fetch.py --dry-run --preview
python scripts\global_economic_data_fetch.py --upsert-local --start-date 2026-04-01
```

Sync economic data to Neon:

```powershell
python scripts\economic_sync_to_neon.py --start-date 2026-01-01
```

Update S&P 500 classification:

```powershell
python scripts\security_classification_fetch.py --dry-run
python scripts\security_classification_fetch.py --upsert-local
python scripts\security_classification_fetch.py --summary
```

ABS metadata:

```powershell
python scripts\abs_metadata_fetch.py --map-all --update-series-names --preview 20
```

News:

```powershell
python scripts\news_fetch.py --dry-run --limit 100
python scripts\news_classify.py --dry-run --limit 20 --show-queue
python scripts\news_signals.py --dry-run --window-hours 24
```

Qwen/DeepSeek multistage report:

```powershell
python scripts\deepseek_multistage_report.py --dry-run --window-hours 24 --include-snapshot
python scripts\deepseek_multistage_report.py --dry-run --window-hours 24 --include-prompts
python scripts\deepseek_multistage_report.py --save --window-hours 24 --timeout 0 --stage-timeout 0 --allow-warnings
```

Scheduled jobs:

```powershell
scripts\auto_macro_db.bat
scripts\auto_news_intelligence.bat
```

## 9. Suggested Starting Point for Next Session

First task:

Verify the current uncommitted state before adding the positioning/flow system.

Inspect first:

- `git status --short`
- `git diff --stat`
- `src/db_builder/report_renderer.py`
- `src/db_builder/rule_based_market_data.py`
- `src/db_builder/market_dispersion.py`
- `src/db_builder/economic_data.py`
- `src/db_builder/security_classification.py`
- `scripts/auto_macro_db.bat`
- `scripts/auto_news_intelligence.bat`
- `scripts/rule_based_market_update.py`

Run first:

```powershell
cd C:\Users\User\OneDrive\Coding\DB_builder
python -m pytest tests\test_economic_data.py tests\test_market_dispersion.py tests\test_sector_theme_alignment.py tests\test_scoring_rules.py
python scripts\rule_based_market_update.py --dry-run --window-hours 24
```

Then decide:

- If tests and report dry-run pass, commit the current report/economic changes in a coherent checkpoint.
- If tests fail, fix only the failures before starting the flow/positioning feature.

## 10. Relevant Reference Map

Root files:

- `AGENTS.md`: project-specific safety and workflow rules.
- `.gitignore`: excludes secrets, reports, logs, caches, notebooks checkpoints, dumps, and `neon-api/`.
- `requirements.txt`: runtime dependencies.
- `requirements-dev.txt`: development/test dependencies.

Configuration:

- `src/db_builder/config.py`: DB URL/engine helpers using environment variables.
- `config/scoring_weights.yaml`: scoring weights for rule-based report components.

Equity/macro core:

- `src/db_builder/eastmoney.py`: Eastmoney data access.
- `src/db_builder/indicators.py`: technical indicator calculation.
- `src/db_builder/neon_sync.py`: Neon sync helpers.
- `src/db_builder/trading_calendar.py`: NYSE calendar/session helpers.
- `scripts/pgSQL_equities_auto.py`: raw equity fetch/upsert.
- `scripts/pgSQL_equities_indicators.py`: indicator generation/upsert.
- `scripts/pgSQL_daily_bulk_sync_to_neon.py`: Neon equity sync.
- `scripts/macro_data_fetch.py`: yfinance macro/index/ETF data fetch.

Economic data:

- `src/db_builder/economic_data.py`: U.S. economic series, release calendar, derived inflation.
- `src/db_builder/global_economic_data.py`: World Bank, ECB, ABS fetch helpers.
- `src/db_builder/abs_metadata.py`: ABS metadata and readable series mapping.
- `scripts/economic_data_fetch.py`: U.S. economic data CLI.
- `scripts/global_economic_data_fetch.py`: global economic data CLI.
- `scripts/economic_sync_to_neon.py`: economic Neon sync.
- `scripts/abs_metadata_fetch.py`: ABS metadata CLI.

Security classification:

- `src/db_builder/security_classification.py`: S&P 500 constituent sector/industry mapping.
- `scripts/security_classification_fetch.py`: update classification table.

News:

- `src/db_builder/news_sources.py`: RSS source registry.
- `src/db_builder/news_fetcher.py`: RSS fetching/parsing.
- `src/db_builder/news_storage.py`: news table setup/upserts.
- `src/db_builder/news_importance.py`: deterministic article priority/event/market relevance scoring.
- `src/db_builder/news_classifier.py`: Ollama classifier.
- `src/db_builder/news_signal_aggregation.py`: classified news aggregation.
- `src/db_builder/news_taxonomy.py`: canonical theme mappings.
- `src/db_builder/news_source_health.py`: source reliability/latency tracking.

Rule-based market update:

- `src/db_builder/rule_based_config.py`: tickers, sectors, themes, economic series.
- `src/db_builder/rule_based_market_data.py`: PostgreSQL query layer.
- `src/db_builder/rule_based_regime.py`: regime/confidence scoring.
- `src/db_builder/market_strength.py`: market strength scoring.
- `src/db_builder/sector_strength.py`: sector scoring.
- `src/db_builder/theme_strength.py`: theme scoring.
- `src/db_builder/news_scoring.py`: headline analytics.
- `src/db_builder/market_dispersion.py`: broad and sector constituent dispersion.
- `src/db_builder/sector_theme_alignment.py`: sector/theme consolidation.
- `src/db_builder/contradiction_audit.py`: audit flags.
- `src/db_builder/report_renderer.py`: markdown rendering and JSON score export.
- `scripts/rule_based_market_update.py`: CLI.

House-view/reporting:

- `src/db_builder/investment_report.py`: legacy/daily/pulse report orchestration.
- `src/db_builder/market_intelligence_report.py`: daily and pulse report formats.
- `src/db_builder/report_sections.py`: reusable report section helpers.
- `src/db_builder/critical_house_view.py`: deterministic PM view layer.
- `scripts/investment_report.py`: report CLI.
- `scripts/daily_house_view_report.py`: daily report wrapper.
- `scripts/market_pulse_report.py`: pulse report wrapper.

LLM/offline reports:

- `src/db_builder/llm_analyst_overlay.py`: optional Qwen overlay.
- `src/db_builder/deepseek_cio_engine.py`: DeepSeek CIO report engine.
- `src/db_builder/deepseek_offline_agent.py`: offline agent orchestration.
- `src/db_builder/deepseek_snapshot_builder.py`: evidence snapshot builder.
- `src/db_builder/knowledge_retriever.py`: local markdown knowledge retrieval.
- `src/db_builder/deepseek_report_validator.py`: report validation.
- `src/db_builder/deepseek_section_generator.py`: section-by-section generation.
- `src/db_builder/deepseek_multistage_agent.py`: multistage Qwen/DeepSeek report pipeline.
- `src/db_builder/deepseek_stage_prompts.py`: stage prompt construction.
- `src/db_builder/deepseek_stage_validator.py`: stage validation.
- `src/db_builder/deepseek_stage_outputs.py`: deterministic stage fallbacks.
- `scripts/deepseek_house_view.py`: DeepSeek CIO workflow.
- `scripts/deepseek_offline_report.py`: offline evidence report workflow.
- `scripts/deepseek_multistage_report.py`: multistage Qwen report workflow.

Scheduled runners:

- `scripts/auto_macro_db.bat`: health check, classification update, macro fetch, economic fetch, global economic fetch, economic Neon sync, and Qwen multistage report.
- `scripts/auto_news_intelligence.bat`: news intelligence pipeline ending in rule-based market update.
- `scripts/auto_news_intelligence_dry_run.bat`: dry-run news intelligence workflow.
- `scripts/auto_market_pulse_3h.bat`: optional 3-hour pulse workflow.

## Verification Results For This Handoff

Focused tests run:

```powershell
C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe -m pytest tests\test_economic_data.py tests\test_market_dispersion.py tests\test_sector_theme_alignment.py tests\test_scoring_rules.py
```

Result:

- `24 passed`
- Warning: pytest cache could not write to `C:\Users\User\OneDrive\Coding\DB_builder\.pytest_cache\...` due permission denied. Tests still passed.

Rule-based dry run:

```powershell
C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe scripts\rule_based_market_update.py --dry-run --window-hours 24
```

Result:

- Command succeeded.
- Report generated in stdout.
- Key observed output:
  - Regime score: `67.46 / 100` (`Moderate Risk-On`)
  - Market strength: `75.35 / 100` (`strong`)
  - Evidence quality: `90.0 / 100`
  - Technical rows loaded: `60`
  - S&P 500 constituent technical rows loaded: `503`
  - Macro rows loaded: `29`
  - Economic rows loaded: `113`
  - News rows loaded: `80`

Dry-run issues still visible:

- `Sector Constituent Dispersion` still has extremely high dispersion for some sectors, e.g. Materials 20D dispersion over `200`. This likely needs outlier handling/winsorization.
- ETF-only sectors/themes can show the same ticker in supporting and detracting columns, e.g. `CIBR` for Cybersecurity. This is expected from the current fallback but is not ideal for readability.
- Economic inflation section shows both SA and NSA derived YoY CPI rows with identical display names such as "Headline CPI year-over-year inflation rate"; labels should distinguish SA vs NSA to avoid confusion.
- Macro snapshot includes `DX-Y.NYB` as "US Dollar Index"; the user previously asked to remove `DXY`, and `DXY` itself is not shown. Confirm whether the proxy row should remain.
- News analytics formatting is now bullet-style and readable, but some headline relevance remains noisy; future scoring can tighten source/category/entity filters.

