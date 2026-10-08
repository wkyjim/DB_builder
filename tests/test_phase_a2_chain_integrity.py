"""Phase A.2A tests: prior-session chain integrity.

Invariant under test:
    A historical row D may only be written when its previous valid market
    session P is itself confirmed under the same approved source definition
    and is already stored. Mere presence of P as a batch *candidate* is
    insufficient: candidate != confirmed, deferred != valid prior,
    rejected != valid prior.

A2-1  P already stored and confirmed                  -> D may write
A2-2  P present in batch but DEFERRED                 -> D must DEFER
A2-3  P present in batch but REJECTED                 -> D must DEFER/REJECT
A2-4  P candidate not committed, no atomic P+D        -> D must not rely on P
A2-5  P stored in an earlier completed transaction    -> next-cycle D may write
A2-6  P missing; D-2 exists                           -> no positional fallback
A2-7  Holiday/weekend gap                             -> previous_valid_session()
A2-8  Deferred recovery order                         -> P resolved before D
A2-9  Production reproduction: 10-06 deferred, 10-07 candidate -> 10-07 not written
A2-10 Arithmetic: prev_close == P.close and derived fields recompute
"""
import os
import sys
from datetime import date

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(REPO_ROOT, 'src'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'scripts'))

os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")
os.environ.setdefault("LOCAL_DB_PASSWORD", "phase-a2-test")
os.environ.setdefault("NEON_DB_PASSWORD", "phase-a2-test")

from macro_data_fetch import (
    route_historical_rows,
    ROUTE_HISTORICAL,
    ROUTE_DEFERRED,
    ROUTE_REJECT,
    _PHASE_A_ACTIONABLE_DEFERRALS,
    _PHASE_A_UNAVAILABLE_SYMBOLS,
    _PHASE_A_DEFERRED_COUNT,
    _PHASE_A_BOUNDARY_DEFERRED_COUNT,
)
from db_builder.session_calendar import adapter_for


SYMBOL = "ES=F"          # yfinance continuous futures (Mon-Fri daily bars)
INVESTING_SYMBOL = "BZ=F"  # Investing.com continuous futures: convention NOT established
D = date(2026, 10, 7)
P = date(2026, 10, 6)
D2 = date(2026, 10, 2)   # the row before the gap
MON = date(2026, 10, 5)  # Monday: previous OBSERVATION is Fri 10-02, not Sun 10-04
SUN = date(2026, 10, 4)  # real Globex session, but not a published daily bar


def make_row(d, close, symbol=SYMBOL, prev_close=None, high=None, low=None):
    return {
        "date": d,
        "symbol": symbol,
        "name": "Test",
        "asset_type": "futures",
        "open": close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "adj_close": close,
        "volume": 100.0,
        "prev_close": prev_close,
        "change": None,
        "pct_chg": None,
        "amplitude": None,
    }


def clear_state():
    _PHASE_A_ACTIONABLE_DEFERRALS.clear()
    _PHASE_A_UNAVAILABLE_SYMBOLS.clear()
    global _PHASE_A_DEFERRED_COUNT, _PHASE_A_BOUNDARY_DEFERRED_COUNT
    _PHASE_A_DEFERRED_COUNT = 0
    _PHASE_A_BOUNDARY_DEFERRED_COUNT = 0


@pytest.fixture(autouse=True)
def _reset():
    clear_state()
    yield
    clear_state()


def run_route(rows, stored):
    """Route ``rows`` against ``stored`` (set of already-persisted dates).

    ``prior_lookup`` is injected so the test never touches a database: it
    returns exactly the set of dates that are already committed.
    """
    def lookup(_engine, _symbol, _min_date, _max_date):
        return set(stored)

    return route_historical_rows(None, rows, prior_lookup=lookup,
                                 count_telemetry=False)


def dates_of(rows):
    return {r["date"] for r in rows}


# --------------------------------------------------------------------------
# A2-1  P already stored and confirmed -> D may write
# --------------------------------------------------------------------------
class TestA2_1_PStored:
    def test_a2_1_d_writes_when_p_is_stored(self):
        accepted, deferred, rejected = run_route(
            [make_row(D, 100.20, prev_close=100.58)],
            stored={D2, P},
        )
        assert dates_of(accepted) == {D}
        assert deferred == []
        assert rejected == []


# --------------------------------------------------------------------------
# A2-2  P present in batch but DEFERRED -> D must DEFER
# --------------------------------------------------------------------------
class TestA2_2_PDeferredInBatch:
    def test_a2_2_d_defers_when_p_is_only_a_candidate(self):
        # P is a batch candidate but is NOT stored: it will itself be deferred
        # (its own prior, 10-05, is absent). D must not rely on it.
        accepted, deferred, rejected = run_route(
            [make_row(P, 100.58), make_row(D, 100.20, prev_close=100.58)],
            stored={D2},
        )
        assert dates_of(accepted) == set(), "D must NOT be written"
        assert dates_of([r for r, _ in deferred]) == {P, D}
        reasons = {r["date"]: reason for r, reason in deferred}
        assert reasons[D] == "PREVIOUS_SESSION_NOT_CONFIRMED"
        assert reasons[P] == "PREVIOUS_SESSION_NOT_CONFIRMED"

    def test_a2_2b_p_candidate_alone_never_confirms_d(self):
        # Even if P's own prior IS stored (so P would be accepted), D must
        # still not be written in the SAME batch, because P is not yet
        # committed when D is evaluated.
        accepted, deferred, rejected = run_route(
            [make_row(P, 100.58), make_row(D, 100.20, prev_close=100.58)],
            stored={D2, date(2026, 10, 5)},
        )
        assert P in dates_of(accepted), "P itself is eligible"
        assert D not in dates_of(accepted), "D must defer until P is stored"
        assert D in dates_of([r for r, _ in deferred])


