---
name: db-builder-maintenance
description: Report-only maintenance audit for the DB_builder code, services, pipelines, Git repositories, and read-only PostgreSQL/Neon health.
---

# DB Builder Maintenance

Operate from `C:\Users\User\OneDrive\Coding\DB_builder` and obey the root `AGENTS.md`.

## Workflow

1. Record timestamp, scope, Git state, and changed areas.
2. Inventory only relevant source/configuration; skip generated/cached/vendor directories.
3. Run existing safe, bounded checks appropriate to changed areas.
4. If dedicated read-only DSNs exist, query database health with a read-only transaction and strict timeouts. Never reveal DSNs or query text containing secrets.
5. Trace cross-project impacts among core pipelines, Neon API, dashboard, Telegram bot, schedules, and published reports.
6. Write evidence-based P0-P3 findings using the project report template.
7. Update only `hermes-agent/reports/` and leave all application/database state unchanged.

## Hard stops

Do not install/upgrade dependencies, edit application source, deploy, commit, push, delete, run migrations, grant privileges, write data, or create/drop/reindex database objects. Report recommended actions for human review.

