"""Regression: yfinance-sourced futures publish Monday-Friday daily bars.

yfinance's continuous futures only ever carry Monday-Friday daily bars: the
Sunday-evening session rolls into Monday's bar, and the Sunday-labelled bar
that yfinance shows while that session is still open is relabelled once it
closes. Treating Sunday as a session made the historical gate queue a session
that can never be filled -- 2026-10-18 for ES=F, NQ=F, YM=F, RTY=F, NIY=F,
GC=F, SI=F, HG=F and NG=F, all parked as MANUAL_REVIEW_REQUIRED forever.
"""
from __future__ import annotations

import datetime as dt
from datetime import date

import pytest

import db_builder.session_calendar as sc
import db_builder.retry_policy as rp
from db_builder.deferred_history import MemoryDeferredStore, DeferredRow

import sys
sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\scripts")

import macro_data_fetch


GLOBEX = ["ES=F", "NQ=F", "YM=F", "RTY=F", "NIY=F",
          "GC=F", "SI=F", "HG=F", "CL=F", "BZ=F", "NG=F"]

SUNDAY = date(2026, 10, 18)      # the session that never existed
MONDAY = date(2026, 10, 19)
FRIDAY = date(2026, 10, 16)
SATURDAY = date(2026, 10, 17)


# ------------------------------------------------------------------- calendar

@pytest.mark.parametrize("symbol", GLOBEX)
def test_globex_symbols_use_the_mon_fri_daily_calendar(symbol):
    assert sc.adapter_for(symbol).name == "globex-daily-mon-fri"


@pytest.mark.parametrize("symbol", GLOBEX)
def test_sunday_is_not_a_session(symbol):
    adapter = sc.adapter_for(symbol)
    assert sc.session_status(symbol, SUNDAY) == sc.SessionStatus.NON_TRADING_DAY
    assert adapter.is_trading_day(SUNDAY) is False
    assert adapter.session_close_utc(SUNDAY) is None


@pytest.mark.parametrize("symbol", GLOBEX)
def test_saturday_is_not_a_session(symbol):
    assert sc.session_status(symbol, SATURDAY) == sc.SessionStatus.NON_TRADING_DAY


@pytest.mark.parametrize("symbol", GLOBEX)
def test_monday_to_friday_are_sessions(symbol):
    for d in (FRIDAY, MONDAY):
        assert sc.session_status(symbol, d) == sc.SessionStatus.CONFIRMED_COMPLETED


@pytest.mark.parametrize("symbol", GLOBEX)
def test_previous_session_skips_the_weekend(symbol):
    # Monday's previous *daily* session is Friday -- not Sunday.
    assert sc.previous_valid_session(symbol, MONDAY) == FRIDAY


def test_globex_previous_observation_matches_the_session():
    # The source definition's own convention (us_futures) is Mon-Fri too, so
    # the two questions can no longer disagree.
    sd = sc.SOURCE_DEFINITION_BY_SYMBOL["ES=F"]
    assert sc.previous_valid_observation_date("ES=F", SUNDAY, sd) == FRIDAY


def test_sunday_is_rejected_by_the_gate_not_deferred():
    decision = sc.historical_eligibility("ES=F", SUNDAY)
    assert decision.eligible is False
    assert decision.routing == sc.ROUTE_REJECT
    assert decision.reason == sc.SessionStatus.NON_TRADING_DAY.value


def test_the_monday_after_the_phantom_sunday_is_eligible():
    decision = sc.historical_eligibility(
        "ES=F", MONDAY, known_prior_sessions={FRIDAY})
    assert decision.eligible is True
    assert decision.expected_prior == FRIDAY


# ------------------------------------------------------- SESSION_OPEN is transient

def test_session_open_is_transient_not_manual():
    # SESSION_OPEN fell through to MANUAL_REVIEW_REQUIRED, which pinned the row
    # forever (MANUAL is a condition-triggered class: never retried).
    assert rp.classify_reason("SESSION_OPEN") == rp.BACKOFF_ACTIVE
    assert rp.is_known_reason("SESSION_OPEN") is True


def test_a_session_open_row_is_retry_eligible():
    row = DeferredRow(symbol="ES=F", session_date=SUNDAY, reason="SESSION_OPEN",
                      status="DEFERRED", first_seen=dt.datetime.now(dt.timezone.utc),
                      last_attempt=dt.datetime.now(dt.timezone.utc), attempt_count=1)
    assert rp.is_retry_eligible(row, dt.datetime.now(dt.timezone.utc)) is True


def test_resolve_retry_class_does_not_downgrade_a_real_manual_row():
    # The precedence rule must still hold for a genuine MANUAL blocker.
    assert rp.resolve_retry_class(rp.MANUAL_REVIEW_REQUIRED,
                                  rp.BACKOFF_ACTIVE) == rp.MANUAL_REVIEW_REQUIRED
    assert rp.resolve_retry_class(None, rp.BACKOFF_ACTIVE) == rp.BACKOFF_ACTIVE


# ------------------------------------------------- queue reconciliation

def test_reconcile_resolves_a_phantom_non_session_row(monkeypatch):
    """A queued session that is not a session at all must not be retried forever."""
    store = MemoryDeferredStore()
    store.record_deferral("ES=F", SUNDAY, "PREVIOUS_SESSION_NOT_CONFIRMED",
                          retry_class=rp.MANUAL_REVIEW_REQUIRED)

    monkeypatch.setattr(macro_data_fetch, "session_is_stored",
                        lambda engine, sym, d: False)

    n = macro_data_fetch.reconcile_stored_sessions(object(), store)
    assert n == 1
    assert store.all_rows()[0].status == "RESOLVED"


def test_reconcile_leaves_a_real_session_row_alone(monkeypatch):
    store = MemoryDeferredStore()
    store.record_deferral("ES=F", MONDAY, "PREVIOUS_SESSION_NOT_CONFIRMED",
                          retry_class=rp.MANUAL_REVIEW_REQUIRED)

    monkeypatch.setattr(macro_data_fetch, "session_is_stored",
                        lambda engine, sym, d: False)

    n = macro_data_fetch.reconcile_stored_sessions(object(), store)
    assert n == 0
    assert store.all_rows()[0].status == "DEFERRED"


def test_reconcile_still_resolves_a_stored_session(monkeypatch):
    store = MemoryDeferredStore()
    store.record_deferral("ES=F", MONDAY, "PREVIOUS_SESSION_NOT_CONFIRMED")

    monkeypatch.setattr(macro_data_fetch, "session_is_stored",
                        lambda engine, sym, d: True)

    assert macro_data_fetch.reconcile_stored_sessions(object(), store) == 1
    assert store.all_rows()[0].status == "RESOLVED"