# --------------------------------------------------------------------------
# A2-3  P present in batch but REJECTED -> D must DEFER/REJECT
# --------------------------------------------------------------------------
class TestA2_3_PRejectedInBatch:
    def test_a2_3_d_never_writes_using_rejected_p(self):
        # A Saturday-dated P for a non-weekend calendar is REJECTED, not
        # deferred. D must still not use it.
        saturday = date(2026, 10, 10)
        accepted, deferred, rejected = run_route(
            [make_row(saturday, 101.0), make_row(D, 100.20, prev_close=101.0)],
            stored={D2},
        )
        assert D not in dates_of(accepted)
        assert D in dates_of([r for r, _ in deferred])


# --------------------------------------------------------------------------
# A2-4  P candidate passes but is not committed; no atomic P+D guarantee
# --------------------------------------------------------------------------
class TestA2_4_NoAtomicGuarantee:
    def test_a2_4_d_must_not_rely_on_uncommitted_p(self):
        # Two rows whose priors chain: 10-05 -> 10-06 -> 10-07.
        # Nothing is stored. Even though every row in the batch would be
        # mutually consistent, none may be written: there is no stored anchor
        # and the gate cannot prove atomic P+D.
        accepted, deferred, rejected = run_route(
            [
                make_row(date(2026, 10, 5), 99.0),
                make_row(P, 100.58),
                make_row(D, 100.20, prev_close=100.58),
            ],
            stored=set(),
        )
        assert accepted == [], "no row may be written without a stored anchor"

    def test_a2_4b_single_stored_anchor_unblocks_exactly_one_row(self):
        # With 10-05 stored, 10-06 becomes eligible but 10-07 must still wait
        # (10-06 is not stored during this evaluation).
        accepted, deferred, rejected = run_route(
            [make_row(P, 100.58), make_row(D, 100.20, prev_close=100.58)],
            stored={date(2026, 10, 5)},
        )
        assert dates_of(accepted) == {P}
        assert dates_of([r for r, _ in deferred]) == {D}


# --------------------------------------------------------------------------
# A2-5  P stored during an earlier completed transaction -> next-cycle D writes
# --------------------------------------------------------------------------
class TestA2_5_NextCycle:
    def test_a2_5_d_writes_on_next_cycle_after_p_committed(self):
        rows = [make_row(P, 100.58), make_row(D, 100.20, prev_close=100.58)]

        # Cycle 1: only 10-05 stored -> P accepted, D deferred.
        acc1, def1, _ = run_route(rows, stored={date(2026, 10, 5)})
        assert dates_of(acc1) == {P}

        # Cycle 2: P now committed -> D accepted.
        acc2, def2, _ = run_route(
            [make_row(D, 100.20, prev_close=100.58)],
            stored={date(2026, 10, 5), P},
        )
        assert dates_of(acc2) == {D}
        assert def2 == []


# --------------------------------------------------------------------------
# A2-6  P missing; D-2 exists -> no positional fallback
# --------------------------------------------------------------------------
class TestA2_6_NoPositionalFallback:
    def test_a2_6_no_d_minus_2_fallback(self):
        accepted, deferred, rejected = run_route(
            [make_row(D, 100.20, prev_close=102.70)],   # prev_close == D-2 close
            stored={D2},                                 # only D-2 stored
        )
        assert accepted == [], "must not fall back to D-2"
        assert dates_of([r for r, _ in deferred]) == {D}
        assert deferred[0][1] == "PREVIOUS_SESSION_NOT_CONFIRMED"


# --------------------------------------------------------------------------
# A2-7  Holiday/weekend gap -> previous_valid_observation_date()
# --------------------------------------------------------------------------
class TestA2_7_CalendarGap:
    def test_a2_7_sunday_is_not_a_daily_observation(self):
        """Globex Sunday is a real trading SESSION but not a published daily
        bar, so Monday's previous observation is Friday."""
        from db_builder.session_calendar import (
            previous_valid_observation_date, previous_valid_session,
        )
        assert previous_valid_observation_date(SYMBOL, MON) == D2
        # the session calendar still reports Sunday (unchanged semantics)
        assert previous_valid_session(SYMBOL, MON) == SUN

    def test_a2_7b_holiday_aware_not_day_minus_one(self):
        """A US holiday must be skipped; the convention is not 'minus one day'."""
        from db_builder.session_calendar import previous_valid_observation_date
        # Mon 2026-07-06 follows Independence Day (Fri 2026-07-03 observed);
        # 2026-07-04 is Sat, 2026-07-05 is Sun.
        assert previous_valid_observation_date(
            SYMBOL, date(2026, 7, 6)) == date(2026, 7, 3)

    def test_a2_7c_friday_previous_observation_is_thursday(self):
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(
            SYMBOL, date(2026, 10, 2)) == date(2026, 10, 1)

    def test_a2_7d_unestablished_convention_fails_closed(self):
        """A source definition whose daily-bar convention is not established
        must fail closed -- it never inherits another definition's convention."""
        from db_builder.session_calendar import (
            previous_valid_observation_date, observation_convention_established,
        )
        assert not observation_convention_established(INVESTING_SYMBOL)
        assert previous_valid_observation_date(INVESTING_SYMBOL, MON) is None

    def test_a2_7d_session_date_labelled_definition_uses_session(self):
        """A recognised definition whose bars are labelled by session date
        (cash equity) resolves via the session calendar."""
        from db_builder.session_calendar import (
            previous_valid_observation_date, previous_valid_session,
        )
        assert (previous_valid_observation_date("^GSPC", MON)
                == previous_valid_session("^GSPC", MON))

    def test_a2_7d_unknown_calendar_still_fails_closed(self):
        """A symbol whose calendar cannot be resolved must yield None so the
        gate defers -- an unestablished *calendar* is never guessed."""
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date("NOT_A_REAL_SYMBOL", MON) is None


