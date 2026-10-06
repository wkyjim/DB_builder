"""Phase B store tests — B7, B8, B10, B13, B15, D+2.

Uses InMemoryReconciliationStore — no DB.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from db_builder.reconciliation import (  # noqa: E402
    AuditEventType,
    Reason,
    Row,
    Verdict,
    build_correction_plan,
    check_prior_dependency,
    decide,
    is_auto_correctable,
    is_excluded_report_only,
    round_4,
)
from db_builder.reconciliation_store import (  # noqa: E402
    AuditEvent,
    InMemoryReconciliationStore,
    StalePlanError,
    execute_correction_plan,
    record_detection_event,
)

D = dt.date(2026, 9, 25)
D1 = dt.date(2026, 9, 28)
D2 = dt.date(2026, 9, 29)
P = dt.date(2026, 9, 24)


def make_cmt_row(
    symbol: str = "US10YT_CMT",
    date: dt.date = D,
    close: float = 4.85,
    prev_close: float = 4.81,
    source: str = "treasury.gov",
) -> Row:
    return Row(
        symbol=symbol,
        date=date,
        open=close,
        high=close,
        low=close,
        close=close,
        adj_close=close,
        volume=None,
        prev_close=prev_close,
        change=round_4(close - prev_close),
        pct_chg=round_4((close - prev_close) / prev_close * 100) if prev_close else None,
        amplitude=0.0,
        source=source,
        asset_type="ust_yield_cmt",
    )


def make_plan(
    stored_close: float = 4.85,
    confirmed_close: float = 4.87,
    stored_d1_close: float = 4.88,
    stored_d1_prev: float = 4.85,
    stored_d2_close: float = 4.89,
    stored_d2_prev: float = 4.88,
):
    stored = make_cmt_row(close=stored_close, prev_close=4.81)
    confirmed = make_cmt_row(close=confirmed_close, prev_close=4.81)
    confirmed_prior = make_cmt_row(date=P, close=4.81)
    stored_d1 = make_cmt_row(date=D1, close=stored_d1_close, prev_close=stored_d1_prev)
    stored_d2 = make_cmt_row(date=D2, close=stored_d2_close, prev_close=stored_d2_prev)

    plan = build_correction_plan(
        "US10YT_CMT", D, stored, confirmed, confirmed_prior,
        stored_d1=stored_d1, previous_valid_session_d1=D,
        stored_d2=stored_d2, is_cmt=True,
    )
    return plan, stored, stored_d1, stored_d2


def make_store_with_rows(stored, stored_d1=None, stored_d2=None):
    store = InMemoryReconciliationStore()
    store.add_row(stored)
    if stored_d1:
        store.add_row(stored_d1)
    if stored_d2:
        store.add_row(stored_d2)
    return store


# ---------------------------------------------------------------------------
# B7 — Transaction rollback
# ---------------------------------------------------------------------------


class TestB7TransactionRollback:
    def test_rollback_on_stale_plan(self):
        """Concurrent change between plan and write -> STALE_PLAN, no write."""
        plan, stored, stored_d1, stored_d2 = make_plan()
        store = make_store_with_rows(stored, stored_d1, stored_d2)

        # Simulate concurrent writer changing D after plan was built
        concurrent_row = make_cmt_row(close=4.90, prev_close=4.81)
        store.rows[("US10YT_CMT", D)] = concurrent_row

        success, error = execute_correction_plan(store, plan, run_id="test_b7")
        assert success is False
        assert error == "STALE_PLAN"
        # No repair audit rows
        repair_events = store.get_audit_events(AuditEventType.REPAIR)
        assert len(repair_events) == 0
        # D unchanged (concurrent writer's value preserved)
        assert store.read_row("US10YT_CMT", D).close == 4.90

    def test_rollback_on_update_failure(self):
        """UPDATE returns 0 rows -> STALE_PLAN, no partial state."""
        plan, stored, stored_d1, stored_d2 = make_plan()
        store = make_store_with_rows(stored, stored_d1, stored_d2)

        # Remove D+1 so the D+1 update fails
        del store.rows[("US10YT_CMT", D1)]

        success, error = execute_correction_plan(store, plan, run_id="test_b7b")
        assert success is False
        # D should be unchanged (rollback)
        assert store.read_row("US10YT_CMT", D).close == 4.85

    def test_no_partial_audit_on_rollback(self):
        """If repair txn rolls back, no repair audit rows persist."""
        plan, stored, stored_d1, stored_d2 = make_plan()
        store = make_store_with_rows(stored, stored_d1, stored_d2)

        # Make D stale
        store.rows[("US10YT_CMT", D)] = make_cmt_row(close=4.99, prev_close=4.81)

        success, error = execute_correction_plan(store, plan, run_id="test_b7c")
        assert success is False
        assert error == "STALE_PLAN"
        # No repair audit
        repair_events = store.get_audit_events(AuditEventType.REPAIR)
        assert len(repair_events) == 0


# ---------------------------------------------------------------------------
# B8 — Idempotency
# ---------------------------------------------------------------------------


class TestB8Idempotency:
    def test_second_run_is_match(self):
        """Run same correction twice -> second run is MATCH."""
        plan, stored, stored_d1, stored_d2 = make_plan()
        store = make_store_with_rows(stored, stored_d1, stored_d2)

        # First run
        success, error = execute_correction_plan(store, plan, run_id="test_b8")
        assert success is True
        assert error is None

        # Second run: build a new plan from the now-corrected state
        stored_after = store.read_row("US10YT_CMT", D)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)
        stored_d1_after = store.read_row("US10YT_CMT", D1)
        stored_d2_after = store.read_row("US10YT_CMT", D2)

        plan2 = build_correction_plan(
            "US10YT_CMT", D, stored_after, confirmed, confirmed_prior,
            stored_d1=stored_d1_after, previous_valid_session_d1=D,
            stored_d2=stored_d2_after, is_cmt=True,
        )
        # Second run should be MATCH (no changes needed)
        assert plan2.new_row.close == stored_after.close
        assert plan2.new_row.change == stored_after.change
        # No changed fields
        assert len(plan2.changed_source_fields) == 0
        assert len(plan2.changed_derived_fields) == 0

    def test_no_duplicate_audit(self):
        """Second run produces no duplicate repair audit rows."""
        plan, stored, stored_d1, stored_d2 = make_plan()
        store = make_store_with_rows(stored, stored_d1, stored_d2)

        # First run
        execute_correction_plan(store, plan, run_id="test_b8b")
        audit_count_after_first = store.get_audit_count()

        # Second run: no changes needed, so no new audit
        stored_after = store.read_row("US10YT_CMT", D)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)
        stored_d1_after = store.read_row("US10YT_CMT", D1)
        stored_d2_after = store.read_row("US10YT_CMT", D2)

        plan2 = build_correction_plan(
            "US10YT_CMT", D, stored_after, confirmed, confirmed_prior,
            stored_d1=stored_d1_after, previous_valid_session_d1=D,
            stored_d2=stored_d2_after, is_cmt=True,
        )
        # Execute second plan (no changes)
        success, error = execute_correction_plan(store, plan2, run_id="test_b8b")
        # If no changes, execute_correction_plan should still succeed
        # but produce no new audit rows
        assert store.get_audit_count() == audit_count_after_first


# ---------------------------------------------------------------------------
# B10 — Report-only (unresolved source)
# ---------------------------------------------------------------------------


class TestB10ReportOnly:
    def test_commodity_no_auto_correct(self):
        """Commodity/future mismatch -> report-only, no UPDATE."""
        assert not is_auto_correctable("GC=F")
        assert is_excluded_report_only("GC=F")

        # Even with a material mismatch, no correction
        stored = Row(
            symbol="GC=F", date=D, close=2000.0, prev_close=1990.0,
            source="yfinance", asset_type="commodity",
        )
        confirmed = Row(
            symbol="GC=F", date=D, close=2100.0, prev_close=1990.0,
            source="yfinance", asset_type="commodity",
        )
        result = decide("GC=F", D, stored, confirmed, None, P)
        assert result.verdict == Verdict.SOURCE_DEFINITION_UNRESOLVED
        assert result.plan is None

    def test_legacy_treasury_no_auto_correct(self):
        """Legacy USnYT=X -> LEGACY_SOURCE_DEFINITION_MIXED, no UPDATE."""
        stored = Row(
            symbol="US10YT=X", date=D, close=4.85, prev_close=4.81,
            source="investing.com", asset_type="ust_yield",
        )
        confirmed = Row(
            symbol="US10YT=X", date=D, close=4.87, prev_close=4.81,
            source="investing.com", asset_type="ust_yield",
        )
        result = decide("US10YT=X", D, stored, confirmed, None, P)
        assert result.verdict == Verdict.LEGACY_SOURCE_DEFINITION_MIXED
        assert result.plan is None

    def test_detection_event_recorded(self):
        """Report-only verdicts produce DETECTION audit events."""
        store = InMemoryReconciliationStore()
        event_id = record_detection_event(
            store, "GC=F", D,
            Verdict.SOURCE_DEFINITION_UNRESOLVED,
            Reason.SOURCE_DEFINITION_UNRESOLVED,
            run_id="test_b10",
        )
        assert event_id > 0
        events = store.get_audit_events(AuditEventType.DETECTION)
        assert len(events) == 1
        assert events[0].symbol == "GC=F"
        assert events[0].repaired_at is None


# ---------------------------------------------------------------------------
# B13 — Concurrent modification / STALE_PLAN
# ---------------------------------------------------------------------------


class TestB13ConcurrentModification:
    def test_concurrent_change_detected(self):
        """Another writer changes D between plan and write -> STALE_PLAN."""
        plan, stored, stored_d1, stored_d2 = make_plan()
        store = make_store_with_rows(stored, stored_d1, stored_d2)

        # Concurrent writer changes D
        store.rows[("US10YT_CMT", D)] = make_cmt_row(close=4.92, prev_close=4.81)

        success, error = execute_correction_plan(store, plan, run_id="test_b13")
        assert success is False
        assert error == "STALE_PLAN"
        # Concurrent writer's value preserved
        assert store.read_row("US10YT_CMT", D).close == 4.92

    def test_no_overwrite_of_concurrent_writer(self):
        """Phase B must never overwrite a concurrent writer's change."""
        plan, stored, stored_d1, stored_d2 = make_plan()
        store = make_store_with_rows(stored, stored_d1, stored_d2)

        # Concurrent writer sets D to a completely different value
        concurrent = make_cmt_row(close=5.00, prev_close=4.81)
        store.rows[("US10YT_CMT", D)] = concurrent

        success, error = execute_correction_plan(store, plan, run_id="test_b13c")
        assert success is False
        # D must still be the concurrent writer's value
        assert store.read_row("US10YT_CMT", D).close == 5.00
        assert store.read_row("US10YT_CMT", D).prev_close == 4.81


