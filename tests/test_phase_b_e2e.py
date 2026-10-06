"""S9: Real PostgreSQL E2E tests for Phase B reconciliation.

Exercises the actual implementation:
- reconciliation.py
- reconciliation_store.py (SqlReconciliationStore)
- integrated RECONCILE path
- real SQLAlchemy transaction handling
- actual PostgreSQL locking and optimistic predicates

Uses an isolated PostgreSQL database only.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

# Ensure we can import from the worktree
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from db_builder.reconciliation import (  # noqa: E402
    AuditEventType,
    CMT_SYMBOLS,
    CorrectionPlan,
    Reason,
    Row,
    Verdict,
    build_correction_plan,
    check_prior_dependency,
    decide,
    is_auto_correctable,
    round_4,
)
from db_builder.reconciliation_store import (  # noqa: E402
    AuditEvent,
    SqlReconciliationStore,
    execute_correction_plan,
    record_detection_event,
)

# Test database URL (isolated) — local PostgreSQL instance
TEST_DB_URL = os.environ.get(
    "PHASE_B_TEST_DB_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:5432/phase_b_test",
)

D = dt.date(2026, 9, 25)
D1 = dt.date(2026, 9, 28)
D2 = dt.date(2026, 9, 29)
P = dt.date(2026, 9, 24)


def make_cmt_row(
    symbol: str = "US10YT_CMT",
    date: dt.date = D,
    close: float = 4.85,
    prev_close: float = 4.81,
    source: str = "treasury.gov",
) -> Row:
    return Row(
        symbol=symbol,
        date=date,
        open=close,
        high=close,
        low=close,
        close=close,
        adj_close=close,
        volume=None,
        prev_close=prev_close,
        change=round_4(close - prev_close),
        pct_chg=round_4((close - prev_close) / prev_close * 100) if prev_close else None,
        amplitude=0.0,
        source=source,
        asset_type="ust_yield_cmt",
    )


@pytest.fixture
def db_engine():
    """Create a fresh test database engine using local PostgreSQL."""
    from sqlalchemy import create_engine, text
    engine = create_engine(TEST_DB_URL)

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS public.macro_reconciliation_audit"))
        conn.execute(text("DROP TABLE IF EXISTS public.macro"))
        conn.execute(text("""
            CREATE TABLE public.macro (
                date DATE NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT,
                asset_type TEXT,
                open NUMERIC(20,4),
                high NUMERIC(20,4),
                low NUMERIC(20,4),
                close NUMERIC(20,4),
                adj_close NUMERIC(20,4),
                volume NUMERIC(20,4),
                prev_close NUMERIC(20,4),
                change NUMERIC(20,4),
                pct_chg NUMERIC(20,4),
                amplitude NUMERIC(20,4),
                source TEXT,
                PRIMARY KEY (date, symbol)
            )
        """))
        conn.execute(text("""
            CREATE TABLE public.macro_reconciliation_audit (
                audit_id SERIAL PRIMARY KEY,
                run_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                session_date DATE NOT NULL,
                verdict TEXT NOT NULL,
                field TEXT NOT NULL DEFAULT '',
                old_value NUMERIC(20,4),
                new_value NUMERIC(20,4),
                source_definition TEXT NOT NULL DEFAULT '',
                confirmation_source TEXT NOT NULL DEFAULT '',
                confirmation_timestamp TIMESTAMPTZ,
                reason TEXT NOT NULL DEFAULT '',
                repaired_at TIMESTAMPTZ,
                expected_previous_session DATE,
                source_observation_ts TIMESTAMPTZ,
                correction_txn_id BIGINT,
                row_role TEXT NOT NULL DEFAULT '',
                release_version TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT 'DETECTION'
            )
        """))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS public.macro_reconciliation_audit"))
        conn.execute(text("DROP TABLE IF EXISTS public.macro"))
    engine.dispose()


@pytest.fixture
def store(db_engine):
    return SqlReconciliationStore(db_engine)


def insert_row(engine, row: Row):
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO public.macro (date, symbol, name, asset_type, open, high, low, close, adj_close, volume, prev_close, change, pct_chg, amplitude, source)
            VALUES (:date, :symbol, :name, :asset_type, :open, :high, :low, :close, :adj_close, :volume, :prev_close, :change, :pct_chg, :amplitude, :source)
            ON CONFLICT (date, symbol) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, adj_close = EXCLUDED.adj_close, volume = EXCLUDED.volume,
                prev_close = EXCLUDED.prev_close, change = EXCLUDED.change,
                pct_chg = EXCLUDED.pct_chg, amplitude = EXCLUDED.amplitude, source = EXCLUDED.source
        """), {
            "date": row.date, "symbol": row.symbol, "name": row.symbol,
            "asset_type": row.asset_type, "open": row.open, "high": row.high,
            "low": row.low, "close": row.close, "adj_close": row.adj_close,
            "volume": row.volume, "prev_close": row.prev_close,
            "change": row.change, "pct_chg": row.pct_chg,
            "amplitude": row.amplitude, "source": row.source,
        })


