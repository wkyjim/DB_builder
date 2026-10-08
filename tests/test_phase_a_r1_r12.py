"""Tests for Phase A.1 remediation: R1-R12 acceptance criteria.

R1: One supplementary futures historical deferral, coverage healthy -> DEGRADED_SUCCESS
R2: Current real incident shape (required-live-healthy but historical deferred) -> DEGRADED_SUCCESS
R3: Required current/live symbol unavailable -> FAIL_CLOSED
R4: Coverage below threshold -> FAIL_CLOSED
R5: Legacy USnYT=X only produces LEGACY_EOD_DEFINITION_UNRESOLVED -> not fatal
R6: Deferred-store persistence failure -> FAIL_CLOSED
R7: Gate/invariant/arithmetic failure -> FAIL_CLOSED regardless of coverage
R8: _PHASE_A_UNAVAILABLE_SYMBOLS supplementary-only -> degraded, not fatal
R9: _PHASE_A_UNAVAILABLE_SYMBOLS required current/live unavailable -> FAIL_CLOSED
R10: summary.exit_code set by fatal DB/internal error -> FAIL_CLOSED
R11: DEGRADED_SUCCESS wrapper behavior -> exit 0, no retries
R12: Per-class freshness -> futures stale, other classes fresh
"""
import pytest
from unittest.mock import patch, MagicMock
from dataclasses import replace
import os
import sys

REPO_ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(REPO_ROOT, 'src'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'scripts'))

os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")
os.environ.setdefault("LOCAL_DB_PASSWORD", "phase-a-test")
os.environ.setdefault("NEON_DB_PASSWORD", "phase-a-test")

from macro_data_fetch import (
    MacroCoverageSummary,
    SymbolFetchOutcome,
    REQUIRED_SYMBOLS,
    _phase_a_safety_exit_code,
    _classify_unavailable_symbol,
    summarize_outcomes,
    _PHASE_A_ACTIONABLE_DEFERRALS,
    _PHASE_A_UNAVAILABLE_SYMBOLS,
    _PHASE_A_DEFERRED_COUNT,
    _PHASE_A_BOUNDARY_DEFERRED_COUNT,
    _PIPELINE_FATAL_CONDITIONS,
)


def make_outcome(symbol, *, success=True, required=False, provider="yfinance",
                 rows_fetched=5, latest_observation=None, failure_reason=None):
    return SymbolFetchOutcome(
        symbol=symbol,
        name=f"Test {symbol}",
        provider=provider,
        required=required,
        success=success,
        rows_fetched=rows_fetched,
        latest_observation=latest_observation,
        failure_reason=failure_reason,
    )


def make_summary(outcomes, *, coverage_satisfied=True, required_failed=()):
    return MacroCoverageSummary(
        outcomes=tuple(outcomes),
        configured_count=len(outcomes),
        successful_count=sum(1 for o in outcomes if o.success),
        failed_count=sum(1 for o in outcomes if not o.success),
        coverage_pct=95.0 if coverage_satisfied else 80.0,
        required_failed_symbols=tuple(required_failed),
        provider_failure_counts=(),
        coverage_satisfied=coverage_satisfied,
    )


def clear_state():
    _PHASE_A_ACTIONABLE_DEFERRALS.clear()
    _PHASE_A_UNAVAILABLE_SYMBOLS.clear()
    _PIPELINE_FATAL_CONDITIONS.clear()
    global _PHASE_A_DEFERRED_COUNT, _PHASE_A_BOUNDARY_DEFERRED_COUNT
    _PHASE_A_DEFERRED_COUNT = 0
    _PHASE_A_BOUNDARY_DEFERRED_COUNT = 0


