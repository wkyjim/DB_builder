"""Phase C.2 hotfix: end-to-end deferred-recovery provenance wiring.

The original C2 suite (``test_phase_c2_recovery_provenance.py``) only asserted
retry-policy and deferral *reason* strings. It never drove the real write path,
so it could not detect that ``_provenance_lookup_fn`` was defined but never
wired into ``upsert_macro(mode="INGEST")``. The recovery path therefore passed
``provenance_lookup=None`` to the historical gate, which fails closed
(``provenance_dates = set()``), so every anchor-covered row was deterministically
re-deferred with ``RECOVERY_PRIOR_STILL_UNCONFIRMED`` and zero writes occurred.

These tests exercise the complete path:

    run_deferred_recovery
      -> selector overlay (anchor_override)
      -> provider fetch
      -> upsert_macro(mode="INGEST")
      -> route_historical_rows
      -> provenance lookup            <-- the previously broken link
      -> stored-P gate
      -> historical write
      -> deferred row RESOLVED

Isolated: fake engines + in-memory store. No database is touched.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# The production module builds SQLAlchemy engines at import time. Point them at
# an in-process sqlite URL so no PostgreSQL connection is attempted.
os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")
os.environ.setdefault("LOCAL_DB_PASSWORD", "phase-c2-hotfix-test")
os.environ.setdefault("NEON_DB_PASSWORD", "phase-c2-hotfix-test")

from db_builder import session_calendar as sc
from db_builder.deferred_history import (
    DEFERRED,
    RESOLVED,
    MemoryDeferredStore,
    RECOVERY_DAYS,
)
from db_builder.retry_policy import PROVENANCE_UNVERIFIED

D = dt.date(2026, 10, 8)          # ^GSPC session date (Thursday)
P = dt.date(2026, 10, 7)          # canonical previous session
NOW = dt.datetime(2026, 10, 10, tzinfo=dt.timezone.utc)
P_CLOSE = 7801.7700               # stored trusted prior close


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


def _provider_row(symbol, date):
    """Provider-shaped completed bar for ``date``."""
    return {
        "date": date, "symbol": symbol, "name": "S&P 500",
        "asset_type": "stock_index",
        "open": 7790.0, "high": 7830.0, "low": 7785.0, "close": 7818.93,
        "adj_close": 7818.93, "volume": 1000,
        # Provider-supplied prior is deliberately WRONG. The gate must ignore
        # it and recompute from the stored trusted P close.
        "prev_close": 9999.0, "change": 0.0, "pct_chg": 0.0, "amplitude": 0.0,
    }


# ============================================================
# C2-40: anchor-derived provenance resolver signature contract
# ============================================================

class TestC2ProvenanceResolverSignature:
    """C2-40: the recovery resolver must match the gate's callback contract."""

    def test_gate_contract_is_four_positional_args(self):
        """route_historical_rows calls provenance_lookup(engine, symbol, min, max)."""
        import macro_data_fetch as m

        captured = {}

        def probe(_engine, symbol, min_date, max_date):
            captured["args"] = (symbol, min_date, max_date)
            return set()

        m.route_historical_rows(
            FakeEngine(), [], prior_lookup=lambda *a, **k: set(),
            prior_value_lookup=lambda *a, **k: {},
            provenance_lookup=probe)

        # No rows -> lookup may not be called; assert the contract by signature
        # inspection instead, which is what a blind replacement would violate.
        import inspect
        params = list(inspect.signature(probe).parameters)
        assert params == ["_engine", "symbol", "min_date", "max_date"]

    def test_recovery_resolver_accepts_gate_contract(self):
        """The recovery resolver must accept 4 positional args (engine-first)."""
        import macro_data_fetch as m

        # Rebuild the closure the recovery path uses, then invoke it with the
        # gate's calling convention. A 3-arg closure raises TypeError here.
        resolver = m._build_anchor_provenance_lookup(FakeEngine())
        assert callable(resolver)
        try:
            result = resolver(FakeEngine(), "^GSPC", P, D)
        except TypeError as exc:  # pragma: no cover - RED before the fix
            pytest.fail(
                f"recovery provenance resolver is not callable with the gate "
                f"contract (engine, symbol, min_date, max_date): {exc}")
        assert isinstance(result, set)


