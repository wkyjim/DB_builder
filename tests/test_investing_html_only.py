"""Regression: Investing.com data must come from its own public pages.

Project rule (user instruction 2026-10-22, extended 2026-10-25): all Investing.com
data is fetched through the public historical-data pages and the date-range
endpoint those pages use to render their own table. The `investiny` Python API
client is blocked for this project - it answers HTTP 403 - and is explicitly
out of scope.

These tests fail if anyone wires `investiny` back into the production path.
"""
import ast
import sys
from datetime import date

import pandas as pd
import pytest

import macro_data_fetch

REPO_ROOT = macro_data_fetch.__file__.rsplit("\\", 2)[0]
MD_PATH = macro_data_fetch.__file__


# --------------------------------------------------------------------------
# 1. The production module must not import the Investing.com JSON API package.
# --------------------------------------------------------------------------
def test_production_module_does_not_import_investiny():
    src = open(MD_PATH, encoding="utf-8").read()
    assert "from investiny" not in src, (
        "macro_data_fetch.py imports investiny (Investing.com JSON API); "
        "Investing.com must go through its own pages"
    )
    assert "import investiny" not in src, (
        "macro_data_fetch.py imports investiny (Investing.com JSON API); "
        "Investing.com must go through its own pages"
    )


def test_production_module_imports_the_html_scraper():
    src = open(MD_PATH, encoding="utf-8").read()
    assert "investing_html_scraper" in src, (
        "macro_data_fetch.py does not reference investing_html_scraper; "
        "the HTML path must be wired in"
    )


# --------------------------------------------------------------------------
# 2. No module-level `investiny` import anywhere in the AST.
# --------------------------------------------------------------------------
def test_no_investiny_import_in_ast():
    tree = ast.parse(open(MD_PATH, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "investiny", "bare `import investiny` found"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "investiny", "`from investiny import ...` found"


# --------------------------------------------------------------------------
# 3. The scraper module itself must be HTML-only.
# --------------------------------------------------------------------------
def test_scraper_module_does_not_use_the_investiny_client():
    """AST-level check: the scraper must not import the investiny client.

    Source-text matching is not usable here - the scraper's own docstring
    describes both access paths it does use.
    """
    import investing_html_scraper as ihs

    tree = ast.parse(open(ihs.__file__, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "investiny", "scraper imports investiny"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "investiny", "scraper imports from investiny"

    # The scraper must fetch over HTTP(S), not through a JSON client.
    src = open(ihs.__file__, encoding="utf-8").read()
    assert "https://" in src, "scraper does not fetch over HTTPS"
    assert "curl_cffi" in src, "scraper does not use a browser TLS fingerprint"


# --------------------------------------------------------------------------
# 4. The fetch path really does call the scraper, with the date range.
# --------------------------------------------------------------------------
def test_fetch_path_calls_the_html_scraper(monkeypatch):
    seen = []

    def fake_fetch(symbol, timeout=45, start_date=None, end_date=None):
        seen.append((symbol, start_date, end_date))
        return [
            {"date": "2026-07-01", "price": 4.55, "open": 4.4,
             "high": 4.6, "low": 4.3},
        ]

    monkeypatch.setattr(
        macro_data_fetch._investing_html, "fetch_historical_data", fake_fetch
    )
    monkeypatch.setattr(
        macro_data_fetch.time, "sleep", lambda *_a, **_k: None
    )

    df, _ = macro_data_fetch.fetch_investiny_symbol_df(
        "US10YT=X", start_date=date(2026, 7, 1)
    )

    assert seen and seen[0][0] == "US10YT=X", "the HTML scraper was not called"
    assert seen[0][1] < date(2026, 7, 1), "the fetch start must be buffered earlier"
    assert seen[0][2] > date(2026, 7, 1), "the fetch end must be in the future"
    assert df.iloc[0]["Close"] == 4.55


# --------------------------------------------------------------------------
# 5. Volume is never fabricated.
# --------------------------------------------------------------------------
def test_volume_is_never_fabricated(monkeypatch):
    monkeypatch.setattr(
        macro_data_fetch._investing_html,
        "fetch_historical_data",
        lambda symbol, timeout=45, start_date=None, end_date=None: [
            {"date": "2026-07-01", "price": 4.55, "open": 4.4,
             "high": 4.6, "low": 4.3},
        ],
    )
    monkeypatch.setattr(
        macro_data_fetch.time, "sleep", lambda *_a, **_k: None
    )

    df, _ = macro_data_fetch.fetch_investiny_symbol_df(
        "US10YT=X", start_date=date(2026, 7, 1)
    )

    assert df["Volume"].isna().all(), "Volume must be NULL, never fabricated"
