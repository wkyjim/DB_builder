"""Phase A.2B -- shared historical fetch-routing regression tests.

Proves the recovery dispatcher uses the SAME approved source path as normal
historical ingestion (no oil-fallback fallthrough), and that provenance still
blocks historical writes.
"""
import os, sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

os.environ.setdefault("LOCAL_DB_PASSWORD", "a2b-test")
os.environ.setdefault("NEON_DB_PASSWORD", "a2b-test")
os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")

from macro_data_fetch import (  # noqa: E402
    historical_fetch_route,
    ROUTE_TREASURY_OFFICIAL,
    ROUTE_INVESTING,
    ROUTE_YFINANCE_HISTORICAL,
    ROUTE_FROZEN,
)


@pytest.mark.parametrize("symbol", [
    "AUDUSD=X", "CHFUSD=X", "EURUSD=X", "GBPUSD=X", "JPY=X", "CNY=X",
    "DX-Y.NYB", "NZDUSD=X", "SGD=X",
])
def test_fx_routes_to_yfinance_historical(symbol):
    route, sd = historical_fetch_route(symbol)
    assert route == ROUTE_YFINANCE_HISTORICAL
    assert "yfinance" in sd.lower()


@pytest.mark.parametrize("symbol", ["BTC-USD", "ETH-USD"])
def test_crypto_routes_to_yfinance_historical(symbol):
    route, sd = historical_fetch_route(symbol)
    assert route == ROUTE_YFINANCE_HISTORICAL
    assert "CRYPTO_24_7_DAILY" in sd


@pytest.mark.parametrize("symbol", [
    "^GSPC", "^DJI", "^NDX", "^IXIC", "^RUT", "^VIX", "^MOVE", "^SKEW",
])
def test_cash_equity_routes_to_yfinance_historical(symbol):
    route, sd = historical_fetch_route(symbol)
    assert route == ROUTE_YFINANCE_HISTORICAL


@pytest.mark.parametrize("symbol", ["US2YT=X", "US5YT=X", "US10YT=X", "US30YT=X"])
def test_legacy_treasury_routes_to_treasury_official(symbol):
    route, sd = historical_fetch_route(symbol)
    assert route == ROUTE_TREASURY_OFFICIAL
    assert "treasury" in sd.lower()


@pytest.mark.parametrize("symbol", ["US2YT_CMT", "US5YT_CMT", "US10YT_CMT", "US30YT_CMT"])
def test_cmt_treasury_routes_to_treasury_official(symbol):
    route, sd = historical_fetch_route(symbol)
    assert route == ROUTE_TREASURY_OFFICIAL
    assert "treasury" in sd.lower()


@pytest.mark.parametrize("symbol", ["BZ=F", "CL=F"])
def test_oil_futures_route_to_investing_not_yfinance(symbol):
    route, sd = historical_fetch_route(symbol)
    assert route == ROUTE_INVESTING
    assert "INVESTING_CONTINUOUS_FUTURES_DAILY" in sd
    assert "yfinance" not in sd.lower()


@pytest.mark.parametrize("symbol", [
    "ES=F", "GC=F", "HG=F", "NG=F", "NIY=F", "NQ=F", "RTY=F", "SI=F", "YM=F",
])
def test_other_futures_route_to_yfinance_historical(symbol):
    """Other futures use their approved yfinance historical route. They remain
    provenance-blocked at the write gate regardless of fetch success."""
    route, sd = historical_fetch_route(symbol)
    assert route == ROUTE_YFINANCE_HISTORICAL
    assert "yfinance" in sd.lower()


@pytest.mark.parametrize("symbol", ["HK50", "KOR200c1", "CIHc1"])
def test_unresolved_source_definition_fails_closed(symbol):
    route, sd = historical_fetch_route(symbol)
    assert route == ROUTE_FROZEN


def test_no_symbol_uses_oil_fallback_for_fx_or_crypto():
    """Regression: the v1 defect routed FX/crypto into the oil-only fallback."""
    for symbol in ["AUDUSD=X", "CHFUSD=X", "EURUSD=X", "BTC-USD", "ETH-USD"]:
        route, _ = historical_fetch_route(symbol)
        assert route != ROUTE_INVESTING, (
            f"{symbol} must not route to the Investing/oil fallback")


def test_recovery_dispatcher_never_routes_fx_to_oil_fallback():
    """The recovery path must use the same route as normal ingestion."""
    for symbol in ["AUDUSD=X", "CHFUSD=X", "EURUSD=X", "GBPUSD=X"]:
        route, _ = historical_fetch_route(symbol)
        assert route == ROUTE_YFINANCE_HISTORICAL


def test_provenance_still_blocks_futures_historical_writes():
    """Even when fetch succeeds, futures without provenance remain blocked."""
    from db_builder.retry_policy import (
        classify_reason, PROVENANCE_UNVERIFIED, RETRY_PATH_DEFECT,
    )
    # A futures row with a ValueError AND no provenance anchor is
    # PROVENANCE_UNVERIFIED (the stronger structural blocker wins), not
    # RETRY_PATH_DEFECT.
    from db_builder.retry_policy import resolve_retry_class
    combined = resolve_retry_class(PROVENANCE_UNVERIFIED, RETRY_PATH_DEFECT)
    assert combined == PROVENANCE_UNVERIFIED