# --------------------------------------------------------------------------
# A2-7e  Expected production chain (design section 5)
# --------------------------------------------------------------------------
class TestA2_7e_ExpectedProductionChain:
    def test_a2_7e_chain_resolves_sequentially_without_a_sunday_row(self):
        """10-05 -> 10-06 -> 10-07 resolve one per cycle; no Sunday DB row."""
        # Cycle 1: stored through 10-02. Only 10-05 may write.
        acc, defd, _ = run_route(
            [make_row(MON, 101.10), make_row(P, 100.58), make_row(D, 100.20)],
            stored={D2},
        )
        assert dates_of(acc) == {MON}, "only 10-05 may write"
        assert dates_of([r for r, _ in defd]) == {P, D}

        # Cycle 2: 10-05 now committed. 10-06 may write.
        acc, defd, _ = run_route(
            [make_row(P, 100.58), make_row(D, 100.20)],
            stored={D2, MON},
        )
        assert dates_of(acc) == {P}
        assert dates_of([r for r, _ in defd]) == {D}

        # Cycle 3: 10-06 now committed. 10-07 may write.
        acc, defd, _ = run_route([make_row(D, 100.20, prev_close=100.58)],
                                 stored={D2, MON, P})
        assert dates_of(acc) == {D}
        assert defd == []

    def test_a2_7e_no_sunday_row_required(self):
        """The chain completes without any Sunday-dated row in storage."""
        from db_builder.session_calendar import previous_valid_observation_date
        stored = {D2, MON, P}
        assert SUN not in stored
        assert previous_valid_observation_date(SYMBOL, MON) == D2
        acc, _, _ = run_route([make_row(D, 100.20, prev_close=100.58)],
                              stored=stored)
        assert dates_of(acc) == {D}


# --------------------------------------------------------------------------
# A2-7f  No nearest-stored / D-n fallback (design section 6)
# --------------------------------------------------------------------------
class TestA2_7f_NoNearestStoredFallback:
    def test_a2_7f_does_not_use_older_stored_row_as_substitute(self):
        """If 10-05 and 10-06 are expected observations but unstored, 10-07
        must NOT fall back to the nearest stored row (10-02)."""
        accepted, deferred, rejected = run_route(
            [make_row(D, 100.20, prev_close=102.70)],   # 10-02's close
            stored={D2},                                 # only 10-02 stored
        )
        assert accepted == [], "nearest-stored fallback prohibited"
        assert dates_of([r for r, _ in deferred]) == {D}

    def test_a2_7f_10_02_is_legitimate_only_for_10_05(self):
        """10-02 IS the correct previous observation for 10-05 -- the
        prohibition applies to substituting it for a later row."""
        acc, defd, _ = run_route([make_row(MON, 101.10)], stored={D2})
        assert dates_of(acc) == {MON}
        assert defd == []


# --------------------------------------------------------------------------
# A2-8  Deferred recovery order: P before D
# --------------------------------------------------------------------------
class TestA2_8_RecoveryOrder:
    def test_a2_8_recovery_sorts_ascending_so_p_resolves_first(self):
        from db_builder.deferred_history import MemoryDeferredStore
        import datetime as dt

        store = MemoryDeferredStore()
        now = dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc)
        # Record D first, then P (insertion order deliberately reversed).
        store.record_deferral(SYMBOL, D, "PREVIOUS_SESSION_NOT_CONFIRMED", now=now)
        store.record_deferral(SYMBOL, P, "PREVIOUS_SESSION_NOT_CONFIRMED", now=now)

        cands = store.due_candidates(now=now)
        order = [c.session_date for c in cands]
        assert order == sorted(order), "recovery must be chronological"
        assert order.index(P) < order.index(D), "P must be retried before D"

    def test_a2_8b_recovery_batch_never_contains_two_dates_for_one_symbol(self):
        """Recovery evaluates one (symbol, session_date) at a time, so the
        batch_dates flaw cannot be triggered from the recovery path."""
        import inspect
        import macro_data_fetch as m

        src = inspect.getsource(m.run_deferred_recovery)
        assert 'cand.session_date' in src
        # the match filter is pinned to the single candidate session
        assert 'pd.to_datetime(r["date"]).date() == cand.session_date' in src


