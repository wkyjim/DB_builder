# DB_builder Architecture

## 1. System Overview

DB_builder is a local-first financial data pipeline and investment intelligence platform. It ingests U.S. equity prices, macro data, news, ETF flows, and short-positioning data into a local PostgreSQL database, computes technical indicators and deterministic regime scores, syncs selected data to Neon (serverless PostgreSQL), and publishes reports via a FastAPI API, a GitHub Pages dashboard, and a Telegram bot.

Key design decisions:
- **Local PostgreSQL is the source of truth** — Neon is a selected subset for API/deployment
- **Rule-based reports are deterministic** — no LLM scoring for core market updates
- **Eastmoney is primary equity source** — yfinance is fallback for missing/failed rows
- **ETF flow uses issuer-derived data** — shares outstanding × NAV, not trading volume
- **Nested repos have independent deployment lifecycles** — neon-api, telegram-bot, dashboard

## 2. High-Level Architecture

```
                        WINDOWS HOST (LOCAL)
  ┌──────────────────────────────────────────────────────────────┐
  │  Scheduled .bat Jobs                                         │
  │  auto_postgreSQL_db.bat  auto_macro_db.bat  auto_news_...bat │
  └──────────────────────────┬───────────────────────────────────┘
                              │
  ┌──────────────────────────▼───────────────────────────────────┐
  │  scripts/ (70+ entry points)                                 │
  │  pgSQL_equities_auto.py  macro_data_fetch.py  news_fetch.py  │
  │  etf_flow_analytics.py   rule_based_market_update.py         │
  └──────────────────────────┬───────────────────────────────────┘
                              │
  ┌──────────────────────────▼───────────────────────────────────┐
  │  src/db_builder/ (core package)                              │
  │  eastmoney.py  indicators.py  rule_based_regime.py          │
  │  etf_flow/*  report_renderer.py  rule_based_market_data.py  │
  └──────────────────────────┬───────────────────────────────────┘
                              │
  ┌──────────────────────────▼───────────────────────────────────┐
  │  LOCAL POSTGRESQL (source of truth)                          │
  │  us_equities  us_equities_indicators  macro  macro_live     │
  │  news  etf_daily_raw  etf_flow_*  short_analytics_*        │
  └──────────────┬───────────────────────────────┬───────────────┘
                  │                               │
     ┌────────────▼───────────┐    ┌──────────────▼──────────────┐
     │  NEON SYNC             │    │  REPORT GENERATION          │
     │  pgSQL_daily_bulk_     │    │  rule_based_market_update.py│
     │  sync_to_neon.py       │    │  → reports/*.md             │
     │  (equities + indicators│    │  → market-dashboard/        │
     │   to Neon)             │    │     data/latest-report.md   │
     └────────────┬───────────┘    └──────────────┬──────────────┘
                  │                               │
     ┌────────────▼───────────┐    ┌──────────────▼──────────────┐
     │  NEON (serverless      │    │  market-dashboard/          │
     │   PostgreSQL)          │    │  (GitHub Pages repo)        │
     │  us_equities           │    │  index.html  app.js         │
     │  us_equities_indicators│    │  data/latest-report.md      │
     │  macro_live            │    │  (pushed via git)           │
     └────────────┬───────────┘    └──────────────┬──────────────┘
                  │                               │
     ┌────────────▼───────────────────────────────▼──────────────┐
     │  ORACLE CLOUD (Ubuntu VM) — Docker Compose Stack           │
     │  ┌──────────┐  ┌──────────────────┐  ┌────────────────┐  │
     │  │ neon-api │  │ telegram-bot     │  │  caddy (TLS)   │  │
     │  │ FastAPI  │  │ FastAPI          │  │  reverse proxy │  │
     │  │ :8000    │  │ :8000            │  │  :80  :443     │  │
     │  └──────────┘  └──────────────────┘  └────────────────┘  │
     └───────────────────────────────────────────────────────────┘
```

## 3. Repository Structure

### Main Repository (`DB_builder/`)

