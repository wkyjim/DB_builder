"""Simplified EOD 5-session rolling refresh.

Replaces C.3A gap-scan with a direct transactional writer.

Core rule:
  EOD_ELIGIBLE_TIME = OFFICIAL_MARKET_CLOSE_TIME + 30 minutes

Flow:
  close+30 → fetch latest 5 valid sessions → compare →
  missing→INSERT, different→UPDATE → recompute D → repair D+1

Does NOT use C2 attempt_count. Does NOT depend on deferred recovery.
"""
from __future__ import annotations

import sys
from pathlib import Path
import types

scripts_path = str(Path(__file__).resolve().parents[1] / "scripts")
src_path = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, scripts_path)
sys.path.insert(0, src_path)

_mock_db_builder = types.ModuleType("db_builder")
_mock_db_builder.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "db_builder")]
sys.modules['db_builder'] = _mock_db_builder

_mock_config = types.ModuleType("db_builder.config")
_mock_config.MACRO_TABLE = "public.macro"
_mock_config.RAW_TABLE = "public.us_equities"
_mock_config.INDICATOR_TABLE = "public.us_equities_indicators"
_mock_config.local_engine = lambda **kw: types.SimpleNamespace()
_mock_config.neon_engine = lambda **kw: types.SimpleNamespace()
sys.modules['db_builder.config'] = _mock_config

import pytest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
from sqlalchemy import text

from scripts.macro_data_fetch import (
    SYMBOL_CLOSE_MAP,
    SOURCE_DEFINITION_BY_SYMBOL,
    C3A_APPROVED_ANCHORS,
    SOURCE_DEFINITION_TRUST_START,
    symbol_date_has_closed,
    _is_r22_protected,
    round_4,
    run_eod_5session_refresh,
    canonical_normalize_daily_bar,
    fetch_yfinance_daily_frame,
)
import scripts.macro_data_fetch as _mdf

# aliases used by the NP tests
m_calculate_rows = _mdf.calculate_rows_for_symbol
m_run_daily_update = _mdf.run_daily_update
from db_builder.session_calendar import (
    CALENDAR_CLOSE_BUFFER_MINUTES,
    adapter_for,
    previous_valid_observation_date,
)




# ============================================================
# Test fixtures: in-memory fake engine
# ============================================================

from sqlalchemy import create_engine  # noqa: F401  (kept for parity)


class _Row:
    """Attribute + index access, mirroring a SQLAlchemy Row."""

    def __init__(self, d):
        self.__dict__.update(d)
        self._order = ["date", "symbol", "open", "high", "low", "close",
                       "adj_close", "volume", "prev_close", "change", "pct_chg", "amplitude"]

    def __getitem__(self, i):
        return self.__dict__[self._order[i]]


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        low = sql.strip().lower()
        if low.startswith("select"):
            if "select close from" in low:
                key = (params["s"], str(params["d"]))
                rec = self.store.get(key)
                return _FakeResult([(rec["close"],)] if rec else [])
            # SELECT * ...
            key = (params["s"], str(params["d"]))
            rec = self.store.get(key)
            if not rec:
                return _FakeResult([])
            return _FakeResult([_Row(rec)])
        if low.startswith("insert"):
            rec = {
                "date": str(params["d"]), "symbol": params["s"],
                "open": params["o"], "high": params["h"], "low": params["l"],
                "close": params["c"], "adj_close": params["ac"], "volume": params["v"],
                "prev_close": params["pc"], "change": params["ch"],
                "pct_chg": params["pct"], "amplitude": params["amp"],
            }
            self.store[(params["s"], str(params["d"]))] = rec
            return _FakeResult([])
        if low.startswith("update"):
            key = (params["s"], str(params["d"]))
            rec = self.store.get(key)
            if rec is None:
                return _FakeResult([])
            alias = {"pc": "prev_close", "ch": "change", "pct": "pct_chg", "amp": "amplitude"}
            for k, v in params.items():
                if k in ("s", "d"):
                    continue
                kk = alias.get(k, k)
                if kk in rec:
                    rec[kk] = v
            return _FakeResult([])
        raise AssertionError(f"unexpected SQL: {sql[:120]}")


class _FakeBegin:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return _FakeConn(self.store)

    def __exit__(self, *a):
        return False


class FakeEngine:
    """In-memory stand-in for local_engine. Tracks the macro rows."""

    def __init__(self, seed_rows=None):
        self.store = {}
        for r in (seed_rows or []):
            self.store[(r["symbol"], str(r["date"]))] = dict(r)

    def begin(self):
        return _FakeBegin(self.store)

    def insert(self, rows):
        for r in rows:
            self.store[(r["symbol"], str(r["date"]))] = dict(r)

    def fetch(self, symbol, d):
        rec = self.store.get((symbol, str(d)))
        if rec is None:
            return None
        return _Row(rec)

    def count(self):
        return len(self.store)


