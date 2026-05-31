"""NYSE trading-session validation helpers."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pandas_market_calendars as mcal
from sqlalchemy import text

from db_builder.config import RAW_TABLE


NYSE = mcal.get_calendar("NYSE")


def _as_date(value) -> date:
    return pd.to_datetime(value).date()


def is_valid_nyse_session(session_date) -> bool:
    session = _as_date(session_date)
    schedule = NYSE.schedule(start_date=session, end_date=session)
    return not schedule.empty


def latest_completed_nyse_session_date(now: datetime | None = None) -> date:
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)

    end_date = now_utc.date()
    start_date = end_date - timedelta(days=14)
    schedule = NYSE.schedule(start_date=start_date, end_date=end_date)
    if schedule.empty:
        raise RuntimeError("Could not determine latest completed NYSE session")

    market_close_utc = schedule["market_close"].dt.tz_convert(timezone.utc)
    completed = schedule[market_close_utc <= pd.Timestamp(now_utc)]
    if completed.empty:
        raise RuntimeError("No completed NYSE sessions found in lookback window")

    return completed.index[-1].date()


def reject_non_trading_dates(
    df: pd.DataFrame,
    *,
    date_column: str = "date",
    allow_non_trading_day: bool = False,
) -> pd.DataFrame:
    if df.empty or date_column not in df.columns:
        return df

    result = df.copy()
    result[date_column] = pd.to_datetime(result[date_column]).dt.date

    invalid_dates = sorted(
        {
            value
            for value in result[date_column].dropna().unique()
            if not is_valid_nyse_session(value)
        }
    )

    if invalid_dates and not allow_non_trading_day:
        raise ValueError(f"Non-NYSE trading dates rejected: {invalid_dates}")

    if invalid_dates:
        print(f"[ALLOW] Non-NYSE trading dates present: {invalid_dates}")

    return result


def filter_valid_trading_dates(
    df: pd.DataFrame,
    *,
    date_column: str = "date",
    allow_non_trading_day: bool = False,
) -> pd.DataFrame:
    if allow_non_trading_day or df.empty or date_column not in df.columns:
        return df

    result = df.copy()
    result[date_column] = pd.to_datetime(result[date_column]).dt.date
    keep_mask = result[date_column].map(is_valid_nyse_session)
    dropped = len(result) - int(keep_mask.sum())

    if dropped:
        print(f"Filtered {dropped:,} non-NYSE-session rows before processing.")

    return result[keep_mask].copy()


def fetch_max_equity_date(engine, raw_table: str = RAW_TABLE):
    sql = text(f"SELECT MAX(date) AS max_date FROM {raw_table}")
    with engine.begin() as conn:
        row = conn.execute(sql).fetchone()

    if row is None or row.max_date is None:
        return None

    return _as_date(row.max_date)


def should_skip_for_latest_session(max_date, latest_session_date) -> bool:
    if max_date is None:
        return False

    return _as_date(max_date) >= _as_date(latest_session_date)


def database_has_latest_session(engine, raw_table: str = RAW_TABLE) -> tuple[bool, date | None, date]:
    latest_session = latest_completed_nyse_session_date()
    max_date = fetch_max_equity_date(engine, raw_table)
    return should_skip_for_latest_session(max_date, latest_session), max_date, latest_session

