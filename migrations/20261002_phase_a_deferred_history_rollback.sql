-- Phase A rollback: disable Phase A WITHOUT restoring the vulnerable writer.
--
-- !! THIS SCRIPT DOES NOT DROP public.macro_deferred_history !!
--
-- Dropping the queue would discard unresolved deferrals that are NOT
-- automatically rediscovered once the 5-day LOOKBACK window has passed.
-- Retain unresolved queue entries. Removing the table requires a separate
-- approved migration plan that first exports and preserves them:
--
--   psql "$DB" -c "\copy (SELECT * FROM public.macro_deferred_history
--        WHERE status <> 'RESOLVED') TO 'deferred_history_backup.csv' CSV HEADER"
--
-- ROLLBACK PROCEDURE (in order):
--
--   1. Revert code commit A-cal (calendar adapter + trading-day validity).
--      -> A-core protections REMAIN: no D-2 positional fallback, no live-as-EOD.
--
--   2. If A-core itself must be reverted, DO NOT restore the old unprotected
--      writer. Instead disable historical macro writes entirely and keep
--      macro_live serving. Ingestion stopping is the correct fail-closed state.
--
--   3. This SQL is a no-op placeholder that records the retained state.
--      It is intentionally non-destructive.
--
-- NEVER re-enable a historical writer that lacks:
--   * the EOD eligibility gate (session CONFIRMED_COMPLETED), and
--   * prior-session resolution (no positional D-2 fallback).
--
-- !! TRANSACTION NOTE !!
-- No BEGIN/COMMIT here, so this can be run inside a caller-controlled
-- transaction (dry runs) without committing. It is non-destructive.

-- Retention assertion: the queue must still exist with its rows intact.
DO $$
DECLARE
    remaining INTEGER;
BEGIN
    IF to_regclass('public.macro_deferred_history') IS NULL THEN
        RAISE EXCEPTION
            'macro_deferred_history is missing; unresolved deferrals may have '
            'been lost. Stop and restore from the pre-rollback export.';
    END IF;

    SELECT count(*) INTO remaining
      FROM public.macro_deferred_history
     WHERE status <> 'RESOLVED';

    RAISE NOTICE
        'Phase A rollback: queue retained with % unresolved entries. '
        'No historical writer has been re-enabled.', remaining;
END $$;

