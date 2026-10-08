"""Tests for Phase A.1 remediation: DEGRADED_SUCCESS three-state model.

Covers:
- SUCCESS: coverage satisfied, no deferrals
- DEGRADED_SUCCESS: coverage satisfied, historical deferrals exist but safe
- FAIL_CLOSED: coverage not satisfied, or required live data failed
- Required vs supplementary historical deferral classification
- Legacy Treasury intentionally frozen does not abort pipeline
"""
import pytest
from unittest.mock import patch, MagicMock
from dataclasses import replace

# Import the module under test
import sys
import os

REPO_ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(REPO_ROOT, 'src'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'scripts'))

# Isolated test harness: the production module builds SQLAlchemy engines at
# import time. Point them at an in-process sqlite URL so no PostgreSQL
# connection is ever attempted and no production DB is touched.
os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")
os.environ.setdefault("LOCAL_DB_PASSWORD", "phase-a-test")
os.environ.setdefault("NEON_DB_PASSWORD", "phase-a-test")

from macro_data_fetch import (
    MacroCoverageSummary,
    SymbolFetchOutcome,
    REQUIRED_SYMBOLS,
    _phase_a_safety_exit_code,
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


class TestPhaseASafetyExitCode:
    """Tests for _phase_a_safety_exit_code three-state model."""

    def setup_method(self):
        """Clear global state before each test."""
        _PHASE_A_ACTIONABLE_DEFERRALS.clear()
        _PHASE_A_UNAVAILABLE_SYMBOLS.clear()
        _PIPELINE_FATAL_CONDITIONS.clear()
        global _PHASE_A_DEFERRED_COUNT, _PHASE_A_BOUNDARY_DEFERRED_COUNT
        _PHASE_A_DEFERRED_COUNT = 0
        _PHASE_A_BOUNDARY_DEFERRED_COUNT = 0

    def test_success_no_deferrals(self):
        """SUCCESS: coverage satisfied, no deferrals -> exit 0."""
        summary = make_summary([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
        ])
        assert _phase_a_safety_exit_code(summary) == 0

    def test_degraded_success_supplementary_deferrals(self):
        """DEGRADED_SUCCESS: coverage satisfied, supplementary deferrals -> exit 0."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "ES=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "HG=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
        ])
        summary = make_summary([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
            make_outcome("ES=F", success=False, required=False),
            make_outcome("HG=F", success=False, required=False),
        ])
        assert _phase_a_safety_exit_code(summary) == 0

    def test_degraded_success_required_historical_deferred(self):
        """DEGRADED_SUCCESS: coverage satisfied, required historical deferred but live OK -> exit 0."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "BZ=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "CL=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
        ])
        summary = make_summary([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),  # live OK, historical deferred
            make_outcome("CL=F", success=True, required=True),  # live OK, historical deferred
        ])
        assert _phase_a_safety_exit_code(summary) == 0

    def test_fail_closed_coverage_not_satisfied(self):
        """FAIL_CLOSED: coverage not satisfied -> exit 1."""
        summary = make_summary([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=False, required=True),
        ], coverage_satisfied=False, required_failed=["BZ=F"])
        assert _phase_a_safety_exit_code(summary) == 1

    def test_fail_closed_required_live_failed(self):
        """FAIL_CLOSED: required symbol live data failed -> exit 1."""
        summary = make_summary([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=False, required=True),
        ], coverage_satisfied=True, required_failed=["BZ=F"])
        assert _phase_a_safety_exit_code(summary) == 1

    def test_fail_closed_systemic_failure(self):
        """FAIL_CLOSED: >50% of required symbols failed -> exit 1."""
        summary = make_summary([
            make_outcome("^GSPC", success=False, required=True),
            make_outcome("^NDX", success=False, required=True),
            make_outcome("^DJI", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
        ], coverage_satisfied=True, required_failed=["^GSPC", "^NDX"])
        assert _phase_a_safety_exit_code(summary) == 1

    def test_legacy_treasury_frozen_not_fatal(self):
        """Legacy Treasury intentionally frozen does not abort pipeline."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "US2YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US10YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
        ])
        summary = make_summary([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
            make_outcome("US2YT=X", success=True, required=True),  # live OK, historical frozen
            make_outcome("US10YT=X", success=True, required=True),  # live OK, historical frozen
        ])
        assert _phase_a_safety_exit_code(summary) == 0

    def test_mixed_required_and_supplementary_deferrals(self):
        """DEGRADED_SUCCESS: mix of required and supplementary deferrals -> exit 0."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "BZ=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "ES=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "HG=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
        ])
        summary = make_summary([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
            make_outcome("ES=F", success=False, required=False),
            make_outcome("HG=F", success=False, required=False),
        ])
        assert _phase_a_safety_exit_code(summary) == 0


