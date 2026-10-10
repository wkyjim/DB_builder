"""Phase A.2B -- migration/backfill idempotency + queue simulation tests.

Pure logic tests (no DB): they exercise the classification/backfill decision
functions that the SQL migration mirrors, and prove re-application is a no-op.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from db_builder import retry_policy as rp

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _backfill_class(reason: str):
    """Mirror of the migration's CASE expression (pure)."""
    if reason == "LEGACY_EOD_DEFINITION_UNRESOLVED":
        return rp.EXPECTED_FROZEN
    if reason in ("PRIOR_PROVENANCE_UNVERIFIED", "PRIOR_BEFORE_TRUST_START",
                  "PREVIOUS_VALUE_NOT_CONFIRMED"):
        return rp.PROVENANCE_UNVERIFIED
    if reason == "SOURCE_DEFINITION_UNRESOLVED":
        return rp.SOURCE_DEFINITION_UNRESOLVED
    if reason in ("WEEKEND_SESSION_REVIEW_REQUIRED", "PRIOR_SESSION_LOOKUP_FAILED",
                  "PRIOR_VALUE_LOOKUP_FAILED"):
        return rp.MANUAL_REVIEW_REQUIRED
    if reason.startswith(("GATE_ERROR:", "CALENDAR")):
        return rp.MANUAL_REVIEW_REQUIRED
    if reason.startswith("RECOVERY_FETCH_ERROR:"):
        return rp.RETRY_PATH_DEFECT
    if reason in rp.TRANSIENT_REASONS:
        return rp.BACKOFF_ACTIVE
    return None


def _backfill(row):
    """Apply the migration to one row dict in place. Returns changed flag."""
    if row.get("status") == "RESOLVED":
        return False
    if row.get("retry_class") is not None:
        return False                      # guarded: never overwrite
    cls = _backfill_class(row["reason"])
    row["retry_class"] = cls
    if cls == rp.BACKOFF_ACTIVE and row.get("next_retry_at") is None:
        row["next_retry_at"] = T0 + dt.timedelta(hours=1)
    if cls in (rp.MANUAL_REVIEW_REQUIRED, rp.SOURCE_DEFINITION_UNRESOLVED,
               rp.PROVENANCE_UNVERIFIED, rp.RETRY_PATH_DEFECT):
        row["manual_review_required"] = True
    return True


# ---------------------------------------------------------------- M1
def test_m1_backfill_classification_for_known_attempt_counts():
    """Migration result is deterministic for representative legacy counts."""
    cases = [
        ("LEGACY_EOD_DEFINITION_UNRESOLVED", 152, rp.EXPECTED_FROZEN),
        ("PREVIOUS_SESSION_NOT_CONFIRMED", 92, rp.BACKOFF_ACTIVE),
        ("PREVIOUS_SESSION_NOT_CONFIRMED", 84, rp.BACKOFF_ACTIVE),
        ("RECOVERY_FETCH_ERROR:ValueError", 82, rp.RETRY_PATH_DEFECT),
        ("RECOVERY_FETCH_ERROR:ValueError", 48, rp.RETRY_PATH_DEFECT),
        ("PRIOR_PROVENANCE_UNVERIFIED", 4, rp.PROVENANCE_UNVERIFIED),
    ]
    for reason, att, expected in cases:
        assert _backfill_class(reason) == expected, (reason, att)


# ---------------------------------------------------------------- M2
def test_m2_backfill_does_not_reset_attempt_count():
    row = {"reason": "PREVIOUS_SESSION_NOT_CONFIRMED", "attempt_count": 152,
           "status": "DEFERRED_HISTORY_UNRESOLVED", "retry_class": None,
           "next_retry_at": None, "manual_review_required": False}
    _backfill(row)
    assert row["attempt_count"] == 152, "attempt_count must never be reset"


# ---------------------------------------------------------------- M3
def test_m3_high_count_does_not_trigger_immediate_retry():
    """A legacy attempt_count of 152 must NOT be immediately retry-eligible."""
    row = {"reason": "PREVIOUS_SESSION_NOT_CONFIRMED", "attempt_count": 152,
           "status": "DEFERRED_HISTORY_UNRESOLVED", "retry_class": None,
           "next_retry_at": None, "manual_review_required": False}
    _backfill(row)
    # next_retry_at is one window in the future, not now/NULL.
    assert row["next_retry_at"] == T0 + dt.timedelta(hours=1)
    assert row["next_retry_at"] > T0


