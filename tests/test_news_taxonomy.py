from db_builder.news_taxonomy import canonical_theme


def test_macro_regulatory_taxonomy_expansion():
    expected = {
        "Federal Reserve": "Fed",
        "FOMC": "Monetary Policy",
        "Powell": "Fed",
        "ECB": "Central Banks",
        "Lagarde": "Central Banks",
        "BOJ": "Central Banks",
        "Ueda": "Central Banks",
        "BOE": "Central Banks",
        "Bank of England": "Central Banks",
        "Inflation": "Inflation",
        "CPI": "Inflation",
        "PPI": "Inflation",
        "Core Inflation": "Inflation",
        "Employment": "Labor Market",
        "Payrolls": "Labor Market",
        "NFP": "Labor Market",
        "Unemployment": "Labor Market",
        "Tariffs": "Trade Policy",
        "Trade War": "Trade Policy",
        "Sanctions": "Sanctions",
        "Export Controls": "Trade Policy",
        "Oil Supply": "Oil",
        "OPEC": "Oil",
        "Brent": "Oil",
        "WTI": "Oil",
        "Treasury Yields": "Rates",
        "Yield Curve": "Rates",
        "Real Rates": "Rates",
    }

    for raw, canonical in expected.items():
        assert canonical_theme(raw) == canonical
