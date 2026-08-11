# Read-Only Database Checks

- Server version and current database/user.
- Database size and connection utilization.
- Sessions over five minutes and idle-in-transaction sessions.
- Lock waits and blockers.
- Largest tables and indexes.
- Live/dead tuples and last vacuum/autovacuum/analyze timestamps.
- Sequential versus index scans by table.
- Index size and scan count; label low-use indexes as candidates only.
- `pg_stat_statements` availability and top total/mean-time queries when already enabled.
- Local versus Neon schema/table coverage using metadata only.

All queries must be bounded, read-only, and redact query text that could contain credentials.

