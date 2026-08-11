# Recovery Runbook

1. Pause automation: `hermes pause` or stop the gateway.
2. Inspect `hermes cron status`, `hermes cron runs`, and `%LOCALAPPDATA%\hermes\logs`.
3. Verify Ollama with `ollama list`, `ollama ps`, and a direct local inference.
4. Verify Hermes with `hermes doctor`, `hermes status`, and `hermes config get model --json`.
5. Confirm scheduled jobs remain report-only and use the DB_builder workdir.
6. If a report is corrupt, preserve it and regenerate; do not delete source/application data.
7. Never resolve an incident by switching to a paid/cloud model without explicit approval.