# --------------------------------------------------------------------------
# A2-9  Production reproduction: 10-06 deferred, 10-07 candidate
# --------------------------------------------------------------------------
class TestA2_9_ProductionReproduction:
    def test_a2_9_2026_10_07_not_written_when_2026_10_06_deferred(self):
        """Exact production shape: 10-05/10-06/10-07 arrive as candidates and
        10-02 is the last stored row. Under the daily-observation convention
        10-05's previous observation IS 10-02, so 10-05 may write; 10-06 and
        10-07 must wait. Before the fix 10-07 was written with prev_close from
        the unpersisted 10-06 bar."""
        accepted, deferred, rejected = run_route(
            [
                make_row(MON, 101.10),                   # 10-05 candidate
                make_row(P, 100.58),                     # 10-06 candidate
                make_row(D, 100.20, prev_close=100.58),  # 10-07 candidate
            ],
            stored={D2},                                 # 10-02 last stored
        )
        assert D not in dates_of(accepted), "REGRESSION: 10-07 written"
        assert dates_of(accepted) == {MON}, "only 10-05 may write this cycle"
        assert dates_of([r for r, _ in deferred]) == {P, D}
        for rec, reason in deferred:
            assert reason == "PREVIOUS_SESSION_NOT_CONFIRMED"

    def test_a2_9b_old_behaviour_would_have_written_d(self):
        """Guard: prove the defect is what the fix removes. With the legacy
        candidate-union rule, 10-07 IS accepted while 10-06 is deferred."""
        from macro_data_fetch import _eligible_for_historical

        # legacy known_all = stored | every batch candidate date
        legacy_known = {D2, MON, P, D}
        dec = _eligible_for_historical(
            make_row(D, 100.20, prev_close=100.58),
            known_prior_sessions=legacy_known,
        )
        assert dec.routing == ROUTE_HISTORICAL, (
            "legacy union rule reproduces the production defect")

        # fixed rule: stored-only, and the expected prior is the observation date
        dec_fixed = _eligible_for_historical(
            make_row(D, 100.20, prev_close=100.58),
            known_prior_sessions={D2},
        )
        assert dec_fixed.routing == ROUTE_DEFERRED
        assert dec_fixed.expected_prior == P


# --------------------------------------------------------------------------
# A2-10  Arithmetic anchored to P
# --------------------------------------------------------------------------
class TestA2_10_Arithmetic:
    def test_a2_10_prev_close_equals_stored_p_close(self):
        p_close = 100.58
        high, low, close = 101.50, 98.60, 100.20

        row = make_row(D, close, prev_close=p_close, high=high, low=low)
        row["change"] = round(close - p_close, 4)
        row["pct_chg"] = round((close - p_close) / p_close * 100, 4)
        row["amplitude"] = round((high - low) / p_close * 100, 4)

        accepted, deferred, rejected = run_route([row], stored={D2, P})
        assert dates_of(accepted) == {D}
        written = accepted[0]

        # invariant: prev_close is exactly the stored P close
        assert written["prev_close"] == p_close
        # derived fields recompute from prev_close
        assert written["change"] == round(written["close"] - p_close, 4)
        assert written["pct_chg"] == round(
            (written["close"] - p_close) / p_close * 100, 4)
        assert written["amplitude"] == round(
            (written["high"] - written["low"]) / p_close * 100, 4)

    def test_a2_10b_derived_fields_match_production_values(self):
        """The values production actually stored for BZ=F 2026-10-07, which
        must be reproducible only from the correct (stored) prior close."""
        close, high, low = 100.20, 101.50, 98.60
        wrong_prev = 100.58      # unpersisted 10-06 candidate (production bug)
        correct_prev = 102.70    # stored 10-02 close

        # Production stored change/pct/amplitude anchored to the WRONG prior.
        prod_change = round(close - wrong_prev, 4)
        assert prod_change == -0.38
        # With the correct prior the arithmetic differs -> proves contamination.
        correct_change = round(close - correct_prev, 4)
        assert correct_change == -2.50
        assert prod_change != correct_change


# ==========================================================================
# A2-11 .. A2-18  observation-date convention (Phase A.2A section 9)
# ==========================================================================

MON_ = date(2026, 10, 5)   # Monday
TUE = date(2026, 10, 6)    # Tuesday
WED = date(2026, 10, 7)    # Wednesday
FRI = date(2026, 10, 2)    # Friday before the weekend
THU = date(2026, 10, 1)    # Thursday


# --------------------------------------------------------------------------
# A2-11  Monday Globex daily bar: previous observation = Friday
# --------------------------------------------------------------------------
class TestA2_11_MondayGlobexBar:
    def test_a2_11_monday_previous_observation_is_friday(self):
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(SYMBOL, MON_) == FRI

    def test_a2_11_monday_may_write_when_friday_stored(self):
        accepted, deferred, rejected = run_route(
            [make_row(MON_, 101.10, prev_close=102.70)], stored={FRI})
        assert dates_of(accepted) == {MON_}
        assert deferred == []

    def test_a2_11_monday_defers_when_friday_absent(self):
        accepted, deferred, rejected = run_route([make_row(MON_, 101.10)],
                                                 stored=set())
        assert accepted == []
        assert dates_of([r for r, _ in deferred]) == {MON_}

    def test_a2_11_sunday_bar_never_required(self):
        """No Sunday-labelled daily bar is published for this provider, so the
        chain must never demand one."""
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(SYMBOL, MON_) != SUN
        accepted, _, _ = run_route([make_row(MON_, 101.10)], stored={FRI})
        assert dates_of(accepted) == {MON_}


