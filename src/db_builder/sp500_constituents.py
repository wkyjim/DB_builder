"""S&P 500 membership history and current constituent refresh.

The current list and the historical change log are both read from Wikipedia.
Membership is stored as half-open intervals ``[start_date, end_date)`` so a
breadth calculation can ask "who was in the index on date D?" without using
today's membership for a historical date.
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

import pandas as pd
import requests
from sqlalchemy import text

CURRENT_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
HISTORY_URL = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"
SOURCE = "wikipedia"
UNKNOWN_START = date(1900, 1, 1)


@dataclass(frozen=True)
class Member:
    ticker: str
    company_name: str | None
    start_date: date
    end_date: date | None
    reason: str | None = None
    sector: str | None = None
    industry: str | None = None
    cik: str | None = None


def _get(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": "db-builder/1.0"}, timeout=45)
    resp.raise_for_status()
    return resp.text


def fetch_current() -> list[dict]:
    tables = pd.read_html(io.StringIO(_get(CURRENT_URL)))
    df = next(t for t in tables if "Symbol" in t.columns and "Security" in t.columns)
    out = []
    for _, row in df.iterrows():
        ticker = str(row["Symbol"]).strip().upper()
        if not ticker or ticker == "NAN":
            continue
        added = pd.to_datetime(row.get("Date added"), errors="coerce")
        out.append({
            "ticker": ticker,
            "company_name": str(row.get("Security") or "").strip() or None,
            "sector": str(row.get("GICS Sector") or "").strip() or None,
            "industry": str(row.get("GICS Sub-Industry") or "").strip() or None,
            "cik": str(row.get("CIK") or "").strip() or None,
            "date_added": None if pd.isna(added) else added.date(),
        })
    return out


def fetch_changes() -> list[dict]:
    tables = pd.read_html(io.StringIO(_get(HISTORY_URL)))
    df = next(t for t in tables if t.shape[1] >= 6 and t.shape[0] > 100)
    df = df.copy()
    df.columns = ["date", "add_ticker", "add_security", "remove_ticker", "remove_security", "reason", "refs"][: len(df.columns)]
    out = []
    for _, row in df.iterrows():
        dt = pd.to_datetime(row["date"], errors="coerce")
        if pd.isna(dt):
            continue
        for side, ticker_col, name_col in (
            ("add", "add_ticker", "add_security"),
            ("remove", "remove_ticker", "remove_security"),
        ):
            ticker = str(row.get(ticker_col) or "").strip().upper()
            if not ticker or ticker == "NAN":
                continue
            out.append({
                "date": dt.date(),
                "action": side,
                "ticker": ticker,
                "company_name": str(row.get(name_col) or "").strip() or None,
                "reason": str(row.get("reason") or "").strip() or None,
            })
    out.sort(key=lambda e: (e["date"], 0 if e["action"] == "remove" else 1))
    return out


def build_intervals(current: list[dict], changes: list[dict], today: date | None = None) -> list[Member]:
    today = today or date.today()
    current_by_ticker = {c["ticker"]: c for c in current}
    intervals: dict[str, list[list]] = {}
    open_start: dict[str, date] = {}

    def close(ticker: str, end: date) -> None:
        start = open_start.pop(ticker, None)
        if start is None:
            start = UNKNOWN_START
        if start >= end:
            return
        bucket = intervals.setdefault(ticker, [])
        if start == UNKNOWN_START:
            existing = next((span for span in bucket if span[0] == UNKNOWN_START), None)
            if existing is not None:
                existing[1] = max(existing[1], end)
                return
        bucket.append([start, end])

    for event in changes:
        ticker = event["ticker"]
        if event["action"] == "remove":
            close(ticker, event["date"])
        else:
            if ticker in open_start:
                close(ticker, event["date"])
            open_start[ticker] = event["date"]

    for ticker, item in current_by_ticker.items():
        start = open_start.get(ticker)
        if start is None:
            # Current Wikipedia "Date added" is the best available start when
            # the ticker predates the change log.
            start = item.get("date_added") or UNKNOWN_START
        intervals.setdefault(ticker, []).append([start, None])
        open_start.pop(ticker, None)

    # A ticker still open but absent from the current list is a stale/missing
    # removal row. Close it at the last observed change date rather than
    # silently treating it as a current constituent.
    last_event = max((e["date"] for e in changes), default=today)
    for ticker in list(open_start):
        close(ticker, last_event)

    rows: list[Member] = []
    seen_start: set[tuple[str, date]] = set()
    for ticker, spans in intervals.items():
        spans.sort()
        for start, end in spans:
            if end is not None and end <= start:
                continue
            if (ticker, start) in seen_start:
                continue
            seen_start.add((ticker, start))
            if rows and rows[-1].ticker == ticker and rows[-1].end_date is None:
                # Keep one open interval only.
                continue
            info = current_by_ticker.get(ticker, {})
            rows.append(Member(
                ticker=ticker,
                company_name=info.get("company_name"),
                start_date=start,
                end_date=end,
                reason=next((e["reason"] for e in changes if e["ticker"] == ticker and e["date"] == end), None),
                sector=info.get("sector"),
                industry=info.get("industry"),
                cik=info.get("cik"),
            ))
    return rows


def setup_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS public.sp500_membership_history (
                ticker text NOT NULL,
                company_name text,
                start_date date NOT NULL,
                end_date date,
                reason text,
                source text NOT NULL DEFAULT 'wikipedia',
                source_url text,
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (ticker, start_date)
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS sp500_membership_lookup_idx
            ON public.sp500_membership_history (ticker, start_date, end_date)
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS public.sp500_sync_state (
                run_date date PRIMARY KEY,
                finished_at timestamptz NOT NULL DEFAULT now(),
                rows_written integer NOT NULL DEFAULT 0
            )
        """))
        conn.execute(text("""
            ALTER TABLE public.security_classification
            ADD COLUMN IF NOT EXISTS sp500_added_date date
        """))
        conn.execute(text("""
            ALTER TABLE public.security_classification
            ADD COLUMN IF NOT EXISTS sp500_removed_date date
        """))