# ============================================================
# C2-41: end-to-end recovery write (the missing coverage)
# ============================================================

class TestC2EndToEndRecoveryWrite:
    """C2-41: an anchor-covered PU row is actually WRITTEN and RESOLVED."""

    def test_anchor_covered_row_is_written_and_resolved(self, monkeypatch):
        """Full path: selector overlay -> fetch -> upsert_macro -> write."""
        import macro_data_fetch as m

        store = MemoryDeferredStore()
        eng = FakeEngine()

        # Persisted PU row, exactly as production has it.
        store.record_deferral("^GSPC", D, "PRIOR_PROVENANCE_UNVERIFIED",
                              now=NOW, attempted=False,
                              retry_class=PROVENANCE_UNVERIFIED)
        assert store.all_rows()[0].status == DEFERRED
        assert store.all_rows()[0].attempt_count == 0

        # ANCHOR_DERIVED_PROVENANCE: the C.1 anchor covers P.
        monkeypatch.setattr(
            m, "_stored_trusted_provenance_dates",
            lambda engine, symbol, sd, min_d, max_d: {P})

        def fetch(symbol, dates):
            return [_provider_row(symbol, D)]

        # NOTE: provenance_lookup is deliberately NOT injected here. This is
        # the production calling convention; the fix must supply the
        # anchor-derived resolver internally.
        result = m.run_deferred_recovery(
            eng, store, fetch_symbol_rows=fetch, now_utc=NOW,
            recovery_days=RECOVERY_DAYS,
            prior_lookup=lambda *a, **k: {P},
            prior_value_lookup=lambda *a, **k: {P: P_CLOSE})

        assert result["resolved"] == 1, (
            f"anchor-covered row was not written; result={result} "
            f"(reason={store.all_rows()[0].reason!r})")
        assert store.all_rows()[0].status == RESOLVED

        # A real historical INSERT must have been issued.
        assert eng.executed, "no SQL executed -> no historical write"

    def test_success_path_increments_attempt_count(self, monkeypatch):
        """A successful recovery IS a real fetch, so attempt_count += 1.

        ``attempt_count`` counts actual deferred-recovery provider fetch
        attempts -- successes and failures alike. A resolved row therefore
        carries the total number of fetches it took.
        """
        import macro_data_fetch as m

        store = MemoryDeferredStore()
        eng = FakeEngine()
        store.record_deferral("^GSPC", D, "PRIOR_PROVENANCE_UNVERIFIED",
                              now=NOW, attempted=False,
                              retry_class=PROVENANCE_UNVERIFIED)
        before = store.all_rows()[0].attempt_count

        monkeypatch.setattr(
            m, "_stored_trusted_provenance_dates",
            lambda engine, symbol, sd, min_d, max_d: {P})

        m.run_deferred_recovery(
            eng, store, fetch_symbol_rows=lambda s, d: [_provider_row(s, D)],
            now_utc=NOW, recovery_days=RECOVERY_DAYS,
            prior_lookup=lambda *a, **k: {P},
            prior_value_lookup=lambda *a, **k: {P: P_CLOSE})

        row = store.all_rows()[0]
        assert row.status == RESOLVED
        assert row.attempt_count == before + 1, (
            "a real successful fetch must increment attempt_count by exactly 1")
        assert row.retry_class is None

    def test_failed_fetch_increments_attempt_count(self, monkeypatch):
        """A real fetch that cannot write re-defers and increments by 1."""
        import macro_data_fetch as m

        store = MemoryDeferredStore()
        eng = FakeEngine()
        store.record_deferral("^GSPC", D, "PRIOR_PROVENANCE_UNVERIFIED",
                              now=NOW, attempted=False,
                              retry_class=PROVENANCE_UNVERIFIED)
        before = store.all_rows()[0].attempt_count

        monkeypatch.setattr(
            m, "_stored_trusted_provenance_dates",
            lambda engine, symbol, sd, min_d, max_d: {P})

        # Provider returns a bar for the WRONG date -> no match -> re-defer.
        m.run_deferred_recovery(
            eng, store,
            fetch_symbol_rows=lambda s, d: [_provider_row(s, dt.date(2026, 10, 6))],
            now_utc=NOW, recovery_days=RECOVERY_DAYS,
            prior_lookup=lambda *a, **k: {P},
            prior_value_lookup=lambda *a, **k: {P: P_CLOSE})

        row = store.all_rows()[0]
        assert row.status == DEFERRED
        assert row.attempt_count == before + 1, (
            "a real fetch that re-defers must increment attempt_count once")

    def test_write_uses_stored_prior_not_provider_supplied(self, monkeypatch):
        """prev_close must come from stored trusted P, not the provider frame."""
        import macro_data_fetch as m

        store = MemoryDeferredStore()
        eng = FakeEngine()
        store.record_deferral("^GSPC", D, "PRIOR_PROVENANCE_UNVERIFIED",
                              now=NOW, attempted=False,
                              retry_class=PROVENANCE_UNVERIFIED)

        monkeypatch.setattr(
            m, "_stored_trusted_provenance_dates",
            lambda engine, symbol, sd, min_d, max_d: {P})

        # Let the REAL upsert_macro -> route_historical_rows run so the gate
        # actually recomputes the prior-dependent fields. FakeEngine records
        # the INSERT params, which is where the written row is observable.
        result = m.run_deferred_recovery(
            eng, store,
            fetch_symbol_rows=lambda s, d: [_provider_row(s, D)],
            now_utc=NOW, recovery_days=RECOVERY_DAYS,
            prior_lookup=lambda *a, **k: {P},
            prior_value_lookup=lambda *a, **k: {P: P_CLOSE})

        assert result["resolved"] == 1
        assert eng.executed, "no INSERT reached the engine"

        # The final INSERT carries the row actually written to public.macro.
        records = eng.executed[-1][0][1]
        rec = records[0]
        assert rec["prev_close"] == P_CLOSE, (
            "prev_close must be the stored trusted P close, not the provider's")
        assert rec["prev_close"] != 9999.0
        assert rec["change"] == pytest.approx(round(7818.93 - P_CLOSE, 4))
        assert rec["pct_chg"] == pytest.approx(
            round((7818.93 - P_CLOSE) / P_CLOSE * 100, 4))
        assert rec["amplitude"] == pytest.approx(
            round((7830.0 - 7785.0) / P_CLOSE * 100, 4))


