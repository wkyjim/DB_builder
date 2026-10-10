"""Bounded, persistent deferred-history recovery queue (Phase A / A.2B).

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

Phase A.2B adds a *retry state* layer that is orthogonal to writability. It
only ever SUPPRESSES fetches; it never makes a row eligible to write. See
``db_builder.retry_policy`` for the state vocabulary and schedule.

The queue is store-agnostic: ``MemoryDeferredStore`` for tests,
``SqlDeferredStore`` for the real migration. No production DB access happens at
import time.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from db_builder import retry_policy as _rp

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
    # --- Phase A.2B retry state (additive; None == legacy/unknown) ---
    retry_class: Optional[str] = None
    next_retry_at: Optional[dt.datetime] = None
    last_error_class: Optional[str] = None
    manual_review_required: bool = False

    @property
    def key(self) -> Tuple[str, dt.date]:
        return (self.symbol, self.session_date)


class DeferredStore:
    """Interface for the deferred-history queue."""

    def record_deferral(self, symbol, session_date, reason, now=None, *,
                        attempted=True, retry_class=None,
                        next_retry_at=None, last_error_class=None,
                        manual_review_required=None) -> DeferredRow:
        raise NotImplementedError

    def record_success(self, symbol, session_date, now=None) -> None:
        raise NotImplementedError

    def due_candidates(self, now=None, limit=RECOVERY_BATCH,
                       recovery_days=RECOVERY_DAYS, *,
                       provider_available=None) -> List[DeferredRow]:
        raise NotImplementedError

    def all_rows(self) -> List[DeferredRow]:
        raise NotImplementedError

    def escalate_stale(self, now=None, recovery_days=RECOVERY_DAYS,
                       max_attempts=MAX_ATTEMPTS) -> List[DeferredRow]:
        raise NotImplementedError

    def release_on_condition_change(self, symbol, session_date, *,
                                    now=None) -> bool:
        """Manually/condition release a structural row to RETRY_ELIGIBLE."""
        raise NotImplementedError


def _apply_retry_state(row: DeferredRow, reason: str, now: dt.datetime, *,
                       attempted: bool, retry_class, next_retry_at,
                       last_error_class, manual_review_required) -> None:
    """Update a row's retry-state fields in place.

    ``attempted`` gates the attempt_count increment: it must only grow when a
    real recovery fetch was made. A structural/frozen observation (no fetch)
    never increments the counter, preserving its operational meaning.

    Precedence: an incidental fetch error must NOT overwrite a stronger
    durable structural blocker. The new class is combined with any existing
    class via :func:`retry_policy.resolve_retry_class`, so e.g.
    PROVENANCE_UNVERIFIED survives an incidental yfinance ValueError.
    """
    if retry_class is None:
        retry_class = _rp.classify_reason(reason)
    prior_class = row.retry_class
    effective = _rp.resolve_retry_class(prior_class, retry_class)
    row.retry_class = effective
    row.last_error_class = (last_error_class if last_error_class is not None
                            else (type(reason).__name__ if reason else None))
    if manual_review_required is None:
        manual_review_required = effective == _rp.MANUAL_REVIEW_REQUIRED
    # A stronger pre-existing structural class keeps manual_review_required
    # unset unless it is itself the manual class.
    row.manual_review_required = bool(manual_review_required)
    if attempted:
        row.attempt_count += 1
    if effective == _rp.BACKOFF_ACTIVE and attempted:
        row.next_retry_at = (next_retry_at if next_retry_at is not None
                             else _rp.next_retry_at(now, row.attempt_count))
    elif next_retry_at is not None:
        row.next_retry_at = next_retry_at
    else:
        # Structural / frozen / manual / defect: no time-based retry.
        row.next_retry_at = None


class MemoryDeferredStore(DeferredStore):
    """In-memory implementation used by the regression tests."""

    def __init__(self):
        self._rows: Dict[Tuple[str, dt.date], DeferredRow] = {}

    def record_deferral(self, symbol, session_date, reason, now=None, *,
                        attempted=True, retry_class=None,
                        next_retry_at=None, last_error_class=None,
                        manual_review_required=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        key = (symbol, session_date)
        row = self._rows.get(key)
        if row is None:
            row = DeferredRow(symbol=symbol, session_date=session_date,
                              reason=reason, status=DEFERRED,
                              first_seen=now, last_attempt=now, attempt_count=0)
            self._rows[key] = row
        row.reason = reason
        if attempted:
            row.last_attempt = now
        # A previously escalated row that is retried and still failing
        # stays escalated; a resolved row reopens as DEFERRED.
        if row.status == RESOLVED:
            row.status = DEFERRED
            row.resolved_at = None
        _apply_retry_state(
            row, reason, now, attempted=attempted, retry_class=retry_class,
            next_retry_at=next_retry_at, last_error_class=last_error_class,
            manual_review_required=manual_review_required)
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
        row.retry_class = None
        row.next_retry_at = None
        row.manual_review_required = False

    def due_candidates(self, now=None, limit=RECOVERY_BATCH,
                       recovery_days=RECOVERY_DAYS, *, provider_available=None,
                       unavailable_keys=None, anchor_override=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        cutoff = now.date() - dt.timedelta(days=recovery_days)
        pool = [r for r in self._rows.values()
                if r.status == DEFERRED and r.session_date >= cutoff]
        eligible = _rp.filter_eligible(pool, now,
                                       provider_available=provider_available,
                                       unavailable_keys=unavailable_keys,
                                       anchor_override=anchor_override)
        return eligible[:limit]

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

    def release_on_condition_change(self, symbol, session_date, *, now=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        row = self._rows.get((symbol, session_date))
        if row is None:
            return False
        if not _rp.manual_reset_allowed(row):
            return False
        row.retry_class = _rp.RETRY_ELIGIBLE
        row.next_retry_at = None
        row.manual_review_required = False
        if row.status == DEFERRED_HISTORY_UNRESOLVED:
            row.status = DEFERRED
        return True


class SqlDeferredStore(DeferredStore):
    """PostgreSQL-backed queue (see migrations/phase_a/001_macro_deferred_history.sql)."""

    def __init__(self, engine):
        self._engine = engine

    _COLS = ("symbol, session_date, reason, status, first_seen, last_attempt, "
             "attempt_count, resolved_at, retry_class, next_retry_at, "
             "last_error_class, manual_review_required")

    @staticmethod
    def _to_row(r) -> DeferredRow:
        return DeferredRow(
            symbol=r[0], session_date=r[1], reason=r[2], status=r[3],
            first_seen=r[4], last_attempt=r[5], attempt_count=r[6],
            resolved_at=r[7], retry_class=r[8], next_retry_at=r[9],
            last_error_class=r[10],
            manual_review_required=bool(r[11]) if len(r) > 11 else False)

    def record_deferral(self, symbol, session_date, reason, now=None, *,
                        attempted=True, retry_class=None,
                        next_retry_at=None, last_error_class=None,
                        manual_review_required=None):
        from sqlalchemy import text
        now = now or dt.datetime.now(dt.timezone.utc)
        if retry_class is None:
            retry_class = _rp.classify_reason(reason)
        if manual_review_required is None:
            manual_review_required = retry_class == _rp.MANUAL_REVIEW_REQUIRED
        inc = 1 if attempted else 0
        # Precedence: keep the stronger of (existing, new) class. The SQL CASE
        # below mirrors retry_policy.resolve_retry_class so an incidental fetch
        # error cannot overwrite a durable structural blocker.
        _rank_sql = """CASE {c}
            WHEN 'EXPECTED_FROZEN' THEN 1
            WHEN 'SOURCE_DEFINITION_UNRESOLVED' THEN 2
            WHEN 'PROVENANCE_UNVERIFIED' THEN 2
            WHEN 'RETRY_PATH_DEFECT' THEN 3
            WHEN 'MANUAL_REVIEW_REQUIRED' THEN 4
            WHEN 'PROVIDER_UNAVAILABLE' THEN 5
            WHEN 'BACKOFF_ACTIVE' THEN 6
            WHEN 'RETRY_ELIGIBLE' THEN 7
            ELSE 99 END"""
        old_rank = _rank_sql.format(c="public.macro_deferred_history.retry_class")
        new_rank = _rank_sql.format(c="EXCLUDED.retry_class")
        keep_new = f"({new_rank}) <= ({old_rank})"
        with self._engine.begin() as conn:
            conn.execute(text(f"""
                INSERT INTO public.macro_deferred_history
                    (symbol, session_date, reason, status,
                     first_seen, last_attempt, attempt_count,
                     retry_class, next_retry_at, last_error_class,
                     manual_review_required)
                VALUES (:symbol, :session_date, :reason, 'DEFERRED',
                        :now, :now, :inc,
                        :retry_class, :next_retry_at, :last_error_class,
                        :manual_review_required)
                ON CONFLICT (symbol, session_date) DO UPDATE SET
                    reason = EXCLUDED.reason,
                    last_attempt = CASE WHEN :inc = 1
                        THEN EXCLUDED.last_attempt
                        ELSE public.macro_deferred_history.last_attempt END,
                    attempt_count = public.macro_deferred_history.attempt_count + :inc,
                    retry_class = CASE
                        WHEN public.macro_deferred_history.retry_class IS NULL
                            THEN EXCLUDED.retry_class
                        WHEN EXCLUDED.retry_class IS NULL
                            THEN public.macro_deferred_history.retry_class
                        WHEN {keep_new}
                            THEN EXCLUDED.retry_class
                        ELSE public.macro_deferred_history.retry_class END,
                    next_retry_at = CASE
                        WHEN public.macro_deferred_history.retry_class IS NULL
                             OR EXCLUDED.retry_class IS NULL
                             OR {keep_new}
                            THEN EXCLUDED.next_retry_at
                        ELSE public.macro_deferred_history.next_retry_at END,
                    last_error_class = EXCLUDED.last_error_class,
                    manual_review_required = CASE
                        WHEN public.macro_deferred_history.retry_class IS NULL
                             OR EXCLUDED.retry_class IS NULL
                             OR {keep_new}
                            THEN EXCLUDED.manual_review_required
                        ELSE public.macro_deferred_history.manual_review_required
                        END,
                    status = CASE
                        WHEN public.macro_deferred_history.status = 'RESOLVED'
                        THEN 'DEFERRED'
                        ELSE public.macro_deferred_history.status END,
                    resolved_at = CASE
                        WHEN public.macro_deferred_history.status = 'RESOLVED'
                        THEN NULL
                        ELSE public.macro_deferred_history.resolved_at END
            """), {"symbol": symbol, "session_date": session_date,
                   "reason": reason, "now": now, "inc": inc,
                   "retry_class": retry_class, "next_retry_at": next_retry_at,
                   "last_error_class": last_error_class,
                   "manual_review_required": manual_review_required})
        return DeferredRow(symbol=symbol, session_date=session_date,
                           reason=reason, first_seen=now, last_attempt=now,
                           attempt_count=inc, retry_class=retry_class,
                           next_retry_at=next_retry_at,
                           last_error_class=last_error_class,
                           manual_review_required=bool(manual_review_required))

    def record_success(self, symbol, session_date, now=None):
        from sqlalchemy import text
        now = now or dt.datetime.now(dt.timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO public.macro_deferred_history
                    (symbol, session_date, reason, status,
                     first_seen, last_attempt, attempt_count, resolved_at,
                     retry_class, next_retry_at, manual_review_required)
                VALUES (:symbol, :session_date, 'RESOLVED', 'RESOLVED',
                        :now, :now, 0, :now, NULL, NULL, false)
                ON CONFLICT (symbol, session_date) DO UPDATE SET
                    status = 'RESOLVED',
                    resolved_at = EXCLUDED.resolved_at,
                    last_attempt = EXCLUDED.last_attempt,
                    retry_class = NULL,
                    next_retry_at = NULL,
                    manual_review_required = false
            """), {"symbol": symbol, "session_date": session_date, "now": now})

    def due_candidates(self, now=None, limit=RECOVERY_BATCH,
                       recovery_days=RECOVERY_DAYS, *, provider_available=None,
                       unavailable_keys=None, anchor_override=None):
        """Rows eligible for a recovery attempt this cycle.

        The provider/class/time/dependency gates are applied in Python via the
        shared retry policy so memory and SQL stores cannot diverge. Only
        non-RESOLVED rows within the retention horizon are loaded.

        Phase C.2: ``anchor_override`` is forwarded to ``filter_eligible``
        so that anchor-covered PU rows can be runtime-eligible without
        changing persisted state.
        """
        from sqlalchemy import text
        now = now or dt.datetime.now(dt.timezone.utc)
        cutoff = now.date() - dt.timedelta(days=recovery_days)
        with self._engine.begin() as conn:
            rows = conn.execute(text(f"""
                SELECT {self._COLS}
                FROM public.macro_deferred_history
                WHERE status <> 'RESOLVED' AND session_date >= :cutoff
                ORDER BY session_date, symbol
            """), {"cutoff": cutoff}).fetchall()
        pool = [self._to_row(r) for r in rows]
        eligible = _rp.filter_eligible(pool, now,
                                       provider_available=provider_available,
                                       unavailable_keys=unavailable_keys,
                                       anchor_override=anchor_override)
        return eligible[:limit]

    def all_rows(self):
        from sqlalchemy import text
        with self._engine.begin() as conn:
            rows = conn.execute(text(f"""
                SELECT {self._COLS}
                FROM public.macro_deferred_history
                ORDER BY session_date, symbol
            """)).fetchall()
        return [self._to_row(r) for r in rows]

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

    def release_on_condition_change(self, symbol, session_date, *, now=None):
        from sqlalchemy import text
        now = now or dt.datetime.now(dt.timezone.utc)
        with self._engine.begin() as conn:
            res = conn.execute(text("""
                UPDATE public.macro_deferred_history
                   SET retry_class = 'RETRY_ELIGIBLE',
                       next_retry_at = NULL,
                       manual_review_required = false,
                       status = CASE WHEN status = 'DEFERRED_HISTORY_UNRESOLVED'
                                     THEN 'DEFERRED' ELSE status END
                 WHERE symbol = :symbol AND session_date = :session_date
                   AND status <> 'RESOLVED'
                   AND retry_class IN ('SOURCE_DEFINITION_UNRESOLVED',
                                       'PROVENANCE_UNVERIFIED',
                                       'MANUAL_REVIEW_REQUIRED',
                                       'PROVIDER_UNAVAILABLE')
            """), {"symbol": symbol, "session_date": session_date})
            return res.rowcount > 0


