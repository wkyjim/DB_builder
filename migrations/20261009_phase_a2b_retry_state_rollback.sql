-- Phase A.2B rollback: retry/backoff state.
--
-- Caller-managed transaction: NO internal BEGIN/COMMIT.
--
-- FAIL-CLOSED BY DESIGN. A.2B is an operational migration: once active, the
-- old code path (unconditional hourly retry of every DEFERRED row) recreates
-- the retry storm this phase exists to stop. Dropping the state columns while
-- provider outages or structural rows are still live would silently restore
-- that churning behaviour with no record of why. The rollback therefore
-- refuses unless the operator first drains the state.
--
-- Refuses when ANY of:
--   * a live (unexpired) provider outage exists;
--   * any row is in MANUAL_REVIEW_REQUIRED (needs a human decision);
--   * any row is in RETRY_PATH_DEFECT (needs the code fix).
--
-- Note: even after a clean rollback the OLD code cannot be restored safely
-- without recreating hourly churn; see the A.2B deployment notes. Treat this
-- as a FORWARD-ONLY operational migration once activated.

DO $$
DECLARE
    live_outages integer;
    manual_rows integer;
    defect_rows integer;
BEGIN
    SELECT COUNT(*) INTO live_outages
      FROM public.macro_provider_backoff
     WHERE state IN ('PROVIDER_UNAVAILABLE', 'MANUAL_REVIEW_REQUIRED')
       AND (unavailable_until IS NULL OR unavailable_until > now()
            OR state = 'MANUAL_REVIEW_REQUIRED');
    SELECT COUNT(*) INTO manual_rows
      FROM public.macro_deferred_history
     WHERE status <> 'RESOLVED' AND retry_class = 'MANUAL_REVIEW_REQUIRED';
    SELECT COUNT(*) INTO defect_rows
      FROM public.macro_deferred_history
     WHERE status <> 'RESOLVED' AND retry_class = 'RETRY_PATH_DEFECT';

    IF live_outages > 0 THEN
        RAISE EXCEPTION
            'REFUSING ROLLBACK: % live provider outage(s) recorded; '
            'resolve or clear them before rolling back A.2B', live_outages;
    END IF;
    IF manual_rows > 0 THEN
        RAISE EXCEPTION
            'REFUSING ROLLBACK: % row(s) require manual review; '
            'resolve them before rolling back A.2B', manual_rows;
    END IF;
    IF defect_rows > 0 THEN
        RAISE EXCEPTION
            'REFUSING ROLLBACK: % RETRY_PATH_DEFECT row(s) pending the code '
            'fix; rolling back would recreate the retry storm', defect_rows;
    END IF;
END $$;

DROP TABLE IF EXISTS public.macro_provider_backoff;
DROP INDEX IF EXISTS public.macro_deferred_history_retry_idx;

ALTER TABLE public.macro_deferred_history
    DROP CONSTRAINT IF EXISTS macro_deferred_history_retry_class_chk;

ALTER TABLE public.macro_deferred_history
    DROP COLUMN IF EXISTS manual_review_required,
    DROP COLUMN IF EXISTS last_error_class,
    DROP COLUMN IF EXISTS next_retry_at,
    DROP COLUMN IF EXISTS retry_class;