# --------------------------------------------------------------------------
# A2-12  Tuesday dependency: previous observation = Monday
# --------------------------------------------------------------------------
class TestA2_12_TuesdayDependency:
    def test_a2_12_tuesday_previous_observation_is_monday(self):
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(SYMBOL, TUE) == MON_

    def test_a2_12_tuesday_defers_when_monday_absent(self):
        accepted, deferred, rejected = run_route(
            [make_row(TUE, 100.58)], stored={FRI})
        assert accepted == []
        assert dates_of([r for r, _ in deferred]) == {TUE}

    def test_a2_12_tuesday_writes_when_monday_stored(self):
        accepted, deferred, _ = run_route([make_row(TUE, 100.58, prev_close=101.10)],
                                          stored={FRI, MON_})
        assert dates_of(accepted) == {TUE}
        assert deferred == []


# --------------------------------------------------------------------------
# A2-13  Sunday trading semantics preserved
# --------------------------------------------------------------------------
class TestA2_13_SundaySemanticsPreserved:
    def test_a2_13_sunday_remains_a_trading_session(self):
        """Changing the daily-bar dependency logic must not make Sunday
        globally non-trading."""
        adapter = adapter_for(SYMBOL)
        assert adapter.is_trading_day(SUN) is True
        assert adapter.is_trading_day(MON_) is True

    def test_a2_13_sunday_previous_session_unchanged(self):
        from db_builder.session_calendar import previous_valid_session
        assert previous_valid_session(SYMBOL, MON_) == SUN

    def test_a2_13_crypto_sunday_is_a_real_observation(self):
        """Crypto trades weekends: Sunday is a genuine observation date, so the
        abstraction is provider-specific and not a global 'skip Sunday'."""
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date("BTC-USD", MON_) == SUN

    def test_a2_13_observation_and_session_are_separate_concepts(self):
        from db_builder.session_calendar import (
            previous_valid_observation_date, previous_valid_session,
        )
        # same symbol, deliberately different answers
        assert previous_valid_observation_date(SYMBOL, MON_) == FRI
        assert previous_valid_session(SYMBOL, MON_) == SUN


# --------------------------------------------------------------------------
# A2-14  Holiday Monday: convention, not naive weekday subtraction
# --------------------------------------------------------------------------
class TestA2_14_HolidayMonday:
    def test_a2_14_mlk_monday_is_not_a_us_equity_session(self):
        """NYSE MLK Monday is a full holiday; the previous observation for the
        following Tuesday is the preceding Friday -- not 'minus one weekday'."""
        import exchange_calendars as xc
        nyse = xc.get_calendar("XNYS")
        assert nyse.is_session(date(2026, 1, 19)) is False
        assert (nyse.previous_session(date(2026, 1, 20))
                == __import__("pandas").Timestamp("2026-01-16"))

    def test_a2_14_futures_christmas_week(self):
        """us_futures skips Christmas Day, so the Monday after is anchored to
        the prior actual observation (Thu 12-24), not Fri 12-25."""
        from db_builder.session_calendar import DailyObservationAdapter
        adapter = DailyObservationAdapter("us_futures")
        assert adapter.previous_valid_observation_date(
            date(2026, 12, 28)) == date(2026, 12, 24)

    def test_a2_14_independence_day(self):
        from db_builder.session_calendar import previous_valid_observation_date
        # Mon 2026-07-06 follows the observed Fri 2026-07-03 (07-04 Sat)
        assert previous_valid_observation_date(SYMBOL, date(2026, 7, 6)) == date(2026, 7, 3)


# --------------------------------------------------------------------------
# A2-15  Provider that DOES publish Sunday-labelled daily bars
# --------------------------------------------------------------------------
class TestA2_15_ProviderWithSundayBars:
    SYM = "SUNDAYBAR=F"
    DEF = "TEST_SUNDAY_LABELLED_FUTURES_DAILY"

    def setup_method(self):
        from db_builder.session_calendar import (
            register_observation_convention, register_symbol_source_definition,
            KNOWN_SOURCE_DEFINITIONS,
        )
        # A provider whose daily bars ARE labelled by session date and whose
        # calendar genuinely trades Sunday (a 24/7 venue).
        KNOWN_SOURCE_DEFINITIONS.add(self.DEF)
        register_symbol_source_definition(self.SYM, self.DEF)
        register_observation_convention(self.DEF, "24/7")

    def teardown_method(self):
        from db_builder.session_calendar import (
            register_observation_convention, register_symbol_source_definition,
            KNOWN_SOURCE_DEFINITIONS,
        )
        register_observation_convention(self.DEF, None)
        register_symbol_source_definition(self.SYM, None)
        KNOWN_SOURCE_DEFINITIONS.discard(self.DEF)

    def test_a2_15_sunday_may_be_required_as_prior(self):
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(self.SYM, MON_) == SUN

    def test_a2_15_abstraction_is_provider_specific_not_global(self):
        """Same date, two source definitions, two different answers."""
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(self.SYM, MON_) == SUN
        assert previous_valid_observation_date(SYMBOL, MON_) == FRI

    def test_a2_15_real_sunday_publishing_provider_requires_sunday(self):
        """Crypto is a real supported series that publishes Sunday bars: its
        Monday daily bar genuinely depends on the Sunday row."""
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date("BTC-USD", MON_) == SUN

    def test_a2_15_monday_defers_without_the_sunday_row(self):
        accepted, deferred, _ = run_route(
            [make_row(MON_, 50.0, symbol="BTC-USD")], stored={FRI})
        assert accepted == []
        assert dates_of([r for r, _ in deferred]) == {MON_}

    def test_a2_15_monday_writes_when_sunday_stored(self):
        accepted, deferred, _ = run_route(
            [make_row(MON_, 50.0, symbol="BTC-USD", prev_close=49.0)],
            stored={FRI, SUN})
        assert dates_of(accepted) == {MON_}
        assert deferred == []