Contains the pipeline core, scripts, core modules, tests, configuration, and documentation.

### Nested Repositories

Three nested git repos exist with independent deployment lifecycles:

| Repo | Deployment | Repository |
|------|------------|------------|
| `neon-api/` | Render/Oracle (Docker) | Separate GitHub repo |
| `market-intelligence-telegram-bot/` | Render/Oracle (Docker) | Separate GitHub repo |
| `market-dashboard/` | GitHub Pages | `wkyjim/market-dashboard` |

Nested repos are intentional. Each has its own Dockerfile, requirements, tests, and deployment mechanism.

## 4. Data Architecture

### 4.1 Data Sources

| Source | Type | Tables Fed |
|--------|------|------------|
| Eastmoney API | REST | `us_equities` |
| yfinance | Python library | `us_equities`, `macro`, `macro_live` |
| FINRA | API | `finra_short_volume`, `finra_short_interest` |
| CFTC COT | Weekly report | `cot_positions` |
| RSS/Atom feeds | Feeds | `news` |
| FRED / World Bank / ECB | Economic APIs | `economic_indicators` |
| ETF issuers | Various | `etf_daily_raw`, `etf_daily_data` |

### 4.2 Ingestion Pipeline

External APIs → Ingestion scripts → Local PostgreSQL → Indicator/Analytics layer → Neon sync → API consumers

Each ingestion script follows a common pattern:
1. Parse CLI arguments
2. Import `_bootstrap.py` to add `src/` to `sys.path`
3. Load config from `db_builder.config`
4. Fetch external data
5. Validate and transform
6. Upsert to local PostgreSQL

### 4.3 Local PostgreSQL Tables (selected)

| Table | Purpose | Primary Key |
|-------|---------|-------------|
| `public.us_equities` | Raw OHLCV equity data | `(date, ticker)` |
| `public.us_equities_indicators` | Technical indicators | `(ticker, date)` |
| `public.macro` | Historical macro/index data | `(date, symbol)` |
| `public.macro_live` | Latest intraday macro snapshot | `(symbol)` |
| `public.news` | Raw news articles | `(url_hash)` or `(id)` |
| `public.news_signals` | Aggregated news signals | `(window, ticker)` |
| `public.etf_daily_raw` | Raw ETF issuer data | `(date, ticker, source)` |
| `public.etf_master` | ETF reference/master data | `(ticker)` |
| `public.etf_flow_daily` | Daily flow calculations | `(date, ticker)` |
| `public.etf_flow_features` | Flow features and z-scores | `(date, ticker)` |
| `public.etf_flow_segment_daily` | Segment-level flow scores | `(date, segment_type, segment)` |
| `public.etf_flow_regime_daily` | ETF flow regime | `(date)` |
| `public.us_equities_short_analytics_latest` | Short analytics snapshot | `(ticker)` |
| `public.positioning_flow_signals` | Unified positioning signals | `(date, ticker)` |
| `public.sync_state` | Neon sync checkpoints | `(table_name)` |

### 4.4 Derived Tables and Analytics

- **Indicators:** MA(5/20/50/100/200), EMA(12/26), RSI(14), MACD, ATR(14), volume MA/ratio, 52-week high/low, returns (5/20/60d), volatility
- **ETF Flow:** Clean flow estimates, winsorized flow/AUM, rolling features (5/20/60d), EMA, slope, acceleration, z-scores, persistence, cross-issuer consensus, segment scores, regime classification, forward setup scores
- **Short Analytics:** Short interest metrics, days to cover, short volume ratios, composite scoring, regime classification
- **Market Regime:** Weighted composite of equity trend, momentum, breadth, volatility, rates, credit, dollar, commodity, ETF flow, news

### 4.5 Sync to Neon

Selected tables are synced from local PostgreSQL to Neon via `pgSQL_daily_bulk_sync_to_neon.py`:
- `us_equities` — equity raw data
- `us_equities_indicators` — technical indicators
- `macro_live` — latest macro snapshots
- `us_equities_short_analytics_latest` — short analytics

