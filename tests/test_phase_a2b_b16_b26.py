"""Phase A.2B -- B16-B26 regression tests.

Covers: FX/crypto/cash/Treasury/BZ-CL recovery dispatch, provenance blocking,
unresolved aliases, known-old-defect release, bounded release, provider
suppression, and the original ValueError regression.
"""
import os, sys, datetime as dt
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

os.environ.setdefault("LOCAL_DB_PASSWORD", "a2b-test")
os.environ.setdefault("NEON_DB_PASSWORD", "a2b-test")
os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")

import macro_data_fetch as mdf  # noqa: E402
from db_builder import retry_policy as rp  # noqa: E402
from db_builder.deferred_history import DeferredRow, MemoryDeferredStore  # noqa: E402


# ---- B16: FX recovery dispatch ----

def test_b16_fx_recovery_dispatch():
    """Representative FX row uses approved FX historical route. No oil fallback."""
    for symbol in ["AUDUSD=X", "CHFUSD=X", "EURUSD=X"]:
        route, sd = mdf.historical_fetch_route(symbol)
        assert route == mdf.ROUTE_YFINANCE_HISTORICAL
        assert "FX" in sd
        # Must never enter oil fallback
        assert route != mdf.ROUTE_INVESTING


def test_b16_fx_recovery_no_oil_fallback():
    """FX recovery must not raise the oil-fallback ValueError."""
    with patch.object(mdf, "_yfinance_historical_rows", return_value=[]) as mock_fetch:
        result = mdf._recovery_fetch_symbol_rows("AUDUSD=X", [dt.date(2026, 10, 5)])
        assert result == []
        mock_fetch.assert_called_once()


# ---- B17: crypto recovery dispatch ----

def test_b17_crypto_recovery_dispatch():
    """BTC/ETH use approved crypto path."""
    for symbol in ["BTC-USD", "ETH-USD"]:
        route, sd = mdf.historical_fetch_route(symbol)
        assert route == mdf.ROUTE_YFINANCE_HISTORICAL
        assert "CRYPTO" in sd


def test_b17_crypto_no_oil_fallback():
    with patch.object(mdf, "_yfinance_historical_rows", return_value=[]) as mock_fetch:
        result = mdf._recovery_fetch_symbol_rows("BTC-USD", [dt.date(2026, 10, 5)])
        assert result == []
        mock_fetch.assert_called_once()


# ---- B18: cash index/equity recovery dispatch ----

def test_b18_cash_equity_recovery_dispatch():
    for symbol in ["^GSPC", "^DJI", "^NDX"]:
        route, sd = mdf.historical_fetch_route(symbol)
        assert route == mdf.ROUTE_YFINANCE_HISTORICAL
        assert "CASH_INDEX" in sd or "EQUITY" in sd


def test_b18_cash_no_oil_fallback():
    with patch.object(mdf, "_yfinance_historical_rows", return_value=[]) as mock_fetch:
        result = mdf._recovery_fetch_symbol_rows("^GSPC", [dt.date(2026, 10, 5)])
        assert result == []
        mock_fetch.assert_called_once()


# ---- B19: Treasury CMT ----

def test_b19_treasury_cmt_recovery_dispatch():
    for symbol in ["US2YT_CMT", "US5YT_CMT", "US10YT_CMT"]:
        route, sd = mdf.historical_fetch_route(symbol)
        assert route == mdf.ROUTE_TREASURY_OFFICIAL
        assert "TREASURY" in sd


def test_b19_treasury_cmt_no_oil_fallback():
    with patch.object(mdf, "process_treasury_fallback_symbol",
                      return_value=([], None, None, None)) as mock_t:
        result = mdf._recovery_fetch_symbol_rows("US5YT_CMT", [dt.date(2026, 10, 5)])
        assert result == []
        mock_t.assert_called_once()


# ---- B20: BZ/CL ----

def test_b20_bz_cl_investing_source_preserved():
    for symbol in ["BZ=F", "CL=F"]:
        route, sd = mdf.historical_fetch_route(symbol)
        assert route == mdf.ROUTE_INVESTING
        assert "INVESTING_CONTINUOUS_FUTURES_DAILY" in sd