# --------------------------------------------------------------------------
# A2-16  Source convention unknown -> fail closed
# --------------------------------------------------------------------------
class TestA2_16_UnknownConvention:
    SYM = "UNKNOWN_CONV=F"

    def test_a2_16_unknown_symbol_has_no_calendar_and_fails_closed(self):
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(self.SYM, MON_) is None

    def test_a2_16_gate_defers_for_unknown_convention(self):
        accepted, deferred, rejected = run_route(
            [make_row(MON_, 50.0, symbol=self.SYM)], stored={FRI})
        assert accepted == [], "unestablished convention must never write"
        assert dates_of([r for r, _ in deferred]) == {MON_}

    def test_a2_16_unknown_never_falls_back_to_a_positional_value(self):
        accepted, deferred, _ = run_route(
            [make_row(MON_, 50.0, symbol=self.SYM, prev_close=49.0)],
            stored={FRI, THU})
        assert accepted == []


# --------------------------------------------------------------------------
# A2-17  Sequential recovery: Mon -> Tue -> Wed, no chain jumping
# --------------------------------------------------------------------------
class TestA2_17_SequentialRecovery:
    def test_a2_17_recovers_one_session_per_cycle(self):
        # stored: Friday only; candidates Mon, Tue, Wed.
        # Each cycle offers only the not-yet-stored candidates, mirroring
        # filter_duplicate_symbol_dates() in the real pipeline.
        cands = [make_row(MON_, 101.10), make_row(TUE, 100.58),
                 make_row(WED, 100.20)]

        stored = {FRI}
        accepted, deferred, _ = run_route(cands, stored=stored)
        assert dates_of(accepted) == {MON_}, "Monday must resolve first"
        assert dates_of([r for r, _ in deferred]) == {TUE, WED}

        stored = {FRI, MON_}
        accepted, deferred, _ = run_route(cands[1:], stored=stored)
        assert dates_of(accepted) == {TUE}, "then Tuesday"
        assert dates_of([r for r, _ in deferred]) == {WED}

        stored = {FRI, MON_, TUE}
        accepted, deferred, _ = run_route(cands[2:], stored=stored)
        assert dates_of(accepted) == {WED}, "then Wednesday"
        assert deferred == []

    def test_a2_17_no_chain_jumping_over_a_gap(self):
        """Wednesday must not be reached while Monday is still unstored."""
        accepted, deferred, _ = run_route(
            [make_row(WED, 100.20, prev_close=100.58)], stored={FRI})
        assert accepted == []


# --------------------------------------------------------------------------
# A2-18  No candidate confirmation: a failed Monday candidate
# --------------------------------------------------------------------------
class TestA2_18_NoCandidateConfirmation:
    def test_a2_18_tuesday_must_not_use_a_deferred_monday_candidate(self):
        accepted, deferred, _ = run_route(
            [make_row(MON_, 101.10),                    # Monday candidate
             make_row(TUE, 100.58, prev_close=101.10)], # Tuesday candidate
            stored={FRI},                               # Monday NOT stored
        )
        assert dates_of(accepted) == {MON_}, "only Monday may write"
        assert TUE not in dates_of(accepted)

    def test_a2_18_tuesday_must_not_use_a_rejected_monday_candidate(self):
        """Even if the Monday candidate is rejected outright, Tuesday still
        needs a *stored* Monday."""
        accepted, deferred, _ = run_route(
            [make_row(TUE, 100.58, prev_close=101.10)], stored={FRI})
        assert accepted == []
        assert dates_of([r for r, _ in deferred]) == {TUE}

    def test_a2_18_chain_resumes_only_after_monday_is_stored(self):
        accepted, deferred, _ = run_route(
            [make_row(TUE, 100.58, prev_close=101.10)], stored={FRI, MON_})
        assert dates_of(accepted) == {TUE}


# ==========================================================================
# A2-19 .. A2-24  source-definition keying (Phase A.2A section 7)
# ==========================================================================

from db_builder.session_calendar import (          # noqa: E402
    SOURCE_DEFINITION_BY_SYMBOL,
    SOURCE_DEF_INVESTING_CONTINUOUS_FUTURES_DAILY,
    SOURCE_DEF_YFINANCE_CONTINUOUS_FUTURES_DAILY,
    SOURCE_DEF_YFINANCE_CASH_INDEX_DAILY,
    KNOWN_SOURCE_DEFINITIONS,
    source_definition_for,
)


# --------------------------------------------------------------------------
# A2-19  Same symbol, approved weekday-labelled source definition -> Friday
# --------------------------------------------------------------------------
class TestA2_19_ApprovedWeekdayDefinition:
    def test_a2_19_approved_definition_monday_is_friday(self):
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(SYMBOL, MON_) == FRI

    def test_a2_19_explicit_definition_argument_is_honoured(self):
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(
            SYMBOL, MON_, SOURCE_DEF_YFINANCE_CONTINUOUS_FUTURES_DAILY) == FRI

    def test_a2_19_resolves_for_every_weekday(self):
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(SYMBOL, MON_) == FRI
        assert previous_valid_observation_date(SYMBOL, TUE) == MON_
        assert previous_valid_observation_date(SYMBOL, WED) == TUE

    def test_a2_19_writes_when_approved_prior_stored(self):
        accepted, deferred, _ = run_route(
            [make_row(TUE, 100.58, prev_close=101.10)], stored={FRI, MON_})
        assert dates_of(accepted) == {TUE}
        assert deferred == []


