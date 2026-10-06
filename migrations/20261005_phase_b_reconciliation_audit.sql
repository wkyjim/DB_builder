-- Phase B migration: append-only reconciliation audit trail
-- Target: public.macro_reconciliation_audit
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
--       psql "$DB" -v ON_ERROR_STOP=1 -1 -f <this_file>
--   * or as a standalone script; PostgreSQL treats each statement atomically
--       and CREATE TABLE / CREATE INDEX IF NOT EXISTS are idempotent.
--
-- Apply (isolated/test DB first, then production at the approved gate):
--   psql "$DB" -v ON_ERROR_STOP=1 -1 -f <this_file>
--
-- Rollback: <date>_phase_b_reconciliation_audit_rollback.sql (retains evidence)

CREATE TABLE IF NOT EXISTS public.macro_reconciliation_audit (
    audit_id                    SERIAL       PRIMARY KEY,
    run_id                      TEXT         NOT NULL,
    symbol                      TEXT         NOT NULL,
    session_date                DATE         NOT NULL,
    verdict                     TEXT         NOT NULL,
    field                       TEXT         NOT NULL DEFAULT '',
    old_value                   NUMERIC(20,4),
    new_value                   NUMERIC(20,4),
    source_definition           TEXT         NOT NULL DEFAULT '',
    confirmation_source         TEXT         NOT NULL DEFAULT '',
    confirmation_timestamp      TIMESTAMPTZ,
    reason                      TEXT         NOT NULL DEFAULT '',
    repaired_at                 TIMESTAMPTZ,
    expected_previous_session   DATE,
    source_observation_ts       TIMESTAMPTZ,
    correction_txn_id           BIGINT,
    row_role                    TEXT         NOT NULL DEFAULT '',
    release_version             TEXT         NOT NULL DEFAULT '',
    event_type                  TEXT         NOT NULL DEFAULT 'DETECTION',

    -- Verdict constraint: all approved Phase B verdict values
    CONSTRAINT macro_reconciliation_audit_verdict_chk CHECK (verdict IN (
        'MATCH',
        'ROUNDING_ONLY',
        'CORRECTION_REQUIRED',
        'SOURCE_DEFINITION_MISMATCH',
        'CONFIRMATION_UNAVAILABLE',
        'LEGACY_SOURCE_DEFINITION_MIXED',
        'MANUAL_REVIEW_REQUIRED',
        'STALE_PLAN',
        'DEPENDENCY_RECONCILE_REQUIRED',
        'SOURCE_DEFINITION_UNRESOLVED'
    )),

    -- Event type constraint: REPAIR or DETECTION only
    CONSTRAINT macro_reconciliation_audit_event_type_chk CHECK (event_type IN (
        'REPAIR',
        'DETECTION'
    )),

    -- Row role constraint
    CONSTRAINT macro_reconciliation_audit_row_role_chk CHECK (row_role IN (
        '',
        'D',
        'D1'
    )),

    -- REPAIR events must have repaired_at set
    CONSTRAINT macro_reconciliation_audit_repair_timing_chk CHECK (
        (event_type = 'REPAIR' AND repaired_at IS NOT NULL)
        OR (event_type = 'DETECTION' AND repaired_at IS NULL)
    ),

    -- DETECTION events must not have correction_txn_id
    CONSTRAINT macro_reconciliation_audit_detection_txn_chk CHECK (
        (event_type = 'DETECTION' AND correction_txn_id IS NULL)
        OR event_type = 'REPAIR'
    )
);

-- Index for run-based queries
CREATE INDEX IF NOT EXISTS idx_macro_reconciliation_audit_run
    ON public.macro_reconciliation_audit (run_id);

-- Index for symbol/session lookups
CREATE INDEX IF NOT EXISTS idx_macro_reconciliation_audit_symbol_session
    ON public.macro_reconciliation_audit (symbol, session_date);

-- Index for event type filtering
CREATE INDEX IF NOT EXISTS idx_macro_reconciliation_audit_event_type
    ON public.macro_reconciliation_audit (event_type);

-- Append-only enforcement: reject UPDATE and DELETE at the database level
CREATE OR REPLACE FUNCTION macro_reconciliation_audit_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'macro_reconciliation_audit is append-only: % is not allowed', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_macro_reconciliation_audit_append_only
    ON public.macro_reconciliation_audit;

CREATE TRIGGER trg_macro_reconciliation_audit_append_only
    BEFORE UPDATE OR DELETE ON public.macro_reconciliation_audit
    FOR EACH ROW EXECUTE FUNCTION macro_reconciliation_audit_append_only();

COMMENT ON TABLE public.macro_reconciliation_audit IS
    'Phase B: append-only reconciliation audit trail. REPAIR events are written '
    'in the same transaction as the correction; DETECTION events are written '
    'independently after a no-write verdict or after a failed repair transaction '
    'has rolled back. No UPDATE or DELETE path exists; enforced by trigger.';
