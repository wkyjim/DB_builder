# Deployment Architecture

## Infrastructure Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     LOCAL WINDOWS MACHINE                        │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  PostgreSQL (local) — source of truth                     │   │
│  │  Conda environment: PostgreSQL_db                         │   │
│  │  Windows Task Scheduler: automated .bat workflows         │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              │                                   │
│                              │ git push                          │
│                              ▼                                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  GitHub (wkyjim/DB_builder)                              │   │
│  │  ├── neon-api/ → Render/Oracle auto-deploy              │   │
│  │  ├── market-intelligence-telegram-bot/ → Render/Oracle   │   │
│  │  └── market-dashboard/ → GitHub Pages                    │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              │                                   │
└──────────────────────────────┼───────────────────────────────────┘
                               │
                               │ sync (pgSQL_daily_bulk_sync_to_neon.py)
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                     NEON (serverless PostgreSQL)                 │
│  - us_equities (subset)                                          │
│  - us_equities_indicators (subset)                               │
│  - macro_live (latest snapshots)                                 │
│  - us_equities_short_analytics_latest                            │
│  - sync_state                                                    │
└─────────────────────────────────────────────────────────────────┘
                               │
                               │ read-only API access
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                     ORACLE CLOUD VM (Ubuntu)                     │
│                                                                  │
│  Docker Compose stack:                                           │
│  ┌────────────┐  ┌──────────────────┐  ┌──────────────────┐    │
│  │ neon-api   │  │ telegram-bot     │  │ caddy (TLS)      │    │
│  │ :8000      │  │ :8000            │  │ :80  :443        │    │
│  │ FastAPI    │  │ FastAPI          │  │ reverse proxy    │    │
│  └────────────┘  └──────────────────┘  └──────────────────┘    │
│                                                                  │
│  Deployed via: deploy/oracle/deploy.sh                           │
│  Bootstrapped via: deploy/oracle/bootstrap-ubuntu.sh             │
│  Firewall: UFW (80, 443, 22 only)                               │
│  TLS: Caddy automatic HTTPS                                     │
│  Public IP: 138.2.69.165                                        │
│  DNS: sslip.io (api.138.2.69.165.sslip.io)                     │
│                                                                  │
│  Telegram webhook: /telegram/webhook/{secret}                    │
│  Scheduled reports: GitHub Actions → /tasks/send/{kind}/{secret}│
└─────────────────────────────────────────────────────────────────┘
```

## Components

### Local Windows Machine

**Role:** Development environment and scheduled job runner

**Software:**
- PostgreSQL (local database)
- Conda environment: `PostgreSQL_db`
- Python: `C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe`
- Windows Task Scheduler

**Scheduled Workflows:**
| Workflow | File | Trigger |
|----------|------|---------|
| Equity pipeline | `scripts/auto_postgreSQL_db.bat` | Daily |
| Macro pipeline | `scripts/auto_macro_db.bat` | Daily |
| News pipeline | `scripts/auto_news_intelligence.bat` | Daily |
| Market pulse | `scripts/auto_market_pulse_3h.bat` | Every 3 hours |

**Environment Variables:**
- `DB_BUILDER_ENV_DIR`: Path to external secret directory
- `LOCAL_DB_PASSWORD`: Local PostgreSQL password
- `NEON_DB_PASSWORD`: Neon PostgreSQL password
- `MARKET_API_BASE_URL`: Public API base URL

### Neon (Serverless PostgreSQL)

**Role:** Deployment database for API consumers

**Connection:** `ep-aged-moon-ao3o4z0j-pooler.c-2.ap-southeast-1.aws.neon.tech`

**Tables:**
- `us_equities` (subset, post-2026-05-01)
- `us_equities_indicators` (subset)
- `macro_live` (latest snapshots)
- `us_equities_short_analytics_latest`
- `sync_state` (sync checkpoints)

**Access:** Read-only from API servers, read-write from sync scripts

### Oracle Cloud VM

**Role:** Production hosting for API and Telegram bot

**Stack:** Docker Compose with three services

**Services:**

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| `neon-api` | Custom (Python 3.12-slim) | 8000 | FastAPI market data API |
| `telegram-bot` | Custom (Python 3.12-slim) | 8000 | Telegram bot webhook handler |
| `caddy` | caddy:2-alpine | 80, 443 | TLS reverse proxy |

**Security:**
- UFW firewall: ports 80, 443, 22 only
- Docker containers run as non-root `app` user
- Read-only container filesystem with tmpfs /tmp
- Caddy automatic HTTPS
- Telegram webhook secret authentication

**Configuration:**
- `deploy/oracle/compose.yaml` — Docker Compose definition
- `deploy/oracle/Caddyfile` — Caddy reverse proxy config
- `deploy/oracle/oracle.env.example` — Environment variable template

**Deployment Scripts:**
- `deploy/oracle/bootstrap-ubuntu.sh` — Initial VM setup (run once)
- `deploy/oracle/deploy.sh` — Deploy/update stack
- `deploy/oracle/Deploy-OracleStack.ps1` — PowerShell orchestrator
- `deploy/oracle/status.sh` — Health check

### GitHub Pages (Static Dashboard)

**Role:** Public-facing market dashboard

**Repository:** `wkyjim/market-dashboard`

**URL:** `https://wkyjim.github.io/market-dashboard/`

