"""D1/D2 hotfix tests: alert severity classification + persistent queue.

D1 — deferrals are classified by session/window CONTEXT, not by reason string.
  * benign boundary deferral  -> non-fatal (no alert, exit 0)
  * actionable deferral       -> alert, non-zero exit
D2 — the deferred-history queue is persistent in the production path, and
bounded recovery resolves, escalates and stays idempotent.
"""

import datetime as dt
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Isolated harness: the production module builds SQLAlchemy engines at import
# time. Point them at an in-process sqlite URL so no PostgreSQL is touched.
os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")
os.environ.setdefault("LOCAL_DB_PASSWORD", "phase-a-test")
os.environ.setdefault("NEON_DB_PASSWORD", "phase-a-test")

UTC = dt.timezone.utc
D = dt.date


@pytest.fixture
def m():
    import macro_data_fetch as m
    m._PHASE_A_UNAVAILABLE_SYMBOLS.clear()
    m._PHASE_A_DEFERRED_COUNT = 0
    m._PHASE_A_BOUNDARY_DEFERRED_COUNT = 0
    m._PHASE_A_ACTIONABLE_DEFERRALS.clear()
    yield m
    m._PHASE_A_UNAVAILABLE_SYMBOLS.clear()
    m._PHASE_A_DEFERRED_COUNT = 0
    m._PHASE_A_BOUNDARY_DEFERRED_COUNT = 0
    m._PHASE_A_ACTIONABLE_DEFERRALS.clear()


class FakeEngine:
    pass


# --------------------------------------------------------------------------
# D1 — classification uses window context, not the reason string
# --------------------------------------------------------------------------

def test_d1_boundary_deferral_is_non_fatal(m):
    """1. first-row prior session outside the lookback window -> non-fatal."""
    # The cycle fetch window starts at now - (LOOKBACK + CALC_BUFFER).
    now = dt.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    window_start = now.date() - dt.timedelta(
        days=m.LOOKBACK_DAYS + m.CALC_BUFFER_DAYS)
    expected_prior = window_start - dt.timedelta(days=3)   # before the window

    assert m._classify_deferral(
        "PREVIOUS_SESSION_NOT_CONFIRMED",
        expected_prior=expected_prior, batch_start=window_start) == "boundary"

    # And a cycle whose only deferrals are of this kind exits 0.
    m._PHASE_A_DEFERRED_COUNT = 7
    m._PHASE_A_BOUNDARY_DEFERRED_COUNT = 7
    summary_ok = m.summarize_outcomes([
        m.SymbolFetchOutcome("^GSPC", "S&P 500", "yfinance", True, True, 5, None, None),
    ])
    assert m._phase_a_safety_exit_code(summary_ok) == 0


def test_d1_missing_prior_inside_range_is_actionable(m):
    """2. missing prior session INSIDE the expected fetch range -> actionable."""
    now = dt.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    window_start = now.date() - dt.timedelta(
        days=m.LOOKBACK_DAYS + m.CALC_BUFFER_DAYS)
    # Prior session is inside the requested range but the provider omitted it.
    expected_prior = window_start + dt.timedelta(days=2)

    assert m._classify_deferral(
        "PREVIOUS_SESSION_NOT_CONFIRMED",
        expected_prior=expected_prior, batch_start=window_start) == "actionable"

    # Exactly on the boundary start is still inside the requested range.
    assert m._classify_deferral(
        "PREVIOUS_SESSION_NOT_CONFIRMED",
        expected_prior=window_start, batch_start=window_start) == "actionable"


def test_d1_unresolved_calendar_or_source_is_actionable(m):
    """3. unresolved calendar / source definition -> actionable."""
    window_start = D(2026, 9, 26)
    for reason in ("CLOSED_UNCONFIRMED", "SOURCE_DEFINITION_UNRESOLVED",
                   "CALENDAR_UNAVAILABLE", "TREASURY_PUBLICATION_NOT_CONFIRMED",
                   "CMT_VALUE_NOT_OFFICIAL", "PRIOR_SESSION_LOOKUP_FAILED",
                   "LEGACY_EOD_DEFINITION_UNRESOLVED"):
        assert m._classify_deferral(
            reason, expected_prior=window_start + dt.timedelta(days=1),
            batch_start=window_start) == "actionable", reason

    # A missing expected_prior can never be proven benign.
    assert m._classify_deferral(
        "CLOSED_UNCONFIRMED", expected_prior=None,
        batch_start=window_start) == "actionable"


