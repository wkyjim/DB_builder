"""The Neon sync's destination filter must be the approved universe.

``sync_recent_macro_to_neon`` used to filter with::

    if key not in C3A_APPROVED_ANCHORS and key not in SOURCE_DEFINITION_TRUST_START:
        skip

That is *anchor coverage*, not the approved EOD/API universe the docstring
names. 45 of the 73 macro symbols have no C.1 trust-start anchor -- including
``000001.SS``, ``HK50``, ``KOR200c1``, ``US10YT=X`` -- so every one of them was
silently withheld from Neon even when LOCAL held correct, current rows.

Isolated: fake engines, no database is touched.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")
os.environ.setdefault("LOCAL_DB_PASSWORD", "neon-sync-filter-test")
os.environ.setdefault("NEON_DB_PASSWORD", "neon-sync-filter-test")

import macro_data_fetch as m  # noqa: E402

UNANCHORED = "000001.SS"      # approved, but no C.1 trust-start anchor
ANCHORED = "^HSI"             # approved, with an anchor


def _row(sym, d):
    return {
        "date": d, "symbol": sym, "name": sym, "asset_type": "stock_index",
        "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0,
        "adj_close": 100.0, "volume": 1000.0, "prev_close": 98.5,
        "change": 1.5, "pct_chg": 1.5, "amplitude": 2.5,
        "source": "yfinance",
        "source_definition": m.SOURCE_DEFINITION_BY_SYMBOL.get(
            sym, "UNKNOWN_DEFINITION"),
    }


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def mappings(self):
        return self


class _Res:
    def __init__(self, rows, rowcount=0):
        self._rows = rows
        self.rowcount = rowcount

    def mappings(self):
        return _Mappings(self._rows)


class _Conn:
    def __init__(self, store):
        self.store = store

    def execute(self, stmt, params=None):
        params = params or {}
        if str(stmt).strip().lower().startswith("select"):
            syms = params.get("symbols") or []
            win = params.get("window") or 5
            out = []
            for sym in syms:
                mine = sorted(
                    [r for r in self.store.values() if r["symbol"] == sym],
                    key=lambda r: r["date"], reverse=True)[:win]
                out.extend(mine)
            return _Res(out)
        if str(stmt).strip().lower().startswith("insert"):
            rows = params if isinstance(params, list) else [params]
            n = 0
            for r in rows:
                self.store[(r["symbol"], str(r["date"]))] = dict(r)
                n += 1
            return _Res([], rowcount=n)
        raise AssertionError("unexpected SQL: %s" % str(stmt)[:120])


class _Beg:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return _Conn(self.store)

    def __exit__(self, *a):
        return False


class _Eng:
    def __init__(self):
        self.store = {}

    def begin(self):
        return _Beg(self.store)

    def connect(self):
        return _Beg(self.store)


def _sync(symbols, monkeypatch):
    import db_builder.config as cfg

    local, neon = _Eng(), _Eng()
    d0 = dt.date(2026, 10, 19)
    for i in range(3):
        local.store[(symbols[0], str(d0 + dt.timedelta(days=i)))] = _row(
            symbols[0], d0 + dt.timedelta(days=i))

    monkeypatch.setattr(cfg, "local_engine", lambda: local)
    monkeypatch.setattr(cfg, "neon_engine", lambda: neon)
    monkeypatch.setattr(m, "local_engine", local)
    monkeypatch.setattr(m, "neon_engine", neon)

    return m.sync_recent_macro_to_neon(symbols=symbols, window=5), neon


class TestDestinationFilterIsTheApprovedUniverse:
    def test_unanchored_approved_symbol_is_synced(self, monkeypatch):
        assert m._resolve_trust_start(
            UNANCHORED, m.SOURCE_DEFINITION_BY_SYMBOL[UNANCHORED]) is None, (
            "premise: %s must have no trust anchor" % UNANCHORED)
        assert UNANCHORED in m.APPROVED_MACRO_SYMBOLS

        counters, neon = _sync([UNANCHORED], monkeypatch)
        assert counters["skipped_rows"] == 0, (
            "an approved symbol with no anchor was withheld from Neon")
        assert counters["candidate_rows"] > 0, (
            "an approved symbol with no anchor produced no sync candidates")

    def test_anchored_symbol_is_still_synced(self, monkeypatch):
        counters, _ = _sync([ANCHORED], monkeypatch)
        assert counters["skipped_rows"] == 0
        assert counters["candidate_rows"] > 0

    def test_symbol_outside_the_universe_is_skipped(self, monkeypatch):
        outsider = "NOT_A_REAL_SYMBOL"
        counters, _ = _sync([outsider], monkeypatch)
        assert counters["skipped_rows"] == 1
        assert counters["committed_rows"] == 0
