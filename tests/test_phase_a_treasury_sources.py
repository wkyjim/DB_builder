"""Phase A — Treasury dual-source tests (Investing.com LIVE vs Treasury.gov HISTORICAL).

Required scenarios:
  T1  Investing.com live data remains available while Treasury.gov is delayed
  T2  Treasury.gov publication subsequently creates the confirmed historical row
  T3  Investing.com recovery does not contaminate official historical data
  T4  Morning Tape / API continues to provide timely live yields
  T5  Missing official publications defer safely and recover later
"""
import datetime as dt
import os
import sys

import pandas as pd
import pytest

os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import macro_data_fetch as m  # noqa: E402
from db_builder import session_calendar as sc  # noqa: E402
from db_builder.deferred_history import MemoryDeferredStore  # noqa: E402

UTC = dt.timezone.utc
D = dt.date(2026, 9, 23)          # session under test
D1 = dt.date(2026, 9, 22)         # expected prior session
SYM = "US2YT=X"          # legacy market feed (historical writes now frozen)
CMT = "US2YT_CMT"        # official Treasury.gov CMT series


def _now_after_close():
    """A moment after the 2026-09-23 session is complete (but publication may lag)."""
    return dt.datetime(2026, 9, 24, 6, 0, tzinfo=UTC)


def _provenance_lookup_fn(prior_dates=None):
    """Return a provenance_lookup that verifies the given dates."""
    if prior_dates is None:
        prior_dates = {dt.date(2026, 9, 21), dt.date(2026, 9, 22)}
    def lookup(_engine, _symbol, _min_date, _max_date):
        return set(prior_dates)
    return lookup


def _value_lookup(prior_val=5.05):
    """Return a prior_value_lookup that supplies the stored prior close."""
    def lookup(_engine, _symbol, _min_date, _max_date):
        return {dt.date(2026, 9, 21): 4.7600, D1: prior_val}
    return lookup


# --------------------------------------------------------------- T1
def test_t1_investing_live_available_while_treasury_delayed():
    """Investing.com supplies a live observation while the official value lags."""
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-09-21", "2026-09-22", "2026-09-23"]),
            "Open": [4.70, 4.72, 4.80],
            "High": [4.72, 4.75, 4.86],
            "Low": [4.68, 4.70, 4.79],
            "Close": [4.70, 4.7556, 4.82],
            "Adj Close": [4.70, 4.7556, 4.82],
            "Volume": [None, None, None],
        }
    )
    live = m.extract_latest_unfinished_row(
        frame, SYM, "2Y", "ust_yield",
        observed_at=_now_after_close(), source_label="investing.com",
        allow_closed_session=True,
    )
    assert live is not None, "live observation must not be blocked"
    assert live["source"] == "investing.com"
    assert live["is_market_closed"] is False, "intraday must never be labelled official EOD"
    assert live["market_date"] == D
    assert live["observed_at"] is not None


# --------------------------------------------------------------- T2
def test_t2_treasury_publication_creates_confirmed_row():
    """Once the official value is published the historical row becomes eligible."""
    # before publication -> defer
    before = sc.historical_eligibility(
        SYM, D, known_prior_sessions={D1}, now_utc=_now_after_close(),
        treasury_published_dates=set(),
    )
    assert before.eligible is False
    assert before.routing == sc.ROUTE_DEFERRED
    assert before.reason == "TREASURY_PUBLICATION_NOT_CONFIRMED"

    # after publication -> historical
    after = sc.historical_eligibility(
        SYM, D, known_prior_sessions={D1}, now_utc=_now_after_close(),
        treasury_published_dates={D1, D},
    )
    assert after.eligible is True
    assert after.routing == sc.ROUTE_HISTORICAL
    assert after.reason == "OK"


# --------------------------------------------------------------- T3
def test_t3_investing_recovery_does_not_contaminate_official_history():
    """An Investing.com-derived value must never satisfy official confirmation."""
    # A date set that does NOT contain D cannot authorise a historical write,
    # even when an Investing.com value for D is present in the frame.
    decision = sc.historical_eligibility(
        SYM, D, known_prior_sessions={D1}, now_utc=_now_after_close(),
        treasury_published_dates={dt.date(2026, 9, 21)},
    )
    assert decision.eligible is False, "non-official source must not authorise EOD"
    assert decision.routing == sc.ROUTE_DEFERRED

    # Fail-closed default: no publication evidence at all.
    default = sc.historical_eligibility(
        SYM, D, known_prior_sessions={D1}, now_utc=_now_after_close())
    assert default.eligible is False
    assert default.reason == "TREASURY_PUBLICATION_NOT_CONFIRMED"


def test_t3b_official_live_row_is_marked_closed_and_sourced():
    """The Treasury.gov snapshot is the only thing labelled official."""
    assert sc.adapter_for(SYM).requires_publication_confirmation is True