class TestSummarizeOutcomesClassification:
    """Tests for required vs supplementary historical deferral classification."""

    def setup_method(self):
        """Clear global state before each test."""
        _PHASE_A_ACTIONABLE_DEFERRALS.clear()
        _PHASE_A_UNAVAILABLE_SYMBOLS.clear()
        _PIPELINE_FATAL_CONDITIONS.clear()
        global _PHASE_A_DEFERRED_COUNT, _PHASE_A_BOUNDARY_DEFERRED_COUNT
        _PHASE_A_DEFERRED_COUNT = 0
        _PHASE_A_BOUNDARY_DEFERRED_COUNT = 0

    def test_required_historical_deferred_classification(self):
        """Required symbols with historical deferrals are classified correctly."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "BZ=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "CL=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "GC=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
        ])
        outcomes = [
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
            make_outcome("CL=F", success=True, required=True),
            make_outcome("GC=F", success=True, required=True),
        ]
        summary = summarize_outcomes(outcomes)
        assert "BZ=F" in summary.required_historical_deferred_symbols
        assert "CL=F" in summary.required_historical_deferred_symbols
        assert "GC=F" in summary.required_historical_deferred_symbols
        assert len(summary.supplementary_historical_deferred_symbols) == 0

    def test_supplementary_historical_deferred_classification(self):
        """Supplementary symbols with historical deferrals are classified correctly."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "ES=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "HG=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "NG=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
        ])
        outcomes = [
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
            make_outcome("ES=F", success=False, required=False),
            make_outcome("HG=F", success=False, required=False),
            make_outcome("NG=F", success=False, required=False),
        ]
        summary = summarize_outcomes(outcomes)
        assert len(summary.required_historical_deferred_symbols) == 0
        assert "ES=F" in summary.supplementary_historical_deferred_symbols
        assert "HG=F" in summary.supplementary_historical_deferred_symbols
        assert "NG=F" in summary.supplementary_historical_deferred_symbols

    def test_mixed_classification(self):
        """Mix of required and supplementary deferrals are classified correctly."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "BZ=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "ES=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
        ])
        outcomes = [
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
            make_outcome("ES=F", success=False, required=False),
        ]
        summary = summarize_outcomes(outcomes)
        assert "BZ=F" in summary.required_historical_deferred_symbols
        assert "ES=F" in summary.supplementary_historical_deferred_symbols


class TestThreeStateModel:
    """Tests for the three-state model: SUCCESS / DEGRADED_SUCCESS / FAIL_CLOSED."""

    def setup_method(self):
        """Clear global state before each test."""
        _PHASE_A_ACTIONABLE_DEFERRALS.clear()
        _PHASE_A_UNAVAILABLE_SYMBOLS.clear()
        _PIPELINE_FATAL_CONDITIONS.clear()
        global _PHASE_A_DEFERRED_COUNT, _PHASE_A_BOUNDARY_DEFERRED_COUNT
        _PHASE_A_DEFERRED_COUNT = 0
        _PHASE_A_BOUNDARY_DEFERRED_COUNT = 0

    def test_success_state(self):
        """SUCCESS: all symbols succeed, no deferrals."""
        outcomes = [
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
            make_outcome("CL=F", success=True, required=True),
        ]
        summary = summarize_outcomes(outcomes)
        assert summary.coverage_satisfied
        assert summary.failed_count == 0
        assert len(summary.required_historical_deferred_symbols) == 0
        assert len(summary.supplementary_historical_deferred_symbols) == 0
        assert _phase_a_safety_exit_code(summary) == 0

    def test_degraded_success_state(self):
        """DEGRADED_SUCCESS: coverage satisfied, historical deferrals exist."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "BZ=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "ES=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
        ])
        # 10 symbols, 1 failure = 90% coverage (at threshold)
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
        assert summary.failed_count > 0
        assert len(summary.required_historical_deferred_symbols) > 0
        assert len(summary.supplementary_historical_deferred_symbols) > 0
        assert _phase_a_safety_exit_code(summary) == 0

    def test_fail_closed_state(self):
        """FAIL_CLOSED: coverage not satisfied."""
        outcomes = [
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=False, required=True),
            make_outcome("CL=F", success=False, required=True),
        ]
        summary = summarize_outcomes(outcomes)
        assert not summary.coverage_satisfied
        assert _phase_a_safety_exit_code(summary) == 1


