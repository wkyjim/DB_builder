"""Tests for the macro_indicators -> Neon mirror.

Contract under test:
  public.macro_indicators is local-authoritative. Neon carries the same schema and
  is written only by this mirror, keyed on (symbol, date), so a rerun is a no-op
  and the hourly run cannot drift from local.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from db_builder import macro_indicators_neon as min_  # noqa: E402

MIGRATION = PROJECT_ROOT / "migrations" / "20261025_macro_indicators.sql"


def _migration_columns() -> list[str]:
    """Column names declared by the CREATE TABLE block, constraints excluded."""
    body = MIGRATION.read_text(encoding="utf-8")
    body = body[body.index("CREATE TABLE"):]
    body = body[body.index("(") + 1:]
    body = body[:body.index("CONSTRAINT")]
    cols = []
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        cols.append(line.split()[0])
    return cols


class TestMigrationContract:
    def test_migration_declares_the_primary_key_on_symbol_and_date(self):
        text = MIGRATION.read_text(encoding="utf-8")
        assert re.search(
            r"CONSTRAINT\s+macro_indicators_pkey\s+PRIMARY KEY\s*\(\s*symbol\s*,\s*date\s*\)",
            text, re.I)

    def test_migration_is_idempotent(self):
        text = MIGRATION.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS" in text
        assert "CREATE INDEX IF NOT EXISTS" in text

    def test_migration_columns_match_the_local_schema(self):
        """The Neon table must mirror local exactly, or the two drift."""
        assert _migration_columns() == ["symbol", "date"] + min_.INDICATOR_COLUMNS

    def test_indicator_columns_are_all_numeric(self):
        text = MIGRATION.read_text(encoding="utf-8")
        for column in min_.INDICATOR_COLUMNS:
            assert re.search(rf"^\s*{re.escape(column)}\s+numeric", text, re.M), column

    def test_table_name_is_the_local_system_of_record(self):
        assert min_.MACRO_INDICATOR_TABLE == "public.macro_indicators"

    def test_primary_key_columns_are_not_null(self):
        text = MIGRATION.read_text(encoding="utf-8")
        assert re.search(r"^\s*symbol\s+varchar NOT NULL", text, re.M)
        assert re.search(r"^\s*date\s+date NOT NULL", text, re.M)


class TestSelectSql:
    def test_full_window_has_no_predicate(self):
        sql, params = min_.select_sql()
        assert "WHERE" not in sql
        assert params == {}
        assert sql.endswith("ORDER BY symbol, date")

    def test_backfill_window_is_bounded_by_since(self):
        sql, params = min_.select_sql("2026-05-01")
        assert "date >= :since" in sql
        assert params == {"since": "2026-05-01"}

    def test_select_projects_every_indicator_column(self):
        sql, _ = min_.select_sql()
        for column in min_.INDICATOR_COLUMNS:
            assert column in sql, column

    def test_count_sql_mirrors_the_same_window(self):
        _, params = min_.count_sql("2026-05-01")
        assert params == {"since": "2026-05-01"}
        assert "date >= :since" in min_.count_sql("2026-05-01")[0]
        assert "WHERE" not in min_.count_sql()[0]


class _FakeEngine:
    """Captures execute() calls; answers SELECT count(*) with a fixed value."""

    def __init__(self, count, *, fail_on=None):
        self.count = count
        self.calls: list[str] = []
        self.fail_on = fail_on

    def connect(self):
        return self

    def begin(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("injected failure")
        return _Count(self.count)


class _Count:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


def _frame(rows=3):
    return pd.DataFrame([
        {"symbol": f"S{i}", "date": "2026-10-20", "ma_5": float(i),
         **{c: None for c in min_.INDICATOR_COLUMNS[1:]}}
        for i in range(rows)
    ])


class TestSync:
    def test_sync_noop_when_local_is_empty(self, monkeypatch):
        monkeypatch.setattr(min_, "fetch_macro_indicators", lambda *a, **k: pd.DataFrame())
        monkeypatch.setattr(min_, "setup_macro_indicators_schema", lambda *a, **k: None)
        neon = _FakeEngine(0)
        result = min_.sync_macro_indicators_to_neon(_FakeEngine(0), neon, since="2026-05-01")
        assert result["uploaded"] == 0
        assert result["pruned_rows"] == 0
        assert neon.calls == []

    def test_sync_reports_the_window_it_sent(self, monkeypatch):
        monkeypatch.setattr(min_, "fetch_macro_indicators", lambda *a, **k: _frame(3))
        monkeypatch.setattr(min_, "setup_macro_indicators_schema", lambda *a, **k: None)
        monkeypatch.setattr(min_, "bulk_upsert_macro_indicators", lambda *a, **k: 3)
        monkeypatch.setattr(min_, "prune_macro_indicators_before", lambda *a, **k: 42)
        neon = _FakeEngine(99)
        result = min_.sync_macro_indicators_to_neon(_FakeEngine(0), neon, since="2026-05-01")
        assert result == {"local_rows": 3, "uploaded": 3, "remote_rows": 99,
                          "failed": 0, "pruned_rows": 42, "since": "2026-05-01"}

    def test_sync_rejects_a_short_remote(self, monkeypatch):
        """If Neon holds fewer rows than local sent, the sync must not report success."""
        monkeypatch.setattr(min_, "fetch_macro_indicators", lambda *a, **k: _frame(500))
        monkeypatch.setattr(min_, "setup_macro_indicators_schema", lambda *a, **k: None)
        monkeypatch.setattr(min_, "bulk_upsert_macro_indicators", lambda *a, **k: 500)
        monkeypatch.setattr(min_, "prune_macro_indicators_before", lambda *a, **k: 0)
        with pytest.raises(RuntimeError, match="macro_indicators Neon sync failed"):
            min_.sync_macro_indicators_to_neon(
                _FakeEngine(0), _FakeEngine(1), since="2026-05-01", retries=2)

    def test_sync_retries_a_transient_neon_failure(self, monkeypatch):
        monkeypatch.setattr(min_, "fetch_macro_indicators", lambda *a, **k: _frame(2))
        monkeypatch.setattr(min_, "setup_macro_indicators_schema", lambda *a, **k: None)
        monkeypatch.setattr(min_, "prune_macro_indicators_before", lambda *a, **k: 0)

        attempts = {"n": 0}

        def _flaky(*a, **k):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient")
            return 2

        monkeypatch.setattr(min_, "bulk_upsert_macro_indicators", _flaky)
        result = min_.sync_macro_indicators_to_neon(
            _FakeEngine(0), _FakeEngine(7), retries=3)
        assert attempts["n"] == 2
        assert result["failed"] == 0

    def test_default_window_is_the_served_mirror_boundary(self):
        """Without an explicit window the sync must not mirror full history."""
        import inspect

        from db_builder.config import NEON_MIRROR_SINCE
        default = inspect.signature(min_.sync_macro_indicators_to_neon).parameters["since"].default
        assert default == NEON_MIRROR_SINCE

    def test_prune_deletes_only_rows_outside_the_window(self):
        import inspect
        source = inspect.getsource(min_.prune_macro_indicators_before)
        assert "DELETE FROM" in source
        assert "date < date :since" in source


class TestHourlyWiring:
    def test_hourly_workflow_runs_the_neon_indicator_sync(self):
        text = (PROJECT_ROOT / "scripts" / "auto_macro_db.ps1").read_text(encoding="utf-8")
        assert "sync_macro_indicators_neon.py" in text

    def test_indicators_are_recomputed_before_they_are_mirrored(self):
        text = (PROJECT_ROOT / "scripts" / "auto_macro_db.ps1").read_text(encoding="utf-8")
        assert text.index("recalc_all.py") < text.index("sync_macro_indicators_neon.py")
