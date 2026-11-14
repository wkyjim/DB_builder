from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, text

import pgSQL_equities_auto as eq


def _engine():
    return create_engine("sqlite://")


def _make_table(engine):
    with engine.begin() as c:
        c.execute(text("""
            CREATE TABLE us_equities (
                date date NOT NULL, ticker text NOT NULL,
                data_source text, is_provisional boolean DEFAULT false,
                close double precision
            )
        """))


def test_canonical_coverage_excludes_legacy_and_provisional(monkeypatch):
    engine = _engine()
    _make_table(engine)
    monkeypatch.setattr(eq, "RAW_TABLE", "us_equities")
    with engine.begin() as c:
        # Prior baseline: 100 tickers on 2026-11-12.
        for i in range(100):
            c.execute(text("INSERT INTO us_equities VALUES ('2026-11-12', :t, NULL, false, 1)"),
                      {"t": f"B{i}"})
        # Target: 50 Massive + 50 provisional/legacy; canonical ratio 50%.
        for i in range(50):
            c.execute(text("INSERT INTO us_equities VALUES ('2026-11-13', :t, 'massive', false, 1)"),
                      {"t": f"M{i}"})
        for i in range(50):
            c.execute(text("INSERT INTO us_equities VALUES ('2026-11-13', :t, NULL, true, 1)"),
                      {"t": f"P{i}"})
    cov = eq.get_canonical_coverage(engine, date(2026, 11, 13))
    assert cov["ticker_count"] == 50
    assert cov["baseline_count"] == 100
    assert cov["ratio"] == 0.5
    assert cov["ok"] is False


def test_source_precedence_forces_fetch_when_provisional(monkeypatch):
    monkeypatch.setattr(eq, "get_canonical_coverage",
                        lambda engine, session: {"ticker_count": 0, "baseline_count": 100,
                                                 "ratio": 0.0, "ok": False})
    md = eq.get_cycle_metadata(None, date(2026, 11, 13), date(2026, 11, 13), date(2026, 11, 13))
    assert md["decision"] == "equities_only"
    assert md["equity_refresh_status"] == "UPDATED"


def test_source_precedence_skips_only_with_canonical_coverage(monkeypatch):
    monkeypatch.setattr(eq, "get_canonical_coverage",
                        lambda engine, session: {"ticker_count": 99, "baseline_count": 100,
                                                 "ratio": 0.99, "ok": True})
    md = eq.get_cycle_metadata(None, date(2026, 11, 13), date(2026, 11, 13), date(2026, 11, 13))
    assert md["decision"] == "skip_both"
    assert md["canonical_coverage_ok"] is True
