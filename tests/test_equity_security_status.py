from datetime import date

import pandas as pd

from db_builder.equity_security_status import (
    build_security_status,
    classify_security_type,
    is_core_coverage_security,
    is_plain_equity,
)


def test_classifies_non_core_exchange_instruments():
    assert classify_security_type("AACOW", "Abony Acquisition Corp Wt") == "warrant"
    assert classify_security_type("AACPR", "Apogee Acquisition Corp Rt") == "right"
    assert classify_security_type("AESPU", "Aeon Acquisition I Corp Unit Cons") == "unit"
    assert classify_security_type("ETHA", "iShares Ethereum Trust ETF") == "etf"
    assert classify_security_type("BTCW", "WisdomTree Bitcoin Fund") == "fund"
    assert classify_security_type("AAPL", "Apple Inc") == "common_stock"
    assert not is_core_coverage_security("AACOW", "Abony Acquisition Corp Wt", 0.25)
    assert is_core_coverage_security("AAPL", "Apple Inc", 200)


def test_status_does_not_claim_delisted_without_authoritative_evidence():
    frame = pd.DataFrame(
        [
            {
                "ticker": "EMPTY",
                "name": "Empty",
                "first_valid_date": None,
                "last_valid_date": None,
                "valid_price_rows": 0,
                "distinct_close_count": 0,
            },
            {
                "ticker": "STALE",
                "name": "Stale Inc",
                "first_valid_date": date(2025, 1, 2),
                "last_valid_date": date(2026, 4, 1),
                "valid_price_rows": 100,
                "distinct_close_count": 90,
            },
        ]
    )
    status = build_security_status(frame, date(2026, 7, 8)).set_index("ticker")

    assert status.loc["EMPTY", "lifecycle_status"] == "no_valid_price"
    assert status.loc["STALE", "lifecycle_status"] == "inactive_candidate"
    assert "delisted" not in set(status["lifecycle_status"])


def test_classifies_series_shares_as_preferred_but_not_class_shares():
    """A Series X share is a preferred; a Class X share is still common stock."""
    assert classify_security_type("ABR_D", "Arbor Realty Trust Inc Series D") == "preferred"
    assert classify_security_type("ACR_D", "ACRES Commercial Realty Corp Series D") == "preferred"
    assert classify_security_type("BRK_B", "Berkshire Hathaway Inc Class B") == "common_stock"
    assert classify_security_type("ADC_A", "Agree Realty Corp Series A Pfd") == "preferred"


def test_detects_option_chain_symbols():
    assert classify_security_type("AAPL26", None) == "option"
    assert classify_security_type("AAPL2026", None) == "option"
    assert classify_security_type("AAPL260119C00150000", None) == "option"
    # A named security keeps its name-derived type even if the symbol looks numeric.
    assert classify_security_type("AAPL26", "Apple Inc") == "common_stock"


def test_is_plain_equity_marks_every_non_equity_class():
    """One flag covers warrant/unit/preferred/option/delisted/ETF."""
    for non_equity in ("warrant", "unit", "preferred", "option", "etf", "right", "debt"):
        assert is_plain_equity(non_equity, "active") is False, non_equity

    assert is_plain_equity("common_stock", "active") is True
    assert is_plain_equity("adr", "active") is True
    assert is_plain_equity("common_stock", "intermittent") is True
    # A delisted / price-less common stock is no longer a plain equity.
    assert is_plain_equity("common_stock", "inactive_candidate") is False
    assert is_plain_equity("common_stock", "no_valid_price") is False


def test_build_security_status_populates_is_plain_equity():
    frame = pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "name": "Apple Inc",
                "first_valid_date": date(2026, 10, 20),
                "last_valid_date": date(2026, 10, 23),
                "valid_price_rows": 4,
                "distinct_close_count": 4,
            },
            {
                "ticker": "SPY",
                "name": "SPDR S&P 500 ETF Trust",
                "first_valid_date": date(2026, 10, 20),
                "last_valid_date": date(2026, 10, 23),
                "valid_price_rows": 4,
                "distinct_close_count": 4,
            },
            {
                "ticker": "ABR_D",
                "name": "Arbor Realty Trust Inc Series D",
                "first_valid_date": date(2026, 10, 20),
                "last_valid_date": date(2026, 10, 23),
                "valid_price_rows": 4,
                "distinct_close_count": 4,
            },
        ]
    )
    status = build_security_status(frame, date(2026, 10, 23)).set_index("ticker")

    assert status.loc["AAPL", "is_plain_equity"] is True or bool(status.loc["AAPL", "is_plain_equity"]) is True
    assert not bool(status.loc["SPY", "is_plain_equity"])
    assert not bool(status.loc["ABR_D", "is_plain_equity"])