def _seed(symbol, d, close, high=None, low=None, open_=None, adj=None, volume=1000,
          prev_close=None, change=None, pct_chg=None, amplitude=None):
    high = high if high is not None else close
    low = low if low is not None else close
    open_ = open_ if open_ is not None else close
    return {
        "date": str(d), "symbol": symbol, "open": open_, "high": high, "low": low,
        "close": close, "adj_close": adj if adj is not None else close, "volume": volume,
        "prev_close": prev_close, "change": change, "pct_chg": pct_chg, "amplitude": amplitude,
    }


def _frame(rows):
    """Provider DataFrame: Date/Open/High/Low/Close/Adj Close/Volume."""
    return pd.DataFrame({
        "Date": pd.to_datetime([r[0] for r in rows]),
        "Open": [r[1] for r in rows],
        "High": [r[2] for r in rows],
        "Low": [r[3] for r in rows],
        "Close": [r[4] for r in rows],
        "Adj Close": [r[4] for r in rows],
        "Volume": [r[5] for r in rows],
    })


ALWAYS_CLOSED = lambda sym, d: True
NEVER_CLOSED = lambda sym, d: False


# ============================================================
# Minimum Tests (18 required)
# ============================================================

class TestEod5SessionRefresh:
    """Tests for simplified EOD 5-session refresh."""

    def test_before_close_30_no_write(self):
        """1. before close+30 → no write."""
        eng = FakeEngine()
        frame = _frame([("2026-10-08", 10, 11, 9, 10.5, 100)])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=NEVER_CLOSED,
        )
        assert c["rows_inserted"] == 0
        assert c["not_yet_finalized"] == 1
        assert eng.count() == 0

    def test_exactly_close_30_eligible(self):
        """2. exactly close+30 → eligible."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-07", 10.0)])
        frame = _frame([("2026-10-08", 10, 11, 9, 10.5, 100)])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        assert c["not_yet_finalized"] == 0
        assert c["sessions_checked"] == 1
        assert c["rows_inserted"] == 1

    def test_latest_five_valid_sessions_fetched(self):
        """3. latest five valid sessions fetched (NOT the earliest)."""
        eng = FakeEngine()
        # 10 consecutive weekdays; the last five are the "latest 5".
        days = ["2026-09-28", "2026-09-29", "2026-09-30",
                "2026-10-01", "2026-10-02",
                "2026-10-05", "2026-10-06", "2026-10-07",
                "2026-10-08", "2026-10-09"]
        rows = [(d, 10, 11, 9, 10.0, 100) for d in days]
        frame = _frame(rows)
        # Seed ALL prior sessions so each latest-5 session has a valid P.
        eng.insert([_seed("TLT", d, 10.0, high=11, low=9, open_=10, volume=100)
                    for d in days[:-1]])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        # Exactly the LATEST 5 sessions are evaluated; the earlier 5 are
        # untouched. Of the latest 5, four are already stored (no_change)
        # and one (2026-10-09) is missing -> inserted.
        assert c["sessions_checked"] == 5
        assert c["no_change"] == 4
        assert c["rows_inserted"] == 1
        assert eng.count() == 10

    def test_weekend_excluded(self):
        """4. weekend excluded."""
        eng = FakeEngine()
        # 2026-10-10 is Saturday, 2026-10-11 Sunday
        frame = _frame([
            ("2026-10-09", 10, 11, 9, 10.0, 100),
            ("2026-10-10", 10, 11, 9, 10.1, 100),
            ("2026-10-11", 10, 11, 9, 10.2, 100),
        ])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=True, engine=eng, close_check=ALWAYS_CLOSED,
        )
        assert c["sessions_checked"] <= 1  # only Friday

    def test_market_holiday_excluded(self):
        """5. market holiday excluded."""
        eng = FakeEngine()
        # 2026-12-25 Christmas — NYSE closed
        frame = _frame([
            ("2026-12-24", 10, 11, 9, 10.0, 100),
            ("2026-12-25", 10, 11, 9, 10.1, 100),
        ])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=True, engine=eng, close_check=ALWAYS_CLOSED,
        )
        assert c["sessions_checked"] == 1

    def test_missing_d_insert(self):
        """6. missing D → insert with prev_close = P.close."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-07", 10.0)])
        frame = _frame([("2026-10-08", 10, 11, 9, 10.5, 100)])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        assert c["rows_inserted"] == 1
        row = eng.fetch("TLT", "2026-10-08")
        assert row is not None
        assert float(row[8]) == 10.0  # prev_close == P.close

    def test_identical_d_no_update(self):
        """7. identical D → no update."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-08", 10.5, high=11, low=9, open_=10)])
        frame = _frame([("2026-10-08", 10, 11, 9, 10.5, 1000)])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        assert c["no_change"] == 1
        assert c["rows_revised"] == 0

    def test_open_revision_overwrites(self):
        """8. open revision → overwrite."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-08", 10.5, high=11, low=9, open_=10, volume=1000)])
        frame = _frame([("2026-10-08", 10.25, 11, 9, 10.5, 1000)])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        assert c["rows_revised"] == 1
        assert float(eng.fetch("TLT", "2026-10-08")[2]) == 10.25

    def test_high_low_revision_amplitude_recalculated(self):
        """9. high/low revision → amplitude recalculated."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-07", 10.0)])
        eng.insert([{**_seed("TLT", "2026-10-08", 10.5, high=11, low=9, open_=10, volume=1000),
                    "prev_close": 10.0, "change": 0.5, "pct_chg": 5.0, "amplitude": 20.0}])
        frame = _frame([("2026-10-08", 10, 12, 8, 10.5, 1000)])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        assert c["rows_revised"] == 1
        row = eng.fetch("TLT", "2026-10-08")
        assert round(float(row[11]), 4) == round((12 - 8) / 10.0 * 100, 4)

    def test_close_revision_d_recomputed(self):
        """10. close revision → D recomputed."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-07", 10.0)])
        eng.insert([{**_seed("TLT", "2026-10-08", 10.5, high=11, low=9, open_=10, volume=1000),
                    "prev_close": 10.0, "change": 0.5, "pct_chg": 5.0, "amplitude": 20.0}])
        frame = _frame([("2026-10-08", 10, 11, 9, 11.0, 1000)])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        assert c["close_revisions"] == 1
        row = eng.fetch("TLT", "2026-10-08")
        assert float(row[5]) == 11.0
        assert float(row[8]) == 10.0  # prev_close still P.close
        assert round(float(row[9]), 4) == 1.0  # change

    def test_close_revision_d1_repaired(self):
        """11. close revision → D+1 repaired."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-07", 10.0)])
        eng.insert([
            {**_seed("TLT", "2026-10-08", 10.5, high=11, low=9, open_=10, volume=1000),
             "prev_close": 10.0, "change": 0.5, "pct_chg": 5.0, "amplitude": 20.0},
            {**_seed("TLT", "2026-10-09", 10.8, high=11, low=10, open_=10.5, volume=1000),
             "prev_close": 10.5, "change": 0.3, "pct_chg": 2.857, "amplitude": 4.762},
        ])
        frame = _frame([
            ("2026-10-08", 10, 11, 9, 11.0, 1000),
            ("2026-10-09", 10.5, 11, 10, 10.8, 1000),
        ])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        assert c["close_revisions"] >= 1
        d1 = eng.fetch("TLT", "2026-10-09")
        assert float(d1[8]) == 11.0  # D+1.prev_close = new D.close

    def test_no_d2_propagation(self):
        """12. no D+2 propagation when D+1.close unchanged."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-07", 10.0)])
        eng.insert([
            {**_seed("TLT", "2026-10-08", 10.5, high=11, low=9, open_=10, volume=1000),
             "prev_close": 10.0, "change": 0.5, "pct_chg": 5.0, "amplitude": 20.0},
            {**_seed("TLT", "2026-10-09", 10.8, high=11, low=10, open_=10.5, volume=1000),
             "prev_close": 10.5, "change": 0.3, "pct_chg": 2.857, "amplitude": 4.762},
            {**_seed("TLT", "2026-10-12", 11.0, high=11.5, low=10.5, open_=10.8, volume=1000),
             "prev_close": 10.8, "change": 0.2, "pct_chg": 1.852, "amplitude": 6.481},
        ])
        frame = _frame([
            ("2026-10-08", 10, 11, 9, 11.0, 1000),
            ("2026-10-09", 10.5, 11, 10, 10.8, 1000),
            ("2026-10-12", 10.8, 11.5, 10.5, 11.0, 1000),
        ])
        run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        d2 = eng.fetch("TLT", "2026-10-12")
        # D+2 prev_close stays 10.8 (D+1.close unchanged)
        assert float(d2[8]) == 10.8

    def test_volume_revision_overwrites(self):
        """13. volume revision → overwrite volume."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-08", 10.5, high=11, low=9, open_=10, volume=1000)])
        frame = _frame([("2026-10-08", 10, 11, 9, 10.5, 9999)])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        assert c["rows_revised"] == 1
        assert c["close_revisions"] == 0
        assert float(eng.fetch("TLT", "2026-10-08")[7]) == 9999

    def test_adj_close_only_no_derived_repair(self):
        """14. adj_close-only revision → no derived repair."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-07", 10.0)])
        eng.insert([{**_seed("TLT", "2026-10-08", 10.5, high=11, low=9, open_=10, adj=10.4, volume=1000),
                    "prev_close": 10.0, "change": 0.5, "pct_chg": 5.0, "amplitude": 20.0}])
        frame = _frame([("2026-10-08", 10, 11, 9, 10.5, 1000)])
        frame["Adj Close"] = [10.9]
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        row = eng.fetch("TLT", "2026-10-08")
        assert float(row[9]) == 0.5  # change unchanged
        assert c["close_revisions"] == 0

    def test_two_sequential_missing_oldest_first(self):
        """15. two sequential missing sessions → oldest first."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-07", 10.0)])
        frame = _frame([
            ("2026-10-08", 10, 11, 9, 10.5, 1000),
            ("2026-10-09", 10.5, 11.5, 10, 11.0, 1000),
        ])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        assert c["rows_inserted"] == 2
        d2 = eng.fetch("TLT", "2026-10-09")
        # Wednesday's prev_close must be committed Tuesday close (10.5), not stale
        assert float(d2[8]) == 10.5

    def test_second_identical_run_zero_writes(self):
        """16. second identical run → zero writes."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-08", 10.5, high=11, low=9, open_=10, volume=1000)])
        frame = _frame([("2026-10-08", 10, 11, 9, 10.5, 1000)])
        c1 = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        c2 = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        assert c2["rows_inserted"] == 0
        assert c2["rows_revised"] == 0

    def test_r22_key_blocked(self):
        """17. R22 key → blocked."""
        assert _is_r22_protected("ES=F", date(2026, 10, 5)) is True
        assert _is_r22_protected("ES=F", date(2026, 10, 8)) is False
        assert _is_r22_protected("TLT", date(2026, 10, 5)) is False

    def test_missing_close_time_mapping_reported(self):
        """18. missing close-time mapping → reported, not guessed."""
        eng = FakeEngine()
        frame = _frame([("2026-10-08", 10, 11, 9, 10.5, 100)])
        c = run_eod_5session_refresh(
            symbols=["NOT_A_REAL_SYMBOL"], provider_data={"NOT_A_REAL_SYMBOL": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )
        assert c["missing_close_time_mapping"] == 1
        assert eng.count() == 0

    def test_c2_attempt_count_not_used(self):
        """19. EOD refresh does not touch C2 attempt_count."""
        import inspect
        src = inspect.getsource(run_eod_5session_refresh)
        body = src.split('"""', 2)[2]  # strip docstring
        assert "record_deferral" not in body
        assert "record_success" not in body
        assert "attempt_count" not in body


# ============================================================
# Item 11 — GENUINE REVISION INJECTION TESTS
# ============================================================

class TestGenuineRevisionInjection:
    """Controlled provider-bar changes: prove revision vs no-op behavior."""

    def test_genuine_high_only_revision(self):
        """Same D, high changed, close unchanged:
        -> exactly 1 row revised
        -> amplitude recomputed
        -> NO D+1 repair
        """
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-07", 10.0)])
        eng.insert([
            {**_seed("TLT", "2026-10-08", 10.5, high=11.0, low=9.0, open_=10.0, volume=1000),
             "prev_close": 10.0, "change": 0.5, "pct_chg": 5.0, "amplitude": 20.0},
            {**_seed("TLT", "2026-10-09", 10.8, high=11.0, low=10.0, open_=10.5, volume=1000),
             "prev_close": 10.5, "change": 0.3, "pct_chg": 2.8571, "amplitude": 4.7619},
        ])
        before_d1 = eng.fetch("TLT", "2026-10-09")
        before_d1_pc = float(before_d1[8])
        before_d1_amp = float(before_d1[11])

        # Inject: raise D high 11.0 -> 12.0; close UNCHANGED at 10.5
        frame = _frame([
            ("2026-10-08", 10.0, 12.0, 9.0, 10.5, 1000),
            ("2026-10-09", 10.5, 11.0, 10.0, 10.8, 1000),
        ])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )

        assert c["rows_revised"] == 1, f"expected 1 revised, got {c['rows_revised']}"
        assert c["high_revisions"] == 1
        assert c["close_revisions"] == 0
        assert c["d1_repairs"] == 0, "high-only revision must NOT repair D+1"

        row = eng.fetch("TLT", "2026-10-08")
        assert float(row[2]) == 10.0                       # open untouched
        assert float(row[3]) == 12.0                       # high updated
        assert float(row[5]) == 10.5                       # close unchanged
        assert float(row[8]) == 10.0                       # prev_close untouched
        assert float(row[9]) == 0.5                        # change untouched
        assert round(float(row[11]), 4) == round((12.0 - 9.0) / 10.0 * 100, 4)  # amplitude recomputed

        after_d1 = eng.fetch("TLT", "2026-10-09")
        assert float(after_d1[8]) == before_d1_pc, "D+1.prev_close must be untouched"
        assert float(after_d1[11]) == before_d1_amp, "D+1.amplitude must be untouched"

    def test_genuine_close_revision_repairs_d1_exactly_once(self):
        """D.close revised:
        -> D derived fields recomputed
        -> D+1 repaired EXACTLY once
        """
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-07", 10.0)])
        eng.insert([
            {**_seed("TLT", "2026-10-08", 10.5, high=11.0, low=9.0, open_=10.0, volume=1000),
             "prev_close": 10.0, "change": 0.5, "pct_chg": 5.0, "amplitude": 20.0},
            {**_seed("TLT", "2026-10-09", 10.8, high=11.0, low=10.0, open_=10.5, volume=1000),
             "prev_close": 10.5, "change": 0.3, "pct_chg": 2.8571, "amplitude": 4.7619},
        ])

        # Inject: D.close 10.5 -> 11.0
        frame = _frame([
            ("2026-10-08", 10.0, 11.0, 9.0, 11.0, 1000),
            ("2026-10-09", 10.5, 11.0, 10.0, 10.8, 1000),
        ])
        c = run_eod_5session_refresh(
            symbols=["TLT"], provider_data={"TLT": frame},
            dry_run=False, engine=eng, close_check=ALWAYS_CLOSED,
        )

        assert c["close_revisions"] == 1
        assert c["d1_repairs"] == 1, f"D+1 must be repaired exactly once, got {c['d1_repairs']}"

        d = eng.fetch("TLT", "2026-10-08")
        assert float(d[5]) == 11.0                          # close revised
        assert float(d[8]) == 10.0                          # prev_close = P.close
        assert float(d[9]) == 1.0                           # change = 11.0 - 10.0
        assert round(float(d[10]), 4) == 10.0               # pct_chg = 1.0/10.0*100

        d1 = eng.fetch("TLT", "2026-10-09")
        assert float(d1[8]) == 11.0                         # D+1.prev_close = new D.close
        assert round(float(d1[9]), 4) == round(10.8 - 11.0, 4)   # -0.2
        assert round(float(d1[10]), 4) == round((10.8 - 11.0) / 11.0 * 100, 4)
        assert round(float(d1[11]), 4) == round((11.0 - 10.0) / 11.0 * 100, 4)

    def test_genuine_revision_idempotent_second_run(self):
        """After a genuine revision is applied, an identical rerun writes zero."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-07", 10.0)])
        eng.insert([{**_seed("TLT", "2026-10-08", 10.5, high=11.0, low=9.0, open_=10.0, volume=1000),
                     "prev_close": 10.0, "change": 0.5, "pct_chg": 5.0, "amplitude": 20.0}])
        frame = _frame([("2026-10-08", 10.0, 12.0, 9.0, 10.5, 1000)])

        r1 = run_eod_5session_refresh(symbols=["TLT"], provider_data={"TLT": frame},
                                      dry_run=False, engine=eng, close_check=ALWAYS_CLOSED)
        r2 = run_eod_5session_refresh(symbols=["TLT"], provider_data={"TLT": frame},
                                      dry_run=False, engine=eng, close_check=ALWAYS_CLOSED)
        assert r1["rows_revised"] == 1
        assert r2["rows_inserted"] == 0
        assert r2["rows_revised"] == 0
        assert r2["d1_repairs"] == 0


# ============================================================
# Item 14 — NORMALIZATION PARITY TESTS (NP-1 .. NP-7)
# ============================================================

class TestNormalizationParity:
    """Parity between production ingestion and refresh normalization."""

    def test_np1_ingestion_and_refresh_normalize_identically(self):
        """NP-1: production ingestion and refresh normalize an identical raw
        bar identically."""
        from scripts.macro_data_fetch import canonical_normalize_daily_bar
        import inspect
        # calculate_rows_for_symbol must call the SAME function
        src = inspect.getsource(m_calculate_rows)
        assert "canonical_normalize_daily_bar" in src
        src2 = inspect.getsource(run_eod_5session_refresh)
        assert "canonical_normalize_daily_bar" in src2

        bar = pd.Series({
            "Open": 77.16999816894531, "High": 77.58000183105469,
            "Low": 77.06999969482422, "Close": 77.27999877929688,
            "Adj Close": 77.27999877929688, "Volume": 31085900.0,
        })
        a = canonical_normalize_daily_bar(bar)
        b = canonical_normalize_daily_bar(bar)
        assert a == b
        assert a == {"open": 77.17, "high": 77.58, "low": 77.07,
                     "close": 77.28, "adj_close": 77.28, "volume": 31085900.0}

    def test_np2_batch_route_and_refresh_route_identical(self):
        """NP-2: the canonical fetch route yields frames the refresh consumes
        unchanged (no second extraction/rounding layer)."""
        from scripts.macro_data_fetch import (
            fetch_yfinance_daily_frame, canonical_normalize_daily_bar,
        )
        import inspect
        # refresh call site must use the canonical fetch, not a bespoke one
        src = inspect.getsource(m_run_daily_update)
        assert "fetch_yfinance_daily_frame" in src
        assert "extract_symbol_df_from_batch" not in src.split("Simplified EOD")[-1]

        fsrc = inspect.getsource(fetch_yfinance_daily_frame)
        assert "download_recent_batch" in fsrc
        assert "extract_symbol_df_from_batch" in fsrc
        assert "remove_unfinished_bars" in fsrc

    def test_np3_volume_normalization_parity(self):
        """NP-3: volume normalizes to round_4(float(v)) on both sides."""
        from scripts.macro_data_fetch import canonical_normalize_daily_bar
        cases = [
            (31085900.0, 31085900.0),
            (30998354.0, 30998354.0),
            (59039100, 59039100.0),
            (None, None),
        ]
        for raw, expect in cases:
            bar = pd.Series({"Volume": raw})
            got = canonical_normalize_daily_bar(bar)["volume"]
            assert got == expect, f"volume {raw!r} -> {got!r}, expected {expect!r}"
            if raw is not None:
                assert isinstance(got, float)

    def test_np4_high_low_precision_parity(self):
        """NP-4: high/low use round_4 on the raw provider value."""
        from scripts.macro_data_fetch import canonical_normalize_daily_bar
        bar = pd.Series({"High": 77.58000183105469, "Low": 76.69000244140625})
        n = canonical_normalize_daily_bar(bar)
        assert n["high"] == 77.58
        assert n["low"] == 76.69
        # a 4-decimal provider value must be preserved, not widened/narrowed
        bar2 = pd.Series({"High": 248.6044, "Low": 246.7001})
        n2 = canonical_normalize_daily_bar(bar2)
        assert n2["high"] == 248.6044
        assert n2["low"] == 246.7001

    def test_np5_adj_close_parity(self):
        """NP-5: adj_close is round_4(Adj Close); missing -> None."""
        from scripts.macro_data_fetch import canonical_normalize_daily_bar
        bar = pd.Series({"Adj Close": 77.27999877929688})
        assert canonical_normalize_daily_bar(bar)["adj_close"] == 77.28
        assert canonical_normalize_daily_bar(pd.Series({"Adj Close": None}))["adj_close"] is None
        assert canonical_normalize_daily_bar(pd.Series({}))["adj_close"] is None

    def test_np6_second_identical_refresh_zero_writes(self):
        """NP-6: second identical refresh produces zero writes."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-07", 10.0)])
        frame = _frame([
            ("2026-10-08", 10.0, 11.0, 9.0, 10.5, 1000),
            ("2026-10-09", 10.5, 11.5, 10.0, 11.0, 1000),
        ])
        r1 = run_eod_5session_refresh(symbols=["TLT"], provider_data={"TLT": frame},
                                      dry_run=False, engine=eng, close_check=ALWAYS_CLOSED)
        r2 = run_eod_5session_refresh(symbols=["TLT"], provider_data={"TLT": frame},
                                      dry_run=False, engine=eng, close_check=ALWAYS_CLOSED)
        assert r1["rows_inserted"] == 2
        assert r2["rows_inserted"] == 0
        assert r2["rows_revised"] == 0
        assert r2["close_revisions"] == 0
        assert r2["d1_repairs"] == 0

    def test_np7_genuine_revision_produces_exactly_one_revision(self):
        """NP-7: a genuine provider revision still produces exactly one."""
        eng = FakeEngine(seed_rows=[_seed("TLT", "2026-10-07", 10.0)])
        eng.insert([{**_seed("TLT", "2026-10-08", 10.5, high=11.0, low=9.0,
                             open_=10.0, volume=1000),
                     "prev_close": 10.0, "change": 0.5, "pct_chg": 5.0, "amplitude": 20.0}])
        # genuine revision: volume only
        frame = _frame([("2026-10-08", 10.0, 11.0, 9.0, 10.5, 2000)])
        c = run_eod_5session_refresh(symbols=["TLT"], provider_data={"TLT": frame},
                                     dry_run=False, engine=eng, close_check=ALWAYS_CLOSED)
        assert c["rows_revised"] == 1
        assert c["volume_revisions"] == 1
        assert c["close_revisions"] == 0
        assert c["d1_repairs"] == 0
        # rerun -> zero
        c2 = run_eod_5session_refresh(symbols=["TLT"], provider_data={"TLT": frame},
                                      dry_run=False, engine=eng, close_check=ALWAYS_CLOSED)
        assert c2["rows_revised"] == 0


