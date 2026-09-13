# DB Builder Agent Rules

This project has two parts:

1. Local data pipeline
   - Fetches data from yfinance
   - Updates local PostgreSQL
   - Calculates indicators
   - Syncs selected data to Neon

2. API deployment
   - Located in neon-api/
   - FastAPI app deployed to Render
   - Reads from Neon only

## Safety rules

Never run destructive SQL without asking:
- DROP TABLE
- TRUNCATE
- DELETE FROM
- ALTER TABLE DROP COLUMN

Never expose secrets:
- Do not print .env values
- Do not commit .env
- Do not hardcode database passwords
- Do not read, list, search, copy, or inspect `C:\Users\User\OneDrive\Coding\DB_builder_env`
- Treat the sibling `DB_builder_env` directory as explicitly outside project and audit scope
- Keep `DB_BUILDER_AUDIT_MODE=1` enabled for every agent audit; never unset or override it

Prefer:
- bulk upserts
- symbol/date deduplication
- minimal Neon reads
- explicit logging
- small test runs before full runs

Before git commit:
- run tests if available
- run affected script with a small ticker subset
- show git diff
- explain what changed

For Render API:
- only edit neon-api/ unless asked
- keep OpenAPI schema GPT-action friendly
- keep response models explicit

## Hermes maintenance agent

`C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder` is the complete project ecosystem. Hermes must inspect relevant child projects, repositories, APIs, pipelines, dashboards, bots, tests, deployment configuration, documentation, and database architecture as one connected system.

Scheduled Hermes activity is LEVEL 1 (observe/report) only. It may read files, inspect Git, run safe existing tests and static checks, query database statistics using read-only credentials, and update files under `hermes-agent/reports/`. It must not modify application source, install or upgrade project dependencies, deploy, commit, push, rewrite Git history, delete files, run migrations, alter schemas, create or drop indexes, or write production data.

Hermes must:
- avoid reading or emitting secret values and never include credentials in prompts or reports;
- keep `DB_BUILDER_AUDIT_MODE=1` set so application imports cannot load external secret files;
- distinguish source code from generated data, logs, caches, reports, virtual environments, and build output;
- use Git status and diffs as a safety boundary and preserve all existing user changes;
- use `LOCAL_PG_READONLY_DSN` and `NEON_READONLY_DSN` only for scheduled database checks;
- set database sessions to read-only and use bounded statement, lock, and idle transaction timeouts;
- report proposed fixes with evidence, risk, effort, confidence, and verification steps;
- treat source-code fixes as LEVEL 2, requiring an explicit interactive request;
- treat architecture, dependency, database privilege, schema, index, migration, deployment, and destructive changes as LEVEL 3, requiring human approval.
