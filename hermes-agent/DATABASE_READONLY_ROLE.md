# Hermes Read-Only PostgreSQL Role

The intended monitoring role is `hermes_monitor`. Creating roles or changing grants is a human-approved security action. Review and execute the following separately for local PostgreSQL and Neon using an authorized administrator; replace database/schema names deliberately.

```sql
CREATE ROLE hermes_monitor LOGIN PASSWORD '<generate-and-store-privately>';
ALTER ROLE hermes_monitor SET default_transaction_read_only = on;
ALTER ROLE hermes_monitor SET statement_timeout = '30s';
ALTER ROLE hermes_monitor SET lock_timeout = '2s';
ALTER ROLE hermes_monitor SET idle_in_transaction_session_timeout = '30s';

GRANT CONNECT ON DATABASE us_equities_historical TO hermes_monitor;
GRANT USAGE ON SCHEMA public TO hermes_monitor;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO hermes_monitor;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO hermes_monitor;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO hermes_monitor;

GRANT pg_monitor TO hermes_monitor;
```

Store resulting DSNs only in `%LOCALAPPDATA%\hermes\.env` as `LOCAL_PG_READONLY_DSN` and `NEON_READONLY_DSN`. Do not reuse application owner credentials.