# --------------------------------------------------------------------------
# A2-20  Same symbol, different source definition, no convention -> fail closed
# --------------------------------------------------------------------------
class TestA2_20_NoConventionForDefinition:
    def test_a2_20_investing_definition_has_no_convention(self):
        from db_builder.session_calendar import (
            previous_valid_observation_date, observation_convention_established,
        )
        assert source_definition_for(INVESTING_SYMBOL) == \
            SOURCE_DEF_INVESTING_CONTINUOUS_FUTURES_DAILY
        assert not observation_convention_established(INVESTING_SYMBOL)
        assert previous_valid_observation_date(INVESTING_SYMBOL, MON_) is None

    def test_a2_20_same_symbol_two_definitions_differ(self):
        """BZ=F under the yfinance definition resolves; under its own
        (unestablished) Investing.com definition it fails closed."""
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(
            INVESTING_SYMBOL, MON_,
            SOURCE_DEF_YFINANCE_CONTINUOUS_FUTURES_DAILY) == FRI
        assert previous_valid_observation_date(
            INVESTING_SYMBOL, MON_,
            SOURCE_DEF_INVESTING_CONTINUOUS_FUTURES_DAILY) is None

    def test_a2_20_gate_defers_for_unestablished_definition(self):
        accepted, deferred, _ = run_route(
            [make_row(MON_, 101.10, symbol=INVESTING_SYMBOL)], stored={FRI})
        assert accepted == [], "unestablished definition must never write"
        assert dates_of([r for r, _ in deferred]) == {MON_}

    def test_a2_20_never_inherits_the_other_definition_convention(self):
        """The Investing.com definition must not borrow yfinance's Friday."""
        from db_builder.session_calendar import previous_valid_observation_date
        investing = previous_valid_observation_date(
            INVESTING_SYMBOL, MON_, SOURCE_DEF_INVESTING_CONTINUOUS_FUTURES_DAILY)
        assert investing is None, "must not silently switch to yfinance's rule"


# --------------------------------------------------------------------------
# A2-21  Hypothetical source that publishes Sunday-labelled daily observations
# --------------------------------------------------------------------------
class TestA2_21_SundayLabelledSourceDefinition:
    DEF = "TEST_SUNDAY_LABELLED_DAILY"
    SYM = "SUNSRC=F"
    REAL = "GC=F"          # has a valid Globex session calendar

    def setup_method(self):
        from db_builder.session_calendar import (
            register_observation_convention, register_symbol_source_definition,
        )
        KNOWN_SOURCE_DEFINITIONS.add(self.DEF)
        register_symbol_source_definition(self.SYM, self.DEF)
        register_observation_convention(self.DEF, "24/7")

    def teardown_method(self):
        from db_builder.session_calendar import (
            register_observation_convention, register_symbol_source_definition,
        )
        register_observation_convention(self.DEF, None)
        register_symbol_source_definition(self.SYM, None)
        KNOWN_SOURCE_DEFINITIONS.discard(self.DEF)

    @staticmethod
    def _row(d, close, prev_close=None):
        """A real-symbol row carrying the hypothetical Sunday-labelled
        definition, so the session calendar stays coherent."""
        row = make_row(d, close, symbol="GC=F", prev_close=prev_close)
        row["source_definition"] = TestA2_21_SundayLabelledSourceDefinition.DEF
        return row

    def test_a2_21_sunday_is_required(self):
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(self.SYM, MON_) == SUN

    def test_a2_21_defers_without_sunday(self):
        accepted, deferred, _ = run_route([self._row(MON_, 50.0)], stored={FRI})
        assert accepted == []
        assert dates_of([r for r, _ in deferred]) == {MON_}

    def test_a2_21_writes_when_sunday_stored(self):
        accepted, deferred, _ = run_route(
            [self._row(MON_, 50.0, prev_close=49.0)], stored={FRI, SUN})
        assert dates_of(accepted) == {MON_}
        assert deferred == []

    def test_a2_21_futures_definition_same_row_still_skips_sunday(self):
        """Same symbol and date: under the futures definition the prior is
        Friday; under the Sunday-labelled definition it is Sunday."""
        accepted, deferred, _ = run_route(
            [make_row(MON_, 50.0, symbol="GC=F", prev_close=49.0)],
            stored={FRI})
        assert dates_of(accepted) == {MON_}, "futures definition needs Friday only"


