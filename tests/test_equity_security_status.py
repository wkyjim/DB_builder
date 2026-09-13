from datetime import date

import pandas as pd

from db_builder.equity_security_status import (
    build_security_status,
    classify_security_type,
    is_core_coverage_security,
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