Sync uses `public.sync_state` table to track latest synced dates. Idempotent upserts via temp staging tables.

## 5. Component Details

### 5.1 Ingestion Layer (`scripts/`)

70+ scripts handle data fetching, transformation, and loading. Key scripts:

| Script | Purpose |
|--------|---------|
| `pgSQL_equities_auto.py` | Main equity fetch + indicator generation |
| `macro_data_fetch.py` | Macro/index/futures/rates/FX/commodity fetch |
| `news_fetch.py` | News article fetching |
| `news_classify.py` | News classification |
| `etf_flows_fetch.py` | ETF daily data fetch |
| `finra_short_incremental.py` | Incremental FINRA short data sync |
| `cot_fetch.py` | CFTC COT fetch |
| `indicator_staged_backfill.py` | Resumable indicator calculation |
| `pgSQL_daily_bulk_sync_to_neon.py` | Local → Neon sync |
| `health_check.py` | Pipeline dependency verification |

### 5.2 Core Package (`src/db_builder/`)

Shared modules organized by domain:

| Module(s) | Purpose |
|-----------|---------|
| `config.py`, `env_loader.py`, `pathing.py` | Configuration and bootstrapping |
| `eastmoney.py`, `yfinance_equity_fallback.py` | Equity data fetching |
| `indicators.py` | Technical indicator calculations |
| `trading_calendar.py` | NYSE calendar and session validation |
| `etf_flow/` | ETF flow analytics (18-module package) |
| `finra_short_analytics.py`, `finra_short_volume.py`, `finra_short_interest.py` | Short analytics |
| `news_*.py` (12 modules) | News fetching, classification, scoring, dedup, storage |
| `sector_*.py` (4 modules) | Sector intelligence, regime, rotation |
| `report_renderer.py`, `report_sections.py` | Report generation |
| `rule_based_regime.py`, `rule_based_market_data.py`, `rule_based_config.py` | Market regime scoring |
| `market_strength.py`, `market_dispersion.py` | Market strength and dispersion |
| `neon_sync.py` | Local to Neon sync |
| `deepseek_*.py` (10 modules) | LLM/DeepSeek agent workflows |

### 5.3 ETF Flow Analytics Package (`src/db_builder/etf_flow/`)

18-module package for structured ETF flow analysis:

| Module | Purpose |
|--------|---------|
| `run.py` | Main orchestrator |
| `repository.py` | Database access layer |
| `aggregation.py` | Flow aggregation |
| `availability.py` | Data availability tracking |
| `feature_engineering.py` | Flow feature calculation |
| `momentum.py` | Flow momentum |
| `normalization.py` | Flow normalization/winsorization |
| `consensus.py` | Cross-issuer consensus |
| `confidence.py` | Confidence adjustment |
| `regime.py` | ETF flow regime classification |
| `forward_signal.py` | Forward setup scoring |
| `price_flow.py` | Price-flow state matrix |
| `representative.py` | Representative ETF selection |
| `report_adapter.py` | Report section adapter |
| `backtest.py` | Backtest scaffold |
| `models.py` | Data models |
| `config.py` | Config loading |
| `validation.py` | Validation utilities |

### 5.4 API Server (`neon-api/main.py`)

FastAPI application (1652 lines) serving:
- Equity endpoints (latest, batch, history, date)
- Macro endpoints (latest, batch, live, history, date)
- Market tape endpoint (grouped snapshot)
- Short analytics endpoints (list, ticker, funding-shorts)
- Telegram webhook endpoints
- Health and privacy endpoints

Reads from Neon PostgreSQL only. Deployed via Docker on Oracle Cloud.

### 5.5 Telegram Bot (`market-intelligence-telegram-bot/main.py`)

FastAPI application (1140 lines) providing:
- Telegram command webhook (`/telegram/webhook/{secret}`)
- Scheduled report endpoints (`/tasks/send/{kind}/{secret}`)
- System alert endpoint (`/tasks/alert/{secret}`)
- Health check and debug config endpoints