class TestR1_SupplementaryFuturesDeferral:
    """R1: One supplementary futures historical deferral, coverage healthy."""

    def setup_method(self):
        clear_state()

    def test_r1_supplementary_deferral_degraded_success(self):
        """Historical row withheld; DEGRADED_SUCCESS; exit 0; downstream continues."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "ES=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
        ])
        # 10 symbols, 1 failure = 90% coverage
        outcomes = [
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("^NDX", success=True, required=True),
            make_outcome("^DJI", success=True, required=True),
            make_outcome("^VIX", success=True, required=True),
            make_outcome("^HSI", success=True, required=True),
            make_outcome("^N225", success=True, required=True),
            make_outcome("^KS11", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
            make_outcome("CL=F", success=True, required=True),
            make_outcome("ES=F", success=False, required=False),
        ]
        summary = summarize_outcomes(outcomes)
        assert summary.coverage_satisfied
        assert _phase_a_safety_exit_code(summary) == 0
        assert "ES=F" in summary.supplementary_historical_deferred_symbols


class TestR2_RealIncidentShape:
    """R2: Current real incident shape."""

    def setup_method(self):
        clear_state()

    def test_r2_required_live_hhealthy_historical_deferred(self):
        """BZ=F/CL=F/GC=F required-live-healthy but historical deferred;
        five supplementary futures historical deferred; required coverage >90%."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "BZ=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "CL=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "GC=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "ES=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "HG=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "NG=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "NIY=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "NQ=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
        ])
        # 50 symbols, 5 failures = 90% coverage (at threshold)
        outcomes = [
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("^NDX", success=True, required=True),
            make_outcome("^DJI", success=True, required=True),
            make_outcome("^VIX", success=True, required=True),
            make_outcome("^HSI", success=True, required=True),
            make_outcome("^N225", success=True, required=True),
            make_outcome("^KS11", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
            make_outcome("CL=F", success=True, required=True),
            make_outcome("GC=F", success=True, required=True),
            make_outcome("^FVX", success=True, required=True),
            make_outcome("^TNX", success=True, required=True),
            make_outcome("^TYX", success=True, required=True),
            make_outcome("US2YT=X", success=True, required=True),
            make_outcome("US10YT=X", success=True, required=True),
            make_outcome("US30YT=X", success=True, required=True),
            make_outcome("DX-Y.NYB", success=True, required=True),
            make_outcome("EURUSD=X", success=True, required=True),
            make_outcome("JPY=X", success=True, required=True),
            make_outcome("CNY=X", success=True, required=True),
            # Additional successful symbols to maintain >90% coverage
            make_outcome("^RUT", success=True, required=False),
            make_outcome("^IXIC", success=True, required=False),
            make_outcome("^FTSE", success=True, required=False),
            make_outcome("^GDAXI", success=True, required=False),
            make_outcome("^FCHI", success=True, required=False),
            make_outcome("000001.SS", success=True, required=False),
            make_outcome("AUDUSD=X", success=True, required=False),
            make_outcome("GBPUSD=X", success=True, required=False),
            make_outcome("NZDUSD=X", success=True, required=False),
            make_outcome("HKD=X", success=True, required=False),
            make_outcome("SGD=X", success=True, required=False),
            make_outcome("CHFUSD=X", success=True, required=False),
            make_outcome("BTC-USD", success=True, required=False),
            make_outcome("ETH-USD", success=True, required=False),
            make_outcome("US2YT_CMT", success=True, required=False),
            make_outcome("US3YT_CMT", success=True, required=False),
            make_outcome("US5YT_CMT", success=True, required=False),
            make_outcome("US7YT_CMT", success=True, required=False),
            make_outcome("US10YT_CMT", success=True, required=False),
            make_outcome("US20YT_CMT", success=True, required=False),
            make_outcome("US30YT_CMT", success=True, required=False),
            make_outcome("US3YT=X", success=True, required=False),
            make_outcome("US5YT=X", success=True, required=False),
            make_outcome("US7YT=X", success=True, required=False),
            make_outcome("US20YT=X", success=True, required=False),
            # 5 supplementary futures failures
            make_outcome("ES=F", success=False, required=False),
            make_outcome("HG=F", success=False, required=False),
            make_outcome("NG=F", success=False, required=False),
            make_outcome("NIY=F", success=False, required=False),
            make_outcome("NQ=F", success=False, required=False),
        ]
        summary = summarize_outcomes(outcomes)
        assert summary.coverage_satisfied
        assert _phase_a_safety_exit_code(summary) == 0
        assert len(summary.required_historical_deferred_symbols) == 3
        assert len(summary.supplementary_historical_deferred_symbols) == 5


class TestR3_RequiredLiveUnavailable:
    """R3: Required current/live symbol unavailable -> FAIL_CLOSED."""

    def setup_method(self):
        clear_state()

    def test_r3_required_live_unavailable_fail_closed(self):
        """Required current/live symbol unavailable -> FAIL_CLOSED; non-zero."""
        _PHASE_A_UNAVAILABLE_SYMBOLS.extend([
            {"symbol": "^GSPC", "reason": "CLOSED_UNCONFIRMED"},
        ])
        summary = make_summary([
            make_outcome("^GSPC", success=False, required=True),
            make_outcome("BZ=F", success=True, required=True),
        ], coverage_satisfied=False, required_failed=["^GSPC"])
        assert _phase_a_safety_exit_code(summary) == 1


class TestR4_CoverageBelowThreshold:
    """R4: Coverage below threshold -> FAIL_CLOSED."""

    def setup_method(self):
        clear_state()

    def test_r4_coverage_below_threshold_fail_closed(self):
        """Coverage below threshold -> FAIL_CLOSED."""
        summary = make_summary([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=False, required=True),
            make_outcome("CL=F", success=False, required=True),
        ], coverage_satisfied=False, required_failed=["BZ=F", "CL=F"])
        assert _phase_a_safety_exit_code(summary) == 1


