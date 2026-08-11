## Hermes scheduled maintenance boundary

Treat DB_builder as one connected ecosystem. Scheduled maintenance is LEVEL 1 report-only. Inspect relevant child projects, use read-only database credentials, protect secrets and existing Git changes, and write only under `hermes-agent/reports/`. Never deploy, push, install dependencies, modify application source, alter schemas/indexes, or write production data without explicit human approval.

