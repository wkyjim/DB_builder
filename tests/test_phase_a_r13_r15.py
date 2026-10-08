"""Tests for Phase A.1 EXPECTED_FROZEN semantics.

R13: Intentional freeze only -> SUCCESS, no WARN
R14: Intentional freeze + genuine degradation -> DEGRADED_SUCCESS
R15: Expected freeze + fatal condition -> FAIL_CLOSED
"""
import pytest
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
    _render_pipeline_status_message,
    _report_per_class_freshness,
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


def clear_state():
    _PHASE_A_ACTIONABLE_DEFERRALS.clear()
    _PHASE_A_UNAVAILABLE_SYMBOLS.clear()
    _PIPELINE_FATAL_CONDITIONS.clear()
    global _PHASE_A_DEFERRED_COUNT, _PHASE_A_BOUNDARY_DEFERRED_COUNT
    _PHASE_A_DEFERRED_COUNT = 0
    _PHASE_A_BOUNDARY_DEFERRED_COUNT = 0


class TestR13_IntentionalFreezeOnly:
    """R13: Intentional freeze only -> SUCCESS, no WARN."""

    def setup_method(self):
        clear_state()

    def test_r13_legacy_freeze_only_success(self):
        """Legacy USnYT=X frozen; all operational sources healthy.
        Expected: SUCCESS; exit 0; expected_frozen populated; no DEGRADED_SUCCESS; no WARN."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "US2YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US3YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US5YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US7YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US10YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US20YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US30YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
        ])
        _PHASE_A_UNAVAILABLE_SYMBOLS.extend([
            {"symbol": "US2YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US3YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US5YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US7YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US10YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US20YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US30YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
        ])
        # All operational symbols healthy
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
            make_outcome("DX-Y.NYB", success=True, required=True),
            make_outcome("EURUSD=X", success=True, required=True),
            make_outcome("JPY=X", success=True, required=True),
            make_outcome("CNY=X", success=True, required=True),
            # Legacy Treasury frozen (live OK, historical frozen)
            make_outcome("US2YT=X", success=True, required=True),
            make_outcome("US3YT=X", success=True, required=False),
            make_outcome("US5YT=X", success=True, required=False),
            make_outcome("US7YT=X", success=True, required=False),
            make_outcome("US10YT=X", success=True, required=True),
            make_outcome("US20YT=X", success=True, required=False),
            make_outcome("US30YT=X", success=True, required=True),
        ]
        summary = summarize_outcomes(outcomes)
        assert summary.coverage_satisfied
        assert _phase_a_safety_exit_code(summary) == 0
        # expected_frozen populated
        assert len(summary.expected_frozen_symbols) == 7
        # No required or supplementary historical degradation
        assert len(summary.required_historical_deferred_symbols) == 0
        assert len(summary.supplementary_historical_deferred_symbols) == 0
        # Classification
        assert _classify_unavailable_symbol("US2YT=X", "LEGACY_EOD_DEFINITION_UNRESOLVED") == "EXPECTED_FROZEN"

    def test_r13_rendered_message_reports_frozen_count(self, capsys):
        """The healthy-cycle SUCCESS message must report expected frozen=7, never 0."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": f"US{t}YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"}
            for t in ("2", "3", "5", "7", "10", "20", "30")
        ])
        outcomes = [
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("^NDX", success=True, required=True),
            make_outcome("BZ=F", success=True, required=True),
            make_outcome("EURUSD=X", success=True, required=True),
            make_outcome("US2YT=X", success=True, required=True),
            make_outcome("US3YT=X", success=True, required=False),
            make_outcome("US5YT=X", success=True, required=False),
            make_outcome("US7YT=X", success=True, required=False),
            make_outcome("US10YT=X", success=True, required=True),
            make_outcome("US20YT=X", success=True, required=False),
            make_outcome("US30YT=X", success=True, required=True),
        ]
        summary = summarize_outcomes(outcomes)
        assert summary.coverage_satisfied
        assert _phase_a_safety_exit_code(summary) == 0

        _render_pipeline_status_message(summary)
        output = capsys.readouterr().out

        # Exact rendered form required by the release spec
        assert (
            "[MACRO UPDATE SUCCESS] coverage=100.00%; "
            "expected frozen=7 legacy Treasury historical symbols." in output
        )
        assert "expected frozen=0" not in output
        assert "[MACRO UPDATE DEGRADED SUCCESS]" not in output
        assert "[MACRO UPDATE FAILURE]" not in output

    def test_r13_healthy_frozen_only_emits_info_not_warn(self, capsys):
        """Expected-frozen-only cycle emits INFO, never WARN."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": f"US{t}YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"}
            for t in ("2", "3", "5", "7", "10", "20", "30")
        ])
        summary = summarize_outcomes([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("US2YT=X", success=True, required=True),
            make_outcome("US10YT=X", success=True, required=True),
            make_outcome("US30YT=X", success=True, required=True),
        ])
        _report_per_class_freshness(summary)
        output = capsys.readouterr().out
        assert "treasury_legacy: EXPECTED_FROZEN" in output
        assert "historical frozen=" in output


class TestR14_FreezePlusDegradation:
    """R14: Intentional freeze + genuine degradation -> DEGRADED_SUCCESS."""

    def setup_method(self):
        clear_state()

    def test_r14_freeze_plus_futures_degradation(self):
        """Legacy Treasury frozen plus current futures historical outage.
        Expected: DEGRADED_SUCCESS; futures counted in degradation;
        legacy Treasury shown separately as expected frozen; exit 0."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            # Legacy Treasury frozen
            {"symbol": "US2YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US3YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US5YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US7YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US10YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US20YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US30YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            # Required futures historical degraded (live OK)
            {"symbol": "BZ=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "CL=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "GC=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            # Supplementary futures historical degraded
            {"symbol": "ES=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "HG=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "NG=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "NIY=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
            {"symbol": "NQ=F", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"},
        ])
        _PHASE_A_UNAVAILABLE_SYMBOLS.extend([
            {"symbol": "US2YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US10YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US30YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
        ])
        # 50 symbols, 5 failures = 90% coverage
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
        # Verify mutually exclusive classification
        assert len(summary.expected_frozen_symbols) == 7
        assert len(summary.required_historical_deferred_symbols) == 3
        assert len(summary.supplementary_historical_deferred_symbols) == 5
        # No double-counting
        assert "US2YT=X" not in summary.required_historical_deferred_symbols
        assert "US2YT=X" not in summary.supplementary_historical_deferred_symbols
        assert "BZ=F" not in summary.expected_frozen_symbols
        assert "ES=F" not in summary.expected_frozen_symbols


class TestR15_FreezePlusFatal:
    """R15: Expected freeze + fatal condition -> FAIL_CLOSED."""

    def setup_method(self):
        clear_state()

    def test_r15_freeze_plus_required_live_failure(self):
        """Legacy Treasury frozen plus required live failure.
        Expected: FAIL_CLOSED; exit non-zero."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "US2YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
            {"symbol": "US10YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
        ])
        _PHASE_A_UNAVAILABLE_SYMBOLS.extend([
            {"symbol": "^GSPC", "reason": "CLOSED_UNCONFIRMED"},
        ])
        # 4 symbols, 1 required failure = 75% coverage (below 90%)
        summary = summarize_outcomes([
            make_outcome("^GSPC", success=False, required=True),
            make_outcome("BZ=F", success=True, required=True),
            make_outcome("US2YT=X", success=True, required=True),
            make_outcome("US10YT=X", success=True, required=True),
        ])
        assert not summary.coverage_satisfied
        assert _phase_a_safety_exit_code(summary) == 1
        # expected_frozen still populated
        assert len(summary.expected_frozen_symbols) == 2

    def test_r15_freeze_plus_coverage_failure(self):
        """Legacy Treasury frozen plus coverage below threshold.
        Expected: FAIL_CLOSED."""
        _PHASE_A_ACTIONABLE_DEFERRALS.extend([
            {"symbol": "US2YT=X", "reason": "LEGACY_EOD_DEFINITION_UNRESOLVED"},
        ])
        # 3 symbols, 2 required failures = 33% coverage (below 90%)
        summary = summarize_outcomes([
            make_outcome("^GSPC", success=True, required=True),
            make_outcome("BZ=F", success=False, required=True),
            make_outcome("CL=F", success=False, required=True),
        ])
        assert not summary.coverage_satisfied
        assert _phase_a_safety_exit_code(summary) == 1
        assert len(summary.expected_frozen_symbols) == 1