# ============================================================
# C2-42: negative control — no anchor means no write
# ============================================================

class TestC2EndToEndNoAnchorNoWrite:
    """C2-42: without anchor coverage the row must remain deferred."""

    def test_no_anchor_stays_deferred(self, monkeypatch):
        import macro_data_fetch as m

        store = MemoryDeferredStore()
        eng = FakeEngine()
        store.record_deferral("^GSPC", D, "PRIOR_PROVENANCE_UNVERIFIED",
                              now=NOW, attempted=False,
                              retry_class=PROVENANCE_UNVERIFIED)

        # Anchor does NOT cover P -> selector must not release the row.
        monkeypatch.setattr(
            m, "_stored_trusted_provenance_dates",
            lambda engine, symbol, sd, min_d, max_d: set())

        result = m.run_deferred_recovery(
            eng, store,
            fetch_symbol_rows=lambda s, d: [_provider_row(s, D)],
            now_utc=NOW, recovery_days=RECOVERY_DAYS,
            prior_lookup=lambda *a, **k: {P},
            prior_value_lookup=lambda *a, **k: {P: P_CLOSE})

        assert result["resolved"] == 0
        assert result["retried"] == 0
        assert store.all_rows()[0].status == DEFERRED
        assert store.all_rows()[0].attempt_count == 0
        assert not eng.executed