# ============================================================
# CRYPTO 24/7 DAILY-SESSION BOUNDARY TESTS (CRYPTO-EOD-1 .. 9)
#
# Convention: daily bar labelled D spans D 00:00:00 UTC -> D+1 00:00:00 UTC.
#   session_end(D)   = D+1 00:00 UTC
#   EOD_ELIGIBLE(D)  = D+1 00:30 UTC
# The current UTC calendar day's bar must never be finalized.
# ============================================================

from datetime import timezone as _timezone


def _closed_at(symbol, bar_date, instant):
    """Ask the production helper with an explicit instant (no wall-clock)."""
    return symbol_date_has_closed(symbol, bar_date, instant)


# ---- CRYPTO-EOD-1 --------------------------------------------------------
def test_crypto_eod_1_same_day_2359_not_finalized():
    """D 23:59 UTC -> NOT FINALIZED."""
    d = date(2026, 10, 12)
    now = datetime(2026, 10, 12, 23, 59, tzinfo=_timezone.utc)
    assert _closed_at("BTC-USD", d, now) is False
    assert _closed_at("ETH-USD", d, now) is False


# ---- CRYPTO-EOD-2 --------------------------------------------------------
def test_crypto_eod_2_next_day_0000_waiting():
    """D+1 00:00 UTC -> CLOSED_WAITING_FINALIZATION_DELAY."""
    d = date(2026, 10, 12)
    now = datetime(2026, 10, 13, 0, 0, tzinfo=_timezone.utc)
    assert _closed_at("BTC-USD", d, now) is False
    assert _closed_at("ETH-USD", d, now) is False


