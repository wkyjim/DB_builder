"""NYSE session helpers for AEGIS shadow timing.

All schedule timestamps are timezone-aware UTC.  The cron expression is in
Asia/Hong_Kong; exchange timing follows the NYSE calendar, including US DST
and holidays.
"""
from __future__ import annotations
from datetime import date, datetime, timezone
import pandas as pd
import pandas_market_calendars as mcal

NYSE = mcal.get_calendar("NYSE")


def next_sessions(as_of: date, n: int) -> list[date]:
    start = pd.Timestamp(as_of) + pd.Timedelta(days=1)
    end = start + pd.Timedelta(days=max(45, n * 3))
    sched = NYSE.schedule(start_date=start, end_date=end)
    return [pd.Timestamp(d).date() for d in sched.index[:n]]


def session_open_utc(day: date) -> datetime:
    sched = NYSE.schedule(start_date=pd.Timestamp(day), end_date=pd.Timestamp(day))
    if sched.empty:
        raise ValueError(f"not an NYSE session: {day}")
    return sched.iloc[0]["market_open"].to_pydatetime().astimezone(timezone.utc)


def last_completed_session(now_utc: datetime | None = None) -> date:
    now_utc = now_utc or datetime.now(timezone.utc)
    # Require the close plus one hour for vendor data availability.
    sched = NYSE.schedule(start_date=pd.Timestamp(now_utc.date()) - pd.Timedelta(days=14),
                          end_date=pd.Timestamp(now_utc.date()))
    for day, row in sched.iloc[::-1].iterrows():
        close = row["market_close"].to_pydatetime().astimezone(timezone.utc)
        if now_utc >= close + pd.Timedelta(hours=1):
            return pd.Timestamp(day).date()
    raise RuntimeError("no completed NYSE session in lookback")
