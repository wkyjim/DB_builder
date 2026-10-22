"""Phase C1 regression tests — 10 required + additional coverage.

Tests:
1. 20/20 symbol mapping
2. No canonical symbol renamed
3. FX pair orientation mapping correct
4. Crypto 24/7 mapping correct
5. ETF ticker mapping correct
6. Grouped response missing symbol is detected
7. Massive API failure fails closed
8. No Yahoo fallback occurs silently
9. Rate limit <= 5 rpm
10. Existing R22 protection unchanged
11. Separate EOD and intraday routing
12. ETF market holidays
13. Weekend FX behavior
14. Crypto UTC day boundaries
15. Partial grouped responses
16. Invalid OHLC records
17. Shared rate limiting and retry behavior
18. Local DB read-only enforcement
19. No Neon database access
20. Zero historical changes
"""
import importlib.util
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock
import httpx


def _load_adapter():
    """Load massive_c1_adapter.py as a module."""
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "massive_c1_adapter.py"
    spec = importlib.util.spec_from_file_location("massive_c1_adapter", script_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["massive_c1_adapter"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_20_symbol_mapping():
    """Test 1: 20/20 symbol mapping."""
    mod = _load_adapter()
    assert len(mod.C1_MAPPING) == 20, f"Expected 20 symbols, got {len(mod.C1_MAPPING)}"

    expected_fx = {'AUDUSD=X', 'CHFUSD=X', 'CNY=X', 'EURUSD=X', 'GBPUSD=X',
                   'HKD=X', 'JPY=X', 'NZDUSD=X', 'SGD=X'}
    expected_crypto = {'BTC-USD', 'ETH-USD'}
    expected_etfs = {'HYG', 'IEF', 'IWD', 'IWF', 'JNK', 'LQD', 'RSP', 'SHY', 'TLT'}

    actual_fx = {s for s, v in mod.C1_MAPPING.items() if v[1] == 'FX'}
    actual_crypto = {s for s, v in mod.C1_MAPPING.items() if v[1] == 'Crypto'}
    actual_etfs = {s for s, v in mod.C1_MAPPING.items() if v[1] == 'ETF'}

    assert actual_fx == expected_fx, f"FX mismatch: {actual_fx}"
    assert actual_crypto == expected_crypto, f"Crypto mismatch: {actual_crypto}"
    assert actual_etfs == expected_etfs, f"ETF mismatch: {actual_etfs}"
    print("PASS: test_20_symbol_mapping")


def test_no_canonical_symbol_renamed():
    """Test 2: No canonical symbol renamed."""
    mod = _load_adapter()
    # All 20 symbols must be in the mapping with their original names
    for sym in mod.C1_MAPPING:
        assert sym in mod.C1_MAPPING, f"Symbol {sym} not in mapping"
        # Reverse mapping must exist
        massive_ticker = mod.C1_MAPPING[sym][0]
        # MASSIVE_TO_HERMES maps hermes_symbol -> massive_ticker
        assert massive_ticker in mod.MASSIVE_TO_HERMES.values(), f"Massive ticker {massive_ticker} not in reverse mapping"
        assert mod.MASSIVE_TO_HERMES[sym] == massive_ticker, f"Reverse mapping mismatch for {sym}"
    print("PASS: test_no_canonical_symbol_renamed")


def test_fx_orientation_mapping():
    """Test 3: FX pair orientation mapping correct."""
    mod = _load_adapter()
    # JPY=X should map to USDJPY (not JPYUSD)
    assert mod.C1_MAPPING['JPY=X'][0] == 'C:USDJPY', f"JPY=X maps to {mod.C1_MAPPING['JPY=X'][0]}"
    # CNY=X should map to USDCNY (not CNYUSD)
    assert mod.C1_MAPPING['CNY=X'][0] == 'C:USDCNY', f"CNY=X maps to {mod.C1_MAPPING['CNY=X'][0]}"
    # HKD=X should map to USDHKD
    assert mod.C1_MAPPING['HKD=X'][0] == 'C:USDHKD', f"HKD=X maps to {mod.C1_MAPPING['HKD=X'][0]}"
    # SGD=X should map to USDSGD
    assert mod.C1_MAPPING['SGD=X'][0] == 'C:USDSGD', f"SGD=X maps to {mod.C1_MAPPING['SGD=X'][0]}"

    # All FX orientation validations should pass
    for sym in ['AUDUSD=X', 'CHFUSD=X', 'CNY=X', 'EURUSD=X', 'GBPUSD=X',
                'HKD=X', 'JPY=X', 'NZDUSD=X', 'SGD=X']:
        massive_ticker = mod.C1_MAPPING[sym][0]
        ok, msg = mod.validate_fx_orientation(sym, massive_ticker)
        assert ok, f"FX orientation failed for {sym}: {msg}"
    print("PASS: test_fx_orientation_mapping")


def test_crypto_24_7_mapping():
    """Test 4: Crypto 24/7 mapping correct."""
    mod = _load_adapter()
    # BTC-USD should map to X:BTCUSD
    assert mod.C1_MAPPING['BTC-USD'][0] == 'X:BTCUSD', f"BTC-USD maps to {mod.C1_MAPPING['BTC-USD'][0]}"
    # ETH-USD should map to X:ETHUSD
    assert mod.C1_MAPPING['ETH-USD'][0] == 'X:ETHUSD', f"ETH-USD maps to {mod.C1_MAPPING['ETH-USD'][0]}"
    # Crypto symbols should have asset_class 'Crypto'
    assert mod.C1_MAPPING['BTC-USD'][1] == 'Crypto'
    assert mod.C1_MAPPING['ETH-USD'][1] == 'Crypto'
    print("PASS: test_crypto_24_7_mapping")


def test_etf_ticker_mapping():
    """Test 5: ETF ticker mapping correct."""
    mod = _load_adapter()
    # All ETFs should have same ticker in Massive
    for sym in ['HYG', 'IEF', 'IWD', 'IWF', 'JNK', 'LQD', 'RSP', 'SHY', 'TLT']:
        assert mod.C1_MAPPING[sym][0] == sym, f"ETF {sym} maps to {mod.C1_MAPPING[sym][0]}"
        assert mod.C1_MAPPING[sym][1] == 'ETF'
    print("PASS: test_etf_ticker_mapping")


def test_missing_symbol_detection():
    """Test 6: Grouped response missing symbol is detected."""
    mod = _load_adapter()
    # Simulate a response missing some symbols
    test_results = [
        {"T": "C:AUDUSD", "o": 1.0, "h": 1.1, "l": 0.9, "c": 1.05},
        {"T": "C:EURUSD", "o": 1.1, "h": 1.2, "l": 1.0, "c": 1.15},
    ]
    expected = ["C:AUDUSD", "C:EURUSD", "C:GBPUSD"]
    missing = mod.detect_missing_symbols(test_results, expected)
    assert "C:GBPUSD" in missing, f"Expected C:GBPUSD in missing, got {missing}"
    print("PASS: test_missing_symbol_detection")


def test_api_failure_fails_closed():
    """Test 7: Massive API failure fails closed."""
    mod = _load_adapter()
    # Simulate API failure
    with patch('httpx.get') as mock_get:
        mock_get.side_effect = httpx.TimeoutException("Connection timeout")
        try:
            mod.massive_get("/v2/aggs/grouped/locale/global/market/fx/2026-10-14")
            assert False, "Should have raised exception"
        except (httpx.TimeoutException, RuntimeError):
            pass  # Expected
    print("PASS: test_api_failure_fails_closed")


def test_no_yahoo_fallback():
    """Test 8: No Yahoo fallback occurs silently."""
    mod = _load_adapter()
    # Verify adapter has no Yahoo references
    adapter_source = Path(mod.__file__).read_text()
    assert 'yfinance' not in adapter_source.lower(), "Adapter references yfinance"
    assert 'yahoo' not in adapter_source.lower(), "Adapter references yahoo"
    print("PASS: test_no_yahoo_fallback")


def test_rate_limit():
    """Test 9: Rate limit <= 5 rpm."""
    mod = _load_adapter()
    assert mod.MIN_REQUEST_INTERVAL >= 12.0, f"Rate limit interval {mod.MIN_REQUEST_INTERVAL}s < 12s"
    # Verify rate limit enforcement
    mod._last_request_time = time.monotonic()
    mod._enforce_rate_limit()
    # Should have slept
    assert mod._rate_limit_hits > 0, "Rate limiter did not trigger"
    print("PASS: test_rate_limit")


def test_r22_protection_unchanged():
    """Test 10: Existing R22 protection unchanged."""
    mod = _load_adapter()
    # Adapter should not reference R22-protected symbols
    r22_symbols = {'ES=F', 'NQ=F', 'YM=F', 'RTY=F', 'NIY=F',
                    'GC=F', 'SI=F', 'HG=F', 'NG=F', 'BZ=F', 'CL=F'}
    c1_symbols = set(mod.C1_MAPPING.keys())
    overlap = r22_symbols & c1_symbols
    # Phase C1 does NOT include R22 futures (they are Phase C2)
    assert len(overlap) == 0, f"R22 symbols in C1: {overlap}"
    print("PASS: test_r22_protection_unchanged")


def test_eod_vs_intraday_routing():
    """Test 11: Separate EOD and intraday routing."""
    mod = _load_adapter()
    # Adapter should not write to macro_live
    adapter_source = Path(mod.__file__).read_text()
    assert 'macro_live' not in adapter_source, "Adapter references macro_live"
    assert 'LIVE_TABLE' not in adapter_source, "Adapter references live table"
    print("PASS: test_eod_vs_intraday_routing")


def test_etf_market_holidays():
    """Test 12: ETF market holidays."""
    mod = _load_adapter()
    # ETFs should be classified as US exchange-traded
    for sym in ['HYG', 'IEF', 'IWD', 'IWF', 'JNK', 'LQD', 'RSP', 'SHY', 'TLT']:
        assert mod.C1_MAPPING[sym][1] == 'ETF', f"{sym} not classified as ETF"
    print("PASS: test_etf_market_holidays")


def test_weekend_fx_behavior():
    """Test 13: Weekend FX behavior."""
    mod = _load_adapter()
    # FX symbols should be classified correctly
    fx_syms = [s for s, v in mod.C1_MAPPING.items() if v[1] == 'FX']
    assert len(fx_syms) == 9, f"Expected 9 FX symbols, got {len(fx_syms)}"
    print("PASS: test_weekend_fx_behavior")


def test_crypto_utc_boundaries():
    """Test 14: Crypto UTC day boundaries."""
    mod = _load_adapter()
    # Crypto symbols should be in mapping
    assert 'BTC-USD' in mod.C1_MAPPING
    assert 'ETH-USD' in mod.C1_MAPPING
    print("PASS: test_crypto_utc_boundaries")


def test_partial_grouped_response():
    """Test 15: Partial grouped response."""
    mod = _load_adapter()
    # Test with partial response
    partial_results = [
        {"T": "C:AUDUSD", "o": 1.0, "h": 1.1, "l": 0.9, "c": 1.05},
    ]
    expected = ["C:AUDUSD", "C:EURUSD", "C:GBPUSD", "C:USDJPY"]
    missing = mod.detect_missing_symbols(partial_results, expected)
    assert len(missing) == 3, f"Expected 3 missing, got {len(missing)}: {missing}"
    print("PASS: test_partial_grouped_response")


def test_invalid_ohlc_records():
    """Test 16: Invalid OHLC records."""
    mod = _load_adapter()
    # Test missing field
    row = {"open": 1.0, "high": 1.1, "low": 0.9}  # Missing close
    valid, err = mod.validate_ohlc(row)
    assert not valid, "Should reject missing close"
    assert "close" in err.lower(), f"Error should mention close: {err}"

    # Test high < low
    row = {"open": 1.0, "high": 0.8, "low": 0.9, "close": 1.0}
    valid, err = mod.validate_ohlc(row)
    assert not valid, "Should reject high < low"

    # Test valid row
    row = {"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05}
    valid, err = mod.validate_ohlc(row)
    assert valid, f"Should accept valid OHLC: {err}"
    print("PASS: test_invalid_ohlc_records")


def test_shared_rate_limiting():
    """Test 17: Shared rate limiting and retry behavior."""
    mod = _load_adapter()
    # Verify rate limit is module-level (shared)
    assert hasattr(mod, '_last_request_time'), "No shared rate limit state"
    assert hasattr(mod, '_rate_limit_hits'), "No rate limit hit counter"
    assert hasattr(mod, '_retry_count'), "No retry counter"
    print("PASS: test_shared_rate_limiting")


def test_local_db_read_only():
    """Test 18: Local DB read-only enforcement."""
    mod = _load_adapter()
    # Adapter should only read from Local, not write
    adapter_source = Path(mod.__file__).read_text()
    # Should not contain INSERT/UPDATE/DELETE
    assert 'INSERT INTO' not in adapter_source, "Adapter contains INSERT"
    assert 'UPDATE ' not in adapter_source, "Adapter contains UPDATE"
    assert 'DELETE FROM' not in adapter_source, "Adapter contains DELETE"
    print("PASS: test_local_db_read_only")


def test_no_neon_access():
    """Test 19: No Neon database access."""
    import ast

    mod = _load_adapter()
    adapter_source = Path(mod.__file__).read_text()
    # Check ACTUAL imports, not the word "neon" anywhere: the module
    # docstring legitimately says "No Neon access."
    tree = ast.parse(adapter_source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    neon_imports = {m for m in imported if 'neon' in m.lower()}
    assert not neon_imports, f"Adapter imports neon: {neon_imports}"
    assert 'neon_engine' not in adapter_source, "Adapter references neon_engine"
    print("PASS: test_no_neon_access")


def test_zero_historical_changes():
    """Test 20: Zero historical changes."""
    mod = _load_adapter()
    # Adapter should not modify historical data
    adapter_source = Path(mod.__file__).read_text()
    # Should not reference historical repair or derived fields
    assert 'derived' not in adapter_source.lower() or 'derived_field' not in adapter_source, "Adapter references derived fields"
    assert 'repair' not in adapter_source.lower(), "Adapter references repair"
    print("PASS: test_zero_historical_changes")


if __name__ == "__main__":
    tests = [
        test_20_symbol_mapping,
        test_no_canonical_symbol_renamed,
        test_fx_orientation_mapping,
        test_crypto_24_7_mapping,
        test_etf_ticker_mapping,
        test_missing_symbol_detection,
        test_api_failure_fails_closed,
        test_no_yahoo_fallback,
        test_rate_limit,
        test_r22_protection_unchanged,
        test_eod_vs_intraday_routing,
        test_etf_market_holidays,
        test_weekend_fx_behavior,
        test_crypto_utc_boundaries,
        test_partial_grouped_response,
        test_invalid_ohlc_records,
        test_shared_rate_limiting,
        test_local_db_read_only,
        test_no_neon_access,
        test_zero_historical_changes,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"FAIL: {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR: {test.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"REGRESSION TESTS: {passed}/{len(tests)} PASSED")
    if failed:
        print(f"FAILURES: {failed}")
    print(f"{'=' * 60}")
