from __future__ import annotations

import pandas as pd

from db_builder.security_classification import (
    fetch_sp500_constituents,
    normalize_ticker,
)


def test_normalize_ticker_converts_dot_to_dash():
    assert normalize_ticker("brk.b") == "BRK-B"


def test_fetch_sp500_constituents_normalizes_wikipedia_table(monkeypatch):
    table = pd.DataFrame(
        [
            {
                "Symbol": "BRK.B",
                "Security": "Berkshire Hathaway",
                "GICS Sector": "Financials",
                "GICS Sub-Industry": "Multi-Sector Holdings",
                "Date added": "2010-02-16",
                "CIK": "1067983",
            }
        ]
    )
    class Response:
        text = "<html></html>"

        def raise_for_status(self):
            return None

    monkeypatch.setattr("db_builder.security_classification.requests.get", lambda *args, **kwargs: Response())
    monkeypatch.setattr("db_builder.security_classification.pd.read_html", lambda html: [table])

    rows = fetch_sp500_constituents()

    assert rows[0]["ticker"] == "BRK-B"
    assert rows[0]["company_name"] == "Berkshire Hathaway"
    assert rows[0]["sector"] == "Financials"
    assert rows[0]["is_active"]
