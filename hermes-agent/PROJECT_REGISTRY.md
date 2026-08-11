# Project Registry

Last discovery: 2026-08-09 (initial inventory; verify during first Hermes scan).

| System | Location | Responsibility | Repository |
|---|---|---|---|
| Core DB builder | root, `src/db_builder`, `scripts`, `tests` | Local market/news/economic ingestion, analytics, reports, local PostgreSQL and Neon sync | Root Git repository |
| Neon API | `neon-api` | FastAPI read API backed by Neon and deployed to Render | Separate Git repository |
| Market dashboard | `market-dashboard` | Static GitHub Pages market dashboard and latest report | Separate Git repository |
| Telegram bot | `market-intelligence-telegram-bot` | FastAPI Telegram webhook, report commands, scheduled notifications | Separate Git repository |
| Knowledge base | `knowledge` | Investment/report frameworks used by local report agents | Root repository |
| Configuration | `config` | Rule-based and ETF-flow scoring configuration | Root repository |
| Migrations | `migrations` | PostgreSQL schema changes | Root repository |

Known relationships:
- Core pipelines write local PostgreSQL and selected datasets to Neon.
- Neon API reads Neon for dashboard/API consumers.
- Rule-based report generation publishes `latest-report.md` into the dashboard repository.
- The Telegram bot reads the published dashboard report and public API.
- Scheduled Windows batch files orchestrate ingestion and reporting.

Inventory caveat: root Git has substantial existing modified/untracked work; Hermes must not normalize or discard it.