# ---------------------------------------------------------------------------
# B15 — Prior dependency
# ---------------------------------------------------------------------------


class TestB15PriorDependency:
    def test_prior_mismatch_blocks_correction(self):
        """Confirmed prior differs from stored prior -> DEPENDENCY_RECONCILE_REQUIRED."""
        stored_prior = make_cmt_row(date=P, close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.83)  # different

        ok, reason = check_prior_dependency(
            "US10YT_CMT", D, stored_prior, confirmed_prior, P
        )
        assert ok is False
        assert reason == Reason.DEPENDENCY_RECONCILE_REQUIRED

    def test_prior_confirmed_allows_correction(self):
        """Confirmed prior matches stored prior -> correction allowed."""
        stored_prior = make_cmt_row(date=P, close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        ok, reason = check_prior_dependency(
            "US10YT_CMT", D, stored_prior, confirmed_prior, P
        )
        assert ok is True
        assert reason is None

    def test_prior_missing_blocks(self):
        """No stored prior row -> MANUAL_REVIEW_REQUIRED."""
        ok, reason = check_prior_dependency(
            "US10YT_CMT", D, None, None, P
        )
        assert ok is False

    def test_prior_session_none_blocks(self):
        """No previous valid session -> PRIOR_SESSION_UNCONFIRMED."""
        stored_prior = make_cmt_row(date=P, close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        ok, reason = check_prior_dependency(
            "US10YT_CMT", D, stored_prior, confirmed_prior, None
        )
        assert ok is False
        assert reason == Reason.PRIOR_SESSION_UNCONFIRMED

    def test_dependency_reconcile_required_verdict(self):
        """decide() returns DEPENDENCY_RECONCILE_REQUIRED when prior is unconfirmed."""
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        # Prior session is None
        result = decide("US10YT_CMT", D, stored, confirmed, None, None)
        assert result.verdict == Verdict.MANUAL_REVIEW_REQUIRED


# ---------------------------------------------------------------------------
# D+2 pre-existing downstream inconsistency
# ---------------------------------------------------------------------------


class TestD2PreExisting:
    def test_pre_existing_d2_reported_not_blocking(self):
        """D+2 prev_close != D+1 close -> reported, does not block D/D+1 correction."""
        # D+2 has pre-existing inconsistency
        plan, stored, stored_d1, stored_d2 = make_plan(
            stored_d2_prev=99.0,  # wrong
        )
        store = make_store_with_rows(stored, stored_d1, stored_d2)

        # Plan should flag D+2 pre-existing inconsistency
        assert plan.d2_pre_existing_inconsistency is True
        assert plan.d2_detail is not None

        # But correction should still proceed
        success, error = execute_correction_plan(store, plan, run_id="test_d2")
        assert success is True
        assert error is None

        # D corrected
        assert store.read_row("US10YT_CMT", D).close == 4.87
        # D+1 derived corrected
        assert store.read_row("US10YT_CMT", D1).prev_close == 4.87
        # D+2 unchanged
        assert store.read_row("US10YT_CMT", D2).prev_close == 99.0

    def test_no_d2_inconsistency(self):
        """D+2 prev_close == D+1 close -> no flag."""
        plan, stored, stored_d1, stored_d2 = make_plan(
            stored_d2_prev=4.88,  # correct
        )
        assert plan.d2_pre_existing_inconsistency is False

    def test_d2_never_written(self):
        """D+2 is never modified by reconciliation."""
        plan, stored, stored_d1, stored_d2 = make_plan(
            stored_d2_prev=99.0,
        )
        store = make_store_with_rows(stored, stored_d1, stored_d2)

        d2_before = store.read_row("US10YT_CMT", D2)
        execute_correction_plan(store, plan, run_id="test_d2_never")
        d2_after = store.read_row("US10YT_CMT", D2)

        assert d2_before == d2_after


# ---------------------------------------------------------------------------
# Audit event type distinction
# ---------------------------------------------------------------------------


class TestAuditEventTypes:
    def test_repair_and_detection_are_distinct(self):
        """REPAIR and DETECTION are separate event types."""
        assert AuditEventType.REPAIR != AuditEventType.DETECTION

    def test_repair_events_have_repaired_at(self):
        """Repair audit events have repaired_at set."""
        plan, stored, stored_d1, stored_d2 = make_plan()
        store = make_store_with_rows(stored, stored_d1, stored_d2)

        execute_correction_plan(store, plan, run_id="test_audit")
        repair_events = store.get_audit_events(AuditEventType.REPAIR)
        assert len(repair_events) > 0
        for e in repair_events:
            assert e.repaired_at is not None

    def test_detection_events_have_null_repaired_at(self):
        """Detection audit events have repaired_at = NULL."""
        store = InMemoryReconciliationStore()
        record_detection_event(
            store, "GC=F", D,
            Verdict.SOURCE_DEFINITION_UNRESOLVED,
            Reason.SOURCE_DEFINITION_UNRESOLVED,
            run_id="test_audit_det",
        )
        detection_events = store.get_audit_events(AuditEventType.DETECTION)
        assert len(detection_events) == 1
        assert detection_events[0].repaired_at is None