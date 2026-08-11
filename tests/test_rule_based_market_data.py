from __future__ import annotations

import pandas as pd

from db_builder import rule_based_market_data


def test_fetch_macro_snapshot_merges_fresh_live_rows(monkeypatch):
    captured = {}

    def fake_safe_read_sql(engine, sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return pd.DataFrame(
            [
                {
                    "symbol": "^GSPC",
                    "name": "S&P 500",
                    "date": "2026-07-21",
                    "market_date": "2026-07-21",
                    "is_live": True,
                    "data_status": "live",
                }
            ]
        )

    monkeypatch.setattr(rule_based_market_data, "_safe_read_sql", fake_safe_read_sql)

    rows = rule_based_market_data.fetch_macro_snapshot(
        object(),
        symbols=["^GSPC"],
        live_max_age_minutes=15,
    )

    assert not rows.empty
    assert "public.macro_live" in captured["sql"]
    assert "public.macro" in captured["sql"]
    assert captured["params"]["symbols"] == ["^GSPC"]
    assert captured["params"]["live_max_age_minutes"] == 15