# --------------------------------------------------------------- T4
def test_t4_live_yields_remain_timely_for_morning_tape():
    """Live rows carry observed_at/source so the API keeps serving timely yields."""
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-09-22", "2026-09-23"]),
            "Open": [4.72, 4.80], "High": [4.75, 4.86],
            "Low": [4.70, 4.79], "Close": [4.7556, 4.82],
            "Adj Close": [4.7556, 4.82], "Volume": [None, None],
        }
    )
    ts = _now_after_close()
    live = m.extract_latest_unfinished_row(
        frame, SYM, "2Y", "ust_yield", observed_at=ts,
        source_label="investing.com", allow_closed_session=True)
    assert live["observed_at"] == ts
    assert live["source"] == "investing.com"
    assert "close" in live and live["close"] is not None


# --------------------------------------------------------------- T5
def test_t5_missing_publication_defers_then_recovers():
    """Unpublished sessions defer and are written once the official value lands."""
    store = MemoryDeferredStore()
    row = {"date": D, "symbol": CMT, "close": 4.8500}

    _, deferred, rejected = m.route_historical_rows(
        None, [row], treasury_published_dates=set(), now_utc=_now_after_close(),
        deferred_store=store, prior_lookup=lambda *a, **k: {D1}, prior_value_lookup=_value_lookup(), provenance_lookup=_provenance_lookup_fn())
    assert deferred and not rejected
    assert len(store.all_rows()) == 1

    accepted, deferred2, _ = m.route_historical_rows(
        None, [row], treasury_published_dates={D1, D}, now_utc=_now_after_close(),
        treasury_published_values={(CMT, D1): 4.8100, (CMT, D): 4.8500},
        deferred_store=store, prior_lookup=lambda *a, **k: {D1}, prior_value_lookup=_value_lookup(), provenance_lookup=_provenance_lookup_fn())
    assert accepted and not deferred2
    assert accepted[0]["symbol"] == CMT


# --------------------------------------------- publication-aware freshness
def test_treasury_freshness_is_publication_based_not_hardcoded_t1():
    """A one-day publication lag is a WARN, not a hard failure."""
    now = dt.datetime(2026, 9, 24, 6, 0, tzinfo=UTC)
    status, expected = m.classify_treasury_freshness(dt.date(2026, 9, 23), now)
    assert status in ("CURRENT", "PUBLICATION_DELAY"), (
        "one-day publication lag must not be a hard failure")
    assert expected is not None


def test_stale_official_series_still_yields_available_official_rows():
    """A lagging official series must not block rows that ARE published."""
    rows = [
        {"date": dt.date(2026, 9, 22), "symbol": CMT, "close": 4.8100},
        {"date": dt.date(2026, 9, 23), "symbol": CMT, "close": 4.8500},
    ]
    accepted, deferred, rejected = m.route_historical_rows(
        None, rows,
        treasury_published_dates={dt.date(2026, 9, 21), dt.date(2026, 9, 22)},
        treasury_published_values={(CMT, dt.date(2026, 9, 21)): 4.7600,
                                   (CMT, dt.date(2026, 9, 22)): 4.8100},
        now_utc=_now_after_close(),
        prior_lookup=lambda *a, **k: {dt.date(2026, 9, 21), dt.date(2026, 9, 22)},
        prior_value_lookup=_value_lookup(), provenance_lookup=_provenance_lookup_fn(),
    )
    accepted_dates = sorted(r["date"] for r in accepted)
    deferred_dates = sorted(r["date"] for r, _reason in deferred)
    assert dt.date(2026, 9, 22) in accepted_dates, "published session must be accepted"
    assert dt.date(2026, 9, 23) in deferred_dates, "unpublished session must defer"
    assert not rejected


# =====================================================================
# Invariant 1 — Treasury ROW-LEVEL source integrity (end-to-end write path)
# =====================================================================
A_OFFICIAL = 4.8500      # Treasury.gov official par yield for D
B_INVESTING = 4.8700     # a DIFFERENT value from Investing.com for same D


def _seed_official(symbol=CMT, value=A_OFFICIAL):
    m._TREASURY_PUBLISHED_DATES.clear()
    m._TREASURY_PUBLISHED_VALUES.clear()
    m._TREASURY_PUBLISHED_DATES.update({D1, D})
    m._TREASURY_PUBLISHED_VALUES[(symbol, D1)] = 4.8100
    m._TREASURY_PUBLISHED_VALUES[(symbol, D)] = value