# ---------------------------------------------------------------- M4
def test_m4_idempotent_reapplication():
    """Applying the backfill twice changes nothing the second time."""
    rows = [
        {"reason": "LEGACY_EOD_DEFINITION_UNRESOLVED", "attempt_count": 152,
         "status": "DEFERRED_HISTORY_UNRESOLVED", "retry_class": None,
         "next_retry_at": None, "manual_review_required": False},
        {"reason": "PREVIOUS_SESSION_NOT_CONFIRMED", "attempt_count": 92,
         "status": "DEFERRED_HISTORY_UNRESOLVED", "retry_class": None,
         "next_retry_at": None, "manual_review_required": False},
        {"reason": "RECOVERY_FETCH_ERROR:ValueError", "attempt_count": 48,
         "status": "DEFERRED", "retry_class": None,
         "next_retry_at": None, "manual_review_required": False},
    ]
    for r in rows:
        _backfill(r)
    snapshot = [dict(r) for r in rows]
    for r in rows:
        assert _backfill(r) is False
    assert rows == snapshot, "second application must be a no-op"


# ---------------------------------------------------------------- M5
def test_m5_idempotent_does_not_overwrite_specific_class():
    """A more specific structural class is never overwritten by backfill."""
    row = {"reason": "RECOVERY_FETCH_ERROR:ValueError", "attempt_count": 82,
           "status": "DEFERRED", "retry_class": rp.PROVENANCE_UNVERIFIED,
           "next_retry_at": None, "manual_review_required": True}
    assert _backfill(row) is False
    assert row["retry_class"] == rp.PROVENANCE_UNVERIFIED


# ---------------------------------------------------------------- M6
def test_m6_expected_frozen_never_reactivated():
    row = {"reason": "LEGACY_EOD_DEFINITION_UNRESOLVED", "attempt_count": 152,
           "status": "DEFERRED_HISTORY_UNRESOLVED", "retry_class": None,
           "next_retry_at": None, "manual_review_required": False}
    _backfill(row)
    assert row["retry_class"] == rp.EXPECTED_FROZEN
    assert row["next_retry_at"] is None
    # Re-run must not reactivate.
    _backfill(row)
    assert row["retry_class"] == rp.EXPECTED_FROZEN
    assert row["next_retry_at"] is None


# ---------------------------------------------------------------- M7
def test_m7_manual_flags_not_reset():
    row = {"reason": "MANUAL_REASON", "attempt_count": 5,
           "status": "DEFERRED", "retry_class": rp.MANUAL_REVIEW_REQUIRED,
           "next_retry_at": None, "manual_review_required": True}
    _backfill(row)
    assert row["manual_review_required"] is True


# ---------------------------------------------------------------- precedence
def test_precedence_provenance_beats_incidental_fetch_error():
    """PROVENANCE_UNVERIFIED survives an incidental yfinance ValueError."""
    assert rp.resolve_retry_class(rp.PROVENANCE_UNVERIFIED,
                                  rp.RETRY_PATH_DEFECT) == rp.PROVENANCE_UNVERIFIED
    assert rp.resolve_retry_class(rp.RETRY_PATH_DEFECT,
                                  rp.PROVENANCE_UNVERIFIED) == rp.PROVENANCE_UNVERIFIED
    assert rp.resolve_retry_class(rp.EXPECTED_FROZEN,
                                  rp.BACKOFF_ACTIVE) == rp.EXPECTED_FROZEN
    assert rp.resolve_retry_class(None, rp.BACKOFF_ACTIVE) == rp.BACKOFF_ACTIVE


def test_provider_granularity_is_not_provider_wide():
    """Different endpoint families of one provider are distinct keys."""
    assert rp.provider_key("BZ=F") == (rp.PROVIDER_INVESTING,
                                       rp.DATA_CLASS_FUTURES)
    assert rp.provider_key("HK50") == (rp.PROVIDER_INVESTING,
                                       rp.DATA_CLASS_ASIA_INDEX)
    assert rp.provider_key("BZ=F") != rp.provider_key("HK50")


def test_access_denial_distinct_from_transient():
    assert rp.is_access_denial("HTTP 403 Forbidden")
    assert not rp.is_transient_provider_error("HTTP 403 Forbidden")
    assert rp.is_transient_provider_error("Connection reset")
    assert not rp.is_access_denial("Connection reset")
