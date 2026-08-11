# DB_builder Hermes Maintenance Agent

Local, report-only maintenance automation for the complete `DB_builder` ecosystem. Hermes uses Ollama at `http://127.0.0.1:11434/v1`. Qwen3 14B remains installed for direct analysis; Hermes uses the compatible `qwen3:4b-hermes` 64K profile because Hermes 0.20 requires at least 64K context.

Scheduled jobs may inspect code, Git, tests, logs, architecture, and read-only PostgreSQL statistics. They may update only `hermes-agent/reports/`. Application edits, database writes, migrations, deployments, commits, and pushes require explicit human approval.

Key files:
- `PROJECT_REGISTRY.md`: project and dependency inventory.
- `PROMPTS.md`: canonical scheduled prompts.
- `SCHEDULE.md`: job definitions.
- `DATABASE_READONLY_ROLE.md`: role setup SQL and operator steps.
- `skills/db-builder-maintenance/SKILL.md`: reusable maintenance workflow.
- `reports/`: latest audit outputs and state.

The private Hermes runtime lives at `%LOCALAPPDATA%\hermes`. Do not commit its `.env` or database DSNs.

## Run And Observe

Run an unlimited Quick Scan from PowerShell:

```powershell
Set-Location C:\Users\User\OneDrive\Coding\DB_builder
.\hermes-agent\scripts\run-maintenance.ps1 -Job quick
```

The command streams Hermes tool previews and writes a permanent timestamped log under `hermes-agent\logs\`. During quiet model-inference periods it prints and records a heartbeat every 30 seconds with total elapsed time. It has no wrapper-level wall-clock cutoff. The local model request allowance is increased to 24 hours per model turn so slow CPU inference is not mistaken for a dead process.

Follow the latest run from a second PowerShell window:

```powershell
Set-Location C:\Users\User\OneDrive\Coding\DB_builder
.\hermes-agent\scripts\watch-latest-run.ps1
```

Use `-Job daily` or `-Job weekly` for deeper audits. Scheduled Hermes jobs retain durable execution records available through `hermes cron runs --limit 50`.
