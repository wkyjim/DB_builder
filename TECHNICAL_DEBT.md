# Technical Debt Register

## TD-001
**Priority:** P2
**Status:** Open
**Component:** `.gitignore`
**Problem:** `*.md` and `*.sql` patterns in `.gitignore` cause all documentation and migration files to be gitignored. This forces use of `git add -f` for every documentation and migration change, making it easy to forget and causing confusion for new contributors.
**Evidence:** `.gitignore` contains `*.md` and `*.sql` patterns. `HANDOFF.md`, `docs/`, and `migrations/` are all untracked despite being intended for version control.
**Impact:** Documentation changes are invisible in git history. Migration files are not tracked, making schema evolution hard to audit. New contributors assume docs are committed when they are not.
**Recommended approach:** Remove `*.md` and `*.sql` from `.gitignore`. Add targeted ignores for specific generated files (e.g., `reports/*.md`, `logs/*.log`). Use `git add -f` one final time to track existing docs/migrations.
**Dependencies:** None
**Validation required:** `git status` shows docs and migrations as tracked files.

---

## TD-002
**Priority:** P1
**Status:** Open
**Component:** `neon-api/main.py`
**Problem:** Single 1652-line file containing API routes, Telegram webhook handlers, markdown parsing utilities, report builders, and Telegram formatting helpers. Mixes multiple concerns and responsibilities.
**Evidence:** File contains `normalize_row()`, `dataframe_to_records()`, `build_report_dashboard()`, `build_report_sectors()`, `build_market_tape_message()`, `build_equity_message()`, `send_telegram_text()`, `split_telegram_message()`, and all FastAPI route handlers.
**Impact:** Difficult to navigate and modify. Changes to report formatting may inadvertently affect API behavior. High cognitive load for new developers. Test setup requires mocking many unrelated functions.
**Recommended approach:** Split into logical modules: `routers/` (API routes), `services/` (data access and business logic), `telegram/` (Telegram-specific logic), `formatters/` (response formatting), and `main.py` (app setup only).
**Dependencies:** None
**Validation required:** All existing tests pass. Manual testing of API endpoints and Telegram webhooks.

---

## TD-003
**Priority:** P1
**Status:** Open
**Component:** `market-intelligence-telegram-bot/main.py`
**Problem:** Single 1140-line file containing all Telegram bot logic, including command parsing, report parsing, message formatting, and external API calls.
**Evidence:** File contains `command_reply()`, `build_morning_report()`, `build_eod_report()`, `build_executive_message()`, `build_market_message()`, `build_positioning_message()`, `build_sector_message()`, `build_etf_flow_message()`, `build_news_message()`, `build_opportunities_message()`, `build_risks_message()`, `build_changes_message()`, `build_full_report_messages()`, `parse_markdown_sections()`, `extract_section()`, `markdown_table_rows()`, and all FastAPI route handlers.
**Impact:** Same maintainability issues as neon-api/main.py. Changes to report parsing affect bot behavior. Difficult to test individual components.
**Recommended approach:** Split into `commands/` (command handlers), `formatters/` (message formatting), `services/` (report fetching and parsing), and `main.py` (app setup only).
**Dependencies:** None
**Validation required:** All existing tests pass. Manual testing of Telegram commands.

---

## TD-004
**Priority:** P1
**Status:** Open
**Component:** `neon-api/main.py`, `market-intelligence-telegram-bot/main.py`, `scripts/rule_based_market_update.py`
**Problem:** Markdown parsing utilities (`extract_markdown_section()`, `markdown_table_rows()`, `normalize_heading()`) are duplicated across three files with minor variations. Bug fixes must be applied in multiple places.
**Evidence:** `extract_markdown_section()` exists in `neon-api/main.py` (lines 544-570) and `market-intelligence-telegram-bot/main.py` (lines 293-314). `markdown_table_rows()` exists in `neon-api/main.py` (lines 573-583) and `market-intelligence-telegram-bot/main.py` (lines 317-327). `normalize_heading()` exists in `neon-api/main.py` (lines 540-541) and `market-intelligence-telegram-bot/main.py` (lines 289-290).
**Impact:** Inconsistent behavior between API and Telegram report consumers. Fixes in one location may not propagate. Increased testing burden.
**Recommended approach:** Create a shared markdown parsing module. Since both apps are separate repos, the shared code should be packaged as a library or copied with clear documentation about the canonical source.
**Dependencies:** Decision on shared library strategy
**Validation required:** All existing tests pass. Report output is identical before and after consolidation.

