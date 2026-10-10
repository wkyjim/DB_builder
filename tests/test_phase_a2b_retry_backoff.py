"""Phase A.2B -- retry / backoff isolation tests (B1-B15).

These tests exercise the shared retry policy and the memory store. No network,
no database, no production access. They assert that the retry layer only ever
SUPPRESSES fetches and never promotes a row toward a historical write.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# Isolated harness: the production module builds SQLAlchemy engines at import
# time. Point them at an in-process sqlite URL so no PostgreSQL connection is
# ever attempted and no production DB is touched.
os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")
os.environ.setdefault("LOCAL_DB_PASSWORD", "phase-a2b-test")
os.environ.setdefault("NEON_DB_PASSWORD", "phase-a2b-test")

from db_builder import retry_policy as rp
from db_builder.deferred_history import (
    MemoryDeferredStore, DeferredRow, DEFERRED, DEFERRED_HISTORY_UNRESOLVED,
)

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _row(symbol, d, reason, *, status=DEFERRED, attempt_count=1,
         retry_class=None, next_retry_at=None, manual_review_required=False):
    return DeferredRow(symbol=symbol, session_date=d, reason=reason,
                       status=status, attempt_count=attempt_count,
                       retry_class=retry_class, next_retry_at=next_retry_at,
                       manual_review_required=manual_review_required)


# ---------------------------------------------------------------- B1
def test_b1_first_transient_failure_retry_eligible_next_interval():
    """A first transient failure is retried at the next normal interval."""
    store = MemoryDeferredStore()
    store.record_deferral("ES=F", dt.date(2026, 10, 8),
                          "RECOVERY_DATA_STILL_MISSING", now=T0, attempted=True)
    rows = store.all_rows()
    assert rows[0].attempt_count == 1
    assert rows[0].retry_class == rp.BACKOFF_ACTIVE
    # Backoff for attempt 1 is 1 hour -> eligible again one hour later.
    later = T0 + dt.timedelta(hours=1, seconds=1)
    due = store.due_candidates(now=later)
    assert [r.symbol for r in due] == ["ES=F"]


# ---------------------------------------------------------------- B2
def test_b2_repeated_transient_backoff_increases_deterministically():
    """Backoff grows deterministically with the attempt count."""
    assert rp.backoff_seconds(1) == 3600
    assert rp.backoff_seconds(2) == 3600
    assert rp.backoff_seconds(3) == 3 * 3600
    assert rp.backoff_seconds(5) == 3 * 3600
    assert rp.backoff_seconds(6) == 6 * 3600
    assert rp.backoff_seconds(10) == 6 * 3600
    assert rp.backoff_seconds(11) == 12 * 3600
    assert rp.backoff_seconds(21) == 24 * 3600
    assert rp.backoff_seconds(999) == rp.BACKOFF_CAP_SECONDS
    # Monotonic non-decreasing.
    seq = [rp.backoff_seconds(n) for n in range(1, 40)]
    assert all(b >= a for a, b in zip(seq, seq[1:]))


# ---------------------------------------------------------------- B3
def test_b3_backoff_active_no_provider_call_attempt_unchanged():
    """While in backoff, the row is not eligible and attempt_count is frozen."""
    store = MemoryDeferredStore()
    store.record_deferral("ES=F", dt.date(2026, 10, 8),
                          "RECOVERY_DATA_STILL_MISSING", now=T0, attempted=True)
    before = store.all_rows()[0].attempt_count
    # 30 minutes later: still inside the 1h backoff window.
    due = store.due_candidates(now=T0 + dt.timedelta(minutes=30))
    assert due == []
    after = store.all_rows()[0].attempt_count
    assert after == before == 1


# ---------------------------------------------------------------- B4
def test_b4_backoff_expires_exactly_one_retry_increment():
    """Once backoff expires the row is retried and attempt_count increments
    exactly once."""
    store = MemoryDeferredStore()
    store.record_deferral("ES=F", dt.date(2026, 10, 8),
                          "RECOVERY_DATA_STILL_MISSING", now=T0, attempted=True)
    later = T0 + dt.timedelta(hours=1, seconds=1)
    due = store.due_candidates(now=later)
    assert len(due) == 1
    # Simulate the single recovery attempt failing again.
    store.record_deferral("ES=F", dt.date(2026, 10, 8),
                          "RECOVERY_DATA_STILL_MISSING", now=later,
                          attempted=True)
    assert store.all_rows()[0].attempt_count == 2
    # Next retry uses the attempt-2 delay (still 1h).
    assert store.all_rows()[0].next_retry_at == later + dt.timedelta(hours=1)


# ---------------------------------------------------------------- B5
def test_b5_provider_outage_suppresses_all_shared_symbols():
    """A provider outage suppresses every symbol that shares the provider."""
    rows = [_row("BZ=F", dt.date(2026, 10, 8), "RECOVERY_DATA_STILL_MISSING",
                 retry_class=rp.RETRY_ELIGIBLE),
            _row("CL=F", dt.date(2026, 10, 8), "RECOVERY_DATA_STILL_MISSING",
                 retry_class=rp.RETRY_ELIGIBLE)]
    eligible = rp.filter_eligible(rows, T0, provider_available=False)
    assert eligible == []


# ---------------------------------------------------------------- B6
def test_b6_provider_recovery_resumes_retry():
    """When the provider recovers, eligible rows resume."""
    rows = [_row("BZ=F", dt.date(2026, 10, 8), "RECOVERY_DATA_STILL_MISSING",
                 retry_class=rp.RETRY_ELIGIBLE)]
    assert rp.filter_eligible(rows, T0, provider_available=True) != []
    assert rp.filter_eligible(rows, T0, provider_available=False) == []


# ---------------------------------------------------------------- B7
def test_b7_source_definition_unresolved_no_hourly_retry():
    """SOURCE_DEFINITION_UNRESOLVED is never time-retried."""
    store = MemoryDeferredStore()
    store.record_deferral("HK50", dt.date(2026, 10, 8),
                          "SOURCE_DEFINITION_UNRESOLVED", now=T0, attempted=False)
    row = store.all_rows()[0]
    assert row.retry_class == rp.SOURCE_DEFINITION_UNRESOLVED
    assert row.next_retry_at is None
    assert row.attempt_count == 0  # no real attempt
    for hours in (1, 24, 24 * 30):
        assert store.due_candidates(now=T0 + dt.timedelta(hours=hours)) == []


# ---------------------------------------------------------------- B8
def test_b8_provenance_unverified_no_hourly_retry():
    """PROVENANCE_UNVERIFIED is never time-retried."""
    store = MemoryDeferredStore()
    store.record_deferral("ES=F", dt.date(2026, 10, 8),
                          "PRIOR_PROVENANCE_UNVERIFIED", now=T0, attempted=False)
    row = store.all_rows()[0]
    assert row.retry_class == rp.PROVENANCE_UNVERIFIED
    assert store.due_candidates(now=T0 + dt.timedelta(hours=48)) == []
    assert row.attempt_count == 0


# ---------------------------------------------------------------- B9
def test_b9_expected_frozen_no_retry_no_increment():
    """EXPECTED_FROZEN never retries and never increments attempt_count."""
    store = MemoryDeferredStore()
    store.record_deferral("US10YT=X", dt.date(2026, 10, 8),
                          "LEGACY_EOD_DEFINITION_UNRESOLVED", now=T0,
                          attempted=False)
    row = store.all_rows()[0]
    assert row.retry_class == rp.EXPECTED_FROZEN
    assert row.attempt_count == 0
    assert row.next_retry_at is None
    assert store.due_candidates(now=T0 + dt.timedelta(hours=100)) == []


# ---------------------------------------------------------------- B10
def test_b10_dependent_blocked_by_prerequisite():
    """D does not retry while its earlier prerequisite P is blocked."""
    P = dt.date(2026, 10, 6)
    D = dt.date(2026, 10, 7)
    rows = [
        _row("ES=F", P, "PRIOR_PROVENANCE_UNVERIFIED",
             retry_class=rp.PROVENANCE_UNVERIFIED),
        _row("ES=F", D, "PREVIOUS_SESSION_NOT_CONFIRMED",
             retry_class=rp.RETRY_ELIGIBLE, next_retry_at=None),
    ]
    eligible = rp.filter_eligible(rows, T0)
    assert eligible == [], "D must not retry while P is structurally blocked"
    # If P is merely in backoff (not structural), D is still suppressed while
    # P cannot resolve this cycle.
    rows[0].retry_class = rp.BACKOFF_ACTIVE
    rows[0].next_retry_at = T0 + dt.timedelta(hours=5)
    assert rp.filter_eligible(rows, T0) == []


# ---------------------------------------------------------------- B11
def test_b11_degraded_success_preserved():
    """Backoff is a retry concern only; it must not itself become a fatal
    condition. The three-state exit model is unchanged by A.2B."""
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "scripts",
                        "macro_data_fetch.py")
    spec = importlib.util.spec_from_file_location("mdf_a2b", path)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except Exception as exc:  # pragma: no cover - import env dependent
        pytest.skip(f"macro_data_fetch import unavailable: {exc}")
    # A DEGRADED_SUCCESS summary (coverage satisfied, no fatal) exits 0.
    class _S:
        coverage_satisfied = True
        required_failed_symbols = []
        outcomes = []
    assert m._phase_a_safety_exit_code(_S()) == 0


# ---------------------------------------------------------------- B12
def test_b12_required_live_failure_still_fatal():
    """Backoff must not hide a genuine required live-symbol failure."""
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "scripts",
                        "macro_data_fetch.py")
    spec = importlib.util.spec_from_file_location("mdf_a2b2", path)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"macro_data_fetch import unavailable: {exc}")

    class _Out:
        def __init__(self, required):
            self.required = required

    class _S:
        coverage_satisfied = True
        required_failed_symbols = ["^GSPC"]
        outcomes = [_Out(True), _Out(True)]
    assert m._phase_a_safety_exit_code(_S()) == 1


# ---------------------------------------------------------------- B13
def test_b13_db_persistence_failure_still_fatal():
    """A deferred-store persistence failure remains a fatal condition."""
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "scripts",
                        "macro_data_fetch.py")
    spec = importlib.util.spec_from_file_location("mdf_a2b3", path)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"macro_data_fetch import unavailable: {exc}")
    m._PIPELINE_FATAL_CONDITIONS.clear()
    m._PIPELINE_FATAL_CONDITIONS.append("DEFERRED_STORE_PERSISTENCE_FAILURE")

    class _S:
        coverage_satisfied = True
        required_failed_symbols = []
        outcomes = []
    try:
        assert m._phase_a_safety_exit_code(_S()) == 1
    finally:
        m._PIPELINE_FATAL_CONDITIONS.clear()


# ---------------------------------------------------------------- B14
def test_b14_restart_persistence_of_next_retry_at():
    """next_retry_at is persisted data, not in-process state: a fresh store
    rebuilt from the same rows yields the same eligibility."""
    d = dt.date(2026, 10, 8)
    nra = rp.next_retry_at(T0, 3)  # 3h
    row = _row("ES=F", d, "RECOVERY_DATA_STILL_MISSING",
               retry_class=rp.BACKOFF_ACTIVE, next_retry_at=nra,
               attempt_count=3)
    # "Restart": same persisted row, new process/now.
    assert rp.is_retry_eligible(row, T0 + dt.timedelta(hours=1)) is False
    assert rp.is_retry_eligible(row, T0 + dt.timedelta(hours=3, seconds=1)) is True


# ---------------------------------------------------------------- B15
def test_b15_manual_reset_only_when_intended():
    """A structural/manual row is retry-eligible again only after an explicit
    reset; EXPECTED_FROZEN is not resettable by the generic path."""
    store = MemoryDeferredStore()
    store.record_deferral("ES=F", dt.date(2026, 10, 8),
                          "PRIOR_PROVENANCE_UNVERIFIED", now=T0, attempted=False)
    assert store.due_candidates(now=T0 + dt.timedelta(days=2)) == []
    assert store.release_on_condition_change("ES=F", dt.date(2026, 10, 8),
                                             now=T0) is True
    assert store.due_candidates(now=T0) != []

    # EXPECTED_FROZEN is not released by the generic condition path.
    store.record_deferral("US10YT=X", dt.date(2026, 10, 8),
                          "LEGACY_EOD_DEFINITION_UNRESOLVED", now=T0,
                          attempted=False)
    assert store.release_on_condition_change("US10YT=X", dt.date(2026, 10, 8),
                                             now=T0) is False
    assert store.due_candidates(now=T0 + dt.timedelta(days=2)) == [] or \
        all(r.symbol != "US10YT=X" for r in store.due_candidates(
            now=T0 + dt.timedelta(days=2)))


# ---------------------------------------------------------------- extra
def test_extra_unknown_reason_fails_safe_to_manual():
    """An unclassified reason never churns hourly."""
    assert rp.classify_reason("SOMETHING_NEW") == rp.MANUAL_REVIEW_REQUIRED
    assert rp.is_known_reason("SOMETHING_NEW") is False
    assert rp.is_known_reason("PREVIOUS_SESSION_NOT_CONFIRMED") is True


def test_extra_transient_vs_deterministic_fetch_error():
    """Transient exception types back off; deterministic ones are a fixable
    retry-path defect (not a manual decision)."""
    assert rp.classify_reason("RECOVERY_FETCH_ERROR:Timeout") == rp.BACKOFF_ACTIVE
    assert rp.classify_reason("RECOVERY_FETCH_ERROR:ValueError") == \
        rp.RETRY_PATH_DEFECT


def test_extra_resolved_rows_never_eligible():
    store = MemoryDeferredStore()
    store.record_deferral("ES=F", dt.date(2026, 10, 8),
                          "RECOVERY_DATA_STILL_MISSING", now=T0, attempted=True)
    store.record_success("ES=F", dt.date(2026, 10, 8), now=T0)
    assert store.due_candidates(now=T0 + dt.timedelta(hours=10)) == []