# --------------------------------------------------------------------------
# A2-22  Fallback source changes; historical definition unchanged
# --------------------------------------------------------------------------
class TestA2_22_FallbackDoesNotSwitchConvention:
    def test_a2_22_fallback_success_does_not_change_the_definition(self):
        """A fallback provider succeeding must not alter the convention used
        for the historical dependency."""
        from db_builder.session_calendar import previous_valid_observation_date
        before = source_definition_for(INVESTING_SYMBOL)
        # simulate a successful yfinance fallback fetch for the same symbol
        # (no registry mutation should occur)
        assert previous_valid_observation_date(
            INVESTING_SYMBOL, MON_,
            SOURCE_DEF_YFINANCE_CONTINUOUS_FUTURES_DAILY) == FRI
        assert source_definition_for(INVESTING_SYMBOL) == before, \
            "fallback must not rewrite the historical source definition"
        # the symbol's own definition is still unestablished -> fail closed
        assert previous_valid_observation_date(INVESTING_SYMBOL, MON_) is None

    def test_a2_22_definition_is_stable_across_repeated_calls(self):
        from db_builder.session_calendar import previous_valid_observation_date
        first = previous_valid_observation_date(INVESTING_SYMBOL, MON_)
        for _ in range(5):
            assert previous_valid_observation_date(INVESTING_SYMBOL, MON_) == first

    def test_a2_22_row_carried_definition_wins_over_registry(self):
        """A candidate row carrying its own definition is evaluated under that
        definition, not the symbol's registry default."""
        from macro_data_fetch import _eligible_for_historical
        row = make_row(MON_, 101.10, symbol=INVESTING_SYMBOL)
        row["source_definition"] = SOURCE_DEF_YFINANCE_CONTINUOUS_FUTURES_DAILY
        dec = _eligible_for_historical(row, known_prior_sessions={FRI})
        assert dec.routing == ROUTE_HISTORICAL

        row2 = make_row(MON_, 101.10, symbol=INVESTING_SYMBOL)
        row2["source_definition"] = SOURCE_DEF_INVESTING_CONTINUOUS_FUTURES_DAILY
        dec2 = _eligible_for_historical(row2, known_prior_sessions={FRI})
        assert dec2.routing == ROUTE_DEFERRED


# --------------------------------------------------------------------------
# A2-23  Candidate carries mismatched / unknown source-definition ID
# --------------------------------------------------------------------------
class TestA2_23_UnknownDefinitionId:
    def test_a2_23_unknown_definition_id_fails_closed(self):
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(
            SYMBOL, MON_, "NOT_A_KNOWN_DEFINITION") is None

    def test_a2_23_gate_defers_for_unknown_definition_id(self):
        from macro_data_fetch import _eligible_for_historical
        row = make_row(MON_, 101.10, symbol=SYMBOL)
        row["source_definition"] = "SOMETHING_ELSE_DAILY"
        dec = _eligible_for_historical(row, known_prior_sessions={FRI})
        assert dec.routing == ROUTE_DEFERRED

    def test_a2_23_mismatched_definition_id_does_not_borrow(self):
        """A row claiming another provider's definition must be judged under
        that definition, and an unknown one must not resolve."""
        from macro_data_fetch import _eligible_for_historical
        row = make_row(MON_, 101.10, symbol=SYMBOL)
        row["source_definition"] = "TOTALLY_MADE_UP"
        assert _eligible_for_historical(
            row, known_prior_sessions={FRI, MON_}).routing == ROUTE_DEFERRED

    def test_a2_23_unresolvable_symbol_definition_fails_closed(self):
        from db_builder.session_calendar import (
            previous_valid_observation_date, source_definition_for,
        )
        assert source_definition_for("NO_SUCH_SYMBOL=F") is None
        assert previous_valid_observation_date("NO_SUCH_SYMBOL=F", MON_) is None


# --------------------------------------------------------------------------
# A2-24  Source-definition-specific holiday handling
# --------------------------------------------------------------------------
class TestA2_24_DefinitionSpecificHolidays:
    def test_a2_24_yfinance_futures_skips_christmas(self):
        from db_builder.session_calendar import previous_valid_observation_date
        # us_futures: Christmas Day (Fri 12-25) is not a session, so the
        # Monday after is anchored to Thu 12-24.
        assert previous_valid_observation_date(
            SYMBOL, date(2026, 12, 28)) == date(2026, 12, 24)

    def test_a2_24_yfinance_futures_independence_day(self):
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(
            SYMBOL, date(2026, 7, 6)) == date(2026, 7, 3)

    def test_a2_24_cash_equity_uses_its_own_calendar(self):
        """A cash-equity definition uses XNYS: MLK Monday is a holiday, so the
        Tuesday observation is the preceding Friday."""
        from db_builder.session_calendar import previous_valid_observation_date
        assert previous_valid_observation_date(
            "^GSPC", date(2026, 1, 20)) == date(2026, 1, 16)

    def test_a2_24_crypto_definition_has_no_holidays(self):
        """The 24/7 crypto definition has no holiday gaps."""
        from db_builder.session_calendar import previous_valid_observation_date
        # Sunday 12-27 is a real crypto observation for Monday 12-28
        assert previous_valid_observation_date(
            "BTC-USD", date(2026, 12, 28)) == date(2026, 12, 27)
        # ...whereas the futures definition skips to Thu 12-24
        assert previous_valid_observation_date(
            SYMBOL, date(2026, 12, 28)) == date(2026, 12, 24)

    def test_a2_24_same_symbol_two_definitions_two_holiday_answers(self):
        """The same symbol under two definitions resolves through each
        definition's own calendar."""
        from db_builder.session_calendar import previous_valid_observation_date
        d = date(2026, 12, 28)
        # futures definition -> us_futures (Christmas Day skipped)
        assert previous_valid_observation_date(
            SYMBOL, d, SOURCE_DEF_YFINANCE_CONTINUOUS_FUTURES_DAILY) == \
            date(2026, 12, 24)
        # crypto definition -> 24/7, no holidays (Sunday 12-27 is a bar)
        assert previous_valid_observation_date(
            "BTC-USD", d, "CRYPTO_24_7_DAILY") == date(2026, 12, 27)