---

## TD-005
**Priority:** P1
**Status:** Open
**Component:** `neon-api/main.py`, `market-intelligence-telegram-bot/main.py`
**Problem:** Report building logic (`build_report_dashboard()`, `build_report_sectors()`, `build_market_tape_message()`, `build_equity_message()`, `build_macro_message()`) is duplicated between the API server and Telegram bot.
**Evidence:** `build_report_dashboard()` exists in `neon-api/main.py` (lines 595-600) and `market-intelligence-telegram-bot/main.py` (lines 815-820). `build_report_sectors()` exists in `neon-api/main.py` (lines 603-618) and `market-intelligence-telegram-bot/main.py` (lines 879-883). `build_market_tape_message()` exists in `neon-api/main.py` (lines 621-636) and `market-intelligence-telegram-bot/main.py` (lines 832-843). `build_equity_message()` exists in `neon-api/main.py` (lines 651-670) and `market-intelligence-telegram-bot/main.py` (lines 847-866). `build_macro_message()` exists in `neon-api/main.py` (lines 639-648).
**Impact:** Same as TD-004. Inconsistent report presentation between API consumers and Telegram users.
**Recommended approach:** Create a shared report builder module. Same strategy decision needed as TD-004.
**Dependencies:** TD-004
**Validation required:** All existing tests pass. Report output is identical before and after consolidation.

---

## TD-006
**Priority:** P1
**Status:** Open
**Component:** `src/db_builder/rule_based_regime.py`, `src/db_builder/report_renderer.py`, `src/db_builder/market_strength.py`, `src/db_builder/sector_strength.py`, `src/db_builder/rule_based_market_data.py`
**Problem:** Core scoring and reporting modules have no unit test coverage. These modules drive all market regime scores, sector rankings, and report content.
**Evidence:** No `test_rule_based_regime.py`, `test_report_renderer.py`, `test_market_strength.py`, `test_sector_strength.py`, or `test_rule_based_market_data.py` exists in `tests/`.
**Impact:** Scoring bugs go undetected. Regressions likely when modifying scoring logic. No safety net for refactoring.
**Recommended approach:** Add comprehensive unit tests for each scoring module. Test edge cases: empty data, single-row data, extreme values, missing columns.
**Dependencies:** None
**Validation required:** New tests pass. Existing tests still pass.

---

## TD-007
**Priority:** P2
**Status:** Open
**Component:** `scripts/auto_postgreSQL_db.bat`
**Problem:** No mutex locking to prevent concurrent runs. If a previous run is still executing when the next scheduled trigger fires, two instances may collide on shared resources.
**Evidence:** `auto_postgreSQL_db.bat` has no mutex logic. `auto_macro_db.bat` has `$Mutex = [System.Threading.Mutex]::new($false, 'Local\\DBBuilderAutoMacroDb')` but the equity pipeline does not.
**Impact:** Concurrent runs can cause duplicate inserts, indicator calculation errors, or Neon sync corruption.
**Recommended approach:** Add the same mutex pattern used in `auto_macro_db.bat` to `auto_postgreSQL_db.bat`. Use a distinct mutex name like `Local\\DBBuilderAutoPostgreSqlDb`.
**Dependencies:** None
**Validation required:** Two concurrent runs should result in one SKIP and one SUCCESS.

---