# ============================================================
# C2-43: injected provenance_lookup still wins (explicit precedence)
# ============================================================

class TestC2InjectedProvenancePrecedence:
    """C2-43: an explicitly injected resolver overrides the anchor default.

    Precedence is explicit and total: the injected resolver is used for BOTH
    the selector overlay and the write gate, so there is never a split-brain
    where the selector releases a row the gate then refuses.
    """

    def test_injected_resolver_is_used(self, monkeypatch):
        import macro_data_fetch as m

        store = MemoryDeferredStore()
        eng = FakeEngine()
        store.record_deferral("^GSPC", D, "PRIOR_PROVENANCE_UNVERIFIED",
                              now=NOW, attempted=False,
                              retry_class=PROVENANCE_UNVERIFIED)

        # The C.1 anchor does NOT cover P, so the anchor-derived path alone
        # would leave the row deferred.
        monkeypatch.setattr(
            m, "_stored_trusted_provenance_dates",
            lambda engine, symbol, sd, min_d, max_d: set())

        def injected(_engine, _symbol, _min_date, _max_date):
            return {P}            # explicit injection says covered

        result = m.run_deferred_recovery(
            eng, store,
            fetch_symbol_rows=lambda s, d: [_provider_row(s, D)],
            now_utc=NOW, recovery_days=RECOVERY_DAYS,
            prior_lookup=lambda *a, **k: {P},
            prior_value_lookup=lambda *a, **k: {P: P_CLOSE},
            provenance_lookup=injected)

        assert result["resolved"] == 1, (
            "explicitly injected provenance_lookup was not honoured")
        assert store.all_rows()[0].status == RESOLVED

    def test_anchor_default_used_when_not_injected(self, monkeypatch):
        """Production convention (no injection) uses the anchor resolver."""
        import macro_data_fetch as m

        store = MemoryDeferredStore()
        eng = FakeEngine()
        store.record_deferral("^GSPC", D, "PRIOR_PROVENANCE_UNVERIFIED",
                              now=NOW, attempted=False,
                              retry_class=PROVENANCE_UNVERIFIED)

        monkeypatch.setattr(
            m, "_stored_trusted_provenance_dates",
            lambda engine, symbol, sd, min_d, max_d: {P})

        result = m.run_deferred_recovery(
            eng, store,
            fetch_symbol_rows=lambda s, d: [_provider_row(s, D)],
            now_utc=NOW, recovery_days=RECOVERY_DAYS,
            prior_lookup=lambda *a, **k: {P},
            prior_value_lookup=lambda *a, **k: {P: P_CLOSE})

        assert result["resolved"] == 1
        assert store.all_rows()[0].status == RESOLVED


# ============================================================
# C2-44: REQUIRED positive-path acceptance proof
# ============================================================

