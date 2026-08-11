# Database Policy

Scheduled checks must use dedicated read-only credentials. Start each session with `SET default_transaction_read_only = on`, `SET statement_timeout = '30s'`, `SET lock_timeout = '2s'`, and `SET idle_in_transaction_session_timeout = '30s'` where supported.

Allowed observations include connectivity, version, database/relation size, activity, locks, tuple statistics, vacuum/analyze state, index usage, sequential scans, and `pg_stat_statements` when already available.

Never run DDL, DML, migrations, index changes, `VACUUM FULL`, `REINDEX`, configuration changes, or extension installation. Recommendations only.

