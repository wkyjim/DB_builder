"""Recovery fetches a market-session window, not a single date.

The write gate refuses a row whose immediately prior valid session is not
stored. A single-date fetch can therefore never satisfy that gate, and a gap
becomes permanent. Recovery must ask for the target together with its priors
so the gate can chain across the hole.
"""
import datetime as dt

import pytest

D = dt.date


class FakeEngine:
    """Minimal engine stand-in; session_is_stored is monkeypatched."""


@pytest.fixture
def m():
    import macro_data_fetch
    return macro_data_fetch


# -- window construction ----------------------------------------------------

class TestRecoverySessionDates:
    def test_target_plus_four_priors_on_the_symbol_calendar(self, m):
        # XSHG was closed 2026-10-05..07 (National Day), so the window must skip
        # the holiday rather than walking back five calendar days.
        assert m.recovery_session_dates("000001.SS", D(2026, 10, 23)) == [
            D(2026, 10, 23), D(2026, 10, 22), D(2026, 10, 21),
            D(2026, 10, 20), D(2026, 10, 19)]

    def test_window_is_anchored_on_the_target_not_on_today(self, m):
        """An old target must still get its own priors, not a recent window."""
        dates = m.recovery_session_dates("^MOVE", D(2026, 10, 9))
        assert dates[0] == D(2026, 10, 9)
        assert len(dates) == 5
        assert dates[-1] < dates[0]

    def test_fails_open_to_the_target_without_a_calendar(self, m):
        assert m.recovery_session_dates("NO_SUCH_SYMBOL", D(2026, 10, 23)) == \
            [D(2026, 10, 23)]

    def test_sessions_one_is_the_legacy_single_date(self, m):
        assert m.recovery_session_dates("000001.SS", D(2026, 10, 23),
                                        sessions=1) == [D(2026, 10, 23)]


# -- the recovery loop fetches and writes the window -----------------------

class TestRecoveryFetchesTheWindow:
    SYMBOL = "^MOVE"
    TARGET = D(2026, 10, 9)

    def _store(self, monkeypatch, m, *, stored):
        from db_builder.deferred_history import MemoryDeferredStore

        store = MemoryDeferredStore()
        base = dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc)
        store.record_deferral(self.SYMBOL, self.TARGET,
                              "PREVIOUS_SESSION_NOT_CONFIRMED", now=base)
        monkeypatch.setattr(m, "session_is_stored",
                            lambda e, s, d: d in stored)
        return store, base

    def test_fetch_receives_the_whole_window(self, m, monkeypatch):
        store, base = self._store(monkeypatch, m, stored={D(2026, 10, 8)})
        seen = {}

        def fetch(symbol, dates):
            seen["dates"] = list(dates)
            return [{"date": d, "symbol": symbol, "close": 1.0} for d in dates]

        monkeypatch.setattr(m, "upsert_macro", lambda *a, **k: 1)

        m.run_deferred_recovery(
            FakeEngine(), store, fetch_symbol_rows=fetch,
            now_utc=base + dt.timedelta(hours=1, seconds=1),
            provenance_lookup=lambda *a: set())

        assert seen["dates"] == m.recovery_session_dates(self.SYMBOL,
                                                         self.TARGET)

    def test_priors_in_the_window_are_backfilled_ascending(self, m, monkeypatch):
        """A hole inside the window must be written earliest-first, so each
        row's prior is already committed when the gate evaluates it."""
        store, base = self._store(monkeypatch, m, stored={D(2026, 10, 8)})
        order = []

        def fetch(symbol, dates):
            return [{"date": d, "symbol": symbol, "close": 1.0} for d in dates]

        def fake_upsert(engine, rows, **kwargs):
            order.append(rows[0]["date"])
            return 1

        monkeypatch.setattr(m, "upsert_macro", fake_upsert)

        result = m.run_deferred_recovery(
            FakeEngine(), store, fetch_symbol_rows=fetch,
            now_utc=base + dt.timedelta(hours=1, seconds=1),
            provenance_lookup=lambda *a: set())

        assert result["resolved"] == 1
        assert order == sorted(order), "priors must be written before successors"
        # the two missing sessions inside the window were backfilled
        assert D(2026, 10, 5) in order
        assert D(2026, 10, 7) in order
        assert self.TARGET in order

    def test_unwritable_target_still_defers(self, m, monkeypatch):
        """The gate refusing the target must keep the row DEFERRED, not drop it."""
        from db_builder.deferred_history import DEFERRED

        store, base = self._store(monkeypatch, m, stored={D(2026, 10, 8)})

        def fetch(symbol, dates):
            return [{"date": d, "symbol": symbol, "close": 1.0} for d in dates]

        monkeypatch.setattr(m, "upsert_macro", lambda *a, **k: 0)

        result = m.run_deferred_recovery(
            FakeEngine(), store, fetch_symbol_rows=fetch,
            now_utc=base + dt.timedelta(hours=1, seconds=1),
            provenance_lookup=lambda *a: set())

        assert result["resolved"] == 0
        assert result["still_deferred"] == 1
        assert [r.status for r in store.all_rows()] == [DEFERRED]