# ---------------------------------------------------------------------
# Provider-level backoff store (one row per provider)
# ---------------------------------------------------------------------
class ProviderBackoffStore:
    """Tracks provider-level availability keyed by (provider, data_class) so N
    symbols sharing one broken endpoint family do not each issue a redundant
    request -- while a *different* endpoint family of the same provider keeps
    working (a provider-wide key would be too coarse)."""

    def __init__(self, engine):
        self._engine = engine

    def is_available(self, provider: str, data_class: str = "",
                     now=None) -> bool:
        from sqlalchemy import text
        now = now or dt.datetime.now(dt.timezone.utc)
        with self._engine.begin() as conn:
            row = conn.execute(text("""
                SELECT unavailable_until, state
                  FROM public.macro_provider_backoff
                 WHERE provider = :p AND data_class = :dc
            """), {"p": provider, "dc": data_class}).fetchone()
        if row is None:
            return True
        unavailable_until, state = row[0], row[1]
        if state == "MANUAL_REVIEW_REQUIRED":
            return False
        if unavailable_until is None:
            return True
        return unavailable_until <= now

    def unavailable_keys(self, now=None):
        """Set of (provider, data_class) currently unavailable."""
        from sqlalchemy import text
        now = now or dt.datetime.now(dt.timezone.utc)
        with self._engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT provider, data_class, unavailable_until, state
                  FROM public.macro_provider_backoff
            """)).fetchall()
        keys = set()
        for provider, dc, until, state in rows:
            if state == "MANUAL_REVIEW_REQUIRED":
                keys.add((provider, dc))
            elif until is not None and until > now:
                keys.add((provider, dc))
        return keys

    def record_failure(self, provider: str, data_class: str, error: str, *,
                       now=None, http_status=None) -> str:
        """Record a provider failure; return the resulting state.

        Access denials (403/429/401) escalate to MANUAL_REVIEW_REQUIRED after
        PROVIDER_ACCESS_ESCALATION consecutive failures -- a persistent 403 is
        an access/policy problem, not a glitch, and must not be retried on a
        fixed window forever. Historical source definition is never changed.
        """
        from sqlalchemy import text
        now = now or dt.datetime.now(dt.timezone.utc)
        access = _rp.is_access_denial(error)
        with self._engine.begin() as conn:
            cur = conn.execute(text("""
                SELECT consecutive_failures FROM public.macro_provider_backoff
                 WHERE provider = :p AND data_class = :dc
            """), {"p": provider, "dc": data_class}).fetchone()
            consec = (cur[0] if cur else 0) + 1
            if access and consec >= _rp.PROVIDER_ACCESS_ESCALATION:
                state = "MANUAL_REVIEW_REQUIRED"
                until = None
            else:
                state = "PROVIDER_UNAVAILABLE"
                until = now + dt.timedelta(seconds=_rp.provider_backoff_seconds(
                    consec, access_denial=access))
            conn.execute(text("""
                INSERT INTO public.macro_provider_backoff
                    (provider, data_class, state, consecutive_failures,
                     last_failure_at, unavailable_until, last_error_class,
                     last_http_status, updated_at)
                VALUES (:p, :dc, :state, :consec, :now, :until, :err, :http, :now)
                ON CONFLICT (provider, data_class) DO UPDATE SET
                    state = EXCLUDED.state,
                    consecutive_failures = EXCLUDED.consecutive_failures,
                    last_failure_at = EXCLUDED.last_failure_at,
                    unavailable_until = EXCLUDED.unavailable_until,
                    last_error_class = EXCLUDED.last_error_class,
                    last_http_status = EXCLUDED.last_http_status,
                    updated_at = EXCLUDED.updated_at
            """), {"p": provider, "dc": data_class, "state": state,
                   "consec": consec, "now": now, "until": until,
                   "err": ("ACCESS_DENIAL" if access else "TRANSIENT"),
                   "http": http_status})
        return state

    def clear(self, provider: str, data_class: str = "", now=None) -> None:
        """A confirmed success clears this endpoint family's backoff."""
        from sqlalchemy import text
        now = now or dt.datetime.now(dt.timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO public.macro_provider_backoff
                    (provider, data_class, state, consecutive_failures,
                     unavailable_until, recovered_at, updated_at)
                VALUES (:p, :dc, 'AVAILABLE', 0, NULL, :now, :now)
                ON CONFLICT (provider, data_class) DO UPDATE SET
                    state = 'AVAILABLE',
                    consecutive_failures = 0,
                    unavailable_until = NULL,
                    recovered_at = EXCLUDED.recovered_at,
                    updated_at = EXCLUDED.updated_at
            """), {"p": provider, "dc": data_class, "now": now})