## TD-008
**Priority:** P2
**Status:** Open
**Component:** `scripts/`
**Problem:** 70+ scripts in a flat directory with inconsistent naming conventions. Mix of `pgSQL_`, `_fetch`, `_analytics`, `_report` prefixes.
**Evidence:** Directory listing shows `pgSQL_equities_auto.py`, `macro_data_fetch.py`, `news_fetch.py`, `etf_flow_analytics.py`, `rule_based_market_update.py`, `auto_postgreSQL_db.bat`, etc.
**Impact:** Hard to find the right script. Naming inconsistencies make it unclear what a script does without reading it. New scripts may follow any convention.
**Recommended approach:** Group scripts into subdirectories: `scripts/equities/`, `scripts/macro/`, `scripts/news/`, `scripts/reports/`, `scripts/maintenance/`. Standardize naming to `{action}_{target}.py` pattern.
**Dependencies:** Update all `.bat` file references and Windows scheduled task paths.
**Validation required:** All scheduled workflows still execute correctly.

---

## TD-009
**Priority:** P2
**Status:** Open
**Component:** `scripts/`, `src/db_builder/`
**Problem:** `scripts/telegram_bot.py` is a trivial wrapper that imports from `src/db_builder/telegram_bot.py`. Creates confusion about which file is the canonical entry point.
**Evidence:** `scripts/telegram_bot.py` contains only: `import _bootstrap`, `from db_builder.telegram_bot import main`, `raise SystemExit(main())`.
**Impact:** Unclear which file to modify when changing Telegram bot behavior. Developers may edit the wrong file.
**Recommended approach:** Remove the wrapper script. Update any scheduled tasks or documentation that reference it to use the module directly.
**Dependencies:** Verify no scheduled tasks or documentation reference `scripts/telegram_bot.py`.
**Validation required:** Telegram bot functionality still works after removal.

---

## TD-010
**Priority:** P2
**Status:** Open
**Component:** `scripts/economic_sync_to_neon.py`
**Problem:** File is intentionally disabled but remains in the repository. Comment says "intentionally disabled" but the file is still present and may be accidentally re-enabled.
**Evidence:** `auto_macro_db.bat` does not call `economic_sync_to_neon.py`. The script exists but is dead code.
**Impact:** Confusion about whether economic data is synced to Neon. May be accidentally re-enabled by a well-meaning developer.
**Recommended approach:** Move to `scripts/deprecated/` or remove entirely. Document the decision in `HANDOFF.md` or `ARCHITECTURE.md`.
**Dependencies:** None
**Validation required:** No references to `economic_sync_to_negon.py` remain in scheduled workflows.

---

## TD-011
**Priority:** P2
**Status:** Open
**Component:** `src/db_builder/market_regime.py`, `src/db_builder/market_regime_v2.py`
**Problem:** Two versions of market regime scoring module exist. Unclear which is canonical and when to use each.
**Evidence:** Both files exist in `src/db_builder/`. `market_regime_v2.py` is used by `scripts/auto_news_intelligence.bat`. `market_regime.py` may be used elsewhere.
**Impact:** Inconsistent regime scoring depending on which version is invoked. Confusion about which version to modify.
**Recommended approach:** Determine which version is canonical. Merge or clearly deprecate the other. Rename to indicate version status (e.g., `market_regime_legacy.py`).
**Dependencies:** Audit all references to both files.
**Validation required:** All tests pass. Report output is identical before and after consolidation.

---

## TD-012
**Priority:** P2
**Status:** Open
**Component:** `src/db_builder/etf_flow/breadth.py`
**Problem:** Module is deprecated but still exists in the etf_flow package. Comment says "Deprecated: ETF breadth is retained for compatibility only and is not used in active scoring/reporting."
**Evidence:** `public.etf_flow_segment_daily.flow_breadth_20d` column has comment: "Deprecated: ETF breadth is retained for compatibility only and is not used in active scoring/reporting."
**Impact:** Dead code may confuse new developers. May still be imported somewhere.
**Recommended approach:** Remove after verifying no references exist. Update migration to drop the deprecated column.
**Dependencies:** Audit all references to `breadth.py` and `flow_breadth_20d`.
**Validation required:** All tests pass. No import errors.