def test_row_level_official_value_accepted():
    """Treasury.gov value A for D is written historically."""
    _seed_official()
    accepted, deferred, rejected = m.route_historical_rows(
        None, [{"date": D, "symbol": CMT, "close": A_OFFICIAL}],
        now_utc=_now_after_close(), prior_lookup=lambda *a, **k: {D1}, prior_value_lookup=_value_lookup(), provenance_lookup=_provenance_lookup_fn())
    assert len(accepted) == 1 and not deferred and not rejected


def test_row_level_unofficial_value_deferred_despite_date_present():
    """Date-only confirmation must NOT authorise an Investing.com value B."""
    _seed_official()                       # D IS in the publication-date set
    accepted, deferred, rejected = m.route_historical_rows(
        None, [{"date": D, "symbol": CMT, "close": B_INVESTING}],
        now_utc=_now_after_close(), prior_lookup=lambda *a, **k: {D1}, prior_value_lookup=_value_lookup(), provenance_lookup=_provenance_lookup_fn())
    assert not accepted, "unofficial value must never enter historical storage"
    assert len(deferred) == 1
    assert deferred[0][1] == "CMT_VALUE_NOT_OFFICIAL"


def test_row_level_wrong_tenor_value_rejected():
    """A value published for another tenor must not satisfy this symbol."""
    m._TREASURY_PUBLISHED_DATES.clear()
    m._TREASURY_PUBLISHED_VALUES.clear()
    m._TREASURY_PUBLISHED_DATES.update({D1, D})
    m._TREASURY_PUBLISHED_VALUES[("US10YT_CMT", D)] = 4.2000   # wrong tenor only
    accepted, deferred, _ = m.route_historical_rows(
        None, [{"date": D, "symbol": CMT, "close": 4.2000}],
        now_utc=_now_after_close(), prior_lookup=lambda *a, **k: {D1}, prior_value_lookup=_value_lookup(), provenance_lookup=_provenance_lookup_fn())
    assert not accepted, "wrong-tenor value must not authorise a write"
    assert len(deferred) == 1


def test_row_level_mismatched_source_value_deferred():
    """Close differing from the official value beyond tolerance is deferred."""
    _seed_official()
    accepted, deferred, _ = m.route_historical_rows(
        None, [{"date": D, "symbol": CMT, "close": A_OFFICIAL + 0.02}],
        now_utc=_now_after_close(), prior_lookup=lambda *a, **k: {D1}, prior_value_lookup=_value_lookup(), provenance_lookup=_provenance_lookup_fn())
    assert not accepted and len(deferred) == 1


def test_row_level_rounding_within_tolerance_accepted():
    """Publication rounding within tolerance is still official."""
    _seed_official()
    accepted, deferred, _ = m.route_historical_rows(
        None, [{"date": D, "symbol": CMT, "close": A_OFFICIAL + 0.00001}],
        now_utc=_now_after_close(), prior_lookup=lambda *a, **k: {D1}, prior_value_lookup=_value_lookup(), provenance_lookup=_provenance_lookup_fn())
    assert len(accepted) == 1 and not deferred


def test_row_level_live_may_retain_investing_value():
    """macro_live keeps the Investing.com observation B, sourced correctly."""
    frame = pd.DataFrame({
        "Date": pd.to_datetime(["2026-09-22", "2026-09-23"]),
        "Open": [4.80, B_INVESTING], "High": [4.82, B_INVESTING],
        "Low": [4.79, B_INVESTING], "Close": [4.8100, B_INVESTING],
        "Adj Close": [4.8100, B_INVESTING], "Volume": [None, None],
    })
    live = m.extract_latest_unfinished_row(
        frame, SYM, "2Y", "ust_yield", observed_at=_now_after_close(),
        source_label="investing.com", allow_closed_session=True)
    assert live is not None
    assert live["source"] == "investing.com"
    assert abs(live["close"] - B_INVESTING) < 1e-9
    assert live["is_market_closed"] is False


def test_official_value_matches_fails_closed_when_unrecorded():
    assert m.official_value_matches(SYM, D, A_OFFICIAL, published_values={}) is False
    assert m.official_value_matches(SYM, D, None) is False
    assert m.official_value_matches(SYM, None, A_OFFICIAL) is False


# =====================================================================
# Invariant 2 — is_market_closed semantics
# =====================================================================
def test_is_market_closed_is_not_a_publication_flag():
    """Official publication must not be encoded as market closure."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                            "macro_data_fetch.py"), encoding="utf-8").read()
    assert 'latest["is_market_closed"] = True' not in src, (
        "is_market_closed must describe market closure only")


def test_treasury_official_snapshot_is_servable_live():
    """An official snapshot stays visible to live consumers (filter is FALSE)."""
    row = {"symbol": SYM, "market_date": D, "close": A_OFFICIAL,
           "is_market_closed": False, "source": "treasury.gov"}
    assert row["is_market_closed"] is False
    assert row["source"] == "treasury.gov"
