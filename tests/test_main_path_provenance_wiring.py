"""The MAIN ingest path must supply the provenance resolver.

Regression test for the defect that left ``000001.SS`` frozen at 2026-10-14.

``upsert_new_macro_rows`` writes LOCAL and NEON rows through
``upsert_macro`` -> ``route_historical_rows``.  ``route_historical_rows``
treats a missing ``provenance_lookup`` as *unknown* and fails closed
(``provenance_dates = set()``), so a call site that omits it defers **every**
historical row with ``PRIOR_PROVENANCE_UNVERIFIED``.

The recovery path supplies the resolver internally, but the recovery selector
only retries rows whose symbol has a C.1 trust-start anchor. 45 of the 73
macro symbols have no anchor (``000001.SS``, ``HK50``, ``KOR200c1``,
``NIY=F``, ``ES=F``, ``US10YT=X`` ...), so for those symbols neither path
wrote anything: the main path refused, and recovery never selected them.

These tests assert the CALL SITE, not the pass-through in ``upsert_macro``.

Isolated: fake engines, no database is touched.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# The production module builds SQLAlchemy engines at import time. Point them at
# an in-process sqlite URL so no PostgreSQL connection is attempted.
os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")
os.environ.setdefault("LOCAL_DB_PASSWORD", "main-path-provenance-test")
os.environ.setdefault("NEON_DB_PASSWORD", "main-path-provenance-test")

import macro_data_fetch as m  # noqa: E402

EMPTY_DF = pd.DataFrame()

D = dt.date(2026, 10, 15)
P = dt.date(2026, 10, 14)


class _FakeEngine:
    """Opaque engine stand-in; the lookups are monkeypatched out."""

    def __init__(self, *a, **k):
        pass


def _candidate(symbol, date, source_definition):
    return {
        "date": date, "symbol": symbol, "name": symbol,
        "asset_type": "stock_index",
        "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
        "adj_close": 1.5, "volume": 10,
        "source_definition": source_definition,
    }


@pytest.fixture
def captured_upserts(monkeypatch):
    """Run the real call site, but capture what it hands to ``upsert_macro``."""
    captured = {"calls": []}

    monkeypatch.setattr(m, "local_engine", lambda: _FakeEngine())
    monkeypatch.setattr(m, "neon_engine", lambda: _FakeEngine())
    monkeypatch.setattr(
        m, "fetch_existing_symbol_dates", lambda engine, **kw: EMPTY_DF)
    monkeypatch.setattr(
        m, "filter_duplicate_symbol_dates", lambda rows, existing_df: list(rows))

    def fake_upsert(engine, rows, **kw):
        captured["calls"].append(kw)
        return len(rows)

    monkeypatch.setattr(m, "upsert_macro", fake_upsert)
    return captured


class TestMainIngestPathSuppliesProvenanceResolver:
    """The call site must never leave the gate's provenance unknown."""

    def test_local_write_supplies_provenance_resolver(self, captured_upserts):
        sd = m.SOURCE_DEFINITION_BY_SYMBOL["000001.SS"]
        m.upsert_new_macro_rows(
            [_candidate("000001.SS", D, sd)],
            ["000001.SS"], upsert_neon=False,
        )

        assert len(captured_upserts["calls"]) == 1, (
            "the LOCAL write must go through upsert_macro exactly once")
        resolver = captured_upserts["calls"][0].get("provenance_lookup")
        assert resolver is not None, (
            "upsert_new_macro_rows passed provenance_lookup=None to the LOCAL "
            "write; route_historical_rows then fails closed and every row is "
            "deferred with PRIOR_PROVENANCE_UNVERIFIED")
        assert callable(resolver)

    def test_neon_write_supplies_provenance_resolver(self, captured_upserts):
        sd = m.SOURCE_DEFINITION_BY_SYMBOL["000001.SS"]
        m.upsert_new_macro_rows(
            [_candidate("000001.SS", D, sd)],
            ["000001.SS"], upsert_neon=True,
        )

        assert len(captured_upserts["calls"]) == 2, (
            "expected one LOCAL and one NEON write")
        for kw in captured_upserts["calls"]:
            assert kw.get("provenance_lookup") is not None, (
                "a write reached upsert_macro without a provenance resolver")


class TestGateFailsClosedOnlyWhenProvenanceIsActuallyUnknown:
    """Document the failure mode the missing resolver caused."""

    def _run(self, monkeypatch, symbol, resolver):
        monkeypatch.setattr(
            m, "_stored_trusted_provenance_dates",
            lambda engine, sym, sd, mn, mx: {P})
        sd = m.SOURCE_DEFINITION_BY_SYMBOL[symbol]
        return m.route_historical_rows(
            _FakeEngine(), [_candidate(symbol, D, sd)],
            prior_lookup=lambda e, s, mn, mx: {P},
            prior_value_lookup=lambda e, s, mn, mx: {P: 1.0},
            provenance_lookup=resolver,
            count_telemetry=False,
        )

    def test_no_resolver_defers_every_row(self, monkeypatch):
        """This is what the main path did before the fix."""
        accepted, deferred, _ = self._run(
            monkeypatch, "000001.SS", None)
        assert accepted == []
        assert [why for _, why in deferred] == ["PRIOR_PROVENANCE_UNVERIFIED"]

    def test_db_resolver_accepts_an_unanchored_symbol(self, monkeypatch):
        """With the resolver wired, a no-anchor symbol is writable."""
        accepted, deferred, _ = self._run(
            monkeypatch, "000001.SS",
            m._build_anchor_provenance_lookup(_FakeEngine()))
        assert [r["date"] for r in accepted] == [D], (
            f"expected 000001.SS {D} to be accepted, got deferred={deferred}")

    def test_anchorless_symbols_are_a_real_population(self):
        """Guard the premise: the defect affected more than one symbol."""
        unanchored = [
            s for s in m.SOURCE_DEFINITION_BY_SYMBOL
            if m._resolve_trust_start(
                s, m.SOURCE_DEFINITION_BY_SYMBOL[s]) is None
        ]
        assert len(unanchored) > 1, (
            "expected multiple symbols without a trust anchor")
        assert "000001.SS" in unanchored