**Deployment:** Automatic on push to main branch via `.github/workflows/pages.yml`

**Content:**
- `index.html` — Dashboard HTML
- `app.js` — Frontend JavaScript (consumes API)
- `data/latest-report.md` — Auto-updated latest report

### Render (Alternative Deployment)

**Role:** Alternative cloud hosting for API and bot

**URL pattern:** `https://<service>.onrender.com`

**Services:**
- `neon-api` — Market data API
- `telegram-bot` — Telegram bot webhook

**Deployment:** Auto-deploy on push to main (if configured)

**Used for:**
- Telegram webhook registration (`TG_RENDER_BASE_URL`)
- Alternative to Oracle VM

## Deployment Procedures

### Initial VM Bootstrap

```bash
# Run once on new Oracle VM
sudo bash deploy/oracle/bootstrap-ubuntu.sh
# Reconnect for Docker group membership
```

### Deploy Stack

```bash
cd deploy/oracle
bash deploy.sh
```

### Manual Container Management

```bash
cd deploy/oracle
docker compose up -d          # Start all services
docker compose down           # Stop all services
docker compose logs -f        # View logs
docker compose ps             # Check status
```

### Dashboard Update

Dashboard updates automatically when `market-dashboard/data/latest-report.md` changes:
1. `rule_based_market_update.py --publish-dashboard --push-dashboard`
2. Copies report to dashboard repo
3. Commits and pushes to `wkyjim/market-dashboard`
4. GitHub Actions deploys to GitHub Pages

## Monitoring

### Health Checks

- **neon-api:** `https://api.138.2.69.165.sslip.io/`
- **telegram-bot:** `https://api.138.2.69.165.sslip.io/health`
- **caddy:** Docker healthcheck on dependent services

### Log Locations

| Service | Log Location |
|---------|--------------|
| neon-api | Docker json-file (max 10m, 5 files) |
| telegram-bot | Docker json-file (max 10m, 5 files) |
| caddy | Docker json-file (max 10m, 5 files) |
| Scripts | `logs/` directory (timestamped) |
| Cron | GitHub Actions run history |

### Alerting

- **GitHub Actions Telegram:** Cron schedule triggers `/tasks/send/{kind}/{secret}`
- **Telegram system alert:** `telegram_system_alert.bat` on workflow failure
- **No data freshness alerting** — gap identified in TECHNICAL_DEBT.md

## Environment Variables

### DB_builder Root

| Variable | Required | Purpose |
|----------|----------|---------|
| `LOCAL_DB_PASSWORD` | Yes | Local PostgreSQL password |
| `NEON_DB_PASSWORD` | Yes | Neon PostgreSQL password |
| `NEON_DATABASE_URL` | No | Alternative: full Neon connection string |
| `TG_TOKEN` | No | Telegram bot token |
| `TG_CHAT_ID` | No | Telegram chat ID |
| `TG_WEBHOOK_SECRET` | No | Telegram webhook path secret |
| `TG_RENDER_BASE_URL` | No | Render/Oracle base URL |
| `MARKET_API_BASE_URL` | No | Public API base URL |
| `OLLAMA_MODEL` | No | Ollama model name |
| `MASSIVE_API_KEY` | No | Massive API key for LLM workflows |

### neon-api

| Variable | Required | Purpose |
|----------|----------|---------|
| `NEON_DATABASE_URL` | Yes* | Full Neon connection string |
| `NEON_DB_USER` | Yes* | Neon username |
| `NEON_DB_PASSWORD` | Yes* | Neon password |
| `NEON_DB_HOST` | Yes* | Neon host |
| `NEON_DB_NAME` | Yes* | Neon database name |
| `MARKET_API_BASE_URL` | No | Public API base URL |

*Either `NEON_DATABASE_URL` or all component variables required.

### telegram-bot

| Variable | Required | Purpose |
|----------|----------|---------|
| `TG_TOKEN` | Yes | Telegram bot token |
| `TG_CHAT_ID` | Yes | Telegram chat ID |
| `TG_WEBHOOK_SECRET` | Yes | Webhook path secret |
| `TG_RENDER_BASE_URL` | Yes | Service base URL |
| `MARKET_API_BASE_URL` | Yes | Market API base URL |
| `TG_REPORT_URL` | Yes | Latest report URL |
| `TG_DASHBOARD_URL` | No | Dashboard URL |

## Disaster Recovery

### Backup Strategy

- **Local PostgreSQL:** Not explicitly backed up — relies on re-fetching from sources
- **Neon:** Managed by Neon (continuous protection, point-in-time recovery)
- **Code:** GitHub repositories (with full git history)
- **Secrets:** `DB_builder_env/` directory (not in git — must be backed up separately)

### Recovery Procedures

1. **Local database loss:** Re-run `pgSQL_equities_auto.py` with appropriate `--date` range
2. **Neon data loss:** Re-run `pgSQL_daily_bulk_sync_to_neon.py` from local
3. **VM loss:** Run `bootstrap-ubuntu.sh` on new VM, then `deploy.sh`
4. **Git repo loss:** Clone from GitHub, re-seed `DB_builder_env/`