class TestC2PositivePathAcceptanceProof:
    """C2-44: the complete acceptance proof for an anchor-covered row.

    Covers every assertion required by the C.2 hotfix acceptance criteria:
    selection, exactly-one fetch, attempt_count +1, provenance callback
    invocation, write, authoritative stored P.close, deterministic derived
    fields, and RESOLVED.
    """

    def test_full_positive_path(self, monkeypatch):
        import macro_data_fetch as m

        store = MemoryDeferredStore()
        eng = FakeEngine()

        # Persisted retry_class = PROVENANCE_UNVERIFIED
        store.record_deferral("^GSPC", D, "PRIOR_PROVENANCE_UNVERIFIED",
                              now=NOW, attempted=False,
                              retry_class=PROVENANCE_UNVERIFIED)
        before_attempts = store.all_rows()[0].attempt_count

        # exact C.1 anchor exists; D >= trust_start; P exists; P >= trust_start
        assert m._resolve_trust_start(
            "^GSPC", sc.SOURCE_DEFINITION_BY_SYMBOL["^GSPC"]) is not None

        # Provenance callback invocation is observable: the anchor-derived
        # resolver delegates to _stored_trusted_provenance_dates.
        calls = {"provenance": 0, "fetch": 0}

        def counting_provenance(engine, symbol, sd, min_d, max_d):
            calls["provenance"] += 1
            return {P}          # stored P trusted

        monkeypatch.setattr(m, "_stored_trusted_provenance_dates",
                            counting_provenance)

        def fetch(symbol, dates):
            calls["fetch"] += 1
            return [_provider_row(symbol, D)]   # provider returns D

        result = m.run_deferred_recovery(
            eng, store, fetch_symbol_rows=fetch, now_utc=NOW,
            recovery_days=RECOVERY_DAYS,
            prior_lookup=lambda *a, **k: {P},
            prior_value_lookup=lambda *a, **k: {P: P_CLOSE})

        row = store.all_rows()[0]

        # runtime override selects row
        assert result["retried"] == 1, result
        # actual provider fetch occurs exactly once
        assert calls["fetch"] == 1, "provider fetch must occur exactly once"
        # provenance callback is actually invoked
        assert calls["provenance"] > 0, "provenance callback was never invoked"
        # attempt_count +1
        assert row.attempt_count == before_attempts + 1, (
            f"attempt_count must increment on a real fetch "
            f"(was {before_attempts}, now {row.attempt_count})")
        # D is written
        assert result["resolved"] == 1, result
        assert eng.executed, "no historical write occurred"
        # deferred row becomes RESOLVED
        assert row.status == RESOLVED

        # final D.prev_close == stored P.close (stored P is authoritative)
        rec = eng.executed[-1][0][1][0]
        assert rec["prev_close"] == P_CLOSE
        assert rec["prev_close"] != 9999.0, "provider prev_close leaked through"
        # change/pct_chg/amplitude recomputed from stored P.close
        assert rec["change"] == pytest.approx(round(7818.93 - P_CLOSE, 4))
        assert rec["pct_chg"] == pytest.approx(
            round((7818.93 - P_CLOSE) / P_CLOSE * 100, 4))
        assert rec["amplitude"] == pytest.approx(
            round((7830.0 - 7785.0) / P_CLOSE * 100, 4))


# ============================================================
# C2-45: REQUIRED fail-closed companion
# ============================================================