def test_d1_escalated_deferred_history_is_actionable(m):
    """4. escalated deferred history -> actionable, and it alerts when coverage fails."""
    m._PHASE_A_ACTIONABLE_DEFERRALS.append(
        {"symbol": "US10YT_CMT", "reason": "DEFERRED_HISTORY_UNRESOLVED"})
    # With coverage not satisfied, exit 1.
    summary_fail = m.summarize_outcomes([
        m.SymbolFetchOutcome("US10YT_CMT", "10Y CMT", "treasury.gov", False, False, 0, None, "DEFERRED_HISTORY_UNRESOLVED"),
    ])
    assert m._phase_a_safety_exit_code(summary_fail) == 1
    # With coverage satisfied, DEGRADED_SUCCESS (exit 0).
    summary_ok = m.summarize_outcomes([
        m.SymbolFetchOutcome("^GSPC", "S&P 500", "yfinance", True, True, 5, None, None),
    ])
    assert m._phase_a_safety_exit_code(summary_ok) == 0


def test_d1_normal_cycle_with_only_boundary_deferrals_exits_zero(m):
    """5. normal cycle, only benign boundary deferrals -> exit 0."""
    m._PHASE_A_DEFERRED_COUNT = 14
    m._PHASE_A_BOUNDARY_DEFERRED_COUNT = 14
    summary_ok = m.summarize_outcomes([
        m.SymbolFetchOutcome("^GSPC", "S&P 500", "yfinance", True, True, 5, None, None),
    ])
    assert m._phase_a_safety_exit_code(summary_ok) == 0
    # A fully clean cycle is also 0.
    m._PHASE_A_DEFERRED_COUNT = 0
    m._PHASE_A_BOUNDARY_DEFERRED_COUNT = 0
    assert m._phase_a_safety_exit_code(summary_ok) == 0


def test_d1_actionable_integrity_deferral_is_non_zero(m):
    """6. actionable integrity deferral with coverage not satisfied -> non-zero exit."""
    m._PHASE_A_ACTIONABLE_DEFERRALS.append(
        {"symbol": "GATE_ERROR", "reason": "GATE_ERROR:ValueError"})
    summary_fail = m.summarize_outcomes([
        m.SymbolFetchOutcome("GATE_ERROR", "Gate", "yfinance", False, False, 0, None, "GATE_ERROR:ValueError"),
    ])
    assert m._phase_a_safety_exit_code(summary_fail) == 1


def test_d1_route_classifies_by_context_not_reason(m):
    """The router must populate the right bucket using real window context."""
    now = dt.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    window_start = now.date() - dt.timedelta(
        days=m.LOOKBACK_DAYS + m.CALC_BUFFER_DAYS)

    # A prior session that is inside the window but absent from the lookup.
    inside = window_start + dt.timedelta(days=1)
    m.route_historical_rows(
        FakeEngine(),
        [{"date": window_start + dt.timedelta(days=4), "symbol": "^GSPC",
          "close": 5000.0}],
        now_utc=now, batch_start=window_start,
        prior_lookup=lambda e, s, a, b: set())

    assert m._PHASE_A_DEFERRED_COUNT == 1
    assert m._PHASE_A_BOUNDARY_DEFERRED_COUNT == 0
    assert len(m._PHASE_A_ACTIONABLE_DEFERRALS) == 1
    # With coverage not satisfied (single symbol failed), exit 1.
    summary_fail = m.summarize_outcomes([
        m.SymbolFetchOutcome("^GSPC", "S&P 500", "yfinance", True, False, 0, None, "PREVIOUS_SESSION_NOT_CONFIRMED"),
    ])
    assert m._phase_a_safety_exit_code(summary_fail) == 1


# --------------------------------------------------------------------------
# D2 — persistent deferred store wiring
# --------------------------------------------------------------------------

def test_d2_production_store_is_persistent_sql_store(m):
    """The production path must use the SQL-backed store, not memory."""
    from db_builder.deferred_history import (
        MemoryDeferredStore, SqlDeferredStore)

    store = m.production_deferred_store()
    assert isinstance(store, SqlDeferredStore)
    assert not isinstance(store, MemoryDeferredStore)
    # Stable identity: one shared store per process.
    assert m.production_deferred_store() is store