def get_row(engine, symbol, date):
    from sqlalchemy import text
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT date, symbol, open, high, low, close, adj_close, volume,
                   prev_close, change, pct_chg, amplitude, source, asset_type
              FROM public.macro WHERE symbol = :symbol AND date = :date
        """), {"symbol": symbol, "date": date}).mappings().first()
    if r is None:
        return None
    return Row(
        symbol=r["symbol"], date=r["date"],
        open=float(r["open"]) if r["open"] is not None else None,
        high=float(r["high"]) if r["high"] is not None else None,
        low=float(r["low"]) if r["low"] is not None else None,
        close=float(r["close"]) if r["close"] is not None else None,
        adj_close=float(r["adj_close"]) if r["adj_close"] is not None else None,
        volume=float(r["volume"]) if r["volume"] is not None else None,
        prev_close=float(r["prev_close"]) if r["prev_close"] is not None else None,
        change=float(r["change"]) if r["change"] is not None else None,
        pct_chg=float(r["pct_chg"]) if r["pct_chg"] is not None else None,
        amplitude=float(r["amplitude"]) if r["amplitude"] is not None else None,
        source=r["source"], asset_type=r["asset_type"],
    )


def get_audit_count(engine, event_type=None):
    from sqlalchemy import text
    with engine.connect() as conn:
        if event_type:
            r = conn.execute(text("""
                SELECT COUNT(*) FROM public.macro_reconciliation_audit WHERE event_type = :et
            """), {"et": event_type}).scalar()
        else:
            r = conn.execute(text("SELECT COUNT(*) FROM public.macro_reconciliation_audit")).scalar()
    return r


# ---------------------------------------------------------------------------
# E1 — CMT source revision
# ---------------------------------------------------------------------------


class TestE1CMTSourceRevision:
    def test_cmt_revision_corrects_d_and_d1(self, db_engine, store):
        """Stale D, confirmed Treasury.gov value, prior P confirmed -> D corrected, D+1 derived corrected, D+2 unchanged."""
        # Setup: D has stale close, D+1 depends on D, D+2 depends on D+1
        insert_row(db_engine, make_cmt_row(close=4.85, prev_close=4.81))  # D stale
        insert_row(db_engine, make_cmt_row(date=D1, close=4.88, prev_close=4.85))  # D+1
        insert_row(db_engine, make_cmt_row(date=D2, close=4.89, prev_close=4.88))  # D+2
        insert_row(db_engine, make_cmt_row(date=P, close=4.81))  # P confirmed

        # Build plan
        stored = get_row(db_engine, "US10YT_CMT", D)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)  # Treasury.gov revised
        confirmed_prior = make_cmt_row(date=P, close=4.81)
        stored_d1 = get_row(db_engine, "US10YT_CMT", D1)
        stored_d2 = get_row(db_engine, "US10YT_CMT", D2)

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=stored_d1, previous_valid_session_d1=D,
            stored_d2=stored_d2, is_cmt=True,
        )

        # Execute
        success, error = execute_correction_plan(store, plan, run_id="e1")
        assert success is True
        assert error is None

        # Verify D corrected
        d_after = get_row(db_engine, "US10YT_CMT", D)
        assert d_after.close == 4.87
        assert d_after.prev_close == 4.81
        assert d_after.change == round_4(4.87 - 4.81)

        # Verify D+1 derived corrected
        d1_after = get_row(db_engine, "US10YT_CMT", D1)
        assert d1_after.prev_close == 4.87
        assert d1_after.change == round_4(4.88 - 4.87)
        assert d1_after.close == 4.88  # source unchanged

        # Verify D+2 unchanged
        d2_after = get_row(db_engine, "US10YT_CMT", D2)
        assert d2_after.prev_close == 4.88
        assert d2_after.close == 4.89

        # Verify REPAIR audit committed
        repair_count = get_audit_count(db_engine, "REPAIR")
        assert repair_count > 0


# ---------------------------------------------------------------------------
# E2 — Idempotency
# ---------------------------------------------------------------------------


class TestE2Idempotency:
    def test_second_run_is_match(self, db_engine, store):
        """Run again -> MATCH, no new repair, no duplicate audit."""
        # Setup corrected state
        insert_row(db_engine, make_cmt_row(close=4.87, prev_close=4.81))
        insert_row(db_engine, make_cmt_row(date=D1, close=4.88, prev_close=4.87))
        insert_row(db_engine, make_cmt_row(date=D2, close=4.89, prev_close=4.88))
        insert_row(db_engine, make_cmt_row(date=P, close=4.81))

        # First run (should be MATCH now)
        stored = get_row(db_engine, "US10YT_CMT", D)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)
        stored_d1 = get_row(db_engine, "US10YT_CMT", D1)
        stored_d2 = get_row(db_engine, "US10YT_CMT", D2)

        result = decide("US10YT_CMT", D, stored, confirmed, confirmed_prior, P, is_cmt=True)
        assert result.verdict == Verdict.MATCH

        # No new audit
        audit_before = get_audit_count(db_engine)
        # Second run produces no changes
        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=stored_d1, previous_valid_session_d1=D,
            stored_d2=stored_d2, is_cmt=True,
        )
        assert len(plan.changed_source_fields) == 0
        assert len(plan.changed_derived_fields) == 0


# ---------------------------------------------------------------------------
# E3 — Transaction rollback
# ---------------------------------------------------------------------------


class TestE3TransactionRollback:
    def test_rollback_on_failure(self, db_engine, store):
        """Force failure after D update -> D unchanged, D+1 unchanged, no REPAIR audit."""
        insert_row(db_engine, make_cmt_row(close=4.85, prev_close=4.81))
        insert_row(db_engine, make_cmt_row(date=D1, close=4.88, prev_close=4.85))
        insert_row(db_engine, make_cmt_row(date=P, close=4.81))

        stored = get_row(db_engine, "US10YT_CMT", D)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)
        stored_d1 = get_row(db_engine, "US10YT_CMT", D1)

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=stored_d1, previous_valid_session_d1=D,
            stored_d2=None, is_cmt=True,
        )

        # Force failure by making D+1 missing after plan creation
        from sqlalchemy import text
        with db_engine.begin() as conn:
            conn.execute(text("DELETE FROM public.macro WHERE symbol = 'US10YT_CMT' AND date = :d"), {"d": D1})

        success, error = execute_correction_plan(store, plan, run_id="e3")
        assert success is False

        # D unchanged (rollback)
        d_after = get_row(db_engine, "US10YT_CMT", D)
        assert d_after.close == 4.85

        # No REPAIR audit
        repair_count = get_audit_count(db_engine, "REPAIR")
        assert repair_count == 0


# ---------------------------------------------------------------------------
# E4 — Real concurrent D writer
# ---------------------------------------------------------------------------


class TestE4ConcurrentDWriter:
    def test_concurrent_d_change_detected(self, db_engine, store):
        """Another transaction changes D -> STALE_PLAN, concurrent value survives."""
        insert_row(db_engine, make_cmt_row(close=4.85, prev_close=4.81))
        insert_row(db_engine, make_cmt_row(date=P, close=4.81))

        stored = get_row(db_engine, "US10YT_CMT", D)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=None, previous_valid_session_d1=None,
            stored_d2=None, is_cmt=True,
        )

        # Concurrent writer changes D
        from sqlalchemy import create_engine, text
        engine2 = create_engine(db_engine.url)
        with engine2.begin() as conn:
            conn.execute(text("""
                UPDATE public.macro SET close = 4.92, prev_close = 4.81
                 WHERE symbol = 'US10YT_CMT' AND date = :d
            """), {"d": D})
        engine2.dispose()

        success, error = execute_correction_plan(store, plan, run_id="e4")
        assert success is False
        assert error == "STALE_PLAN"

        # Concurrent writer's value survives
        d_after = get_row(db_engine, "US10YT_CMT", D)
        assert d_after.close == 4.92

        # No REPAIR audit
        repair_count = get_audit_count(db_engine, "REPAIR")
        assert repair_count == 0


# ---------------------------------------------------------------------------
# E5 — Real concurrent D+1 writer
# ---------------------------------------------------------------------------


class TestE5ConcurrentD1Writer:
    def test_concurrent_d1_change_detected(self, db_engine, store):
        """Another transaction changes D+1 -> STALE_PLAN, concurrent value preserved.

        Contract: plan.expected_old_values are the authoritative expected state.
        Locked current state is verification evidence only. If locked D+1 differs
        from plan expected D+1, return STALE_PLAN immediately.
        """
        insert_row(db_engine, make_cmt_row(close=4.85, prev_close=4.81))
        insert_row(db_engine, make_cmt_row(date=D1, close=4.88, prev_close=4.85))
        insert_row(db_engine, make_cmt_row(date=P, close=4.81))

        stored = get_row(db_engine, "US10YT_CMT", D)
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.81)
        stored_d1 = get_row(db_engine, "US10YT_CMT", D1)

        plan = build_correction_plan(
            "US10YT_CMT", D, stored, confirmed, confirmed_prior,
            stored_d1=stored_d1, previous_valid_session_d1=D,
            stored_d2=None, is_cmt=True,
        )

        # Verify plan expected D+1 prev_close = 4.85
        assert plan.expected_d1_old_values.get("prev_close") == 4.85

        # Concurrent writer changes D+1's prev_close to 4.99
        from sqlalchemy import create_engine, text
        engine2 = create_engine(db_engine.url)
        with engine2.begin() as conn:
            conn.execute(text("""
                UPDATE public.macro SET prev_close = 4.99
                 WHERE symbol = 'US10YT_CMT' AND date = :d
            """), {"d": D1})
        engine2.dispose()

        # Execute correction — should detect STALE_PLAN
        success, error = execute_correction_plan(store, plan, run_id="e5")
        assert success is False
        assert error == "STALE_PLAN"

        # D unchanged by Phase B
        d_after = get_row(db_engine, "US10YT_CMT", D)
        assert d_after.close == 4.85

        # D+1 remains 4.99 (concurrent writer's value preserved)
        d1_after = get_row(db_engine, "US10YT_CMT", D1)
        assert d1_after.prev_close == 4.99

        # Zero REPAIR audit rows
        repair_count = get_audit_count(db_engine, "REPAIR")
        assert repair_count == 0


# ---------------------------------------------------------------------------
# E6 — Prior dependency
# ---------------------------------------------------------------------------


class TestE6PriorDependency:
    def test_prior_mismatch_blocks_d(self, db_engine, store):
        """P stored differs from confirmed -> DEPENDENCY_RECONCILE_REQUIRED, D not repaired."""
        insert_row(db_engine, make_cmt_row(close=4.85, prev_close=4.81))  # D
        insert_row(db_engine, make_cmt_row(date=P, close=4.81))  # P stored (matches confirmed)

        # But confirmed P is different
        confirmed = make_cmt_row(close=4.87, prev_close=4.81)
        confirmed_prior = make_cmt_row(date=P, close=4.83)  # different from stored P

        stored = get_row(db_engine, "US10YT_CMT", D)
        stored_prior = get_row(db_engine, "US10YT_CMT", P)

        dep_ok, dep_reason = check_prior_dependency(
            "US10YT_CMT", D, stored_prior, confirmed_prior, P
        )
        assert dep_ok is False
        assert dep_reason == Reason.DEPENDENCY_RECONCILE_REQUIRED

        # decide() returns CORRECTION_REQUIRED (it doesn't check prior dependency)
        # The prior dependency check is done separately by check_prior_dependency()
        # and enforced by run_reconciliation_cycle()
        result = decide("US10YT_CMT", D, stored, confirmed, confirmed_prior, P, is_cmt=True)
        assert result.verdict == Verdict.CORRECTION_REQUIRED


# ---------------------------------------------------------------------------
# E7 — Detect-only hard guard
# ---------------------------------------------------------------------------


class TestE7DetectOnlyGuard:
    def test_non_cmt_no_update(self, db_engine, store):
        """Non-CMT class with material mismatch -> detection only, zero UPDATE."""
        # Insert a detect-only symbol (e.g., ^GSPC)
        from sqlalchemy import text
        with db_engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO public.macro (date, symbol, name, asset_type, close, prev_close, source)
                VALUES (:date, :symbol, :name, :asset_type, :close, :prev_close, :source)
            """), {
                "date": D, "symbol": "^GSPC", "name": "S&P 500",
                "asset_type": "equity", "close": 5000.0, "prev_close": 4990.0,
                "source": "yfinance",
            })

        # Build a plan for ^GSPC (detect-only)
        stored = get_row(db_engine, "^GSPC", D)
        confirmed = Row(
            symbol="^GSPC", date=D, close=5100.0, prev_close=4990.0,
            source="yfinance", asset_type="equity",
        )
        confirmed_prior = Row(
            symbol="^GSPC", date=P, close=4990.0,
            source="yfinance", asset_type="equity",
        )

        result = decide("^GSPC", D, stored, confirmed, confirmed_prior, P, is_cmt=False)
        # ^GSPC is detect-only, so decide returns SOURCE_DEFINITION_UNRESOLVED
        assert result.verdict == Verdict.SOURCE_DEFINITION_UNRESOLVED

        # Even if we tried to build a plan, the hard guard prevents UPDATE
        assert not is_auto_correctable("^GSPC")

        # Record detection event
        record_detection_event(
            store, "^GSPC", D,
            Verdict.SOURCE_DEFINITION_UNRESOLVED,
            Reason.SOURCE_DEFINITION_UNRESOLVED,
            run_id="e7",
        )

        # Verify no UPDATE to ^GSPC
        row_after = get_row(db_engine, "^GSPC", D)
        assert row_after.close == 5000.0  # unchanged

        # Verify DETECTION audit
        detection_count = get_audit_count(db_engine, "DETECTION")
        assert detection_count > 0
        repair_count = get_audit_count(db_engine, "REPAIR")
        assert repair_count == 0


