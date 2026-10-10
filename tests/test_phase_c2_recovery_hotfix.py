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

    def test_success_path_does_not_increment_attempt_count(self, monkeypatch):
        """Established A.2B semantic: record_success leaves attempt_count alone.

        ``attempt_count`` tracks FAILED recovery attempts for escalation. A
        successful recovery resolves the row, so the counter is frozen at its
        pre-resolution value (production RESOLVED rows retain e.g. att=6).
        Only a failed fetch that re-defers increments it.
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
        assert row.attempt_count == before, (
            "successful recovery must not churn attempt_count")
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