def test_d2_route_persists_deferral_to_store(m):
    """A deferral must not exist only in memory."""
    from db_builder.deferred_history import MemoryDeferredStore

    store = MemoryDeferredStore()
    m.route_historical_rows(
        FakeEngine(),
        [{"date": D(2026, 9, 23), "symbol": "^GSPC", "close": 5000.0}],
        now_utc=dt.datetime(2026, 9, 30, tzinfo=UTC),
        prior_lookup=lambda e, s, a, b: set(),
        deferred_store=store)

    rows = store.all_rows()
    assert len(rows) == 1
    assert rows[0].symbol == "^GSPC"
    assert rows[0].session_date == D(2026, 9, 23)


def test_d2_upsert_macro_forwards_store(m):
    """upsert_macro(INGEST) must forward the store to the router."""
    from db_builder.deferred_history import MemoryDeferredStore

    store = MemoryDeferredStore()
    written = m.upsert_macro(
        FakeEngine(),
        [{"date": D(2026, 9, 23), "symbol": "^GSPC", "close": 5000.0}],
        mode="INGEST", prior_lookup=lambda e, s, a, b: set(),
        deferred_store=store)
    assert written == 0
    assert len(store.all_rows()) == 1


def test_d2_recovery_resolves_and_is_idempotent(m, monkeypatch):
    """DEFER -> persisted -> retry -> RESOLVED -> no duplicate."""
    from db_builder.deferred_history import (
        DEFERRED, RESOLVED, MemoryDeferredStore)

    store = MemoryDeferredStore()
    session = D(2026, 9, 23)
    base = dt.datetime(2026, 9, 30, tzinfo=UTC)
    store.record_deferral("^GSPC", session, "PREVIOUS_SESSION_NOT_CONFIRMED",
                          now=base)

    assert [r.status for r in store.all_rows()] == [DEFERRED]

    # The write itself is exercised against real PostgreSQL separately; here we
    # verify the recovery control flow (resolve + idempotency).
    monkeypatch.setattr(m, "upsert_macro", lambda *a, **k: 1)

    def fetch(symbol, dates):
        return [{"date": session, "symbol": symbol, "close": 5000.0}]

    prior = lambda e, s, a, b: {D(2026, 9, 22)}   # prior session confirmed
    # Phase A.2B: retry after the transient backoff window has elapsed.
    retry_now = base + dt.timedelta(hours=1, seconds=1)
    result = m.run_deferred_recovery(
        FakeEngine(), store, fetch_symbol_rows=fetch,
        now_utc=retry_now, prior_lookup=prior)
    assert result["resolved"] == 1
    assert [r.status for r in store.all_rows()] == [RESOLVED]

    # Second retry: a RESOLVED row is not a due candidate -> idempotent.
    result2 = m.run_deferred_recovery(
        FakeEngine(), store, fetch_symbol_rows=fetch,
        now_utc=retry_now, prior_lookup=prior)
    assert result2["retried"] == 0
    assert len(store.all_rows()) == 1


def test_d2_unresolved_case_persists_and_escalates(m):
    """An unresolvable deferral stays tracked and escalates."""
    from db_builder.deferred_history import (
        DEFERRED_HISTORY_UNRESOLVED, MemoryDeferredStore)

    store = MemoryDeferredStore()
    session = D(2026, 9, 23)
    base = dt.datetime(2026, 9, 30, tzinfo=UTC)
    store.record_deferral("^GSPC", session, "PREVIOUS_SESSION_NOT_CONFIRMED",
                          now=base)

    def fetch_nothing(symbol, dates):
        return []

    # Phase A.2B: retry after the transient backoff window has elapsed.
    result = m.run_deferred_recovery(
        FakeEngine(), store, fetch_symbol_rows=fetch_nothing,
        now_utc=base + dt.timedelta(hours=1, seconds=1))
    assert result["still_deferred"] == 1
    assert [r.status for r in store.all_rows()] == ["DEFERRED"]

    # Force escalation via the attempt limit; the row must remain tracked.
    escalated = store.escalate_stale(
        now=dt.datetime(2026, 9, 30, tzinfo=UTC), max_attempts=1)
    assert len(escalated) == 1
    assert store.all_rows()[0].status == DEFERRED_HISTORY_UNRESOLVED
    assert len(store.all_rows()) == 1
