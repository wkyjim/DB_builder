"""``upsert_macro`` must persist a provider label on every row.

``calculate_rows_for_symbol`` emits ``source_definition`` but not ``source``,
and nothing downstream filled it in -- yet the INSERT binds ``%(source)s``. Any
row that reached the INSERT raised::

    sqlalchemy.exc.StatementError:
        A value is required for bind parameter 'source'

That made the whole historical write path unusable: every symbol that reached
the write failed, and only rows already carrying ``source`` (from the one-off
backfill of ``public.macro``) survived.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")
os.environ.setdefault("LOCAL_DB_PASSWORD", "row-source-test")
os.environ.setdefault("NEON_DB_PASSWORD", "row-source-test")

import macro_data_fetch as m  # noqa: E402


class TestEnsureRowSources:
    def test_fetches_emit_no_source_label(self):
        """Premise: the fetch layer really does omit ``source``."""
        frame = pd.DataFrame({
            "Date": ["2026-10-14", "2026-10-15"],
            "Open": [1.0, 1.1], "High": [2.0, 2.1],
            "Low": [0.5, 0.6], "Close": [1.5, 1.6],
            "Adj Close": [1.5, 1.6], "Volume": [10, 11],
        })
        keys = set()
        for rec in m.calculate_rows_for_symbol(
            df=frame, symbol="000001.SS", name="SSE Composite Index",
            asset_type="stock_index",
        ):
            keys |= set(rec)
        assert keys, "premise changed: calculate_rows_for_symbol returned nothing"
        assert "source_definition" in keys
        assert "source" not in keys, (
            "premise changed: the fetch layer now emits 'source'; "
            "revisit this regression test")

    @pytest.mark.parametrize("symbol,expected", [
        ("000001.SS", "yfinance"),
        ("^HSI", "yfinance"),
        ("ES=F", "yfinance"),
        ("US10YT=X", "investing.com"),
        ("HK50", "investing.com"),
    ])
    def test_label_is_derived_from_the_symbol(self, symbol, expected):
        rows = [{"symbol": symbol, "date": "2026-10-15"}]
        out = m.ensure_row_sources(rows)
        assert out[0]["source"] == expected

    def test_existing_label_is_never_overwritten(self):
        rows = [{"symbol": "000001.SS", "source": "manual-override"}]
        m.ensure_row_sources(rows)
        assert rows[0]["source"] == "manual-override"

    def test_none_symbol_does_not_raise(self):
        rows = [{"symbol": None}]
        m.ensure_row_sources(rows)
        assert "source" in rows[0]