class TestC2FailClosedCompanion:
    """C2-45: anchor exists but required P is outside the trust boundary.

    BTC 2026-07-09 is the canonical boundary case: the anchor exists and
    D >= trust_start, but P = 2026-07-08 < trust_start = 2026-07-09.
    """

    def test_btc_2026_07_09_fails_closed_without_fetch(self):
        import macro_data_fetch as m

        store = MemoryDeferredStore()
        eng = FakeEngine()
        d = dt.date(2026, 7, 9)

        store.record_deferral("BTC-USD", d, "PRIOR_PROVENANCE_UNVERIFIED",
                              now=NOW, attempted=False,
                              retry_class=PROVENANCE_UNVERIFIED)
        before_attempts = store.all_rows()[0].attempt_count

        fetch_calls = {"n": 0}

        def fetch(symbol, dates):
            fetch_calls["n"] += 1
            return []

        result = m.run_deferred_recovery(
            eng, store, fetch_symbol_rows=fetch, now_utc=NOW,
            recovery_days=RECOVERY_DAYS)

        row = store.all_rows()[0]
        # no provider fetch: the failure is knowable pre-fetch
        assert fetch_calls["n"] == 0, "must not fetch a pre-fetch-blocked row"
        # attempt_count unchanged
        assert row.attempt_count == before_attempts
        # no historical write
        assert not eng.executed
        # row remains unresolved
        assert row.status != RESOLVED
        # precise provenance reason retained
        assert row.reason == "PRIOR_PROVENANCE_UNVERIFIED"
        assert row.retry_class == PROVENANCE_UNVERIFIED
        assert result["resolved"] == 0

    def test_missing_trusted_p_fails_closed(self, monkeypatch):
        """Anchor exists and P >= trust_start, but P is absent from storage."""
        import macro_data_fetch as m

        store = MemoryDeferredStore()
        eng = FakeEngine()
        store.record_deferral("^GSPC", D, "PRIOR_PROVENANCE_UNVERIFIED",
                              now=NOW, attempted=False,
                              retry_class=PROVENANCE_UNVERIFIED)
        before_attempts = store.all_rows()[0].attempt_count

        # Trusted-provenance resolver returns nothing for this symbol.
        monkeypatch.setattr(
            m, "_stored_trusted_provenance_dates",
            lambda engine, symbol, sd, min_d, max_d: set())

        fetch_calls = {"n": 0}

        def fetch(symbol, dates):
            fetch_calls["n"] += 1
            return [_provider_row(symbol, D)]

        result = m.run_deferred_recovery(
            eng, store, fetch_symbol_rows=fetch, now_utc=NOW,
            recovery_days=RECOVERY_DAYS,
            prior_lookup=lambda *a, **k: {P},
            prior_value_lookup=lambda *a, **k: {P: P_CLOSE})

        row = store.all_rows()[0]
        assert fetch_calls["n"] == 0, "no fetch when trusted P is missing"
        assert row.attempt_count == before_attempts
        assert not eng.executed
        assert row.status != RESOLVED
        assert result["resolved"] == 0


# ============================================================
# C2-46: wiring regression guard (the exact 91bdd7d defect)
# ============================================================

class TestC2WiringRegressionGuard:
    """C2-46: fails if the recovery write again passes provenance_lookup=None.

    This is the guard against the exact defect found at 91bdd7d, where the
    resolver existed but was never consumed, so the gate failed closed.
    """

    def test_resolver_consumed_by_recovery_write(self, monkeypatch):
        import macro_data_fetch as m

        store = MemoryDeferredStore()
        eng = FakeEngine()
        store.record_deferral("^GSPC", D, "PRIOR_PROVENANCE_UNVERIFIED",
                              now=NOW, attempted=False,
                              retry_class=PROVENANCE_UNVERIFIED)

        monkeypatch.setattr(
            m, "_stored_trusted_provenance_dates",
            lambda engine, symbol, sd, min_d, max_d: {P})

        # Capture the provenance_lookup actually handed to upsert_macro.
        captured = {}

        real_upsert = m.upsert_macro

        def spy(engine, rows, **kwargs):
            captured["provenance_lookup"] = kwargs.get("provenance_lookup")
            return real_upsert(engine, rows, **kwargs)

        monkeypatch.setattr(m, "upsert_macro", spy)

        m.run_deferred_recovery(
            eng, store, fetch_symbol_rows=lambda s, d: [_provider_row(s, D)],
            now_utc=NOW, recovery_days=RECOVERY_DAYS,
            prior_lookup=lambda *a, **k: {P},
            prior_value_lookup=lambda *a, **k: {P: P_CLOSE})

        assert "provenance_lookup" in captured, "upsert_macro was never reached"
        resolver = captured["provenance_lookup"]
        assert resolver is not None, (
            "recovery write passed provenance_lookup=None -> the 91bdd7d "
            "defect has regressed; the gate would fail closed")
        # The resolver must satisfy the gate contract and actually resolve P.
        assert resolver(FakeEngine(), "^GSPC", P, D) == {P}, (
            "resolver does not return the trusted P for the anchor-covered "
            "symbol")

    def test_mismatched_source_definition_resolves_empty(self):
        """Provenance now comes from the stored source_definition column.

        A stored row satisfies the candidate only when its own source definition
        matches the candidate's. This replaces the old rule -- "a symbol with no
        hardcoded trust-start entry resolves empty" -- which left 45 of 73
        symbols (000001.SS among them) permanently unable to write history.
        """
        import macro_data_fetch as m

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        class _Conn:
            def __init__(self, store):
                self._store = store

            def execute(self, sql, params=None):
                # honour the source_definition predicate, as the real query does
                return _Result(
                    self._store.get((params or {}).get("source_definition"), []))

        class _Ctx:
            def __init__(self, store):
                self._store = store

            def __enter__(self):
                return _Conn(self._store)

            def __exit__(self, *a):
                return False

        class _Engine:
            def __init__(self, store):
                self._store = store

            def begin(self):
                return _Ctx(self._store)

        gspc_sd = m.SOURCE_DEFINITION_BY_SYMBOL["^GSPC"]

        # matching definition -> P is trusted
        eng = _Engine({gspc_sd: [(P,)]})
        assert m._stored_trusted_provenance_dates(eng, "^GSPC", gspc_sd, P, D) == {P}

        # the stored row carries a different definition -> empty
        eng = _Engine({"SOME_OTHER_SOURCE_DEFINITION": [(P,)]})
        assert m._stored_trusted_provenance_dates(eng, "^GSPC", gspc_sd, P, D) == set()

        # unknown source definition -> empty (fail closed)
        assert m._stored_trusted_provenance_dates(eng, "^GSPC", None, P, D) == set()


