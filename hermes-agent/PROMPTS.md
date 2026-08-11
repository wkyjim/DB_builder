# Canonical Scheduled Prompts

## Quick Scan

Load the `db-builder-maintenance` skill. Operate at LEVEL 1 report-only. Announce each phase before using tools: repository boundaries and Git state; material changes since `hermes-agent/reports/state.md`; syntax/import/test/log/config and structure checks; read-only database checks only when dedicated DSNs exist; report updates. Do not modify application files or databases. Update `latest-maintenance-report.md`, `project-structure.md` only if materially changed, and `state.md`. Never include secrets. Finish with files inspected, checks run, findings, and report files updated.

## Daily Maintenance Audit

Load the `db-builder-maintenance` skill. Operate at LEVEL 1 report-only. Announce progress before each phase. Review current Git state, relevant code/configuration, safe existing tests, logs, dependency manifests, cross-project contracts, and read-only local/Neon database health when dedicated DSNs are configured. Write evidence-based P0-P3 findings to `latest-maintenance-report.md`, `database-health.md`, `improvement-backlog.md`, and `state.md`. No application edits, database writes, installs, deployments, commits, or pushes.

## Weekly Optimization Review

Load the `db-builder-maintenance` skill. Operate at LEVEL 1 report-only. Announce progress before each phase. Perform a senior-engineer review of architecture, project boundaries, PostgreSQL/Neon design, pipelines, APIs, code quality, reliability, security, observability, dependencies, technical debt, duplication, scalability, performance, resource use, simplification, and consolidation opportunities. Update `weekly-optimization-report.md`, `improvement-backlog.md`, `project-structure.md`, `database-health.md`, and `state.md`. Never make source or database changes.
