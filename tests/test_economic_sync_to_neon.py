from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts.economic_sync_to_neon import COLUMNS, fetch_local_rows


def test_economic_sync_columns_include_raw_payload():
    assert COLUMNS[-1] == "raw_payload"
    assert "series_id" in COLUMNS
    assert "realtime_start" in COLUMNS


def test_fetch_local_rows_filters_to_us_country():
    class FakeEngine:
        pass

    captured = {}

    def fake_read_sql(query, engine, params):
        captured["query"] = str(query)
        captured["params"] = params
        return []

    import scripts.economic_sync_to_neon as sync_module

    original = sync_module.pd.read_sql
    sync_module.pd.read_sql = fake_read_sql
    try:
        fetch_local_rows(FakeEngine(), start_date="2026-01-01")
    finally:
        sync_module.pd.read_sql = original

    assert "country = 'US'" in captured["query"]
    assert captured["params"] == {"start_date": "2026-01-01"}


def test_main_does_not_connect_to_neon(monkeypatch, capsys):
    import scripts.economic_sync_to_neon as sync_module

    monkeypatch.setattr(sync_module, "parse_args", lambda: None)

    sync_module.main()

    assert "Neon sync is permanently disabled" in capsys.readouterr().out
    assert not hasattr(sync_module, "bulk_upsert_neon")
    assert not hasattr(sync_module, "create_neon_economic_indicators_table")