---

## TD-013
**Priority:** P2
**Status:** Open
**Component:** `src/db_builder/indicators.py`
**Problem:** Indicator calculation loads 320 lookback rows per ticker even when only 1 new row is needed. Inefficient for daily updates.
**Evidence:** `LOOKBACK_ROWS = 320` is used to fetch historical data for each ticker. For daily updates, only the latest row needs to be calculated, but the full lookback is loaded to ensure correct rolling calculations.
**Impact:** Slower indicator generation than necessary. Database fetches more data than needed.
**Recommended approach:** For daily updates, only fetch rows needed for the rolling window plus 1 new row. For backfills, keep the full lookback approach.
**Dependencies:** None
**Validation required:** Indicator values are identical before and after optimization.

---

## TD-014
**Priority:** P2
**Status:** Open
**Component:** `scripts/etf_flow_analytics.py`, `src/db_builder/etf_flow/`
**Problem:** ETF flow analytics recalculates from scratch when `--start-date` is provided. No incremental mode for daily updates.
**Evidence:** `scripts/etf_flow_analytics.py` calls `run_etf_flow_analytics()` which recalculates all dates from `start_date` to `as_of_date`.
**Impact:** Slow daily refresh. Unnecessary recalculation of historical data that hasn't changed.
**Recommended approach:** Add `--incremental` flag that only calculates for the latest date. Historical data would only be recalculated when explicitly requested.
**Dependencies:** None
**Validation required:** Incremental results match full recalculation results.

---

## TD-015
**Priority:** P2
**Status:** Open
**Component:** `src/db_builder/config.py`
**Problem:** Hardcoded Neon host in config file. If Neon project is migrated or pooler endpoint changes, the code must be updated.
**Evidence:** `NEON_HOST = "ep-aged-moon-ao3o4z0j-pooler.c-2.ap-southeast-1.aws.neon.tech"` is hardcoded.
**Impact:** Infrastructure changes require code changes. Risk of typo in hardcoded value.
**Recommended approach:** Move host to environment variable with the hardcoded value as default. Document the fallback behavior.
**Dependencies:** None
**Validation required:** Both default and override behaviors work correctly.

---

## TD-016
**Priority:** P2
**Status:** Open
**Component:** `neon-api/main.py`
**Problem:** No rate limiting on API endpoints. Public API can be abused without throttling.
**Evidence:** No rate limiting middleware or decorators in `neon-api/main.py`.
**Impact:** API abuse could degrade performance for legitimate users. No protection against accidental loops in dashboard code.
**Recommended approach:** Add rate limiting middleware (e.g., `slowapi` or custom middleware). Configure reasonable limits per endpoint.
**Dependencies:** Add rate limiting dependency.
**Validation required:** Rate-limited requests return 429 status. Normal requests are unaffected.

---

## TD-017
**Priority:** P2
**Status:** Open
**Component:** `neon-api/main.py`, `market-intelligence-telegram-bot/main.py`
**Problem:** `/debug/config` endpoint reveals configuration state including which secrets are configured. Exposed without authentication.
**Evidence:** `market-intelligence-telegram-bot/main.py` lines 1041-1053 expose `tg_token_configured`, `tg_chat_id_configured`, etc.
**Impact:** Information disclosure. Attackers can determine which services are configured and target unconfigured ones.
**Recommended approach:** Remove the debug endpoint or restrict to internal IPs. Move to health check endpoint with less detail.
**Dependencies:** None
**Validation required:** Endpoint returns 404 or 403 from external IPs.

---

