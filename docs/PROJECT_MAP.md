# Project Map

Annotated directory tree of the DB_builder repository.

```
DB_builder/
├── .env.example                               # Template for required environment variables
├── .gitignore                                 # ⚠ Contains *.md and *.sql patterns — forces git add -f for docs/migrations
├── AGENTS.md                                  # Agent safety rules and quick start
├── ARCHITECTURE.md                            # Full architecture documentation
├── HANDOFF.md                                 # Prior session context and decisions
├── TECHNICAL_DEBT.md                          # Prioritized improvement backlog
├── requirements.txt                           # Python dependencies (pandas, numpy, yfinance, etc.)
├── requirements-dev.txt                       # Dev dependencies (pytest)
│
├── config/                                    # File-based configuration
│   ├── etf_flow_analytics.yaml                # ETF flow windows, weights, thresholds
│   ├── finra_short_analytics.json             # ⚠ JSON format, inconsistent with YAML configs
│   └── scoring_weights.yaml                   # Regime/sector/theme scoring weights
│
├── deploy/oracle/                             # Oracle Cloud deployment manifests
│   ├── bootstrap-ubuntu.sh                    # VM bootstrap (Docker, UFW, firewall)
│   ├── Caddyfile                              # Caddy reverse proxy config
│   ├── compose.yaml                           # Docker Compose: neon-api + telegram-bot + caddy
│   ├── Deploy-OracleStack.ps1                 # PowerShell deploy orchestrator
│   ├── deploy.sh                              # Deployment script
│   ├── Initialize-OracleVm.ps1                # VM initialization script
│   ├── neon-api.env.example                   # Neon API env template
│   ├── oracle.env.example                     # Oracle stack env template
│   ├── README.md                              # Deployment documentation
│   ├── status.sh                              # Stack health check script
│   └── telegram-bot.env.example               # Telegram bot env template
│
├── docs/                                      # ⚠ Currently gitignored (*.md pattern)
│   ├── DATA_FLOW.md                           # Data sources, pipelines, and table relationships
│   ├── DEPLOYMENT.md                          # Infrastructure and deployment procedures
│   └── PROJECT_MAP.md                         # This file
│
├── hermes-agent/                              # Hermes PM agent configuration
│   ├── DATABASE_CHECKS.md                    # Scheduled database check procedures
│   ├── DATABASE_POLICY.md                     # Database access policy
│   ├── DATABASE_READONLY_ROLE.md             # Read-only role configuration
│   ├── ENVIRONMENT.md                         # Environment configuration
│   ├── INSTALL.md                             # Installation instructions
│   ├── MAINTENANCE_POLICY.md                  # Maintenance agent policy
│   ├── MODEL_SETUP.md                         # Model configuration
│   ├── Modelfile.qwen3-4b-hermes              # Hermes model file
│   ├── OFFICIAL_REFERENCES.md                 # Reference documentation
│   ├── OPTIMIZATION_FRAMEWORK.md             # Performance optimization notes
│   ├── PROJECT_REGISTRY.md                    # Project tracking
│   ├── PROJECT_SCOPE.md                       # Scope definition
│   ├── PROMPTS.md                             # Prompt templates
│   ├── README.md                              # Hermes agent overview
│   ├── RECOVERY_RUNBOOK.md                    # Recovery procedures
│   ├── REPORT_TEMPLATE.md                     # Report format template
│   ├── SAFETY_POLICY.md                       # Safety rules
│   ├── SCHEDULE.md                            # Maintenance schedule
│   ├── SOUL_TEMPLATE.md                       # Agent personality template
│   ├── logs/                                  # Generated log files (ignored)
│   ├── scripts/
│   │   ├── run-maintenance.ps1               # Maintenance runner
│   │   └── watch-latest-run.ps1              # Log watcher
│   └── skills/db-builder-maintenance/SKILL.md # Maintenance skill definition
│
├── knowledge/                                 # Markdown knowledge files for LLM workflows
│   ├── commodity_framework.md
│   ├── confidence_scoring_framework.md
│   ├── cross_asset_relationships.md
│   ├── deepseek_agent_system_prompt.md
│   ├── deepseek_cio_framework.md
│   ├── deepseek_report_generation_playbook.md
│   ├── equity_playbook.md
│   ├── fixed_income_playbook.md
│   ├── historical_regimes.md
│   ├── institutional_output_templates.md
│   ├── macro_regime_framework.md
│   ├── market_state_schema.md
│   ├── portfolio_construction_framework.md
│   ├── quant_factor_models.md
│   └── risk_management_framework.md
│
├── migrations/                                # ⚠ Currently gitignored (*.sql pattern)
│   ├── 20260711_etf_flow_analytics.sql       # ETF flow analytics schema (421 lines, idempotent)
│   ├── 20260812_finra_short_analytics.sql    # Short analytics schema
│   └── 20260814_short_analytics_latest.sql   # Short analytics latest snapshot
│
├── notebooks/                                 # Exploratory Jupyter notebooks
│   ├── Equities_pgSQL_db.ipynb
│   ├── Single_Equities_pgSQL.ipynb
│   ├── Untitled.ipynb                         # ⚠ Unclear purpose
│   ├── Untitled1.ipynb                        # ⚠ Unclear purpose
│   ├── macro_data.ipynb
│   └── pgSQL_equities_indicators.ipynb
│
├── scripts/                                   # CLI entry points and scheduled .bat jobs (70+ files)
│   ├── _bootstrap.py                          # Path helper: adds src/ to sys.path
│   │
│   ├── --- EQUITY PIPELINE ---
│   ├── pgSQL_equities_auto.py                 # Main equity fetch + indicator + sync
│   ├── single_equity_custom_date_fetch.py      # Single-ticker custom-date fetch
│   ├── equity_security_status.py              # Classify instruments, flag coverage
│   ├── equity_session_coverage_audit.py       # Audit core/all session coverage
│   ├── repair_historical_equity_gaps.py       # Repair missing rows via yfinance
│   ├── recalculate_core_ytd.py                # Split-safe YTD recalculation
│   ├── indicator_staged_backfill.py           # Resumable CSV-staged indicator backfill
│   ├── pgSQL_daily_bulk_sync_to_neon.py       # Local to Neon sync
│   ├── security_classification_fetch.py       # Fetch security classifications
│   │
│   ├── --- MACRO PIPELINE ---
│   ├── macro_data_fetch.py                    # Macro close/live fetch (1141 lines)
│   ├── economic_data_fetch.py                 # Economic indicator fetch (local-only)
│   ├── economic_sync_to_neon.py               # ⚠ Intentionally disabled
│   ├── global_economic_data_fetch.py          # Global economic data (World Bank, etc.)
│   ├── abs_metadata_fetch.py                  # ABS metadata fetch
│   ├── cot_fetch.py                           # CFTC COT fetch
│   ├── finra_short_volume_fetch.py            # FINRA short volume fetch
│   ├── finra_short_interest_fetch.py          # FINRA short interest fetch
│   ├── finra_short_incremental.py             # Incremental short data sync
│   ├── finra_short_analytics.py               # Short analytics calculation
│   ├── finra_short_backtest.py                # Short analytics backtest
│   ├── ici_flows_fetch.py                     # ICI flow fetch (scaffold)
│   ├── etf_holdings_fetch.py                  # ETF holdings fetch (scaffold)
│   ├── sec_13f_fetch.py                       # SEC 13F fetch (scaffold)
│   ├── etf_flows_fetch.py                     # ETF daily data fetch
│   ├── etf_flow_analytics.py                  # ETF flow analytics CLI
│   ├── flow_source_check.py                   # Flow source health check
│   ├── positioning_flow_signals.py            # Positioning/flow signal generation
│   ├── short_analytics_latest_sync.py         # Short analytics latest sync
│   ├── short_recalc_progress.py               # Short recalculation progress
│   ├── short_snapshot_verify.py               # Short snapshot verification
│   │
│   ├── --- NEWS PIPELINE ---
│   ├── news_fetch.py                          # News fetch
│   ├── news_classify.py                       # News classification
│   ├── news_signals.py                        # News signal generation
│   ├── news_discover_keywords.py              # Keyword discovery
│   ├── news_source_health.py                  # News source health check
│   │
│   ├── --- ANALYTICS / SIGNALS ---
│   ├── market_regime.py                       # Market regime scoring (v1)
│   ├── market_regime_v2.py                    # Market regime scoring (v2)
│   ├── sector_intelligence.py                 # Sector intelligence
│   ├── sector_regime.py                       # Sector regime
│   ├── sector_rotation.py                     # Sector rotation
│   ├── secular_themes.py                      # Secular themes
│   ├── opportunity_report.py                  # Opportunity scanner
│   ├── market_pulse_report.py                 # Market pulse report
│   │
│   ├── --- LLM / DEEPSEEK ---
│   ├── deepseek_house_view.py                 # DeepSeek house view
│   ├── deepseek_multistage_report.py          # DeepSeek multistage report
│   ├── deepseek_offline_report.py             # DeepSeek offline report
│   ├── deep_cio_report.py                     # Deep CIO report
│   ├── llm_analyst_report.py                  # LLM analyst report
│   │
│   ├── --- REPORTING ---
│   ├── rule_based_market_update.py            # Deterministic rule-based report
│   ├── daily_house_view_report.py             # Daily house view report
│   ├── investment_report.py                   # Investment report
│   │
│   ├── --- TELEGRAM ---
│   ├── telegram_bot.py                         # ⚠ Trivial wrapper for src/db_builder/telegram_bot.py
│   │
│   ├── --- MAINTENANCE ---
│   ├── health_check.py                        # Pipeline dependency health check
│   ├── update_scheduled_task_paths.ps1        # Update Windows scheduled task paths
│   │
│   └── --- SCHEDULED .bat JOBS ---
│       ├── auto_postgreSQL_db.bat             # Equity pipeline (no mutex)
│       ├── auto_macro_db.bat                  # Macro pipeline (with mutex)
│       ├── auto_news_intelligence.bat         # News pipeline
│       ├── auto_market_pulse_3h.bat           # 3-hourly market pulse
│       ├── auto_news_intelligence_dry_run.bat # News pipeline dry run
│       ├── telegram_eod_report.bat            # EOD Telegram report
│       ├── telegram_morning_report.bat        # Morning Telegram report
│       └── telegram_system_alert.bat          # System failure alert
│
├── src/db_builder/                            # Core reusable Python package
│   ├── __init__.py
│   ├── config.py                              # DB config, engines, env loading
│   ├── env_loader.py                          # External env file loader (DB_builder_env)
│   ├── pathing.py                             # Import path helper
│   ├── trading_calendar.py                    # NYSE calendar, session validation
│   │
│   ├── --- INGESTION ---
│   ├── eastmoney.py                           # Eastmoney equity fetch + YTD replacement
│   ├── yfinance_equity_fallback.py            # yfinance fallback for missing rows
│   ├── economic_data.py                       # Economic data models
│   ├── global_economic_data.py                # Global economic data models
│   ├── etf_flows.py                           # ETF daily data fetch
│   ├── etf_holdings.py                        # ETF holdings fetch
│   ├── ici_flows.py                           # ICI flows
│   ├── sec_13f_holdings.py                    # SEC 13F holdings
│   ├── flow_sources.py                        # Flow source health tracking
│   ├── security_classification.py             # Security classification
│   │
│   ├── --- NEWS ---
│   ├── news_fetcher.py                        # News fetching
│   ├── news_storage.py                        # News storage
│   ├── news_classifier.py                     # News classification
│   ├── news_taxonomy.py                       # News taxonomy
│   ├── news_dedup.py                          # News deduplication
│   ├── news_importance.py                     # News importance scoring
│   ├── news_keywords.py                       # News keywords
│   ├── news_keyword_discovery.py              # Keyword discovery
│   ├── news_scoring.py                        # News scoring
│   ├── news_signal_aggregation.py             # News signal aggregation
│   ├── news_sources.py                        # News sources
│   ├── news_source_health.py                  # News source health
│   │
│   ├── --- INDICATORS ---
│   ├── indicators.py                          # Technical indicators (MA, EMA, RSI, MACD, ATR)
│   ├── market_strength.py                     # Market strength scoring
│   ├── market_dispersion.py                   # Market dispersion metrics
│   ├── sector_strength.py                     # Sector strength ranking
│   ├── sector_intelligence.py                 # Sector intelligence
│   ├── sector_regime.py                       # Sector regime
│   ├── sector_rotation.py                     # Sector rotation
│   ├── sector_theme_alignment.py              # Sector-theme alignment
│   ├── theme_strength.py                      # Theme strength ranking
│   ├── secular_theme_engine.py                # Secular theme engine
│   ├── positioning_flow_signals.py            # Positioning flow signals
│   ├── opportunity_scanner.py                 # Opportunity scanner
│   ├── contradiction_audit.py                 # Contradiction/audit flagging
│   │
│   ├── --- ETF FLOW ANALYTICS ---
│   ├── etf_flow/
│   │   ├── __init__.py
│   │   ├── config.py                          # ETF flow config loading
│   │   ├── models.py                          # Data models
│   │   ├── repository.py                      # DB access layer
│   │   ├── aggregation.py                     # Flow aggregation
│   │   ├── availability.py                    # Data availability tracking
│   │   ├── backtest.py                         # Backtest scaffold
│   │   ├── breadth.py                          # ⚠ Deprecated
│   │   ├── confidence.py                       # Confidence adjustment
│   │   ├── consensus.py                        # Cross-issuer consensus
│   │   ├── exposure_mapping.py                 # Exposure mapping
│   │   ├── feature_engineering.py              # Feature engineering
│   │   ├── forward_signal.py                   # Forward setup scoring
│   │   ├── momentum.py                         # Flow momentum
│   │   ├── normalization.py                    # Flow normalization/winsorization
│   │   ├── price_flow.py                       # Price-flow state matrix
│   │   ├── regime.py                           # ETF flow regime
│   │   ├── representative.py                   # Representative ETF selection
│   │   ├── report_adapter.py                   # Report adapter for ETF flow section
│   │   ├── run.py                              # Main orchestrator
│   │   └── validation.py                       # Validation utilities
│   │
│   ├── --- SHORT ANALYTICS ---
│   ├── finra_short_analytics.py               # Short analytics calculation
│   ├── finra_short_interest.py                # Short interest models
│   ├── finra_short_volume.py                  # Short volume models
│   ├── short_analytics_config.py              # Short analytics config
│   ├── short_backtest.py                      # Short backtest
│   ├── short_pipeline.py                      # Short pipeline orchestrator
│   ├── short_regime.py                        # Short regime classification
│   ├── short_snapshot.py                      # Short snapshot builder
│   ├── cot_positions.py                        # CFTC COT positions
│   │
│   ├── --- REPORTING ---
│   ├── report_renderer.py                     # Deterministic rule-based report renderer
│   ├── report_sections.py                     # Report section builders
│   ├── rule_based_config.py                   # Scoring weights, core tickers, macro symbols
│   ├── rule_based_market_data.py              # SQL data access for reports
│   ├── rule_based_regime.py                   # Market regime scoring engine
│   ├── investment_report.py                   # Investment report models
│   ├── critical_house_view.py                 # Critical house view
│   ├── knowledge_retriever.py                 # Knowledge retrieval for LLM workflows
│   ├── llm_analyst_overlay.py                 # LLM analyst overlay
│   │
│   ├── --- DEEPSEEK / LLM ---
│   ├── deepseek_cio_engine.py                 # DeepSeek CIO engine
│   ├── deepseek_multistage_agent.py           # DeepSeek multistage agent
│   ├── deepseek_offline_agent.py              # DeepSeek offline agent
│   ├── deepseek_report_validator.py           # Report validation
│   ├── deepseek_section_generator.py           # Section generation
│   ├── deepseek_snapshot_builder.py           # Snapshot building
│   ├── deepseek_stage_outputs.py              # Stage output models
│   ├── deepseek_stage_prompts.py              # Stage prompts
│   ├── deepseek_stage_validator.py            # Stage validation
│   │
│   ├── --- SYNC / DEPLOYMENT ---
│   ├── neon_sync.py                           # Local to Neon sync
│   └── telegram_bot.py                        # Telegram bot module
│
├── tests/                                     # Pytest test suite (~70 test files)
│   ├── conftest.py                            # Adds src/ to sys.path
│   ├── test_abs_metadata.py
│   ├── test_cot_positions.py
│   ├── test_critical_house_view.py
│   ├── test_eastmoney.py
│   ├── test_economic_data.py
│   ├── test_economic_sync_to_neon.py
│   ├── test_env_loader.py
│   ├── test_equity_security_status.py
│   ├── test_equity_session_coverage_audit.py
│   ├── test_etf_flow_analytics.py
│   ├── test_etf_flows.py
│   ├── test_finra_short_analytics.py
│   ├── test_finra_short_interest.py
│   ├── test_finra_short_volume.py
│   ├── test_flow_sources.py
│   ├── test_global_economic_data.py
│   ├── test_indicator_staged_backfill.py
│   ├── test_indicators.py
│   ├── test_investment_report.py
│   ├── test_knowledge_retriever.py
│   ├── test_llm_analyst_overlay.py
│   ├── test_macro_data_fetch.py
│   ├── test_market_dashboard_short_positioning.py
│   ├── test_market_dispersion.py
│   ├── test_market_intelligence_report.py
│   ├── test_market_regime.py
│   ├── test_market_regime_v2.py
│   ├── test_neon_sync.py
│   ├── test_neon_sync_reconciliation.py
│   ├── test_news_classifier.py
│   ├── test_news_dedup.py
│   ├── test_news_fetcher.py
│   ├── test_news_importance.py
│   ├── test_news_keywords.py
│   ├── test_news_signal_aggregation.py
│   ├── test_news_source_health.py
│   ├── test_news_sources.py
│   ├── test_news_storage.py
│   ├── test_news_taxonomy.py
│   ├── test_opportunity_scanner.py
│   ├── test_positioning_flow_signals.py
│   ├── test_recalculate_core_ytd.py
│   ├── test_repair_historical_equity_gaps.py
│   ├── test_rule_based_market_data.py
│   ├── test_rule_based_market_update_script.py
│   ├── test_scoring_rules.py
│   ├── test_sector_intelligence.py
│   ├── test_sector_regime.py
│   ├── test_sector_rotation.py
│   ├── test_sector_theme_alignment.py
│   ├── test_secular_theme_engine.py
│   ├── test_security_classification.py
│   ├── test_short_backtest.py
│   ├── test_short_snapshot.py
│   ├── test_telegram_bot.py
│   ├── test_trading_calendar.py
│   └── test_yfinance_equity_fallback.py
│
├── neon-api/                                  # ⚠ NESTED GIT REPO — independent deployment
│   ├── main.py                                # FastAPI app (1652 lines) — API + Telegram
│   ├── Dockerfile                             # Python 3.12-slim + uvicorn
│   ├── privacy_policy.html
│   ├── requirements.txt
│   ├── TELEGRAM_WEBHOOK.md
│   ├── .env.example
│   └── tests/test_api.py                      # API tests (~353 lines)
│
├── market-intelligence-telegram-bot/          # ⚠ NESTED GIT REPO — independent deployment
│   ├── main.py                                # FastAPI app (1140 lines) — Telegram commands
│   ├── Dockerfile                             # Python 3.12-slim + uvicorn
│   ├── requirements.txt
│   ├── .env.example
│   ├── .github/workflows/telegram-reports.yml # GitHub Actions cron
│   └── tests/test_main.py                     # Bot tests (~583 lines)
│
└── market-dashboard/                          # ⚠ NESTED GIT REPO — GitHub Pages
    ├── index.html                             # Dashboard HTML
    ├── app.js                                 # Frontend JavaScript
    ├── styles.css                             # Dashboard styles
    ├── config.js                              # Runtime config (API base URL)
    ├── config.example.js
    ├── data/
    │   ├── latest-report.md                   # Latest published report (auto-updated)
    │   ├── market-intelligence-report.md
    │   └── market-intelligence-report.html
    ├── scripts/build-runtime-config.mjs
    ├── assets/images/aegis/                   # Branding images
    ├── .github/workflows/pages.yml            # GitHub Pages deployment
    └── README.md
```
