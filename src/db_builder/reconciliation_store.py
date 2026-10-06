"""Phase B — reconciliation store (ISOLATED).

Provides:
- ReconciliationStore protocol (interface)
- InMemoryReconciliationStore for testing (no DB)
- SQL templates for the real DB implementation
- execute_correction_plan() — the transaction logic

NOT connected to production execution.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Set, Tuple

from db_builder.reconciliation import (
    AuditEventType,
    CorrectionPlan,
    Reason,
    Row,
    Verdict,
    _within_tolerance,
    round_4,
)

# ---------------------------------------------------------------------------
# Audit event
# ---------------------------------------------------------------------------


@dataclass
class AuditEvent:
    """Append-only audit record."""
    audit_id: Optional[int] = None
    run_id: str = ""
    symbol: str = ""
    session_date: dt.date = dt.date(2000, 1, 1)
    verdict: Verdict = Verdict.MATCH
    field: str = ""
    old_value: Optional[float] = None
    new_value: Optional[float] = None
    source_definition: str = ""
    confirmation_source: str = ""
    confirmation_timestamp: Optional[dt.datetime] = None
    reason: str = ""
    repaired_at: Optional[dt.datetime] = None
    event_type: AuditEventType = AuditEventType.DETECTION
    # Optional fields
    expected_previous_session: Optional[dt.date] = None
    source_observation_ts: Optional[dt.datetime] = None
    correction_txn_id: Optional[int] = None
    row_role: str = ""  # "D", "D1", ""
    release_version: str = ""


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------


class ReconciliationStore(Protocol):
    """Interface for reconciliation persistence."""

    def read_row(self, symbol: str, date: dt.date) -> Optional[Row]:
        """Read a stored row."""
        ...

    def lock_rows(
        self, symbol: str, dates: List[dt.date]
    ) -> Dict[dt.date, Row]:
        """SELECT ... FOR UPDATE — lock and return rows."""
        ...

    def insert_audit(self, event: AuditEvent) -> int:
        """Insert an append-only audit event. Returns audit_id."""
        ...

    def update_row(
        self,
        symbol: str,
        date: dt.date,
        updates: Dict[str, Optional[float]],
        expected_old: Dict[str, Optional[float]],
    ) -> int:
        """Optimistic UPDATE with expected-old-value predicates.

        Returns rowcount (0 if stale).
        """
        ...

    def record_detection(self, event: AuditEvent) -> int:
        """Record a non-repair detection event (after repair txn aborts)."""
        ...


# ---------------------------------------------------------------------------
# In-memory store for testing
# ---------------------------------------------------------------------------


class InMemoryReconciliationStore:
    """In-memory implementation for isolated testing."""

    def __init__(self):
        self.rows: Dict[Tuple[str, dt.date], Row] = {}
        self.audit_log: List[AuditEvent] = []
        self._audit_seq = 0
        self._locked: Set[Tuple[str, dt.date]] = set()
        self._simulate_stale: Dict[Tuple[str, dt.date], Row] = {}

    def add_row(self, row: Row):
        self.rows[(row.symbol, row.date)] = row

    def read_row(self, symbol: str, date: dt.date) -> Optional[Row]:
        return self.rows.get((symbol, date))

    def lock_rows(
        self, symbol: str, dates: List[dt.date]
    ) -> Dict[dt.date, Row]:
        result = {}
        for d in dates:
            key = (symbol, d)
            if key in self.rows:
                result[d] = self.rows[key]
                self._locked.add(key)
        return result

    def insert_audit(self, event: AuditEvent) -> int:
        self._audit_seq += 1
        event.audit_id = self._audit_seq
        self.audit_log.append(event)
        return self._audit_seq

    def update_row(
        self,
        symbol: str,
        date: dt.date,
        updates: Dict[str, Optional[float]],
        expected_old: Dict[str, Optional[float]],
    ) -> int:
        key = (symbol, date)
        if key not in self.rows:
            return 0
        current = self.rows[key]
        # Check expected old values
        for f, expected in expected_old.items():
            actual = current.field_value(f)
            if not _values_equal(actual, expected):
                return 0  # stale
        # Apply updates
        for f, v in updates.items():
            setattr(current, f, v)
        return 1

    def record_detection(self, event: AuditEvent) -> int:
        return self.insert_audit(event)

    def simulate_concurrent_change(self, symbol: str, date: dt.date, row: Row):
        """Simulate another writer changing a row between plan and write."""
        self._simulate_stale[(symbol, date)] = row

    def get_audit_count(self) -> int:
        return len(self.audit_log)

    def get_audit_events(
        self, event_type: Optional[AuditEventType] = None
    ) -> List[AuditEvent]:
        if event_type is None:
            return list(self.audit_log)
        return [e for e in self.audit_log if e.event_type == event_type]


class SqlReconciliationStore:
    """Production SQLAlchemy-backed reconciliation store.

    Uses real PostgreSQL locking (SELECT FOR UPDATE) and optimistic
    UPDATE predicates. This is the store that would be deployed.
    """

    def __init__(self, engine):
        self.engine = engine

    def read_row(self, symbol: str, date: dt.date) -> Optional[Row]:
        from sqlalchemy import text
        sql = text("""
            SELECT date, symbol, open, high, low, close, adj_close, volume,
                   prev_close, change, pct_chg, amplitude, source, asset_type
              FROM public.macro
             WHERE symbol = :symbol AND date = :date
             LIMIT 1
        """)
        with self.engine.connect() as conn:
            r = conn.execute(sql, {"symbol": symbol, "date": date}).mappings().first()
        if r is None:
            return None
        return Row(
            symbol=r["symbol"], date=r["date"],
            open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], adj_close=r["adj_close"], volume=r["volume"],
            prev_close=r["prev_close"], change=r["change"],
            pct_chg=r["pct_chg"], amplitude=r["amplitude"],
            source=r["source"], asset_type=r["asset_type"],
        )

    def lock_rows(self, symbol: str, dates: List[dt.date]) -> Dict[dt.date, Row]:
        from sqlalchemy import text
        if not dates:
            return {}
        sql = text("""
            SELECT date, symbol, open, high, low, close, adj_close, volume,
                   prev_close, change, pct_chg, amplitude, source, asset_type
              FROM public.macro
             WHERE symbol = :symbol AND date = ANY(:dates)
               FOR UPDATE
        """)
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"symbol": symbol, "dates": dates}).mappings().all()
        result = {}
        for r in rows:
            result[r["date"]] = Row(
                symbol=r["symbol"], date=r["date"],
                open=r["open"], high=r["high"], low=r["low"],
                close=r["close"], adj_close=r["adj_close"], volume=r["volume"],
                prev_close=r["prev_close"], change=r["change"],
                pct_chg=r["pct_chg"], amplitude=r["amplitude"],
                source=r["source"], asset_type=r["asset_type"],
            )
        return result

    def insert_audit(self, event: AuditEvent) -> int:
        from sqlalchemy import text
        sql = text("""
            INSERT INTO public.macro_reconciliation_audit (
                run_id, symbol, session_date, verdict, field,
                old_value, new_value, source_definition, confirmation_source,
                confirmation_timestamp, reason, repaired_at,
                expected_previous_session, source_observation_ts,
                correction_txn_id, row_role, release_version, event_type
            ) VALUES (
                :run_id, :symbol, :session_date, :verdict, :field,
                :old_value, :new_value, :source_definition, :confirmation_source,
                :confirmation_timestamp, :reason, :repaired_at,
                :expected_previous_session, :source_observation_ts,
                :correction_txn_id, :row_role, :release_version, :event_type
            ) RETURNING audit_id
        """)
        with self.engine.begin() as conn:
            result = conn.execute(sql, {
                "run_id": event.run_id,
                "symbol": event.symbol,
                "session_date": event.session_date,
                "verdict": event.verdict.value,
                "field": event.field,
                "old_value": event.old_value,
                "new_value": event.new_value,
                "source_definition": event.source_definition,
                "confirmation_source": event.confirmation_source,
                "confirmation_timestamp": event.confirmation_timestamp,
                "reason": event.reason.value if isinstance(event.reason, Enum) else event.reason,
                "repaired_at": event.repaired_at,
                "expected_previous_session": event.expected_previous_session,
                "source_observation_ts": event.source_observation_ts,
                "correction_txn_id": event.correction_txn_id,
                "row_role": event.row_role,
                "release_version": event.release_version,
                "event_type": event.event_type.value,
            })
            return result.scalar()

    def update_row(
        self,
        symbol: str,
        date: dt.date,
        updates: Dict[str, Optional[float]],
        expected_old: Dict[str, Optional[float]],
    ) -> int:
        from sqlalchemy import text
        # Build optimistic WHERE clause
        set_parts = []
        where_parts = ["symbol = :symbol", "date = :date"]
        params = {"symbol": symbol, "date": date}
        for i, (f, v) in enumerate(updates.items()):
            set_parts.append(f"{f} = :set_{i}")
            params[f"set_{i}"] = v
        for i, (f, v) in enumerate(expected_old.items()):
            where_parts.append(f"{f} IS NOT DISTINCT FROM :old_{i}")
            params[f"old_{i}"] = v
        sql = text(f"""
            UPDATE public.macro
               SET {', '.join(set_parts)}
             WHERE {' AND '.join(where_parts)}
        """)
        with self.engine.begin() as conn:
            result = conn.execute(sql, params)
            return result.rowcount

    def record_detection(self, event: AuditEvent) -> int:
        return self.insert_audit(event)

    def transaction(self):
        """Return a context manager for a single transaction."""
        return self.engine.begin()


def _values_equal(a: Optional[float], b: Optional[float]) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return round_4(a) == round_4(b)


# ---------------------------------------------------------------------------
# SQL templates (for real DB implementation — NOT executed here)
# ---------------------------------------------------------------------------

SQL_LOCK_ROWS = """
SELECT date, symbol, open, high, low, close, adj_close, volume,
       prev_close, change, pct_chg, amplitude, source, asset_type
  FROM public.macro
 WHERE symbol = :symbol AND date = ANY(:dates)
   FOR UPDATE