def replace_history(engine, members: list[Member]) -> int:
    rows = [{
        "ticker": m.ticker, "company_name": m.company_name,
        "start_date": m.start_date, "end_date": m.end_date, "reason": m.reason,
        "source": SOURCE, "source_url": HISTORY_URL,
    } for m in members]
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM public.sp500_membership_history WHERE source = :s"), {"s": SOURCE})
        if rows:
            conn.execute(text("""
                INSERT INTO public.sp500_membership_history
                    (ticker, company_name, start_date, end_date, reason, source, source_url)
                VALUES (:ticker, :company_name, :start_date, :end_date, :reason, :source, :source_url)
            """), rows)
    return len(rows)


def sync_classification(engine, current: list[dict], members: list[Member], today: date | None = None) -> None:
    today = today or date.today()
    current_set = {c["ticker"] for c in current}
    added = {m.ticker: m.start_date for m in members if m.end_date is None and m.ticker in current_set}
    removed = {
        m.ticker: (m.start_date, m.end_date, m.company_name)
        for m in members
        if m.end_date is not None and m.ticker not in current_set
    }
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE public.security_classification
            SET is_sp500 = false, is_active = false
            WHERE is_sp500 = true
        """))
        for item in current:
            conn.execute(text("""
                INSERT INTO public.security_classification
                    (ticker, company_name, sector, industry, source, is_sp500, is_active,
                     date_added, sp500_added_date, sp500_removed_date, cik, updated_at)
                VALUES (:ticker, :company_name, :sector, :industry, 'wikipedia', true, true,
                        :start, :start, NULL, :cik, now())
                ON CONFLICT (ticker) DO UPDATE SET
                    is_sp500 = true, is_active = true,
                    date_added = COALESCE(EXCLUDED.date_added, public.security_classification.date_added),
                    sp500_added_date = EXCLUDED.sp500_added_date,
                    sp500_removed_date = NULL,
                    updated_at = now()
            """), {**item, "start": added.get(item["ticker"]) or item.get("date_added") or UNKNOWN_START})
        for ticker, (start, end, company) in removed.items():
            conn.execute(text("""
                INSERT INTO public.security_classification
                    (ticker, company_name, source, is_sp500, is_active,
                     sp500_added_date, sp500_removed_date, updated_at)
                VALUES (:ticker, :company, 'wikipedia', false, false, :start, :end, now())
                ON CONFLICT (ticker) DO UPDATE SET
                    is_sp500 = false, is_active = false,
                    sp500_added_date = COALESCE(security_classification.sp500_added_date, EXCLUDED.sp500_added_date),
                    sp500_removed_date = EXCLUDED.sp500_removed_date,
                    updated_at = now()
            """), {"ticker": ticker, "start": start, "end": end, "company": company})


def weekly_due(engine, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() != 5:  # Saturday
        return False
    with engine.connect() as conn:
        last = conn.execute(text("SELECT max(run_date) FROM public.sp500_sync_state")).scalar()
    return last != now.date()


def run(engine, *, weekly: bool = False, force: bool = False, dry_run: bool = False) -> dict:
    setup_schema(engine)
    if weekly and not force and not weekly_due(engine):
        return {"status": "skipped", "reason": "not Saturday or already ran this week"}
    current = fetch_current()
    changes = fetch_changes()
    members = build_intervals(current, changes)
    if dry_run:
        return {"status": "dry_run", "current": len(current), "changes": len(changes), "intervals": len(members)}
    n = replace_history(engine, members)
    sync_classification(engine, current, members)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO public.sp500_sync_state (run_date, rows_written)
            VALUES (:d, :n)
            ON CONFLICT (run_date) DO UPDATE SET finished_at = now(), rows_written = EXCLUDED.rows_written
        """), {"d": date.today(), "n": n})
    return {"status": "ok", "current": len(current), "changes": len(changes), "intervals": n}