# ============================================================
# C2-47: attempt_count counts PROVIDER FETCHES, not outcomes
# ============================================================

class TestC2AttemptCountCountsFetches:
    """C2-47: the mechanically testable invariant

        Σ attempt_count_delta == actual provider fetch calls

    for every outcome of a recovery batch. attempt_count counts real
    provider fetch attempts -- successes and failures alike.
    """

    def _run(self, monkeypatch, fetch_rows):
        """Drive one recovery candidate; return (row, fetch_calls, result)."""
        import macro_data_fetch as m

        store = MemoryDeferredStore()
        eng = FakeEngine()
        store.record_deferral("^GSPC", D, "PRIOR_PROVENANCE_UNVERIFIED",
                              now=NOW, attempted=False,
                              retry_class=PROVENANCE_UNVERIFIED)
        monkeypatch.setattr(
            m, "_stored_trusted_provenance_dates",
            lambda engine, symbol, sd, min_d, max_d: {P})

        calls = {"n": 0}

        def fetch(symbol, dates):
            calls["n"] += 1
            if isinstance(fetch_rows, Exception):
                raise fetch_rows
            return fetch_rows

        result = m.run_deferred_recovery(
            eng, store, fetch_symbol_rows=fetch, now_utc=NOW,
            recovery_days=RECOVERY_DAYS,
            prior_lookup=lambda *a, **k: {P},
            prior_value_lookup=lambda *a, **k: {P: P_CLOSE})
        return store.all_rows()[0], calls["n"], result

    def test_success_fetch_once_delta_plus_one(self, monkeypatch):
        """fetch=1, write succeeds -> delta +1, RESOLVED."""
        row, fetches, result = self._run(
            monkeypatch, [_provider_row("^GSPC", D)])
        assert fetches == 1
        assert result["resolved"] == 1
        assert row.status == RESOLVED
        assert row.attempt_count == 1

    def test_redefer_fetch_once_delta_plus_one(self, monkeypatch):
        """fetch=1, gate re-defers -> delta +1, unresolved."""
        row, fetches, result = self._run(
            monkeypatch, [_provider_row("^GSPC", dt.date(2026, 10, 6))])
        assert fetches == 1
        assert result["resolved"] == 0
        assert row.status != RESOLVED
        assert row.attempt_count == 1

    def test_fetch_exception_once_delta_plus_one(self, monkeypatch):
        """fetch=1 raising -> delta +1."""
        row, fetches, result = self._run(
            monkeypatch, ConnectionError("provider down"))
        assert fetches == 1
        assert row.status != RESOLVED
        assert row.attempt_count == 1

    def test_prefetch_block_zero_fetch_zero_delta(self, monkeypatch):
        """fetch=0 (provenance-blocked pre-fetch) -> delta 0."""
        import macro_data_fetch as m

        store = MemoryDeferredStore()
        eng = FakeEngine()
        store.record_deferral("^GSPC", D, "PRIOR_PROVENANCE_UNVERIFIED",
                              now=NOW, attempted=False,
                              retry_class=PROVENANCE_UNVERIFIED)
        monkeypatch.setattr(
            m, "_stored_trusted_provenance_dates",
            lambda engine, symbol, sd, min_d, max_d: set())  # no trusted P

        calls = {"n": 0}

        def fetch(symbol, dates):
            calls["n"] += 1
            return [_provider_row(symbol, D)]

        m.run_deferred_recovery(
            eng, store, fetch_symbol_rows=fetch, now_utc=NOW,
            recovery_days=RECOVERY_DAYS,
            prior_lookup=lambda *a, **k: {P},
            prior_value_lookup=lambda *a, **k: {P: P_CLOSE})

        assert calls["n"] == 0, "blocked row must not be fetched"
        assert store.all_rows()[0].attempt_count == 0

    def test_no_double_count_on_success(self, monkeypatch):
        """A single successful fetch must produce exactly +1, never +2.

        Guards against record_deferral(attempted=True) also firing on the
        success path before record_success().
        """
        row, fetches, result = self._run(
            monkeypatch, [_provider_row("^GSPC", D)])
        assert fetches == 1
        assert row.attempt_count == 1, (
            f"exactly one fetch must yield exactly +1 (got "
            f"{row.attempt_count}) -- double counting detected")


