# Schedule

Hermes uses the local Windows timezone (Asia/Hong_Kong system setting).

| Job | Schedule | Workdir | Delivery | Mode |
|---|---|---|---|---|
| DB Builder Quick Scan | `0 */4 * * *` | DB_builder root | local | Report-only |
| DB Builder Daily Audit | `0 2 * * *` | DB_builder root | local | Report-only |
| DB Builder Weekly Optimization | `0 3 * * 0` | DB_builder root | local | Report-only |

Each job must explicitly pin provider `custom`, the local Qwen model, and the `db-builder-maintenance` skill. The gateway scheduler must be running.

Every scheduled execution is recorded in Hermes durable cron history. Inspect it with:

```powershell
hermes cron runs --limit 50
```

Manual runs use `scripts/run-maintenance.ps1` and are mirrored to timestamped files under `logs/`. No wrapper-level wall-clock timeout is applied.