# ---- CRYPTO-EOD-3 --------------------------------------------------------
def test_crypto_eod_3_next_day_0029_59_waiting():
    """D+1 00:29:59 UTC -> CLOSED_WAITING_FINALIZATION_DELAY."""
    d = date(2026, 10, 12)
    now = datetime(2026, 10, 13, 0, 29, 59, tzinfo=_timezone.utc)
    assert _closed_at("BTC-USD", d, now) is False
    assert _closed_at("ETH-USD", d, now) is False


# ---- CRYPTO-EOD-4 --------------------------------------------------------
def test_crypto_eod_4_next_day_0030_eligible():
    """D+1 00:30:00 UTC -> ELIGIBLE."""
    d = date(2026, 10, 12)
    now = datetime(2026, 10, 13, 0, 30, 0, tzinfo=_timezone.utc)
    assert _closed_at("BTC-USD", d, now) is True
    assert _closed_at("ETH-USD", d, now) is True


# ---- CRYPTO-EOD-5 --------------------------------------------------------
def test_crypto_eod_5_remove_unfinished_bars_excludes_current_utc_day():
    """Current UTC day's crypto bar must be removed by remove_unfinished_bars()."""
    import scripts.macro_data_fetch as mdf
    today = datetime.now(_timezone.utc).date()
    df = pd.DataFrame({
        "Date": pd.to_datetime([str(today - timedelta(days=2)),
                                str(today - timedelta(days=1)),
                                str(today)]),
        "Open": [1.0, 2.0, 3.0], "High": [1.1, 2.1, 3.1],
        "Low": [0.9, 1.9, 2.9], "Close": [1.0, 2.0, 3.0],
        "Adj Close": [1.0, 2.0, 3.0], "Volume": [10, 20, 30],
    })
    kept = mdf.remove_unfinished_bars(df.copy(), "BTC-USD")
    kept_dates = [pd.to_datetime(d).date() for d in kept["Date"]]
    assert today not in kept_dates, "current UTC day must NOT be finalized"
    assert (today - timedelta(days=1)) in kept_dates or (today - timedelta(days=2)) in kept_dates


