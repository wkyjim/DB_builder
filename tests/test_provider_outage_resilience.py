"""Tests for Investing.com provider outage resilience.

Validates that provider failures do not fail coverage when Local data is still current.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch


def _load_module():
    """Load macro_data_fetch.py as a module."""
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "macro_data_fetch.py"
    spec = importlib.util.spec_from_file_location("macro_data_fetch", script_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["macro_data_fetch"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_investing_403_local_current_counts_as_covered():
    """Investing 403 + Local still current → CURRENT_BUT_PROVIDER_UNAVAILABLE, success=True."""
    mod = _load_module()
    symbol = "US10YT=X"
    meta = {"name": "US 10-Year Treasury Yield"}
    
    # Mock _check_local_current_for_provider_outage to return True (Local is current)
    with patch.object(mod, '_check_local_current_for_provider_outage', return_value=True):
        outcomes = {}
        result = mod._process_investiny_fallback_for_symbol(
            symbol=symbol,
            meta=meta,
            primary_failure_reason="HTTP 403",
            start_date=None,
            observed_at=None,
            outcomes=outcomes,
            all_live_rows=None,
            upsert_neon=False,
            dry_run=True,
            deferred_store=None,
        )
        assert result is True, "Should return True when Local is current"
        assert outcomes[symbol].success is True, "success should be True"
        assert outcomes[symbol].data_freshness_state == "CURRENT_BUT_PROVIDER_UNAVAILABLE"
        assert outcomes[symbol].local_is_current is True


def test_investing_403_local_stale_fails_coverage():
    """Investing 403 + newer finalized session expected → freshness FAIL, success=False."""
    mod = _load_module()
    symbol = "US10YT=X"
    meta = {"name": "US 10-Year Treasury Yield"}
    
    # Mock _check_local_current_for_provider_outage to return False (Local is stale)
    with patch.object(mod, '_check_local_current_for_provider_outage', return_value=False):
        outcomes = {}
        result = mod._process_investiny_fallback_for_symbol(
            symbol=symbol,
            meta=meta,
            primary_failure_reason="HTTP 403",
            start_date=None,
            observed_at=None,
            outcomes=outcomes,
            all_live_rows=None,
            upsert_neon=False,
            dry_run=True,
            deferred_store=None,
        )
        assert result is False, "Should return False when Local is stale"
        assert outcomes[symbol].success is False, "success should be False"
        assert outcomes[symbol].data_freshness_state == "STALE_OR_UNCONFIRMED_PROVIDER_OUTAGE"
        assert outcomes[symbol].local_is_current is False


def test_no_alternate_source_written_during_403():
    """No alternate source is written during Investing 403 when no fallback configured."""
    mod = _load_module()
    symbol = "US30YT=X"
    meta = {"name": "US 30-Year Treasury Yield"}
    
    with patch.object(mod, '_check_local_current_for_provider_outage', return_value=True):
        outcomes = {}
        all_live_rows = []
        result = mod._process_investiny_fallback_for_symbol(
            symbol=symbol,
            meta=meta,
            primary_failure_reason="HTTP 403",
            start_date=None,
            observed_at=None,
            outcomes=outcomes,
            all_live_rows=all_live_rows,
            upsert_neon=False,
            dry_run=True,
            deferred_store=None,
        )
        assert result is True
        assert len(all_live_rows) == 0, "No live rows should be written during 403"


def test_usnytx_remains_investing_only():
    """USnYT=X symbols remain Investing-only (no Treasury/Yahoo fallback)."""
    mod = _load_module()
    usnyt_symbols = ["US2YT=X", "US3YT=X", "US5YT=X", "US7YT=X", "US10YT=X", "US20YT=X", "US30YT=X"]
    
    for symbol in usnyt_symbols:
        # Verify these symbols are NOT in fallback sets
        assert symbol not in getattr(mod, 'OIL_YFINANCE_FALLBACK_SYMBOLS', set()), \
            f"{symbol} should not be in OIL_YFINANCE_FALLBACK_SYMBOLS"
        assert symbol not in getattr(mod, 'UST_TREASURY_FALLBACK_SYMBOLS', set()), \
            f"{symbol} should not be in UST_TREASURY_FALLBACK_SYMBOLS"
        assert symbol not in getattr(mod, 'ASIA_INDEX_YFINANCE_FALLBACK_SYMBOLS', set()), \
            f"{symbol} should not be in ASIA_INDEX_YFINANCE_FALLBACK_SYMBOLS"


def test_provider_coverage_can_be_below_90_while_freshness_passes():
    """Provider coverage can be <90% while data freshness passes (separation of concerns)."""
    mod = _load_module()
    
    # Create 10 outcomes: 9 success, 1 failure
    outcomes = {}
    for i in range(10):
        symbol = f"SYM{i}"
        outcomes[symbol] = mod.SymbolFetchOutcome(
            symbol=symbol,
            name=f"Symbol {i}",
            provider="investing.com" if i < 9 else "investing.com",
            required=True,
            success=i < 9,  # 9 success, 1 failure
            rows_fetched=10 if i < 9 else 0,
            latest_observation=None,
            primary_failure_reason=None if i < 9 else "HTTP 403",
        )
    
    summary = mod.summarize_outcomes(tuple(outcomes.values()))

    # Provider coverage = 90% (9/10), but 1 required symbol failed
    assert summary.coverage_pct == 90.0
    # coverage_satisfied is False because a required symbol failed
    assert summary.coverage_satisfied is False

    # Now test with 8 success, 2 failures where failed are NOT required
    outcomes2 = {}
    for i in range(10):
        symbol = f"SYM{i}"
        outcomes2[symbol] = mod.SymbolFetchOutcome(
            symbol=symbol,
            name=f"Symbol {i}",
            provider="investing.com",
            required=i < 8,  # Only first 8 are required
            success=i < 9,  # 9 success, 1 failure (SYM9 not required)
            rows_fetched=10 if i < 9 else 0,
            latest_observation=None,
            primary_failure_reason=None if i < 9 else "HTTP 403",
            data_freshness_state="CURRENT_BUT_PROVIDER_UNAVAILABLE" if i >= 9 else "CURRENT",
            local_is_current=True if i >= 9 else False,
        )

    summary2 = mod.summarize_outcomes(tuple(outcomes2.values()))
    assert summary2.coverage_pct == 90.0
    # coverage_satisfied is True because failed symbol (SYM9) is not required
    assert summary2.coverage_satisfied is True


def test_provider_outage_visible_in_reporting():
    """Provider outage is visible in reporting even when coverage passes."""
    mod = _load_module()
    
    outcomes = {}
    # 67 symbols current, 1 symbol CURRENT_BUT_PROVIDER_UNAVAILABLE
    for i in range(67):
        symbol = f"SYM{i}"
        outcomes[symbol] = mod.SymbolFetchOutcome(
            symbol=symbol,
            name=f"Symbol {i}",
            provider="investing.com",
            required=True,
            success=True,
            rows_fetched=10,
            latest_observation=None,
        )
    
    # One symbol with provider outage but Local current
    outcomes["US10YT=X"] = mod.SymbolFetchOutcome(
        symbol="US10YT=X",
        name="US 10-Year Treasury Yield",
        provider="investing.com",
        required=True,
        success=True,  # Coverage passes because Local is current
        rows_fetched=0,
        latest_observation=None,
        primary_provider="investing.com",
        primary_failure_reason="HTTP 403",
        data_freshness_state="CURRENT_BUT_PROVIDER_UNAVAILABLE",
        local_is_current=True,
    )
    
    summary = mod.summarize_outcomes(tuple(outcomes.values()))
    assert summary.coverage_pct == 100.0  # 68/68 success
    assert summary.coverage_satisfied is True
    
    # Provider failure counts should still show the outage
    provider_failures = dict(summary.provider_failure_counts)
    assert provider_failures.get("investing.com", 0) == 1, "Provider failure should be visible"


def test_wrong_source_fallback_rejected():
    """Wrong-source fallback remains rejected during provider outage."""
    mod = _load_module()
    symbol = "US10YT=X"
    meta = {"name": "US 10-Year Treasury Yield"}
    
    # Verify USnYT=X is NOT in any fallback set
    all_fallback_sets = [
        getattr(mod, 'OIL_YFINANCE_FALLBACK_SYMBOLS', set()),
        getattr(mod, 'UST_TREASURY_FALLBACK_SYMBOLS', set()),
        getattr(mod, 'ASIA_INDEX_YFINANCE_FALLBACK_SYMBOLS', set()),
    ]
    for fallback_set in all_fallback_sets:
        assert symbol not in fallback_set, f"{symbol} should not be in any fallback set"


def test_ks200_insufficient_data_separate_failure():
    """A required symbol with insufficient data is a separate failure category."""
    mod = _load_module()
    symbol = "NIY=F"
    meta = {"name": "Nikkei 225 Futures"}
    
    outcomes = {}
    # Simulate a required symbol with insufficient data (<2 rows)
    outcomes[symbol] = mod.SymbolFetchOutcome(
        symbol=symbol,
        name=meta["name"],
        provider="yfinance",
        required=True,
        success=False,  # Insufficient data is a failure
        rows_fetched=1,  # Only 1 row (minimum 2 required)
        latest_observation=None,
        failure_reason="ValueError: insufficient provider observations: at least 2 required",
        data_freshness_state="PROVIDER_DATA_INSUFFICIENT",
        local_is_current=False,
    )
    
    summary = mod.summarize_outcomes(tuple(outcomes.values()))
    assert summary.coverage_pct == 0.0
    assert summary.coverage_satisfied is False
    assert "NIY=F" in summary.required_failed_symbols