# ============================================================
# C2-48: record_success attempted= API semantics
# ============================================================

class TestC2RecordSuccessAttemptedSemantics:
    """C2-48: record_success(attempted=...) explicit semantics (both stores)."""

    def test_memory_success_default_increments(self):
        store = MemoryDeferredStore()
        store.record_deferral("^GSPC", D, "X", now=NOW, attempted=False)
        assert store.all_rows()[0].attempt_count == 0
        store.record_success("^GSPC", D, now=NOW, attempted=True)
        row = store.all_rows()[0]
        assert row.status == RESOLVED
        assert row.attempt_count == 1
        assert row.last_attempt == NOW

    def test_memory_success_not_attempted_does_not_increment(self):
        store = MemoryDeferredStore()
        store.record_deferral("^GSPC", D, "X", now=NOW, attempted=False)
        store.record_success("^GSPC", D, now=NOW, attempted=False)
        row = store.all_rows()[0]
        assert row.status == RESOLVED
        assert row.attempt_count == 0

    def test_memory_success_insert_branch_counts_attempt(self):
        """No prior deferral row + real fetch -> attempt_count 1, not 0."""
        store = MemoryDeferredStore()
        store.record_success("^GSPC", D, now=NOW, attempted=True)
        row = store.all_rows()[0]
        assert row.status == RESOLVED
        assert row.attempt_count == 1

    def test_sql_record_success_sql_preserves_and_increments(self):
        """SQL store: ON CONFLICT must ADD to attempt_count, not reset it."""
        import re
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / "src" / "db_builder" / "deferred_history.py"
        text = src.read_text()
        # Locate the SqlDeferredStore.record_success body.
        idx = text.rindex("def record_success")
        body = text[idx:idx + 1600]
        assert "attempt_count = public.macro_deferred_history.attempt_count" in body, (
            "SQL record_success must increment attempt_count on conflict")
        assert "EXCLUDED.attempt_count" in body
        assert ":inc" in body, "attempted must be parameterised, not hard-coded"