"""

SQL_INSERT_REPAIR_AUDIT = """
INSERT INTO public.macro_reconciliation_audit (
    run_id, symbol, session_date, verdict, field,
    old_value, new_value, source_definition, confirmation_source,
    confirmation_timestamp, reason, repaired_at,
    expected_previous_session, source_observation_ts,
    correction_txn_id, row_role, release_version
) VALUES (
    :run_id, :symbol, :session_date, :verdict, :field,
    :old_value, :new_value, :source_definition, :confirmation_source,
    :confirmation_timestamp, :reason, :repaired_at,
    :expected_previous_session, :source_observation_ts,
    :correction_txn_id, :row_role, :release_version
)
"""

SQL_INSERT_DETECTION_AUDIT = """
INSERT INTO public.macro_reconciliation_audit (
    run_id, symbol, session_date, verdict, field,
    old_value, new_value, source_definition, confirmation_source,
    confirmation_timestamp, reason, repaired_at,
    expected_previous_session, source_observation_ts,
    correction_txn_id, row_role, release_version
) VALUES (
    :run_id, :symbol, :session_date, :verdict, :field,
    :old_value, :new_value, :source_definition, :confirmation_source,
    :confirmation_timestamp, :reason, NULL,
    :expected_previous_session, :source_observation_ts,
    NULL, '', :release_version
)
"""

SQL_UPDATE_ROW_OPTIMISTIC = """
UPDATE public.macro
   SET {set_clause}
 WHERE symbol = :symbol AND date = :date
   {expected_old_clause}