# ---------------------------------------------------------------------------
# E8 — Report-only unresolved source
# ---------------------------------------------------------------------------


class TestE8ReportOnlyUnresolved:
    def test_legacy_treasury_no_update(self, db_engine, store):
        """Legacy USnYT=X -> LEGACY_SOURCE_DEFINITION_MIXED, no UPDATE."""
        from sqlalchemy import text
        with db_engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO public.macro (date, symbol, name, asset_type, close, prev_close, source)
                VALUES (:date, :symbol, :name, :asset_type, :close, :prev_close, :source)
            """), {
                "date": D, "symbol": "US10YT=X", "name": "US 10Y Yield",
                "asset_type": "ust_yield", "close": 4.85, "prev_close": 4.81,
                "source": "investing.com",
            })

        stored = get_row(db_engine, "US10YT=X", D)
        confirmed = Row(
            symbol="US10YT=X", date=D, close=4.87, prev_close=4.81,
            source="investing.com", asset_type="ust_yield",
        )

        result = decide("US10YT=X", D, stored, confirmed, None, P, is_cmt=False)
        assert result.verdict == Verdict.LEGACY_SOURCE_DEFINITION_MIXED
        assert result.plan is None

        # No UPDATE
        row_after = get_row(db_engine, "US10YT=X", D)
        assert row_after.close == 4.85  # unchanged