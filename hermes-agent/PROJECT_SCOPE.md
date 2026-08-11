# Project Scope

Hermes monitors the complete DB_builder root, including Python ingestion and analytics, PostgreSQL/Neon synchronization, FastAPI services, report generation, market dashboard, Telegram bot, scripts, tests, workflows, documentation, and cross-project contracts.

Exclude routine traversal of `.git`, virtual environments, `node_modules`, caches, build output, generated artifacts, large logs, and generated reports unless directly relevant to an incident.

Scheduled scope is report-only. Reports belong under `hermes-agent/reports/`.