# ---- CRYPTO-EOD-6 --------------------------------------------------------
def test_crypto_eod_6_eod_refresh_excludes_current_utc_day():
    """The EOD refresh must not write the still-open current UTC crypto day."""
    today = datetime.now(_timezone.utc).date()
    prev = today - timedelta(days=1)
    df = pd.DataFrame({
        "Date": pd.to_datetime([str(prev - timedelta(days=1)), str(prev), str(today)]),
        "Open": [100.0, 101.0, 102.0], "High": [101.0, 102.0, 103.0],
        "Low": [99.0, 100.0, 101.0], "Close": [100.5, 101.5, 102.5],
        "Adj Close": [100.5, 101.5, 102.5], "Volume": [10, 20, 30],
    })
    eng = FakeEngine()
    res = run_eod_5session_refresh(
        symbols=["BTC-USD"], provider_data={"BTC-USD": df},
        dry_run=False, engine=eng,
    )
    written = [k for k in eng.store if k[1] == str(today)]
    assert written == [], f"current UTC day was written: {written}"
    assert res["not_yet_finalized"] >= 1


# ---- CRYPTO-EOD-7 --------------------------------------------------------
def test_crypto_eod_7_previous_utc_day_eligible_only_after_plus_30m():
    """Previous UTC day becomes eligible only at its own +30m boundary."""
    d = date(2026, 10, 11)
    assert _closed_at("BTC-USD", d, datetime(2026, 10, 12, 0, 29, tzinfo=_timezone.utc)) is False
    assert _closed_at("BTC-USD", d, datetime(2026, 10, 12, 0, 30, tzinfo=_timezone.utc)) is True


