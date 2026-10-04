"""Bounded, persistent deferred-history recovery queue (Phase A).

A completed historical bar whose prior valid session is not yet confirmed is
DEFERRED. It must:

  * not be written to ``public.macro`` (unconfirmed prior session),
  * not be written to ``public.macro_live`` (that table holds only the latest
    live snapshot per symbol),
  * survive the normal LOOKBACK_DAYS=5 ingestion window,
  * be retried in bounded batches so hourly runs never become a historical
    backfill,
  * escalate to DEFERRED_HISTORY_UNRESOLVED after repeated failures *without
    disappearing from tracking*.

The queue is store-agnostic: ``MemoryDeferredStore`` for tests,
``SqlDeferredStore`` for the real migration. No production DB access happens at
import time.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DEFERRED = "DEFERRED"
RESOLVED = "RESOLVED"
DEFERRED_HISTORY_UNRESOLVED = "DEFERRED_HISTORY_UNRESOLVED"

#: Queue retention/retry horizon. Independent of LOOKBACK_DAYS so a deferred
#: session keeps being retried long after the 5-day window has expired.
RECOVERY_DAYS = 30
#: Maximum rows attempted per hourly run (bounds runtime + provider calls).
RECOVERY_BATCH = 50
#: Escalate once this many attempts have failed.
MAX_ATTEMPTS = 48


@dataclass
class DeferredRow:
    symbol: str
    session_date: dt.date
    reason: str
    status: str = DEFERRED
    first_seen: Optional[dt.datetime] = None
    last_attempt: Optional[dt.datetime] = None
    attempt_count: int = 0
    resolved_at: Optional[dt.datetime] = None

    @property
    def key(self) -> Tuple[str, dt.date]:
        return (self.symbol, self.session_date)


class DeferredStore:
    """Interface for the deferred-history queue."""

    def record_deferral(self, symbol, session_date, reason, now=None) -> DeferredRow:
        raise NotImplementedError

    def record_success(self, symbol, session_date, now=None) -> None:
        raise NotImplementedError

    def due_candidates(self, now=None, limit=RECOVERY_BATCH,
                       recovery_days=RECOVERY_DAYS) -> List[DeferredRow]:
        raise NotImplementedError

    def all_rows(self) -> List[DeferredRow]:
        raise NotImplementedError

    def escalate_stale(self, now=None, recovery_days=RECOVERY_DAYS,
                       max_attempts=MAX_ATTEMPTS) -> List[DeferredRow]:
        raise NotImplementedError


class MemoryDeferredStore(DeferredStore):
    """In-memory implementation used by the regression tests."""

    def __init__(self):
        self._rows: Dict[Tuple[str, dt.date], DeferredRow] = {}

    def record_deferral(self, symbol, session_date, reason, now=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        key = (symbol, session_date)
        row = self._rows.get(key)
        if row is None:
            row = DeferredRow(symbol=symbol, session_date=session_date,
                              reason=reason, status=DEFERRED,
                              first_seen=now, last_attempt=now, attempt_count=1)
            self._rows[key] = row
        else:
            row.attempt_count += 1
            row.last_attempt = now
            row.reason = reason
            # A previously escalated row that is retried and still failing
            # stays escalated; a resolved row reopens as DEFERRED.
            if row.status == RESOLVED:
                row.status = DEFERRED
                row.resolved_at = None
        return row

    def record_success(self, symbol, session_date, now=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        key = (symbol, session_date)
        row = self._rows.get(key)
        if row is None:
            row = DeferredRow(symbol=symbol, session_date=session_date,
                              reason="RESOLVED_WITHOUT_PRIOR_DEFERRAL",
                              status=RESOLVED, first_seen=now,
                              last_attempt=now, resolved_at=now)
            self._rows[key] = row
        else:
            row.status = RESOLVED
            row.resolved_at = now
            row.last_attempt = now

    def due_candidates(self, now=None, limit=RECOVERY_BATCH,
                       recovery_days=RECOVERY_DAYS):
        now = now or dt.datetime.now(dt.timezone.utc)
        cutoff = now.date() - dt.timedelta(days=recovery_days)
        out = [r for r in self._rows.values()
               if r.status == DEFERRED and r.session_date >= cutoff]
        out.sort(key=lambda r: (r.session_date, r.symbol))
        return out[:limit]

    def all_rows(self):
        return list(self._rows.values())

    def escalate_stale(self, now=None, recovery_days=RECOVERY_DAYS,
                       max_attempts=MAX_ATTEMPTS):
        now = now or dt.datetime.now(dt.timezone.utc)
        cutoff = now.date() - dt.timedelta(days=recovery_days)
        escalated = []
        for row in self._rows.values():
            if row.status != DEFERRED:
                continue
            if row.attempt_count >= max_attempts or row.session_date < cutoff:
                row.status = DEFERRED_HISTORY_UNRESOLVED
                escalated.append(row)
        return escalated


class SqlDeferredStore(DeferredStore):
    """PostgreSQL-backed queue (see migrations/phase_a/001_macro_deferred_history.sql)."""

    def __init__(self, engine):
        self._engine = engine

    def record_deferral(self, symbol, session_date, reason, now=None):
        from sqlalchemy import text
        now = now or dt.datetime.now(dt.timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO public.macro_deferred_history
                    (symbol, session_date, reason, status,
                     first_seen, last_attempt, attempt_count)
                VALUES (:symbol, :session_date, :reason, 'DEFERRED',
                        :now, :now, 1)
                ON CONFLICT (symbol, session_date) DO UPDATE SET
                    reason = EXCLUDED.reason,
                    last_attempt = EXCLUDED.last_attempt,
                    attempt_count = public.macro_deferred_history.attempt_count + 1,
                    status = CASE
                        WHEN public.macro_deferred_history.status = 'RESOLVED'
                        THEN 'DEFERRED'
                        ELSE public.macro_deferred_history.status END,
                    resolved_at = CASE
                        WHEN public.macro_deferred_history.status = 'RESOLVED'
                        THEN NULL
                        ELSE public.macro_deferred_history.resolved_at END
            """), {"symbol": symbol, "session_date": session_date,
                   "reason": reason, "now": now})
        return DeferredRow(symbol=symbol, session_date=session_date,
                           reason=reason, first_seen=now, last_attempt=now,
                           attempt_count=1)

    def record_success(self, symbol, session_date, now=None):
        from sqlalchemy import text
        now = now or dt.datetime.now(dt.timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO public.macro_deferred_history
                    (symbol, session_date, reason, status,
                     first_seen, last_attempt, attempt_count, resolved_at)
                VALUES (:symbol, :session_date, 'RESOLVED', 'RESOLVED',
                        :now, :now, 0, :now)
                ON CONFLICT (symbol, session_date) DO UPDATE SET
                    status = 'RESOLVED',
                    resolved_at = EXCLUDED.resolved_at,
                    last_attempt = EXCLUDED.last_attempt
            """), {"symbol": symbol, "session_date": session_date, "now": now})

    def due_candidates(self, now=None, limit=RECOVERY_BATCH,
                       recovery_days=RECOVERY_DAYS):
        from sqlalchemy import text
        now = now or dt.datetime.now(dt.timezone.utc)
        cutoff = now.date() - dt.timedelta(days=recovery_days)
        with self._engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT symbol, session_date, reason, status,
                       first_seen, last_attempt, attempt_count, resolved_at
                FROM public.macro_deferred_history
                WHERE status = 'DEFERRED' AND session_date >= :cutoff
                ORDER BY session_date, symbol
                LIMIT :limit
            """), {"cutoff": cutoff, "limit": limit}).fetchall()
        return [DeferredRow(*r) for r in rows]

    def all_rows(self):
        from sqlalchemy import text
        with self._engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT symbol, session_date, reason, status,
                       first_seen, last_attempt, attempt_count, resolved_at
                FROM public.macro_deferred_history
                ORDER BY session_date, symbol
            """)).fetchall()
        return [DeferredRow(*r) for r in rows]

    def escalate_stale(self, now=None, recovery_days=RECOVERY_DAYS,
                       max_attempts=MAX_ATTEMPTS):
        from sqlalchemy import text
        now = now or dt.datetime.now(dt.timezone.utc)
        cutoff = now.date() - dt.timedelta(days=recovery_days)
        with self._engine.begin() as conn:
            conn.execute(text("""
                UPDATE public.macro_deferred_history
                   SET status = 'DEFERRED_HISTORY_UNRESOLVED'
                 WHERE status = 'DEFERRED'
                   AND (attempt_count >= :max_attempts
                        OR session_date < :cutoff)
            """), {"max_attempts": max_attempts, "cutoff": cutoff})
        return [r for r in self.all_rows()
                if r.status == DEFERRED_HISTORY_UNRESOLVED]