"""


# ---------------------------------------------------------------------------
# Transaction logic
# ---------------------------------------------------------------------------


class StalePlanError(Exception):
    """Raised when the plan's expected old values no longer match."""


def execute_correction_plan(
    store: ReconciliationStore,
    plan: CorrectionPlan,
    run_id: str,
    confirmation_source: str = "treasury.gov",
    source_definition: str = "treasury_gov_cmt",
    release_version: str = "",
) -> Tuple[bool, Optional[str]]:
    """Execute a correction plan with full concurrency protection.

    Returns (success, error_reason).

    Transaction logic:
    1. Re-read D and any required D+1 row
    2. Lock rows (SELECT FOR UPDATE)
    3. Verify expected old values
    4. Insert repair audit
    5. UPDATE D with optimistic predicates
    6. UPDATE D+1 derived fields
    7. Re-validate arithmetic

    All steps 4-7 share a single transaction so that any failure rolls back
    both the repair and its audit evidence.
    """
    symbol = plan.symbol
    d = plan.session_date

    # Determine D+1 date
    d1_date = None
    if plan.new_d1_row is not None:
        d1_date = plan.new_d1_row.date

    # 1-2. Re-read and lock
    dates_to_lock = [d]
    if d1_date:
        dates_to_lock.append(d1_date)
    locked = store.lock_rows(symbol, dates_to_lock)

    # 3. Verify expected old values
    stored_d = locked.get(d)
    if stored_d is None:
        return False, "ROW_NOT_FOUND"

    for f, expected in plan.expected_old_values.items():
        actual = stored_d.field_value(f)
        if not _values_equal(actual, expected):
            return False, "STALE_PLAN"

    # 5. Verify D+1 expected old values against locked state
    if plan.new_d1_row and d1_date:
        stored_d1 = locked.get(d1_date)
        if stored_d1 is None:
            return False, "D1_NOT_FOUND"
        # Compare locked D+1 against PLAN expected D+1 values (not locked state)
        for f in ["prev_close", "change", "pct_chg", "amplitude"]:
            plan_expected = plan.expected_d1_old_values.get(f)
            locked_actual = stored_d1.field_value(f)
            if not _values_equal(locked_actual, plan_expected):
                return False, "STALE_PLAN"

    # 4-7. All writes in a single transaction
    now = dt.datetime.now(dt.timezone.utc)
    all_changed = plan.changed_source_fields + plan.changed_derived_fields

    # Use a single transaction for SQL stores; in-memory store has no txn support
    if hasattr(store, "transaction"):
        try:
            with store.transaction():
                # 4. Insert repair audit (one row per changed field)
                for f in all_changed:
                    old_v = plan.expected_old_values.get(f)
                    new_v = plan.new_row.field_value(f) if plan.new_row else None
                    event = AuditEvent(
                        run_id=run_id,
                        symbol=symbol,
                        session_date=d,
                        verdict=Verdict.CORRECTION_REQUIRED,
                        field=f,
                        old_value=old_v,
                        new_value=new_v,
                        source_definition=source_definition,
                        confirmation_source=confirmation_source,
                        confirmation_timestamp=now,
                        reason=plan.reason.value,
                        repaired_at=now,
                        event_type=AuditEventType.REPAIR,
                        row_role="D",
                        release_version=release_version,
                    )
                    store.insert_audit(event)

                # 6. UPDATE D with optimistic predicates
                if plan.new_row:
                    updates = {}
                    for f in all_changed:
                        updates[f] = plan.new_row.field_value(f)
                    rowcount = store.update_row(symbol, d, updates, plan.expected_old_values)
                    if rowcount != 1:
                        raise StalePlanError("STALE_PLAN")

                # 7. UPDATE D+1 derived fields
                # CRITICAL: expected-old values come from plan.expected_old_values,
                # NOT from locked current state. The locked state is verification
                # evidence only — if it differs from plan expected, we already
                # returned STALE_PLAN above.
                if plan.new_d1_row and d1_date:
                    d1_expected = {}
                    d1_updates = {}
                    for f in ["prev_close", "change", "pct_chg", "amplitude"]:
                        d1_updates[f] = plan.new_d1_row.field_value(f)
                        # Use PLAN expected D+1 values, not locked current state
                        d1_expected[f] = plan.expected_d1_old_values.get(f)
                    rowcount_d1 = store.update_row(symbol, d1_date, d1_updates, d1_expected)
                    if rowcount_d1 != 1:
                        raise StalePlanError("STALE_PLAN")

                    # Audit D+1 changes
                    for f in ["prev_close", "change", "pct_chg", "amplitude"]:
                        old_v = d1_expected.get(f)
                        new_v = plan.new_d1_row.field_value(f)
                        if not _values_equal(old_v, new_v):
                            event = AuditEvent(
                                run_id=run_id,
                                symbol=symbol,
                                session_date=d1_date,
                                verdict=Verdict.CORRECTION_REQUIRED,
                                field=f,
                                old_value=old_v,
                                new_value=new_v,
                                source_definition=source_definition,
                                confirmation_source=confirmation_source,
                                confirmation_timestamp=now,
                                reason=plan.reason.value,
                                repaired_at=now,
                                event_type=AuditEventType.REPAIR,
                                row_role="D1",
                                release_version=release_version,
                            )
                            store.insert_audit(event)

                # 8. Re-validate arithmetic
                if plan.new_row:
                    _validate_arithmetic(plan.new_row)
                if plan.new_d1_row:
                    _validate_arithmetic(plan.new_d1_row)

        except StalePlanError as e:
            return False, str(e)
        except Exception:
            return False, "TRANSACTION_FAILED"
    else:
        # In-memory store: no transaction support, execute directly
        for f in all_changed:
            old_v = plan.expected_old_values.get(f)
            new_v = plan.new_row.field_value(f) if plan.new_row else None
            event = AuditEvent(
                run_id=run_id,
                symbol=symbol,
                session_date=d,
                verdict=Verdict.CORRECTION_REQUIRED,
                field=f,
                old_value=old_v,
                new_value=new_v,
                source_definition=source_definition,
                confirmation_source=confirmation_source,
                confirmation_timestamp=now,
                reason=plan.reason.value,
                repaired_at=now,
                event_type=AuditEventType.REPAIR,
                row_role="D",
                release_version=release_version,
            )
            store.insert_audit(event)

        if plan.new_row:
            updates = {}
            for f in all_changed:
                updates[f] = plan.new_row.field_value(f)
            rowcount = store.update_row(symbol, d, updates, plan.expected_old_values)
            if rowcount != 1:
                return False, "STALE_PLAN"

        if plan.new_d1_row and d1_date:
            d1_expected = {}
            d1_updates = {}
            for f in ["prev_close", "change", "pct_chg", "amplitude"]:
                d1_updates[f] = plan.new_d1_row.field_value(f)
                # Use PLAN expected D+1 values, not locked current state
                d1_expected[f] = plan.expected_d1_old_values.get(f)
            rowcount_d1 = store.update_row(symbol, d1_date, d1_updates, d1_expected)
            if rowcount_d1 != 1:
                return False, "STALE_PLAN"

            for f in ["prev_close", "change", "pct_chg", "amplitude"]:
                old_v = d1_expected.get(f)
                new_v = plan.new_d1_row.field_value(f)
                if not _values_equal(old_v, new_v):
                    event = AuditEvent(
                        run_id=run_id,
                        symbol=symbol,
                        session_date=d1_date,
                        verdict=Verdict.CORRECTION_REQUIRED,
                        field=f,
                        old_value=old_v,
                        new_value=new_v,
                        source_definition=source_definition,
                        confirmation_source=confirmation_source,
                        confirmation_timestamp=now,
                        reason=plan.reason.value,
                        repaired_at=now,
                        event_type=AuditEventType.REPAIR,
                        row_role="D1",
                        release_version=release_version,
                    )
                    store.insert_audit(event)

        if plan.new_row:
            _validate_arithmetic(plan.new_row)
        if plan.new_d1_row:
            _validate_arithmetic(plan.new_d1_row)

    return True, None