# ---- CRYPTO-EOD-8 --------------------------------------------------------
def test_crypto_eod_8_ingestion_and_refresh_share_eligibility():
    """Normal ingestion and EOD refresh must make the SAME eligibility decision."""
    import scripts.macro_data_fetch as mdf
    for sym in ("BTC-USD", "ETH-USD"):
        for bar_date, instant, expected in (
            (date(2026, 10, 12), datetime(2026, 10, 12, 23, 59, tzinfo=_timezone.utc), False),
            (date(2026, 10, 12), datetime(2026, 10, 13, 0, 0, tzinfo=_timezone.utc), False),
            (date(2026, 10, 12), datetime(2026, 10, 13, 0, 29, 59, tzinfo=_timezone.utc), False),
            (date(2026, 10, 12), datetime(2026, 10, 13, 0, 30, 0, tzinfo=_timezone.utc), True),
        ):
            one = mdf.symbol_date_has_closed(sym, bar_date, instant)
            assert one is expected, f"{sym} {bar_date} {instant}: {one} != {expected}"
        # remove_unfinished_bars is the ingestion-side consumer of the same helper
        df = pd.DataFrame({
            "Date": pd.to_datetime(["2026-10-10", "2026-10-11", "2026-10-12"]),
            "Open": [1.0, 2.0, 3.0], "High": [1.1, 2.1, 3.1],
            "Low": [0.9, 1.9, 2.9], "Close": [1.0, 2.0, 3.0],
            "Adj Close": [1.0, 2.0, 3.0], "Volume": [10, 20, 30],
        })
        kept = {pd.to_datetime(d).date() for d in mdf.remove_unfinished_bars(df.copy(), sym)["Date"]}
        for bd in (date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12)):
            assert (bd in kept) == mdf.symbol_date_has_closed(sym, bd), (
                f"ingestion/refresh disagree for {sym} {bd}")