## TD-018
**Priority:** P2
**Status:** Open
**Component:** `scripts/*.bat`
**Problem:** Hardcoded conda paths in `.bat` files. Not portable to other machines or conda installations.
**Evidence:** `call "C:\Users\User\anaconda3\Scripts\activate.bat"` and `$Python = 'C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe'` are hardcoded.
**Impact:** Scripts fail on other machines. Cannot easily switch conda environments.
**Dependencies:** Decide on path resolution strategy.
**Validation required:** Workflows execute correctly on the target machine.
**Recommended approach:** Use environment variables or detect conda installation path dynamically.

---

## TD-019
**Priority:** P3
**Status:** Open
**Component:** `notebooks/`
**Problem:** Untitled notebooks (`Untitled.ipynb`, `Untitled1.ipynb`) with unclear purpose clutter the repository.
**Evidence:** Files exist in `notebooks/` directory.
**Impact:** Confusion about whether these notebooks contain important work.
**Recommended approach:** Rename with descriptive names or remove if no longer needed.
**Dependencies:** None
**Validation required:** No broken references to renamed notebooks.

---

## TD-020
**Priority:** P3
**Status:** Open
**Component:** `config/finra_short_analytics.json`
**Problem:** JSON config file alongside YAML config files. Inconsistent format.
**Evidence:** `config/etf_flow_analytics.yaml` and `config/scoring_weights.yaml` are YAML. `config/finra_short_analytics.json` is JSON.
**Impact:** Minor confusion about which format to use for new configs.
**Recommended approach:** Convert to YAML for consistency.
**Dependencies:** Update all code that reads the JSON file.
**Validation required:** Short analytics still works correctly.

---

## TD-021
**Priority:** P3
**Status:** Open
**Component:** Root directory
**Problem:** `postgresql-us-equities-api.md` exists in project root rather than `docs/` directory.
**Evidence:** File is at `/workspace/DB_builder/postgresql-us-equities-api.md`.
**Impact:** Cluttered root directory. Documentation is not grouped with other docs.
**Recommended approach:** Move to `docs/` directory.
**Dependencies:** Update any references to the file.
**Validation required:** No broken links or references.

---

## TD-022
**Priority:** P3
**Status:** Open
**Component:** `src/db_builder/news_*.py` (12 modules)
**Problem:** News-related modules are flat in `src/db_builder/` without a subpackage. Hard to see the news pipeline as a cohesive unit.
**Evidence:** `news_classifier.py`, `news_dedup.py`, `news_fetcher.py`, `news_importance.py`, `news_keyword_discovery.py`, `news_keywords.py`, `news_scoring.py`, `news_signal_aggregation.py`, `news_source_health.py`, `news_sources.py`, `news_storage.py`, `news_taxonomy.py` all exist in `src/db_builder/`.
**Impact:** Hard to find news-related code. No clear entry point for the news pipeline.
**Recommended approach:** Consider grouping into `src/db_builder/news/` subpackage if news pipeline grows.
**Dependencies:** Update all imports and references.
**Validation required:** All tests pass. News pipeline still works.

---

## TD-023
**Priority:** P3
**Status:** Open
**Component:** `src/db_builder/deepseek_*.py` (10 modules)
**Problem:** DeepSeek-related modules are flat in `src/db_builder/` without a subpackage. Mixes LLM workflow code with core pipeline code.
**Evidence:** `deepseek_cio_engine.py`, `deepseek_multistage_agent.py`, `deepseek_offline_agent.py`, `deepseek_report_validator.py`, `deepseek_section_generator.py`, `deepseek_snapshot_builder.py`, `deepseek_stage_outputs.py`, `deepseek_stage_prompts.py`, `deepseek_stage_validator.py` all exist in `src/db_builder/`.
**Impact:** Clutters core module directory. LLM workflow code is experimental/production boundary is unclear.
**Recommended approach:** Consider grouping into `src/db_builder/llm/` subpackage to separate experimental LLM workflows from production pipeline code.
**Dependencies:** Update all imports and references.
**Validation required:** All tests pass. LLM workflows still work.