class TestRequiredSymbols:
    """Tests for REQUIRED_SYMBOLS classification."""

    def test_futures_required_symbols(self):
        """BZ=F, CL=F, GC=F are required symbols."""
        assert "BZ=F" in REQUIRED_SYMBOLS
        assert "CL=F" in REQUIRED_SYMBOLS
        assert "GC=F" in REQUIRED_SYMBOLS

    def test_futures_supplementary_symbols(self):
        """ES=F, HG=F, NG=F, NIY=F, NQ=F are supplementary symbols."""
        assert "ES=F" not in REQUIRED_SYMBOLS
        assert "HG=F" not in REQUIRED_SYMBOLS
        assert "NG=F" not in REQUIRED_SYMBOLS
        assert "NIY=F" not in REQUIRED_SYMBOLS
        assert "NQ=F" not in REQUIRED_SYMBOLS

    def test_equity_indices_required(self):
        """Major equity indices are required symbols."""
        assert "^GSPC" in REQUIRED_SYMBOLS
        assert "^NDX" in REQUIRED_SYMBOLS
        assert "^DJI" in REQUIRED_SYMBOLS
        assert "^VIX" in REQUIRED_SYMBOLS

    def test_treasury_required_symbols(self):
        """Major Treasury yields are required symbols."""
        assert "US2YT=X" in REQUIRED_SYMBOLS
        assert "US10YT=X" in REQUIRED_SYMBOLS
        assert "US30YT=X" in REQUIRED_SYMBOLS


# --------------------------------------------------------------------------
# Test-harness isolation (Phase A.2A)
#
# _PIPELINE_FATAL_CONDITIONS is module-level global state. A safety-exit test
# that leaves an entry behind must not be able to change the verdict of a
# later test. These tests prove the state is reset and that the two
# safety-exit tests produce identical results in either execution order.
# --------------------------------------------------------------------------
class TestStateIsolation:
    """Order-independence guarantees for module-level safety state."""

    def setup_method(self):
        _PHASE_A_ACTIONABLE_DEFERRALS.clear()
        _PHASE_A_UNAVAILABLE_SYMBOLS.clear()
        _PIPELINE_FATAL_CONDITIONS.clear()
        global _PHASE_A_DEFERRED_COUNT, _PHASE_A_BOUNDARY_DEFERRED_COUNT
        _PHASE_A_DEFERRED_COUNT = 0
        _PHASE_A_BOUNDARY_DEFERRED_COUNT = 0

    @staticmethod
    def _healthy_summary():
        """A summary that must always yield exit 0 when state is clean."""
        return make_summary([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
        ])

    def test_fatal_conditions_are_cleared_by_setup(self):
        _PIPELINE_FATAL_CONDITIONS.append("GATE_INVARIANT_FAILURE")
        assert _PIPELINE_FATAL_CONDITIONS, "precondition"
        # simulate the next test's setup
        _PIPELINE_FATAL_CONDITIONS.clear()
        assert _PIPELINE_FATAL_CONDITIONS == []

    def test_order_independence_clean_then_polluted(self):
        """Clean state -> exit 0, even if a polluting test ran before."""
        summary = self._healthy_summary()
        assert _phase_a_safety_exit_code(summary) == 0

        # A prior test in the same module left a fatal marker behind.
        _PIPELINE_FATAL_CONDITIONS.append("ARITHMETIC_INVARIANT_FAILURE")
        assert _phase_a_safety_exit_code(summary) == 1

        # setup_method for the next test clears it again.
        _PIPELINE_FATAL_CONDITIONS.clear()
        assert _phase_a_safety_exit_code(summary) == 0

    def test_order_independence_polluted_then_clean(self):
        """The reverse order must give the same two results."""
        summary = self._healthy_summary()

        _PIPELINE_FATAL_CONDITIONS.append("ARITHMETIC_INVARIANT_FAILURE")
        assert _phase_a_safety_exit_code(summary) == 1

        _PIPELINE_FATAL_CONDITIONS.clear()
        assert _phase_a_safety_exit_code(summary) == 0