Commands: `/start`, `/market`, `/dashboard`, `/report`, `/report_*`, `/signals`, `/sectors`, `/equity`, `/risk`, `/status`

Reads from the API server and report URL. Sends messages via Telegram API.

### 5.6 Dashboard (`market-dashboard/`)

Static GitHub Pages site:
- `index.html` — Dashboard HTML
- `app.js` — Frontend JavaScript
- `styles.css` — Styling
- `config.js` — Runtime API base URL
- `data/latest-report.md` — Auto-updated latest report

Consumes API endpoints and displays report markdown.

## 6. Deployment Architecture

### 6.1 Local Windows (Development + Scheduled Jobs)

- Conda environment: `PostgreSQL_db`
- Local PostgreSQL: source of truth
- Windows Task Scheduler runs `.bat` workflow files
- Each `.bat` calls PowerShell which orchestrates Python scripts with retry logic

### 6.2 Oracle Cloud VM (Production)

- Ubuntu VM with Docker Engine
- Docker Compose stack managed via `deploy/oracle/compose.yaml`
- Services: `neon-api`, `telegram-bot`, `caddy`
- Caddy provides TLS termination and reverse proxy
- UFW firewall: ports 80, 443, 22 only

### 6.3 Neon (Serverless PostgreSQL)

- Hosted at `ep-aged-moon-ao3o4z0j-pooler.c-2.ap-southeast-1.aws.neon.tech`
- Contains subset of local data for API consumption
- `sync_state` table tracks sync checkpoints

### 6.4 GitHub Pages (Static Dashboard)

- Repository: `wkyjim/market-dashboard`
- Auto-deploys on push to main
- Displays latest report from `data/latest-report.md`
- Consumes API for live market data

### 6.5 Render (Alternative Deployment)

- `neon-api` and `telegram-bot` can also deploy to Render
- Render URL used for Telegram webhook registration

## 7. Configuration and Secrets

### 7.1 Secret Storage

Secrets are stored externally in `DB_builder_env/` (sibling directory, outside git):

```
DB_builder_env/
├── .env                              # Main DB_builder secrets
├── neon-api/.env                     # API server secrets
├── market-intelligence-telegram-bot/.env  # Telegram bot secrets
└── ...
```

`DB_BUILDER_ENV_DIR` env var can override the external path.

`DB_BUILDER_AUDIT_MODE=1` prevents loading external secrets (for agent audits).

### 7.2 Environment Variables

| Variable | Purpose | Used By |
|----------|---------|---------|
| `LOCAL_DB_PASSWORD` | Local PostgreSQL password | All scripts |
| `NEON_DB_PASSWORD` | Neon PostgreSQL password | Sync, API |
| `NEON_DATABASE_URL` | Alternative: full Neon connection string | Sync, API |
| `TG_TOKEN` | Telegram bot token | Telegram bot, API |
| `TG_CHAT_ID` | Telegram chat ID | Telegram bot, API |
| `TG_WEBHOOK_SECRET` | Telegram webhook path secret | Telegram bot, API |
| `TG_RENDER_BASE_URL` | Render/Oracle base URL | Telegram bot, API |
| `MARKET_API_BASE_URL` | Public API base URL | Dashboard, Telegram bot |
| `TG_REPORT_URL` | Latest report URL | Telegram bot |
| `OLLAMA_MODEL` | Ollama model name | LLM workflows |
| `MASSIVE_API_KEY` | Massive API key | LLM workflows |

### 7.3 File-Based Configuration

| File | Purpose |
|------|---------|
| `config/etf_flow_analytics.yaml` | ETF flow windows, weights, thresholds |
| `config/scoring_weights.yaml` | Regime/sector/theme scoring weights |
| `config/finra_short_analytics.json` | Short analytics configuration |
| `market-dashboard/config.js` | Runtime API base URL |

## 8. Automation

### 8.1 Windows Scheduled Tasks

