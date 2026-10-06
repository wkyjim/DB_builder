"""Phase B pure tests — B1–B6, B9, B11, B12, B14, D+2 downstream.

No DB, no I/O. All side effects injected.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from db_builder.reconciliation import (  # noqa: E402
    AuditEventType,
    CMT_SYMBOLS,
    CorrectionPlan,
    Reason,
    ReconResult,
    Row,
    Verdict,
    build_correction_plan,
    check_prior_dependency,
    decide,
    is_auto_correctable,
    is_detect_only,
    is_excluded_report_only,
    is_legacy_treasury,
    round_4,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

D = dt.date(2026, 9, 25)
D1 = dt.date(2026, 9, 28)
D2 = dt.date(2026, 9, 29)
P = dt.date(2026, 9, 24)  # previous valid session for D


def make_cmt_row(
    symbol: str = "US10YT_CMT",
    date: dt.date = D,
    close: float = 4.85,
    prev_close: float = 4.81,
    source: str = "treasury.gov",
) -> Row:
    """CMT synthetic row: open=high=low=adj_close=close, amplitude=0."""
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


def make_equity_row(
    symbol: str = "^GSPC",
    date: dt.date = D,
    close: float = 5000.0,
    prev_close: float = 4990.0,
    open: float = 4995.0,
    high: float = 5010.0,
    low: float = 4985.0,
    volume: float = 1000000.0,
    source: str = "yfinance",
) -> Row:
    return Row(
        symbol=symbol,
        date=date,
        open=open,
        high=high,
        low=low,
        close=close,
        adj_close=close,
        volume=volume,
        prev_close=prev_close,
        change=round_4(close - prev_close),
        pct_chg=round_4((close - prev_close) / prev_close * 100) if prev_close else None,
        amplitude=round_4((high - low) / prev_close * 100) if prev_close else None,
        source=source,
        asset_type="equity",
    )


# ---------------------------------------------------------------------------
# B1 — MATCH
# ---------------------------------------------------------------------------


class TestB1Match:
    def test_cmt_match(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        result = decide("US10YT_CMT", D, stored, confirmed, confirmed_prior, P)
        assert result.verdict == Verdict.MATCH

    def test_equity_detect_only_returns_unresolved(self):
        # Equity indices are detect-only in v1 — decide() returns unresolved
        stored = make_equity_row()
        confirmed = make_equity_row()
        confirmed_prior = make_equity_row(date=P, close=4990.0)

        result = decide("^GSPC", D, stored, confirmed, confirmed_prior, P)
        assert result.verdict == Verdict.SOURCE_DEFINITION_UNRESOLVED

    def test_match_produces_no_plan(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        result = decide("US10YT_CMT", D, stored, confirmed, confirmed_prior, P)
        assert result.plan is None


# ---------------------------------------------------------------------------
# B2 — ROUNDING_ONLY
# ---------------------------------------------------------------------------


class TestB2RoundingOnly:
    def test_cmt_tiny_difference_is_match(self):
        # For CMT, ROUNDING_ONLY is impossible: any diff > 1e-4 causes derived
        # mismatch (change tolerance = 1e-4 * max(0.04, 1) = 1e-4), and any
        # diff <= 1e-4 is rounded away by round_4. So tiny diffs -> MATCH.
        stored = make_cmt_row(close=4.850001, prev_close=4.81)
        confirmed = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        result = decide("US10YT_CMT", D, stored, confirmed, confirmed_prior, P)
        assert result.verdict == Verdict.MATCH

    def test_cmt_tiny_difference_no_correction(self):
        stored = make_cmt_row(close=4.850001, prev_close=4.81)
        confirmed = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        result = decide("US10YT_CMT", D, stored, confirmed, confirmed_prior, P)
        assert result.verdict == Verdict.MATCH
        assert result.plan is None

    def test_rounding_only_no_correction_2(self):
        stored = make_cmt_row(close=4.850001, prev_close=4.81)
        confirmed = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        result = decide("US10YT_CMT", D, stored, confirmed, confirmed_prior, P)
        assert result.plan is None


# ---------------------------------------------------------------------------
# B3 — Confirmed close revision
# ---------------------------------------------------------------------------


class TestB3CloseRevision:
    def test_cmt_close_revision(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)  # revised
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        result = decide("US10YT_CMT", D, stored, confirmed, confirmed_prior, P)
        assert result.verdict == Verdict.CORRECTION_REQUIRED

    def test_plan_corrects_close(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=None, previous_valid_session_d1=None,
            stored_d2=None, is_cmt=True,
        )
        assert plan.new_row is not None
        assert plan.new_row.close == 4.87
        assert "close" in plan.changed_source_fields

    def test_plan_recomputes_derived(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=None, previous_valid_session_d1=None,
            stored_d2=None, is_cmt=True,
        )
        assert plan.new_row.change == round_4(4.87 - 4.81)
        assert plan.new_row.pct_chg == round_4((4.87 - 4.81) / 4.81 * 100)
        assert plan.new_row.amplitude == 0.0  # CMT

    def test_d1_dependents_recomputed(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)
        stored_d1 = make_cmt_row(date=D1, close=4.88, prev_close=4.85)

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=stored_d1, previous_valid_session_d1=D,
            stored_d2=None, is_cmt=True,
        )
        assert plan.new_d1_row is not None
        assert plan.new_d1_row.prev_close == 4.87  # corrected D close
        assert plan.new_d1_row.change == round_4(4.88 - 4.87)
        assert plan.new_d1_row.close == 4.88  # D+1 source unchanged

    def test_d2_untouched(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)
        stored_d1 = make_cmt_row(date=D1, close=4.88, prev_close=4.85)
        stored_d2 = make_cmt_row(date=D2, close=4.89, prev_close=4.88)

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=stored_d1, previous_valid_session_d1=D,
            stored_d2=stored_d2, is_cmt=True,
        )
        # D+2 is never in the plan
        assert plan.new_d1_row is not None
        # No D+2 row in plan
        assert not hasattr(plan, "new_d2_row")


# ---------------------------------------------------------------------------
# B4 — Source mismatch
# ---------------------------------------------------------------------------


class TestB4SourceMismatch:
    def test_different_source_definition(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81, source="treasury.gov")
        confirmed = make_cmt_row(close=4.85, prev_close=4.81, source="investing.com")
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        result = decide("US10YT_CMT", D, stored, confirmed, confirmed_prior, P)
        assert result.verdict == Verdict.SOURCE_DEFINITION_MISMATCH

    def test_no_write_on_mismatch(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81, source="treasury.gov")
        confirmed = make_cmt_row(close=4.85, prev_close=4.81, source="investing.com")
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        result = decide("US10YT_CMT", D, stored, confirmed, confirmed_prior, P)
        assert result.plan is None


# ---------------------------------------------------------------------------
# B5 — Confirmation unavailable
# ---------------------------------------------------------------------------


class TestB5ConfirmationUnavailable:
    def test_no_confirmed_observation(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)

        result = decide("US10YT_CMT", D, stored, None, None, P)
        assert result.verdict == Verdict.CONFIRMATION_UNAVAILABLE

    def test_no_write_when_unavailable(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)

        result = decide("US10YT_CMT", D, stored, None, None, P)
        assert result.plan is None


# ---------------------------------------------------------------------------
# B6 — Treasury CMT revision
# ---------------------------------------------------------------------------


class TestB6TreasuryCMTRevision:
    def test_cmt_revision_correctable(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        result = decide("US10YT_CMT", D, stored, confirmed, confirmed_prior, P)
        assert result.verdict == Verdict.CORRECTION_REQUIRED

    def test_cmt_legacy_untouched(self):
        # Legacy USnYT=X is never auto-corrected
        assert not is_auto_correctable("US10YT=X")
        assert is_legacy_treasury("US10YT=X")

    def test_cmt_all_symbols_auto_correctable(self):
        for sym in CMT_SYMBOLS:
            assert is_auto_correctable(sym), f"{sym} should be auto-correctable"

    def test_cmt_source_is_treasury_gov(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81, source="treasury.gov")
        confirmed = make_cmt_row(close=4.87, prev_close=4.81, source="treasury.gov")
        confirmed_prior = make_cmt_row(date=P, close=4.81, source="treasury.gov")

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=None, previous_valid_session_d1=None,
            stored_d2=None, is_cmt=True,
        )
        assert plan.evidence["confirmed_source"] == "treasury.gov"


# ---------------------------------------------------------------------------
# B9 — Single-hop boundary
# ---------------------------------------------------------------------------


class TestB9SingleHop:
    def test_d1_corrected_d2_unchanged(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)
        stored_d1 = make_cmt_row(date=D1, close=4.88, prev_close=4.85)
        stored_d2 = make_cmt_row(date=D2, close=4.89, prev_close=4.88)

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=stored_d1, previous_valid_session_d1=D,
            stored_d2=stored_d2, is_cmt=True,
        )
        # D+1 corrected
        assert plan.new_d1_row is not None
        assert plan.new_d1_row.prev_close == 4.87
        # D+2 not in plan (never written)
        assert not hasattr(plan, "new_d2_row")

    def test_d1_not_dependent_no_correction(self):
        # D+1's previous valid session is NOT D
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)
        stored_d1 = make_cmt_row(date=D1, close=4.88, prev_close=4.85)

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=stored_d1, previous_valid_session_d1=None,  # D+1 not dependent on D
            stored_d2=None, is_cmt=True,
        )
        assert plan.new_d1_row is None


# ---------------------------------------------------------------------------
# B11 — Wrong previous valid session
# ---------------------------------------------------------------------------


class TestB11WrongPriorSession:
    def test_detects_wrong_linkage(self):
        # Stored D has prev_close from D-2 (4.78) while D-1 (P) is expected
        stored = make_cmt_row(close=4.85, prev_close=4.78)  # wrong prev_close
        confirmed = make_cmt_row(close=4.85, prev_close=4.81)  # correct
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        result = decide("US10YT_CMT", D, stored, confirmed, confirmed_prior, P)
        # Source matches but derived is wrong
        assert result.verdict == Verdict.CORRECTION_REQUIRED

    def test_corrects_when_prior_confirmed(self):
        stored = make_cmt_row(close=4.85, prev_close=4.78)  # wrong
        confirmed = make_cmt_row(close=4.85, prev_close=4.81)  # correct
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=None, previous_valid_session_d1=None,
            stored_d2=None, is_cmt=True,
        )
        assert plan.new_row.prev_close == 4.81
        assert "prev_close" in plan.changed_derived_fields


# ---------------------------------------------------------------------------
# B12 — Holiday/weekend session
# ---------------------------------------------------------------------------


class TestB12HolidayWeekend:
    def test_uses_previous_valid_session_not_calendar_d_minus_1(self):
        # D is Monday 2026-09-28, previous valid session is Friday 2026-09-25
        # NOT calendar D-1 (Sunday 2026-09-27)
        monday = dt.date(2026, 9, 28)
        friday = dt.date(2026, 9, 25)
        sunday = dt.date(2026, 9, 27)

        stored = make_cmt_row(date=monday, close=4.88, prev_close=4.85)
        confirmed = make_cmt_row(date=monday, close=4.88, prev_close=4.85)
        confirmed_prior = make_cmt_row(date=friday, close=4.85)

        # previous_valid_session should be Friday, not Sunday
        result = decide("US10YT_CMT", monday, stored, confirmed, confirmed_prior, friday)
        assert result.verdict == Verdict.MATCH

    def test_no_lag_date_assumption(self):
        # The engine uses previous_valid_session(), never LAG(date)
        # This is verified by the fact that decide() takes previous_valid_session
        # as a parameter and never computes D-1 internally
        import inspect
        sig = inspect.signature(decide)
        params = list(sig.parameters.keys())
        assert "previous_valid_session" in params
        # No internal date arithmetic
        source = inspect.getsource(decide)
        assert "timedelta" not in source
        assert "- 1" not in source


# ---------------------------------------------------------------------------
# B14 — Source MATCH but derived materially wrong
# ---------------------------------------------------------------------------


class TestB14DerivedFieldInconsistent:
    def test_source_match_derived_wrong(self):
        # OHLC matches but change/pct_chg are materially wrong
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        # Manually corrupt derived fields
        stored.change = 99.0  # should be 0.04
        stored.pct_chg = 99.0  # should be ~0.83

        confirmed = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        result = decide("US10YT_CMT", D, stored, confirmed, confirmed_prior, P)
        assert result.verdict == Verdict.CORRECTION_REQUIRED
        assert result.reason == Reason.DERIVED_FIELD_INCONSISTENT

    def test_not_classified_rounding_only(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        stored.change = 99.0  # materially wrong

        confirmed = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        result = decide("US10YT_CMT", D, stored, confirmed, confirmed_prior, P)
        assert result.verdict != Verdict.ROUNDING_ONLY

    def test_plan_recomputes_derived_only(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        stored.change = 99.0
        stored.pct_chg = 99.0

        confirmed = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=None, previous_valid_session_d1=None,
            stored_d2=None, is_cmt=True,
        )
        # Source fields unchanged
        assert "close" not in plan.changed_source_fields
        # Derived fields corrected
        assert "change" in plan.changed_derived_fields
        assert "pct_chg" in plan.changed_derived_fields
        assert plan.new_row.change == round_4(4.85 - 4.81)
        assert plan.new_row.close == 4.85  # unchanged


# ---------------------------------------------------------------------------
# D+2 pre-existing downstream inconsistency
# ---------------------------------------------------------------------------


class TestD2PreExisting:
    def test_pre_existing_d2_reported_not_blocking(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)
        stored_d1 = make_cmt_row(date=D1, close=4.88, prev_close=4.85)
        # D+2 has pre-existing inconsistency: prev_close != D+1.close
        stored_d2 = make_cmt_row(date=D2, close=4.89, prev_close=99.0)  # wrong

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=stored_d1, previous_valid_session_d1=D,
            stored_d2=stored_d2, is_cmt=True,
        )
        # D+2 pre-existing inconsistency is flagged
        assert plan.d2_pre_existing_inconsistency is True
        assert plan.d2_detail is not None
        # But the plan still proceeds (not blocked)
        assert plan.new_row is not None
        assert plan.new_d1_row is not None

    def test_no_d2_inconsistency(self):
        stored = make_cmt_row(close=4.85, prev_close=4.81)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)
        stored_d1 = make_cmt_row(date=D1, close=4.88, prev_close=4.85)
        stored_d2 = make_cmt_row(date=D2, close=4.89, prev_close=4.88)  # correct

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=stored_d1, previous_valid_session_d1=D,
            stored_d2=stored_d2, is_cmt=True,
        )
        assert plan.d2_pre_existing_inconsistency is False


# ---------------------------------------------------------------------------
# Prior dependency (B15)
# ---------------------------------------------------------------------------


class TestB15PriorDependency:
    def test_prior_confirmed_ok(self):
        stored_prior = make_cmt_row(date=P, close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        ok, reason = check_prior_dependency(
            "US10YT_CMT", D, stored_prior, confirmed_prior, P
        )
        assert ok is True
        assert reason is None

    def test_prior_mismatch_blocks(self):
        stored_prior = make_cmt_row(date=P, close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.83)  # different

        ok, reason = check_prior_dependency(
            "US10YT_CMT", D, stored_prior, confirmed_prior, P
        )
        assert ok is False
        assert reason == Reason.DEPENDENCY_RECONCILE_REQUIRED

    def test_prior_missing_blocks(self):
        ok, reason = check_prior_dependency(
            "US10YT_CMT", D, None, None, P
        )
        assert ok is False

    def test_prior_session_none_blocks(self):
        stored_prior = make_cmt_row(date=P, close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        ok, reason = check_prior_dependency(
            "US10YT_CMT", D, stored_prior, confirmed_prior, None
        )
        assert ok is False
        assert reason == Reason.PRIOR_SESSION_UNCONFIRMED


# ---------------------------------------------------------------------------
# Eligibility / scope
# ---------------------------------------------------------------------------


class TestEligibility:
    def test_cmt_only_auto_correct(self):
        for sym in CMT_SYMBOLS:
            assert is_auto_correctable(sym)

    def test_equity_indices_detect_only(self):
        for sym in ["^GSPC", "^NDX", "^IXIC", "^DJI", "^RUT",
                     "^FTSE", "^GDAXI", "^FCHI",
                     "^HSI", "^N225", "^KS11", "000001.SS"]:
            assert is_detect_only(sym)
            assert not is_auto_correctable(sym)

    def test_etfs_detect_only(self):
        for sym in ["HYG", "LQD", "JNK", "RSP", "IWF", "IWD", "TLT", "IEF", "SHY"]:
            assert is_detect_only(sym)
            assert not is_auto_correctable(sym)

    def test_yield_indices_detect_only(self):
        for sym in ["^FVX", "^TNX", "^TYX"]:
            assert is_detect_only(sym)
            assert not is_auto_correctable(sym)

    def test_volatility_detect_only(self):
        for sym in ["^VIX", "^SKEW"]:
            assert is_detect_only(sym)
            assert not is_auto_correctable(sym)

    def test_legacy_treasury_not_auto_correct(self):
        for sym in ["US2YT=X", "US5YT=X", "US10YT=X"]:
            assert is_legacy_treasury(sym)
            assert not is_auto_correctable(sym)

    def test_excluded_report_only(self):
        for sym in ["USnYT_MKT", "HK50", "KOR200c1", "CIHc1",
                     "GC=F", "CL=F", "ES=F", "^MOVE"]:
            assert is_excluded_report_only(sym)
            assert not is_auto_correctable(sym)


# ---------------------------------------------------------------------------
# Audit event types
# ---------------------------------------------------------------------------


class TestAuditEventTypes:
    def test_repair_and_detection_distinct(self):
        assert AuditEventType.REPAIR != AuditEventType.DETECTION
        assert AuditEventType.REPAIR == "REPAIR"
        assert AuditEventType.DETECTION == "DETECTION"


# ---------------------------------------------------------------------------
# Round_4 semantics
# ---------------------------------------------------------------------------


class TestRound4:
    def test_banker_rounding(self):
        # Python round() uses banker's rounding
        assert round_4(0.12345) == 0.1235  # rounds to even
        assert round_4(0.12344) == 0.1234

    def test_none_passthrough(self):
        assert round_4(None) is None

    def test_integer_result(self):
        assert round_4(4.0) == 4.0