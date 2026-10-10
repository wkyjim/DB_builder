-- Phase A.2B -- deferred-history retry/backoff state (additive only).
--
-- Caller-managed transaction: NO internal BEGIN/COMMIT.
--
-- Adds retry-policy columns to the existing queue and a provider-level backoff
-- table keyed by (provider, data_class) so N symbols sharing one broken
-- endpoint family do not each issue a redundant request, while a different
-- endpoint family of the same provider keeps working.
--
-- IDEMPOTENT: safe to re-run. Every statement is guarded; the backfill only
-- touches rows whose retry_class IS NULL and never mutates attempt_count,
-- next_retry_at of already-classified rows, or manual-review flags.

ALTER TABLE public.macro_deferred_history
    ADD COLUMN IF NOT EXISTS retry_class text,
    ADD COLUMN IF NOT EXISTS next_retry_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_error_class text,
    ADD COLUMN IF NOT EXISTS manual_review_required boolean NOT NULL DEFAULT false;

ALTER TABLE public.macro_deferred_history
    DROP CONSTRAINT IF EXISTS macro_deferred_history_retry_class_chk;
ALTER TABLE public.macro_deferred_history
    ADD CONSTRAINT macro_deferred_history_retry_class_chk
    CHECK (retry_class IS NULL OR retry_class IN (
        'RETRY_ELIGIBLE',
        'BACKOFF_ACTIVE',
        'PROVIDER_UNAVAILABLE',
        'SOURCE_DEFINITION_UNRESOLVED',
        'PROVENANCE_UNVERIFIED',
        'EXPECTED_FROZEN',
        'RETRY_PATH_DEFECT',
        'MANUAL_REVIEW_REQUIRED'
    ));

CREATE INDEX IF NOT EXISTS macro_deferred_history_retry_idx
    ON public.macro_deferred_history (retry_class, next_retry_at)
    WHERE status <> 'RESOLVED';

-- Provider-level backoff, keyed by (provider, data_class).
CREATE TABLE IF NOT EXISTS public.macro_provider_backoff (
    provider             text NOT NULL,
    data_class           text NOT NULL DEFAULT '',
    state                text NOT NULL,
    consecutive_failures integer NOT NULL DEFAULT 0,
    last_failure_at      timestamptz,
    unavailable_until    timestamptz,
    last_error_class     text,
    last_http_status     integer,
    recovered_at         timestamptz,
    updated_at           timestamptz NOT NULL,
    PRIMARY KEY (provider, data_class),
    CONSTRAINT macro_provider_backoff_state_chk
        CHECK (state IN ('AVAILABLE', 'PROVIDER_UNAVAILABLE',
                         'MANUAL_REVIEW_REQUIRED'))
);

-- Idempotent backfill: classify ONLY un-migrated rows. A more specific
-- structural class is never overwritten (guarded by retry_class IS NULL).
-- attempt_count is deliberately untouched. next_retry_at is set ONLY for
-- transient rows so a high legacy attempt_count does not immediately trigger
-- a provider call: the first retry is scheduled one full backoff window out.
--
-- Futures with RECOVERY_FETCH_ERROR:% are reclassified to PROVENANCE_UNVERIFIED
-- (their real blocker is the absent provenance anchor, not the routing defect).
-- BZ=F/CL=F are Investing.com and keep their existing classification.
UPDATE public.macro_deferred_history
   SET retry_class = CASE
        WHEN reason = 'LEGACY_EOD_DEFINITION_UNRESOLVED'
            THEN 'EXPECTED_FROZEN'
        WHEN reason IN ('PRIOR_PROVENANCE_UNVERIFIED',
                        'PRIOR_BEFORE_TRUST_START',
                        'PREVIOUS_VALUE_NOT_CONFIRMED')
            THEN 'PROVENANCE_UNVERIFIED'
        WHEN reason = 'SOURCE_DEFINITION_UNRESOLVED'
            THEN 'SOURCE_DEFINITION_UNRESOLVED'
        WHEN reason IN ('WEEKEND_SESSION_REVIEW_REQUIRED',
                        'PRIOR_SESSION_LOOKUP_FAILED',
                        'PRIOR_VALUE_LOOKUP_FAILED')
            THEN 'MANUAL_REVIEW_REQUIRED'
        WHEN reason LIKE 'GATE_ERROR:%' OR reason LIKE 'CALENDAR%'
            THEN 'MANUAL_REVIEW_REQUIRED'
        WHEN reason LIKE 'RECOVERY_FETCH_ERROR:%'
             AND symbol LIKE '%=F'
             AND symbol NOT IN ('BZ=F', 'CL=F')
            THEN 'PROVENANCE_UNVERIFIED'
        WHEN reason LIKE 'RECOVERY_FETCH_ERROR:%'
            THEN 'RETRY_PATH_DEFECT'
        WHEN reason IN ('CLOSED_UNCONFIRMED',
                        'PREVIOUS_SESSION_NOT_CONFIRMED',
                        'PREVIOUS_SESSION_OFFICIAL_NOT_CONFIRMED',
                        'TREASURY_PUBLICATION_NOT_CONFIRMED',
                        'RECOVERY_DATA_STILL_MISSING',
                        'RECOVERY_PRIOR_STILL_UNCONFIRMED',
                        'CMT_VALUE_NOT_OFFICIAL',
                        'CMT_PREVIOUS_VALUE_NOT_OFFICIAL')
            THEN 'BACKOFF_ACTIVE'
        ELSE NULL
   END
 WHERE status <> 'RESOLVED'
   AND retry_class IS NULL;

-- Stamp the known-fixed defect identifier on RETRY_PATH_DEFECT rows so the
-- runtime can release them for bounded re-evaluation after deployment.
UPDATE public.macro_deferred_history
   SET last_error_class = 'RECOVERY_ROUTING_V1_OIL_FALLTHROUGH'
 WHERE status <> 'RESOLVED'
   AND retry_class = 'RETRY_PATH_DEFECT'
   AND last_error_class IS NULL;

-- Deterministic first post-deployment retry time for transient rows only.
-- Uses a fixed backoff window so a legacy attempt_count of 152 does not cause
-- an immediate fetch; no time-based retry is set for structural/frozen/defect.
UPDATE public.macro_deferred_history
   SET next_retry_at = now() + interval '1 hour'
 WHERE status <> 'RESOLVED'
   AND retry_class = 'BACKOFF_ACTIVE'
   AND next_retry_at IS NULL;

-- Structural / frozen / manual rows are never retry-eligible on time.
-- RETRY_PATH_DEFECT rows with a known-fixed defect identifier are NOT marked
-- manual_review_required; they are released for bounded re-evaluation.
UPDATE public.macro_deferred_history
   SET manual_review_required = true
 WHERE status <> 'RESOLVED'
   AND retry_class IN ('MANUAL_REVIEW_REQUIRED',
                       'SOURCE_DEFINITION_UNRESOLVED',
                       'PROVENANCE_UNVERIFIED')
   AND manual_review_required = false;

-- Known-fixed-defect rows: manual_review_required = false (released).
UPDATE public.macro_deferred_history
   SET manual_review_required = false
 WHERE status <> 'RESOLVED'
   AND retry_class = 'RETRY_PATH_DEFECT'
   AND last_error_class = 'RECOVERY_ROUTING_V1_OIL_FALLTHROUGH'
   AND manual_review_required = true;
