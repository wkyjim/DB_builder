"""Phase A integrity regression tests (A1-A10, T-REC-1/2, extended cases).

Isolated: uses fake engines and in-memory stores. No database is touched.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Isolated test harness: the production module builds SQLAlchemy engines at
# import time. Point them at an in-process sqlite URL so no PostgreSQL
# connection is ever attempted and no production DB is touched.
os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")
os.environ.setdefault("LOCAL_DB_PASSWORD", "phase-a-test")
os.environ.setdefault("NEON_DB_PASSWORD", "phase-a-test")

from db_builder import session_calendar as sc
from db_builder.deferred_history import (
    DEFERRED,
    DEFERRED_HISTORY_UNRESOLVED,
    RESOLVED,
    MemoryDeferredStore,
    RECOVERY_DAYS,
)


# --------------------------------------------------------------- fixtures

def _frame(rows):
    """Build a provider-shaped frame: list of (date, close)."""
    return pd.DataFrame({
        "Date": [pd.Timestamp(d) for d, _ in rows],
        "Open": [c for _, c in rows],
        "High": [c + 1 for _, c in rows],
        "Low": [c - 1 for _, c in rows],
        "Close": [c for _, c in rows],
        "Adj Close": [c for _, c in rows],
        "Volume": [1000 for _ in rows],
    })


class FakeEngine:
    """Records executed statements; no real database."""

    def __init__(self):
        self.executed = []

    def execute(self, *a, **kw):
        self.executed.append((a, kw))
        return types.SimpleNamespace(rowcount=0, fetchall=lambda: [])

    def begin(self):
        engine = self

        class _Ctx:
            def __enter__(self):
                return engine

            def __exit__(self, *a):
                return False

            def execute(self, *a, **kw):
                return engine.execute(*a, **kw)

        return _Ctx()


@pytest.fixture(autouse=True)
def _clear_adapter_cache():
    sc.reset_adapter_cache()
    yield
    sc.reset_adapter_cache()


# =============================================================== A1 / A2

def test_a1_missing_prior_session_defers_and_never_uses_older_value():
    """Frame [09-21, 09-23]: 09-23 must NOT use 09-21 as prev_close."""
    import macro_data_fetch as m

    frame = _frame([("2026-09-21", 7700.0), ("2026-09-23", 7706.0)])
    rows = m.calculate_rows_for_symbol(frame, "^GSPC", "S&P 500", "stock_index")

    assert rows == [], "09-23 must not be emitted without its prior session"
    assert all(r.get("prev_close") != 7700.0 for r in rows)


def test_a2_recovery_uses_confirmed_prior_close():
    """Frame [09-21, 09-22, 09-23]: prev_close must be the 09-22 close."""
    import macro_data_fetch as m

    frame = _frame([("2026-09-21", 7700.0),
                    ("2026-09-22", 7764.6401),
                    ("2026-09-23", 7706.0298)])
    rows = m.calculate_rows_for_symbol(frame, "^GSPC", "S&P 500", "stock_index")

    d23 = [r for r in rows if r["date"] == dt.date(2026, 9, 23)]
    assert len(d23) == 1
    row = d23[0]
    assert row["prev_close"] == 7764.6401
    assert row["change"] == round(7706.0298 - 7764.6401, 4)
    assert row["pct_chg"] == round(
        (7706.0298 - 7764.6401) / 7764.6401 * 100, 4)
    assert row["amplitude"] == round((7707.0298 - 7705.0298) / 7764.6401 * 100, 4)


def test_a8_incident_replay_never_produces_the_stale_value():
    """The confirmed incident: 09-21 close must never become 09-23 prev_close."""
    import macro_data_fetch as m

    frame = _frame([("2026-09-14", 7000.0), ("2026-09-15", 7100.0),
                    ("2026-09-16", 7200.0), ("2026-09-17", 7300.0),
                    ("2026-09-18", 7400.0), ("2026-09-21", 7764.7002),
                    ("2026-09-23", 7706.0298)])
    rows = m.calculate_rows_for_symbol(frame, "^GSPC", "S&P 500", "stock_index")

    assert not any(r["date"] == dt.date(2026, 9, 23) for r in rows)
    assert not any(r["prev_close"] == 7764.7002 for r in rows)


# =============================================================== A3 / A4

def _no_prior(_engine, _symbol, _min_date, _max_date):
    """Isolated prior-session lookup: only the 09-22 session is confirmed."""
    return {dt.date(2026, 9, 22)}


def _provenance_lookup_fn(prior_dates=None):
    """Return a provenance_lookup that verifies the given dates."""
    if prior_dates is None:
        prior_dates = {dt.date(2026, 9, 21), dt.date(2026, 9, 22)}
    def lookup(_engine, _symbol, _min_date, _max_date):
        return set(prior_dates)
    return lookup


def _prior_excludes_d_minus_1(_engine, _symbol, _min_date, _max_date):
    """09-22 is NOT confirmed -> 09-23 must defer."""
    return {dt.date(2026, 9, 21)}


def test_a3_us_premarket_is_live_not_historical():
    """Before the US cash close the session is OPEN -> LIVE, never historical."""
    import macro_data_fetch as m

    d = dt.date(2026, 9, 23)
    premarket = dt.datetime(2026, 9, 23, 2, 0, tzinfo=dt.timezone.utc)  # 10:00 HKT
    eng = FakeEngine()

    accepted, deferred, rejected = m.route_historical_rows(
        eng, [{"date": d, "symbol": "^GSPC", "close": 7706.0}],
        now_utc=premarket, prior_lookup=_no_prior,
        prior_value_lookup=lambda *a, **k: {dt.date(2026, 9, 22): 7764.6401},
        provenance_lookup=_provenance_lookup_fn())

    assert accepted == []
    assert not eng.executed, "no SQL may run for a pre-market observation"
    assert deferred or rejected


def test_a4_asia_completed_while_us_open_uses_per_instrument_calendar():
    """HSI may qualify while US cash rows remain unconfirmed -> no global NYSE."""
    d = dt.date(2026, 9, 23)
    # 2026-09-23 09:00 UTC = 17:00 HKT (HK closed + 30min buffer)
    #                      = 05:00 ET (US not open; NYSE closes 20:00 UTC).
    now = dt.datetime(2026, 9, 23, 9, 0, tzinfo=dt.timezone.utc)

    hsi = sc.session_status("^HSI", d, now)
    spx = sc.session_status("^GSPC", d, now)

    assert hsi == sc.SessionStatus.CONFIRMED_COMPLETED
    assert spx == sc.SessionStatus.OPEN

    # And HSI is eligible while US cash is not.
    hsi_decision = sc.historical_eligibility(
        "^HSI", d, known_prior_sessions={dt.date(2026, 9, 22)}, now_utc=now)
    spx_decision = sc.historical_eligibility(
        "^GSPC", d, known_prior_sessions={dt.date(2026, 9, 22)}, now_utc=now)
    assert hsi_decision.eligible is True
    assert spx_decision.eligible is False
    assert spx_decision.routing == sc.ROUTE_LIVE


# =============================================================== A5 / weekend

def test_a5_crypto_weekend_is_a_valid_session():
    sat = dt.date(2026, 9, 12)
    sun = dt.date(2026, 9, 13)
    for d in (sat, sun):
        assert sc.session_status("BTC-USD", d,
                                 dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)) \
            == sc.SessionStatus.CONFIRMED_COMPLETED


def test_a5b_crypto_weekend_prev_session_is_previous_calendar_day():
    assert sc.previous_valid_session("BTC-USD", dt.date(2026, 9, 13)) \
        == dt.date(2026, 9, 12)


def test_weekend_globex_sunday_is_a_legitimate_session():
    """Globex has a real Sunday session -- must not be treated as corruption."""
    sun = dt.date(2026, 9, 13)
    now = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
    assert sc.session_status("ES=F", sun, now) == sc.SessionStatus.CONFIRMED_COMPLETED
    assert sc.adapter_for("ES=F").is_trading_day(sun) is True


def test_weekend_saturday_futures_is_non_trading():
    sat = dt.date(2026, 9, 12)
    now = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
    assert sc.session_status("ES=F", sat, now) == sc.SessionStatus.NON_TRADING_DAY


def test_weekend_fx_sunday_is_review_required_not_silently_accepted():
    """FX Sunday is the weekly roll, not a completed daily session."""
    sun = dt.date(2026, 9, 13)
    now = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc)
    assert sc.session_status("EURUSD=X", sun, now) \
        == sc.SessionStatus.WEEKEND_SESSION_REVIEW_REQUIRED


def test_weekend_fx_roll_convention_is_17_et():
    """FX daily roll is 17:00 ET on weekdays."""
    d = dt.date(2026, 9, 23)
    close = sc.adapter_for("EURUSD=X").session_close_utc(d)
    assert close is not None
    # 17:00 ET == 21:00 or 22:00 UTC depending on US DST; both are valid.
    assert close.hour in (21, 22)


def test_classify_weekend_rows_never_repairs_or_deletes():
    dates = [dt.date(2026, 9, 12), dt.date(2026, 9, 13)]
    crypto = sc.classify_weekend_rows("BTC-USD", dates)
    assert set(crypto.values()) == {"LEGITIMATE_WEEKEND_SESSION"}

    fx = sc.classify_weekend_rows("EURUSD=X", dates)
    assert fx[dt.date(2026, 9, 12)] == "WEEKEND_SESSION_REVIEW_REQUIRED"
    assert fx[dt.date(2026, 9, 13)] == "WEEKEND_SESSION_REVIEW_REQUIRED"

    futures = sc.classify_weekend_rows("ES=F", dates)
    assert futures[dt.date(2026, 9, 13)] == "LEGITIMATE_WEEKEND_SESSION"


# =============================================================== A6 / A7

def test_a6_unknown_calendar_fails_closed():
    """Unresolved source definition -> CLOSED_UNCONFIRMED, defer, never write."""
    d = dt.date(2026, 9, 23)
    now = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)
    for sym in ("HK50", "KOR200c1", "CIHc1"):
        assert sc.session_status(sym, d, now) == sc.SessionStatus.CLOSED_UNCONFIRMED
        decision = sc.historical_eligibility(
            sym, d, known_prior_sessions={dt.date(2026, 9, 22)}, now_utc=now)
        assert decision.eligible is False
        assert decision.routing == sc.ROUTE_DEFERRED


def test_a6b_unavailable_calendar_backend_fails_closed(monkeypatch):
    monkeypatch.setattr(sc, "_xc", None)
    monkeypatch.setattr(sc, "_XC_IMPORT_ERROR", ImportError("simulated"))
    sc.reset_adapter_cache()

    adapter = sc.adapter_for("^GSPC")
    assert adapter.is_trading_day(dt.date(2026, 9, 23)) is False
    assert adapter.session_status(
        dt.date(2026, 9, 23), dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)) \
        == sc.SessionStatus.CLOSED_UNCONFIRMED


def test_a7_provider_row_labelled_a_holiday_is_rejected():
    """H1: a holiday-labelled row must not pass on time alone."""
    d = dt.date(2026, 12, 25)          # NYSE holiday
    now = dt.datetime(2027, 1, 5, tzinfo=dt.timezone.utc)
    assert sc.session_status("^GSPC", d, now) == sc.SessionStatus.HOLIDAY

    decision = sc.historical_eligibility(
        "^GSPC", d, known_prior_sessions={dt.date(2026, 12, 24)}, now_utc=now)
    assert decision.eligible is False
    assert decision.routing == sc.ROUTE_REJECT


def test_a9_treasury_requires_publication_confirmation():
    """USnYT=X: closed session alone is insufficient; publication is required."""
    d = dt.date(2026, 9, 23)
    now = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)
    prior = {dt.date(2026, 9, 22)}

    unconfirmed = sc.historical_eligibility(
        "US10YT=X", d, known_prior_sessions=prior, now_utc=now,
        treasury_published_dates=[dt.date(2026, 9, 22)])
    assert unconfirmed.eligible is False
    assert unconfirmed.reason == "TREASURY_PUBLICATION_NOT_CONFIRMED"

    confirmed = sc.historical_eligibility(
        "US10YT=X", d, known_prior_sessions=prior, now_utc=now,
        treasury_published_dates=[dt.date(2026, 9, 22), dt.date(2026, 9, 23)])
    assert confirmed.eligible is True
    assert confirmed.routing == sc.ROUTE_HISTORICAL


# =============================================================== A10 / gate

def test_a10_deferred_never_routed_to_live_or_historical():
    """A completed bar with a missing prior session is DEFERRED, full stop."""
    import macro_data_fetch as m

    d = dt.date(2026, 9, 23)
    now = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)
    decision = sc.historical_eligibility(
        "^GSPC", d, known_prior_sessions={dt.date(2026, 9, 21)}, now_utc=now)

    assert decision.eligible is False
    assert decision.routing == sc.ROUTE_DEFERRED
    assert decision.reason == "PREVIOUS_SESSION_NOT_CONFIRMED"
    assert decision.routing != sc.ROUTE_LIVE

    store = MemoryDeferredStore()
    eng = FakeEngine()
    accepted, deferred, rejected = m.route_historical_rows(
        eng, [{"date": d, "symbol": "^GSPC", "close": 7706.0}],
        now_utc=now, deferred_store=store,
        prior_lookup=_prior_excludes_d_minus_1,
        prior_value_lookup=lambda *a, **k: {dt.date(2026, 9, 22): 7764.6401},
        provenance_lookup=_provenance_lookup_fn())

    assert accepted == []
    assert len(deferred) == 1
    assert not eng.executed
    assert store.all_rows()[0].status == DEFERRED


def test_gate_error_fails_closed(monkeypatch):
    import macro_data_fetch as m

    def _boom(*a, **kw):
        raise RuntimeError("calendar exploded")

    # Patch the reference the production module actually calls.
    monkeypatch.setattr(m, "historical_eligibility", _boom)
    decision = m._eligible_for_historical({"date": dt.date(2026, 9, 23),
                                           "symbol": "^GSPC"})
    assert decision.eligible is False
    assert decision.routing == sc.ROUTE_DEFERRED


def test_holiday_and_early_close_boundaries():
    now = dt.datetime(2026, 11, 28, tzinfo=dt.timezone.utc)
    # Thanksgiving 2026-11-26 is not a session
    assert sc.session_status("^GSPC", dt.date(2026, 11, 26), now) \
        == sc.SessionStatus.HOLIDAY
    # Early close 2026-11-27 -> session, and completed well before now
    assert sc.session_status("^GSPC", dt.date(2026, 11, 27), now) \
        == sc.SessionStatus.CONFIRMED_COMPLETED


def test_dst_boundary_prev_session():
    assert sc.previous_valid_session("^GSPC", dt.date(2026, 3, 9)) \
        == dt.date(2026, 3, 6)          # weekend + DST change
    assert sc.previous_valid_session("^GSPC", dt.date(2026, 9, 28)) \
        == dt.date(2026, 9, 25)         # weekend


# =============================================================== T-REC

def test_t_rec_1_successful_recovery_and_idempotency():
    import macro_data_fetch as m

    store = MemoryDeferredStore()
    eng = FakeEngine()
    d = dt.date(2026, 9, 23)
    now = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)

    store.record_deferral("^GSPC", d, "PREVIOUS_SESSION_NOT_CONFIRMED", now=now)
    assert store.all_rows()[0].status == DEFERRED

    def fetch(symbol, dates):
        return [{"date": d, "symbol": symbol, "name": "S&P 500",
                 "asset_type": "stock_index", "open": 7706.0, "high": 7707.0,
                 "low": 7705.0, "close": 7706.0, "adj_close": 7706.0,
                 "volume": 1000, "prev_close": 7764.6401, "change": -58.6401,
                 "pct_chg": -0.7552, "amplitude": 0.0258}]

    # Batch too small to matter here; force small window so the row is due.
    result = m.run_deferred_recovery(eng, store, fetch_symbol_rows=fetch,
                                     now_utc=now, recovery_days=RECOVERY_DAYS,
                                     prior_lookup=_no_prior,
                                     prior_value_lookup=lambda *a, **k: {dt.date(2026, 9, 22): 7764.6401},
        provenance_lookup=_provenance_lookup_fn())
    assert result["resolved"] == 1
    assert store.all_rows()[0].status == RESOLVED

    # Idempotent retry: a resolved row is no longer a candidate.
    result2 = m.run_deferred_recovery(eng, store, fetch_symbol_rows=fetch,
                                      now_utc=now, prior_lookup=_no_prior,
                                      prior_value_lookup=lambda *a, **k: {dt.date(2026, 9, 22): 7764.6401},
        provenance_lookup=_provenance_lookup_fn())
    assert result2["retried"] == 0


def test_t_rec_2_permanently_missing_data_escalates_and_is_retained():
    import macro_data_fetch as m

    store = MemoryDeferredStore()
    eng = FakeEngine()
    d = dt.date(2026, 8, 1)
    now = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)

    def fetch(symbol, dates):
        return []                       # provider never supplies the data

    # Simulate repeated failures.
    for _ in range(50):
        store.record_deferral("^GSPC", d, "PREVIOUS_SESSION_NOT_CONFIRMED",
                              now=now)
        m.run_deferred_recovery(eng, store, fetch_symbol_rows=fetch, now_utc=now,
                                prior_lookup=_no_prior,
                                prior_value_lookup=lambda *a, **k: {dt.date(2026, 9, 22): 7764.6401},
        provenance_lookup=_provenance_lookup_fn())

    rows = store.all_rows()
    assert len(rows) == 1, "the row must never disappear from tracking"
    assert rows[0].status == DEFERRED_HISTORY_UNRESOLVED
    assert rows[0].attempt_count >= 48


def test_deferred_rows_survive_beyond_five_day_lookback():
    """Retry horizon is independent of LOOKBACK_DAYS=5."""
    store = MemoryDeferredStore()
    d = dt.date(2026, 9, 10)
    now = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)   # 20 days later

    store.record_deferral("^GSPC", d, "PREVIOUS_SESSION_NOT_CONFIRMED", now=now)
    due = store.due_candidates(now=now, recovery_days=RECOVERY_DAYS)
    assert len(due) == 1, "a 20-day-old deferral must still be retried"

    # But beyond the horizon it is no longer retried (still retained).
    store2 = MemoryDeferredStore()
    store2.record_deferral("^GSPC", d, "PREVIOUS_SESSION_NOT_CONFIRMED", now=now)
    far = dt.datetime(2026, 11, 30, tzinfo=dt.timezone.utc)
    assert store2.due_candidates(now=far, recovery_days=RECOVERY_DAYS) == []
    assert len(store2.all_rows()) == 1, "still tracked"


def test_recovery_batch_is_bounded():
    store = MemoryDeferredStore()
    now = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)
    for i in range(120):
        store.record_deferral("^GSPC", dt.date(2026, 9, 1) + dt.timedelta(days=i),
                              "X", now=now)
    due = store.due_candidates(now=now, limit=50)
    assert len(due) == 50, "hourly runs must never become a full backfill"


def test_deferral_attempts_accumulate():
    store = MemoryDeferredStore()
    now = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)
    for _ in range(3):
        store.record_deferral("^GSPC", dt.date(2026, 9, 23), "X", now=now)
    assert store.all_rows()[0].attempt_count == 3


def test_treasury_published_dates_wired_from_fetch_path():
    """USnYT=X must be eligible when the run actually observed the publication."""
    import macro_data_fetch as m

    m._TREASURY_PUBLISHED_DATES.clear()
    m._TREASURY_PUBLISHED_VALUES.clear()
    m._TREASURY_PUBLISHED_DATES.update(
        {dt.date(2026, 9, 22), dt.date(2026, 9, 23)})
    # A publication DATE alone must not authorise a write: the official VALUE
    # for the session (and its prior session) must also be observed.
    m._TREASURY_PUBLISHED_VALUES[("US10YT_CMT", dt.date(2026, 9, 22))] = 5.05
    m._TREASURY_PUBLISHED_VALUES[("US10YT_CMT", dt.date(2026, 9, 23))] = 5.11
    try:
        d = dt.date(2026, 9, 23)
        now = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)
        accepted, deferred, rejected = m.route_historical_rows(
            FakeEngine(), [{"date": d, "symbol": "US10YT_CMT", "close": 5.11}],
            now_utc=now, prior_lookup=_no_prior,
            prior_value_lookup=lambda *a, **k: {dt.date(2026, 9, 22): 5.05},
            provenance_lookup=_provenance_lookup_fn())
        assert len(accepted) == 1, "observed publication must make it eligible"
        assert deferred == []

        # A session with no observed publication still defers.
        m._TREASURY_PUBLISHED_DATES.discard(dt.date(2026, 9, 23))
        accepted2, deferred2, _ = m.route_historical_rows(
            FakeEngine(), [{"date": d, "symbol": "US10YT_CMT", "close": 5.11}],
            now_utc=now, prior_lookup=_no_prior,
            prior_value_lookup=lambda *a, **k: {dt.date(2026, 9, 22): 5.05},
            provenance_lookup=_provenance_lookup_fn())
        assert accepted2 == []
        assert len(deferred2) == 1
    finally:
        m._TREASURY_PUBLISHED_DATES.clear()
        m._TREASURY_PUBLISHED_VALUES.clear()


def test_phase_a_safety_alert_fires_on_calendar_failure(monkeypatch):
    """A calendar failure must raise an explicit alert (non-zero exit)."""
    import macro_data_fetch as m

    m._PHASE_A_UNAVAILABLE_SYMBOLS.clear()
    m._PHASE_A_DEFERRED_COUNT = 0
    m._PHASE_A_BOUNDARY_DEFERRED_COUNT = 0
    m._PHASE_A_ACTIONABLE_DEFERRALS.clear()
    try:
        # No summary available -> fail-closed (return 1).
        assert m._phase_a_safety_exit_code(None) == 1

        # Calendar unavailable with coverage not satisfied -> non-zero.
        m._PHASE_A_UNAVAILABLE_SYMBOLS.append(
            {"symbol": "^GSPC", "reason": "CLOSED_UNCONFIRMED"})
        summary = m.summarize_outcomes([
            m.SymbolFetchOutcome("^GSPC", "S&P 500", "yfinance", True, False, 0, None, "CLOSED_UNCONFIRMED"),
        ])
        assert m._phase_a_safety_exit_code(summary) == 1

        # D1: BENIGN boundary deferrals alone must NOT alert.
        m._PHASE_A_UNAVAILABLE_SYMBOLS.clear()
        m._PHASE_A_DEFERRED_COUNT = 3
        m._PHASE_A_BOUNDARY_DEFERRED_COUNT = 3
        m._PHASE_A_ACTIONABLE_DEFERRALS.clear()
        summary_ok = m.summarize_outcomes([
            m.SymbolFetchOutcome("^GSPC", "S&P 500", "yfinance", True, True, 5, None, None),
        ])
        assert m._phase_a_safety_exit_code(summary_ok) == 0

        # D1: an ACTIONABLE deferral with coverage satisfied -> DEGRADED_SUCCESS (0).
        m._PHASE_A_ACTIONABLE_DEFERRALS.append(
            {"symbol": "^GSPC", "reason": "PREVIOUS_SESSION_NOT_CONFIRMED"})
        assert m._phase_a_safety_exit_code(summary_ok) == 0
    finally:
        m._PHASE_A_UNAVAILABLE_SYMBOLS.clear()
        m._PHASE_A_DEFERRED_COUNT = 0
        m._PHASE_A_BOUNDARY_DEFERRED_COUNT = 0
        m._PHASE_A_ACTIONABLE_DEFERRALS.clear()


def test_gate_records_unavailable_symbol_telemetry():
    import macro_data_fetch as m

    m._PHASE_A_UNAVAILABLE_SYMBOLS.clear()
    m._PHASE_A_DEFERRED_COUNT = 0
    m._PHASE_A_BOUNDARY_DEFERRED_COUNT = 0
    m._PHASE_A_ACTIONABLE_DEFERRALS.clear()
    try:
        now = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)
        m.route_historical_rows(
            FakeEngine(), [{"date": dt.date(2026, 9, 23), "symbol": "HK50",
                            "close": 24678.5}],
            now_utc=now, prior_lookup=_no_prior,
            prior_value_lookup=lambda *a, **k: {dt.date(2026, 9, 22): 24000.0},
            provenance_lookup=_provenance_lookup_fn())
        assert m._PHASE_A_DEFERRED_COUNT == 1
        assert any(r["symbol"] == "HK50"
                   for r in m._PHASE_A_UNAVAILABLE_SYMBOLS)
        # With coverage not satisfied (single symbol failed), exit 1.
        summary = m.summarize_outcomes([
            m.SymbolFetchOutcome("HK50", "Hang Seng", "investing.com", False, False, 0, None, "CLOSED_UNCONFIRMED"),
        ])
        assert m._phase_a_safety_exit_code(summary) == 1
    finally:
        m._PHASE_A_UNAVAILABLE_SYMBOLS.clear()
        m._PHASE_A_DEFERRED_COUNT = 0
        m._PHASE_A_BOUNDARY_DEFERRED_COUNT = 0
        m._PHASE_A_ACTIONABLE_DEFERRALS.clear()


def test_us_session_close_plus_buffer_threshold_hkt():
    """Resolve the 05:00 HKT ambiguity explicitly.

    NYSE close is 16:00 ET. On 2026-09-23 (EDT, UTC-4) that is 20:00 UTC
    = 04:00 HKT (next day, UTC+8). With GLOBAL_BUFFER_MINUTES=30 the
    confirmation threshold is 20:30 UTC = 04:30 HKT.
    """
    import macro_data_fetch as m

    d = dt.date(2026, 9, 23)
    prior = {dt.date(2026, 9, 22)}

    def status_at(utc_dt):
        return sc.session_status("^GSPC", d, utc_dt)

    # 16:00 ET exactly -> still OPEN (close not yet passed)
    assert status_at(dt.datetime(2026, 9, 23, 20, 0, tzinfo=dt.timezone.utc)) \
        == sc.SessionStatus.OPEN
    # 1 minute before threshold -> OPEN
    assert status_at(dt.datetime(2026, 9, 23, 20, 29, tzinfo=dt.timezone.utc)) \
        == sc.SessionStatus.OPEN
    # exactly at threshold (20:30 UTC = 04:30 HKT) -> CONFIRMED
    assert status_at(dt.datetime(2026, 9, 23, 20, 30, tzinfo=dt.timezone.utc)) \
        == sc.SessionStatus.CONFIRMED_COMPLETED
    # 05:00 HKT = 21:00 UTC -> well past threshold -> CONFIRMED
    assert status_at(dt.datetime(2026, 9, 23, 21, 0, tzinfo=dt.timezone.utc)) \
        == sc.SessionStatus.CONFIRMED_COMPLETED

    # Eligibility must flip exactly at the threshold.
    before = sc.historical_eligibility(
        "^GSPC", d, known_prior_sessions=prior,
        now_utc=dt.datetime(2026, 9, 23, 20, 29, tzinfo=dt.timezone.utc))
    after = sc.historical_eligibility(
        "^GSPC", d, known_prior_sessions=prior,
        now_utc=dt.datetime(2026, 9, 23, 20, 30, tzinfo=dt.timezone.utc))
    assert before.eligible is False and before.routing == sc.ROUTE_LIVE
    assert after.eligible is True and after.routing == sc.ROUTE_HISTORICAL

    # DST contrast: after US DST ends (2026-11-06, EST UTC-5) close = 21:00 UTC.
    d_nov = dt.date(2026, 11, 6)
    assert sc.session_status("^GSPC", d_nov,
                             dt.datetime(2026, 11, 6, 20, 30, tzinfo=dt.timezone.utc)) \
        == sc.SessionStatus.OPEN
    assert sc.session_status("^GSPC", d_nov,
                             dt.datetime(2026, 11, 6, 21, 30, tzinfo=dt.timezone.utc)) \
        == sc.SessionStatus.CONFIRMED_COMPLETED


def test_rollback_retains_deferred_queue():
    """Rollback must NOT drop the queue (unresolved deferrals would be lost)."""
    sql = (REPO_ROOT / "migrations"
           / "20261002_phase_a_deferred_history_rollback.sql").read_text()
    assert "DROP TABLE" not in sql.upper(), "rollback must not drop the queue"
    assert "macro_deferred_history" in sql
    assert "retained" in sql.lower() or "retain" in sql.lower()


def test_resolved_then_refailed_reopens():
    store = MemoryDeferredStore()
    now = dt.datetime(2026, 9, 30, tzinfo=dt.timezone.utc)
    d = dt.date(2026, 9, 23)
    store.record_deferral("^GSPC", d, "X", now=now)
    store.record_success("^GSPC", d, now=now)
    assert store.all_rows()[0].status == RESOLVED
    store.record_deferral("^GSPC", d, "Y", now=now)
    assert store.all_rows()[0].status == DEFERRED