# ---- CRYPTO-EOD-9 --------------------------------------------------------
def test_crypto_eod_9_second_identical_finalized_refresh_zero_writes():
    """A second identical refresh over FINALIZED crypto bars writes nothing."""
    # bars well in the past => all finalized
    df = pd.DataFrame({
        "Date": pd.to_datetime(["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]),
        "Open": [100.0, 101.0, 102.0, 103.0, 104.0],
        "High": [101.0, 102.0, 103.0, 104.0, 105.0],
        "Low": [99.0, 100.0, 101.0, 102.0, 103.0],
        "Close": [100.5, 101.5, 102.5, 103.5, 104.5],
        "Adj Close": [100.5, 101.5, 102.5, 103.5, 104.5],
        "Volume": [10, 20, 30, 40, 50],
    })
    eng = FakeEngine()
    r1 = run_eod_5session_refresh(symbols=["BTC-USD"], provider_data={"BTC-USD": df.copy()},
                                  dry_run=False, engine=eng)
    r2 = run_eod_5session_refresh(symbols=["BTC-USD"], provider_data={"BTC-USD": df.copy()},
                                  dry_run=False, engine=eng)
    assert r2["rows_inserted"] == 0
    assert r2["rows_revised"] == 0
    assert r2["close_revisions"] == 0
    assert r2["d1_repairs"] == 0