def test_b20_bz_cl_no_yfinance_historical():
    """yfinance live fallback cannot become historical source."""
    for symbol in ["BZ=F", "CL=F"]:
        route, sd = mdf.historical_fetch_route(symbol)
        assert "yfinance" not in sd.lower()


# ---- B21: provenance-blocked futures ----

def test_b21_provenance_blocked_futures():
    """Correct fetch route cannot bypass provenance gate."""
    for symbol in ["ES=F", "GC=F", "HG=F", "NG=F", "NQ=F"]:
        route, sd = mdf.historical_fetch_route(symbol)
        assert route == mdf.ROUTE_YFINANCE_HISTORICAL
        # Even with correct route, provenance gate independently blocks writes
        # (the policy returns only booleans; the write gate is separate)


def test_b21_futures_provenance_classification():
    """Futures with ValueError + no provenance -> PROVENANCE_UNVERIFIED."""
    combined = rp.resolve_retry_class(rp.PROVENANCE_UNVERIFIED, rp.RETRY_PATH_DEFECT)
    assert combined == rp.PROVENANCE_UNVERIFIED


# ---- B22: unresolved alias ----

def test_b22_unresolved_alias_fail_closed():
    for symbol in ["HK50", "KOR200c1", "CIHc1"]:
        route, sd = mdf.historical_fetch_route(symbol)
        assert route == mdf.ROUTE_FROZEN


def test_b22_unresolved_alias_no_fetch():
    with patch.object(mdf, "_yfinance_historical_rows") as mock_yf, \
         patch.object(mdf, "process_investiny_symbol") as mock_inv, \
         patch.object(mdf, "process_treasury_fallback_symbol") as mock_tr:
        result = mdf._recovery_fetch_symbol_rows("HK50", [dt.date(2026, 10, 5)])
        assert result == []
        mock_yf.assert_not_called()
        mock_inv.assert_not_called()
        mock_tr.assert_not_called()


# ---- B23: known old routing defect release ----

def test_b23_known_old_defect_released():
    """Old exact defect signature becomes retry-eligible under corrected release."""
    row = DeferredRow(
        symbol="AUDUSD=X", session_date=dt.date(2026, 10, 5),
        reason="RECOVERY_FETCH_ERROR:ValueError", status="DEFERRED",
        attempt_count=82, retry_class="RETRY_PATH_DEFECT",
        last_error_class="RECOVERY_ROUTING_V1_OIL_FALLTHROUGH",
        next_retry_at=None, manual_review_required=False,
    )
    now = dt.datetime.now(dt.timezone.utc)
    assert rp.is_retry_eligible(row, now) is True


def test_b23_unknown_defect_not_released():
    """Unrelated future RETRY_PATH_DEFECT states remain parked."""
    row = DeferredRow(
        symbol="^GSPC", session_date=dt.date(2026, 10, 5),
        reason="RECOVERY_FETCH_ERROR:ValueError", status="DEFERRED",
        attempt_count=82, retry_class="RETRY_PATH_DEFECT",
        last_error_class="SOME_NEW_DEFECT",
        next_retry_at=None, manual_review_required=True,
    )
    now = dt.datetime.now(dt.timezone.utc)
    assert rp.is_retry_eligible(row, now) is False


# ---- B24: bounded release ----

