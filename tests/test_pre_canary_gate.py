"""Pre-canary release gate verification tests."""
import sys
from pathlib import Path
import types

# Ensure scripts and src are in path
scripts_path = str(Path(__file__).resolve().parents[1] / "scripts")
src_path = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, scripts_path)
sys.path.insert(0, src_path)

# Mock db_builder package before importing macro_data_fetch
_mock_db_builder = types.ModuleType("db_builder")
_mock_db_builder.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "db_builder")]
sys.modules['db_builder'] = _mock_db_builder

_mock_config = types.ModuleType("db_builder.config")
_mock_config.MACRO_TABLE = "public.macro"
_mock_config.RAW_TABLE = "public.us_equities"
_mock_config.INDICATOR_TABLE = "public.us_equities_indicators"
_mock_config.local_engine = lambda **kw: types.SimpleNamespace()
_mock_config.neon_engine = lambda **kw: types.SimpleNamespace()
sys.modules['db_builder.config'] = _mock_config

import pytest
from datetime import date
import pandas as pd
from scripts.macro_data_fetch import (
    _resolve_trust_start,
    SOURCE_DEFINITION_TRUST_START,
    C3A_APPROVED_ANCHORS,
    run_c3a_gap_scan,
)


class TestTrustBoundary:
    """Verify trust boundaries are correct after remediation."""

    def test_c1_anchor_gspc_unchanged(self):
        actual = _resolve_trust_start("^GSPC", "YFINANCE_CASH_EQUITY_DAILY")
        assert actual == date(2025, 1, 3)

    def test_c1_anchor_dji_unchanged(self):
        actual = _resolve_trust_start("^DJI", "YFINANCE_CASH_EQUITY_DAILY")
        assert actual == date(2025, 1, 3)

    def test_c1_anchor_vix_unchanged(self):
        actual = _resolve_trust_start("^VIX", "YFINANCE_CASH_EQUITY_DAILY")
        assert actual == date(2026, 6, 3)

    def test_c1_anchor_btc_unchanged(self):
        actual = _resolve_trust_start("BTC-USD", "CRYPTO_24_7_DAILY")
        assert actual == date(2026, 7, 9)

    def test_c3a_anchor_gdaxi_uses_c3a_trust_start(self):
        actual = _resolve_trust_start("^GDAXI", "YFINANCE_CASH_EQUITY_DAILY")
        assert actual == date(2025, 1, 3)

    def test_c3a_anchor_tlt_uses_c3a_trust_start(self):
        actual = _resolve_trust_start("TLT", "YFINANCE_CASH_EQUITY_DAILY")
        assert actual == date(2025, 1, 2)

    def test_c3a_anchor_rsp_uses_c3a_trust_start(self):
        actual = _resolve_trust_start("RSP", "YFINANCE_CASH_EQUITY_DAILY")
        assert actual == date(2025, 1, 2)

    def test_symbol_in_neither_map_returns_none(self):
        assert _resolve_trust_start("000001.SS", "YFINANCE_CASH_EQUITY_DAILY") is None
        assert _resolve_trust_start("ES=F", "YFINANCE_FUTURES_DAILY") is None
        assert _resolve_trust_start("EURUSD=X", "YFINANCE_FX_DAILY") is None

    def test_c1_first_precedence_no_overlap(self):
        """Verify C1 and C3A anchors are disjoint (no overlap in current config)."""
        c1_keys = set(SOURCE_DEFINITION_TRUST_START.keys())
        c3a_keys = set(C3A_APPROVED_ANCHORS.keys())
        overlap = c1_keys & c3a_keys
        assert len(overlap) == 0, f"Unexpected overlap: {overlap}"

    def test_c1_first_precedence_with_mock_overlap(self):
        """Regression test: C1 anchor must NOT be weakened by C3A anchor."""
        # Simulate a future scenario where C3A has an earlier date for a C1 symbol
        # The resolver must use C1 date, not min()
        import scripts.macro_data_fetch as mf
        
        # Save original
        original_c3a = mf.C3A_APPROVED_ANCHORS.copy()
        
        try:
            # Add a C3A anchor with an earlier date for ^GSPC (C1 has 2025-01-03)
            mf.C3A_APPROVED_ANCHORS[("^GSPC", "YFINANCE_CASH_EQUITY_DAILY")] = "2024-01-01"
            
            # C1-first semantics: must return C1 date (2025-01-03), not min (2024-01-01)
            actual = mf._resolve_trust_start("^GSPC", "YFINANCE_CASH_EQUITY_DAILY")
            assert actual == date(2025, 1, 3), \
                f"C1 anchor weakened! Expected 2025-01-03 (C1), got {actual}"
            
            # C3A anchor NOT in C1: must return C3A date
            actual = mf._resolve_trust_start("^GDAXI", "YFINANCE_CASH_EQUITY_DAILY")
            assert actual == date(2025, 1, 3)
            
        finally:
            # Restore original
            mf.C3A_APPROVED_ANCHORS.clear()
            mf.C3A_APPROVED_ANCHORS.update(original_c3a)


class TestSequentialChain:
    """Verify 10/08 -> 10/09 sequential chain for ^GDAXI and TLT."""

    def _run_chain(self, sym):
        stored_dates_map = {sym: {date(2026, 10, 7)}}
        stored_pcloses_map = {sym: {date(2026, 10, 7): 100.0}}
        stored_rows_map = {sym: [{
            "date": date(2026, 10, 7), "open": 101.0, "high": 102.0,
            "low": 99.0, "close": 100.0, "adj_close": 100.0, "volume": 1000000,
        }]}

        provider_data = {sym: pd.DataFrame({
            "Date": pd.to_datetime(["2026-10-07", "2026-10-08", "2026-10-09"]),
            "Open": [101.0, 99.0, 98.0],
            "High": [102.0, 100.0, 99.0],
            "Low": [99.0, 98.0, 97.0],
            "Close": [100.0, 98.0, 97.5],
            "Adj Close": [100.0, 98.0, 97.5],
            "Volume": [1000000, 900000, 950000],
        })}

        result = run_c3a_gap_scan(
            symbols=[sym],
            stored_dates_map=stored_dates_map,
            stored_pcloses_map=stored_pcloses_map,
            stored_rows_map=stored_rows_map,
            provider_data=provider_data,
        )

        return result, stored_dates_map, stored_pcloses_map

    def test_gdaxi_sequential_chain(self):
        sym = "^GDAXI"
        result, stored_dates_map, stored_pcloses_map = self._run_chain(sym)

        # After scan, all three dates should be stored
        assert date(2026, 10, 7) in stored_dates_map[sym]
        assert date(2026, 10, 8) in stored_dates_map[sym]
        assert date(2026, 10, 9) in stored_dates_map[sym]

        # Verify prev_close chain
        # 10/08.prev_close should equal 10/07.close
        assert stored_pcloses_map[sym][date(2026, 10, 8)] == 98.0
        # 10/09.prev_close should equal 10/08.close
        assert stored_pcloses_map[sym][date(2026, 10, 9)] == 97.5

    def test_tlt_sequential_chain(self):
        sym = "TLT"
        result, stored_dates_map, stored_pcloses_map = self._run_chain(sym)

        # After scan, all three dates should be stored
        assert date(2026, 10, 7) in stored_dates_map[sym]
        assert date(2026, 10, 8) in stored_dates_map[sym]
        assert date(2026, 10, 9) in stored_dates_map[sym]

        # Verify prev_close chain
        assert stored_pcloses_map[sym][date(2026, 10, 8)] == 98.0
        assert stored_pcloses_map[sym][date(2026, 10, 9)] == 97.5