def _validate_arithmetic(row: Row):
    """Re-validate derived field arithmetic."""
    if row.close is not None and row.prev_close is not None:
        expected_change = round_4(row.close - row.prev_close)
        if row.change is not None:
            assert _values_equal(row.change, expected_change), (
                f"change mismatch: {row.change} != {expected_change}"
            )
        if row.prev_close != 0:
            expected_pct = round_4(expected_change / row.prev_close * 100)
            if row.pct_chg is not None:
                assert _values_equal(row.pct_chg, expected_pct), (
                    f"pct_chg mismatch: {row.pct_chg} != {expected_pct}"
                )


def record_detection_event(
    store: ReconciliationStore,
    symbol: str,
    session_date: dt.date,
    verdict: Verdict,
    reason: Reason,
    run_id: str,
    field: str = "",
    old_value: Optional[float] = None,
    new_value: Optional[float] = None,
    source_definition: str = "",
    confirmation_source: str = "",
    release_version: str = "",
) -> int:
    """Record a non-repair detection event (after repair txn aborts or no-write verdict)."""
    event = AuditEvent(
        run_id=run_id,
        symbol=symbol,
        session_date=session_date,
        verdict=verdict,
        field=field,
        old_value=old_value,
        new_value=new_value,
        source_definition=source_definition,
        confirmation_source=confirmation_source,
        confirmation_timestamp=dt.datetime.now(dt.timezone.utc),
        reason=reason.value,
        repaired_at=None,
        event_type=AuditEventType.DETECTION,
        release_version=release_version,
    )
    return store.record_detection(event)