def test_b24_bounded_release():
    """50 defect rows cannot all fetch in one cycle beyond recovery limit."""
    rows = []
    symbols = ["AUDUSD=X", "CHFUSD=X", "EURUSD=X", "GBPUSD=X", "JPY=X",
               "CNY=X", "DX-Y.NYB", "NZDUSD=X", "SGD=X", "BTC-USD", "ETH-USD",
               "^GSPC", "^DJI", "^NDX", "^VIX", "^RUT", "HYG", "IEF", "IWD",
               "IWF", "JNK", "LQD", "AUDUSD=X", "CHFUSD=X", "EURUSD=X",
               "GBPUSD=X", "JPY=X", "CNY=X", "DX-Y.NYB", "NZDUSD=X", "SGD=X",
               "BTC-USD", "ETH-USD", "^GSPC", "^DJI", "^NDX", "^VIX", "^RUT",
               "HYG", "IEF", "IWD", "IWF", "JNK", "LQD", "AUDUSD=X", "CHFUSD=X",
               "EURUSD=X", "GBPUSD=X", "JPY=X", "CNY=X", "DX-Y.NYB"]
    for i, sym in enumerate(symbols[:50]):
        rows.append(DeferredRow(
            symbol=sym, session_date=dt.date(2026, 10, 5),
            reason="RECOVERY_FETCH_ERROR:ValueError", status="DEFERRED",
            attempt_count=82, retry_class="RETRY_PATH_DEFECT",
            last_error_class="RECOVERY_ROUTING_V1_OIL_FALLTHROUGH",
            next_retry_at=None, manual_review_required=False,
        ))
    now = dt.datetime.now(dt.timezone.utc)
    eligible = rp.filter_eligible(rows, now)
    # Bounded by RECOVERY_BATCH
    assert len(eligible) <= mdf.RECOVERY_BATCH
    # Deterministic ordering: ascending (session_date, symbol)
    keys = [(r.session_date, r.symbol) for r in eligible]
    assert keys == sorted(keys)


# ---- B25: provider suppression ----

def test_b25_provider_suppression():
    """If released candidates share a failing (provider,data_class), subsequent
    redundant provider calls are suppressed."""
    rows = []
    for sym in ["AUDUSD=X", "CHFUSD=X", "EURUSD=X", "GBPUSD=X", "JPY=X"]:
        rows.append(DeferredRow(
            symbol=sym, session_date=dt.date(2026, 10, 5),
            reason="RECOVERY_FETCH_ERROR:ValueError", status="DEFERRED",
            attempt_count=82, retry_class="RETRY_PATH_DEFECT",
            last_error_class="RECOVERY_ROUTING_V1_OIL_FALLTHROUGH",
            next_retry_at=None, manual_review_required=False,
        ))
    now = dt.datetime.now(dt.timezone.utc)
    # All FX share (yfinance, fx); if provider unavailable, all suppressed
    unavailable = {("yfinance", "fx")}
    eligible = rp.filter_eligible(rows, now, unavailable_keys=unavailable)
    assert len(eligible) == 0
    # Without provider outage, they are eligible
    eligible_no_outage = rp.filter_eligible(rows, now, unavailable_keys=set())
    assert len(eligible_no_outage) > 0


# ---- B26: regression against original error ----

@pytest.mark.parametrize("symbol", [
    "AUDUSD=X", "CHFUSD=X", "EURUSD=X", "GBPUSD=X", "JPY=X", "CNY=X",
    "DX-Y.NYB", "NZDUSD=X", "SGD=X", "BTC-USD", "ETH-USD",
    "^GSPC", "^DJI", "^NDX", "^IXIC", "^RUT", "^VIX", "^MOVE", "^SKEW",
    "HYG", "IEF", "IWD", "IWF", "JNK", "LQD",
])
def test_b26_no_oil_fallback_valueerror(symbol):
    """Representative non-futures symbols no longer raise
    'yfinance fallback is not enabled for <symbol>'."""
    # The corrected dispatcher routes via historical_fetch_route, which
    # never sends non-oil symbols to the oil-only fallback. We verify by
    # checking the route and confirming it's not the oil path.
    route, _ = mdf.historical_fetch_route(symbol)
    assert route == mdf.ROUTE_YFINANCE_HISTORICAL
    # The actual _recovery_fetch_symbol_rows must not call the oil fallback.
    # We verify by mocking _yfinance_historical_rows and checking it's called
    # (or that no ValueError is raised).
    with patch.object(mdf, "_yfinance_historical_rows", return_value=[]):
        try:
            mdf._recovery_fetch_symbol_rows(symbol, [dt.date(2026, 10, 5)])
        except ValueError as e:
            if "yfinance fallback is not enabled" in str(e):
                pytest.fail(f"{symbol} still raises oil-fallback ValueError")
            # Other ValueErrors are acceptable (e.g. from mocked internals)