| Workflow | File | Steps |
|----------|------|-------|
| Equity pipeline | `auto_postgreSQL_db.bat` | health_check → fetch → repair → sync → indicators → sync indicators |
| Macro pipeline | `auto_macro_db.bat` | health_check → security_class → macro → short → short_analytics → economics → global_economics → report |
| News pipeline | `auto_news_intelligence.bat` | news_fetch → news_classify → news_signals → market_regime → opportunity → sector_intel → market_regime_v2 → sector_regime → sector_rotation → secular_themes → report |
| Market pulse | `auto_market_pulse_3h.bat` | 3-hourly market pulse |

### 8.2 GitHub Actions

| Workflow | File | Schedule |
|----------|------|----------|
| Telegram reports | `market-intelligence-telegram-bot/.github/workflows/telegram-reports.yml` | 06:00/18:00 HKT |
| Dashboard deploy | `market-dashboard/.github/workflows/pages.yml` | On push to main |

### 8.3 Retry Behavior

All `.bat` workflows use PowerShell `Invoke-RetryPython` with:
- 3 attempts per script
- 60-second delay between retries
- Configurable timeout per script (600-3600 seconds)
- Exit on final failure

`auto_macro_db.bat` includes a mutex (`Local\DBBuilderAutoMacroDb`) to prevent concurrent runs.

## 9. API Reference

### 9.1 neon-api Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Health check |
| `/privacy` | GET | Privacy policy |
| `/equities/latest/{ticker}` | GET | Latest equity + indicators |
| `/equities/batch/latest` | GET | Batch latest equities |
| `/equities/history/{ticker}` | GET | OHLCV history |
| `/equities/date/{ticker}` | GET | Equity by date |
| `/macro/latest/{symbol}` | GET | Latest macro (live or close) |
| `/macro/batch/latest` | GET | Batch latest macro |
| `/macro/live/{symbol}` | GET | Live macro only |
| `/macro/batch/live` | GET | Batch live macro |
| `/macro/date/{symbol}` | GET | Macro by date |
| `/macro/history/{symbol}` | GET | Macro history |
| `/market-tape` | GET | Grouped market snapshot |
| `/short-analytics/latest` | GET | Short analytics list |
| `/short-analytics/latest/{ticker}` | GET | Single ticker short analytics |
| `/short-analytics/funding-shorts` | GET | Top funding shorts |
| `/telegram/webhook/{secret}` | POST | Telegram command webhook |
| `/telegram/webhook/register/{secret}` | POST | Register webhook |

### 9.2 telegram-bot Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Health check |
| `/health` | GET | Health check alias |
| `/debug/config` | GET | Config diagnostics |
| `/telegram/webhook/{secret}` | POST | Telegram command handler |
| `/telegram/set-webhook/{secret}` | POST | Register webhook |
| `/tasks/send/{kind}/{secret}` | POST | Scheduled report |
| `/tasks/alert/{secret}` | POST | System alert |

## 10. Known Architectural Decisions

1. **Local PostgreSQL is source of truth** — Neon is a deployment subset
2. **Eastmoney YTD is not trusted** — replaced with local calculation before upsert
3. **ETF flow uses issuer-derived data** — shares outstanding × NAV, not trading volume
4. **FINRA short volume ≠ short interest** — different metrics, different tables
5. **CFTC COT is weekly and delayed** — positioning context, not intraday signal
6. **Economic indicators are local-only** — not synced to Neon (storage concern)
7. **Rule-based reports are deterministic** — no LLM scoring for core market updates
8. **Nested repos have independent deployment** — each has own Dockerfile and CI/CD
9. **Indicator generation uses staged CSV** — resumable after interruption
10. **ETF flow regime is separate** — does not overwrite existing market regime score

## 11. Related Documentation

- `docs/PROJECT_MAP.md` — Annotated directory tree
- `docs/DATA_FLOW.md` — Data sources, pipelines, and table relationships
- `docs/DEPLOYMENT.md` — Infrastructure and deployment procedures
- `TECHNICAL_DEBT.md` — Prioritized improvement backlog
- `HANDOFF.md` — Prior session context and decisions
