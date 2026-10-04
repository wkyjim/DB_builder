-- Phase A migration: persistent deferred-history queue
-- Target: public.macro_deferred_history
--
-- Idempotent. Adds a NEW table only. It does not read, write, alter or delete
-- any row in public.macro or public.macro_live.
--
-- !! TRANSACTION NOTE !!
-- This file intentionally contains NO BEGIN/COMMIT. Wrapping the DDL in its own
-- BEGIN/COMMIT makes the script commit immediately, so a caller that runs it
-- inside a larger transaction (e.g. a dry run expecting to ROLLBACK) would
-- instead create the table permanently.
-- Run it either:
--   * inside an explicit transaction you control (recommended for dry runs):
--       psql "$DB" -v ON_ERROR_STOP=1 -1 -f 20261002_phase_a_deferred_history.sql
--   * or as a standalone script; PostgreSQL treats each statement atomically
--     and CREATE TABLE / CREATE INDEX IF NOT EXISTS are idempotent.
--
-- Apply (isolated/test DB first, then production at the approved gate):
--   psql "$DB" -v ON_ERROR_STOP=1 -1 -f 20261002_phase_a_deferred_history.sql
--
-- Rollback: 20261002_phase_a_deferred_history_rollback.sql (retains the queue)

CREATE TABLE IF NOT EXISTS public.macro_deferred_history (
    symbol          TEXT        NOT NULL,
    session_date    DATE        NOT NULL,
    reason          TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'DEFERRED',
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempt    TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempt_count   INTEGER     NOT NULL DEFAULT 0,
    resolved_at     TIMESTAMPTZ,
    PRIMARY KEY (symbol, session_date),
    CONSTRAINT macro_deferred_history_status_chk
        CHECK (status IN ('DEFERRED', 'RESOLVED', 'DEFERRED_HISTORY_UNRESOLVED'))
);

-- Retry scans are "oldest unresolved first, bounded".
CREATE INDEX IF NOT EXISTS idx_macro_deferred_history_due
    ON public.macro_deferred_history (session_date, symbol)
    WHERE status = 'DEFERRED';

-- Escalation / operational reporting.
CREATE INDEX IF NOT EXISTS idx_macro_deferred_history_status
    ON public.macro_deferred_history (status, session_date);

COMMENT ON TABLE public.macro_deferred_history IS
    'Phase A: completed historical bars deferred because their prior valid '
    'session was not yet confirmed. Bounded retry queue; independent of the '
    '5-day ingestion lookback. Escalates to DEFERRED_HISTORY_UNRESOLVED but '
    'rows are never deleted by the pipeline.';