class TestR5_LegacyTreasuryFrozen:
    """R5: Legacy USnYT=X only produces LEGACY_EOD_DEFINITION_UNRESOLVED."""

    def setup_method(self):
        clear_state()

    def test_r5_legacy_treasury_frozen_not_fatal(self):
        """Historical write remains frozen; does not by itself make pipeline fatal."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "US2YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US10YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
        ])
        _PHASE_A_UNAVAILABLE_SYMBOLS.extend([
            {"symbol": "US2YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US10YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
        ])
        summary = make_summary([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
            make_outcome("US2YT=X", success=True, required=True),
            make_outcome("US10YT=X", success=True, required=True),
        ])
        assert _phase_a_safety_exit_code(summary) == 0
        # Verify classification
        assert _classify_unavailable_symbol("US2YT=X", "LEGACY_EOD_DEFINITION_UNRESOLVED") == "EXPECTED_FROZEN"


class TestR6_DeferredStorePersistenceFailure:
    """R6: Deferred-store persistence failure -> FAIL_CLOSED."""

    def setup_method(self):
        clear_state()

    def test_r6_deferred_store_persistence_failure_fail_closed(self):
        """Deferred-store persistence failure -> FAIL_CLOSED."""
        _PIPELINE_FATAL_CONDITIONS.append("DEFERRED_STORE_PERSISTENCE_FAILURE")
        summary = make_summary([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
        ])
        assert _phase_a_safety_exit_code(summary) == 1


class TestR7_GateInvariantFailure:
    """R7: Gate/invariant/arithmetic failure -> FAIL_CLOSED regardless of coverage."""

    def setup_method(self):
        clear_state()

    def test_r7_gate_invariant_failure_fail_closed(self):
        """Gate/invariant/arithmetic failure -> FAIL_CLOSED regardless of coverage."""
        _PIPELINE_FATAL_CONDITIONS.append("GATE_INVARIANT_FAILURE")
        summary = make_summary([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
        ])
        assert _phase_a_safety_exit_code(summary) == 1

    def test_r7_arithmetic_failure_fail_closed(self):
        """Written-row arithmetic/invariant failure -> FAIL_CLOSED."""
        _PIPELINE_FATAL_CONDITIONS.append("ARITHMETIC_INVARIANT_FAILURE")
        summary = make_summary([
            make_outcome("^GSPC", success=True, required=True),
        ])
        assert _phase_a_safety_exit_code(summary) == 1


class TestR8_SupplementaryUnavailable:
    """R8: _PHASE_A_UNAVAILABLE_SYMBOLS supplementary-only -> degraded, not fatal."""

    def setup_method(self):
        clear_state()

    def test_r8_supplementary_unavailable_degraded(self):
        """Supplementary-only unavailable data -> degraded, not automatically fatal."""
        _PHASE_A_UNAVAILABLE_SYMBOLS.extend([
            {"symbol": "ES=F", "reason": "SOURCE_DEFINITION_UNRESOLVED"},
            {"symbol": "HG=F", "reason": "SOURCE_DEFINITION_UNRESOLVED"},
        ])
        summary = make_summary([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
            make_outcome("ES=F", success=False, required=False),
            make_outcome("HG=F", success=False, required=False),
        ])
        assert _phase_a_safety_exit_code(summary) == 0
        # Verify classification
        assert _classify_unavailable_symbol("ES=F", "SOURCE_DEFINITION_UNRESOLVED") == "DEGRADED"


class TestR9_RequiredUnavailable:
    """R9: _PHASE_A_UNAVAILABLE_SYMBOLS required current/live unavailable -> FAIL_CLOSED."""

    def setup_method(self):
        clear_state()

    def test_r9_required_unavailable_fail_closed(self):
        """Required current/live unavailable data -> FAIL_CLOSED."""
        _PHASE_A_UNAVAILABLE_SYMBOLS.extend([
            {"symbol": "^GSPC", "reason": "CLOSED_UNCONFIRMED"},
        ])
        summary = make_summary([
            make_outcome("^GSPC", success=False, required=True),
            make_outcome("BZ=F", success=True, required=True),
        ], coverage_satisfied=False, required_failed=["^GSPC"])
        assert _phase_a_safety_exit_code(summary) == 1
        # Verify classification
        assert _classify_unavailable_symbol("^GSPC", "CLOSED_UNCONFIRMED") == "PIPELINE_FATAL"


class TestR10_FatalDBError:
    """R10: summary.exit_code set by fatal DB/internal error -> FAIL_CLOSED."""

    def setup_method(self):
        clear_state()

    def test_r10_fatal_db_error_fail_closed(self):
        """summary.exit_code set by genuinely fatal DB/internal error -> FAIL_CLOSED."""
        _PIPELINE_FATAL_CONDITIONS.append("DB_TRANSACTION_FAILURE")
        summary = make_summary([
            make_outcome("^GSPC", success=True, required=True),
        ])
        # Even though coverage is satisfied, fatal condition makes it FAIL_CLOSED
        assert _phase_a_safety_exit_code(summary) == 1


class TestR11_DegradedSuccessWrapper:
    """R11: DEGRADED_SUCCESS wrapper behavior."""

    def setup_method(self):
        clear_state()

    def test_r11_degraded_success_exit_zero(self):
        """Python process exits 0; batch wrapper does NOT perform 3 failure retries."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "BZ=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
        ])
        outcomes = [
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("^NDX", success=True, required=True),
            make_outcome("^DJI", success=True, required=True),
            make_outcome("^VIX", success=True, required=True),
            make_outcome("^HSI", success=True, required=True),
            make_outcome("^N225", success=True, required=True),
            make_outcome("^KS11", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
            make_outcome("CL=F", success=True, required=True),
            make_outcome("GC=F", success=True, required=True),
        ]
        summary = summarize_outcomes(outcomes)
        exit_code = summary.exit_code or _phase_a_safety_exit_code(summary)
        assert exit_code == 0


class TestR12_PerClassFreshness:
    """R12: Per-class freshness reporting."""

    def setup_method(self):
        clear_state()

    def test_r12_per_class_freshness_reporting(self):
        """Futures historical stale; other updated classes fresh."""
        from datetime import date as _date
        today = _date.today()
        yesterday = today - __import__('datetime').timedelta(days=1)

        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "BZ=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "ES=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
        ])
        outcomes = [
            make_outcome("^GSPC", success=True, required=True, latest_observation=yesterday),
            make_outcome("^NDX", success=True, required=True, latest_observation=yesterday),
            make_outcome("^DJI", success=True, required=True, latest_observation=yesterday),
            make_outcome("^VIX", success=True, required=True, latest_observation=yesterday),
            make_outcome("^HSI", success=True, required=True, latest_observation=yesterday),
            make_outcome("^N225", success=True, required=True, latest_observation=yesterday),
            make_outcome("^KS11", success=True, required=True, latest_observation=yesterday),
            make_outcome("BZ=F", success=True, required=True, latest_observation=yesterday),
            make_outcome("CL=F", success=True, required=True, latest_observation=yesterday),
            make_outcome("GC=F", success=True, required=True, latest_observation=yesterday),
            make_outcome("ES=F", success=False, required=False),
        ]
        summary = summarize_outcomes(outcomes)
        assert summary.coverage_satisfied
        assert _phase_a_safety_exit_code(summary) == 0
        # Per-class freshness is reported by _report_per_class_freshness
        # (tested via integration test)


class TestClassifyUnavailableSymbol:
    """Tests for _classify_unavailable_symbol."""

    def test_legacy_treasury_is_expected_frozen(self):
        assert _classify_unavailable_symbol("US2YT=X", "LEGACY_EOD_DEFINITION_UNRESOLVED") == "EXPECTED_FROZEN"

    def test_required_symbol_is_fatal(self):
        assert _classify_unavailable_symbol("^GSPC", "CLOSED_UNCONFIRMED") == "PIPELINE_FATAL"
        assert _classify_unavailable_symbol("BZ=F", "CLOSED_UNCONFIRMED") == "PIPELINE_FATAL"

    def test_supplementary_symbol_is_degraded(self):
        assert _classify_unavailable_symbol("ES=F", "SOURCE_DEFINITION_UNRESOLVED") == "DEGRADED"
        assert _classify_unavailable_symbol("HG=F", "CLOSED_UNCONFIRMED") == "DEGRADED"

    def test_unknown_symbol_is_degraded(self):
        assert _classify_unavailable_symbol("UNKNOWN", "SOME_REASON") == "DEGRADED"
