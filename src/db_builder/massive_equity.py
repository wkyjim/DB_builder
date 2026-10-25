"""Massive (Polygon) grouped-daily equity fetcher.

Replaces the Eastmoney clist paginated fetch for the daily EOD snapshot.

Why this exists
---------------
Eastmoney's ``/api/qt/clist/get`` returns the same universe but needs ~114
sequential paginated calls (``pz`` is capped at 100) and that endpoint flaps:
it returned nginx 502 from every host (``push2delay``, ``62.push2delay``,
``push2``, ``push2his`` and the numbered shards) from three independent
networks while sibling paths stayed up. The Massive grouped-daily endpoint
returns the whole US market in a single request.

What this endpoint gives us
--------------------------
``GET /v2/aggs/grouped/locale/us/market/stocks/{date}``

One call, no pagination, ~12.5k rows, with OHLCV plus VWAP and trade count.
Fields::

    {"T":"DPZ","v":443805.06,"vw":313.46,"o":314.88,"c":313.61,
     "h":317.26,"l":311.6,"t":1792785600000,"n":19794}

Coverage against our local 2026-10-23 session (11,309 tickers):
10,992 covered (97.2%). The 317 gaps are warrants, units, preferreds and
option contracts -- exactly the classes the indicator pipeline excludes.

Rate limit
----------
429 if called more than once per minute. ``MIN_REQUEST_INTERVAL`` enforces the
13s spacing the API needs, and ``_throttle`` keeps that spacing process-wide so
concurrent callers cannot trip it.

Not available
-------------
``/v2/snapshot/locale/us/markets/stocks/tickers`` is the one-call
today+prevDay endpoint, but this key is not entitled to it (403
NOT_AUTHORIZED, "upgrade your plan"). Grouped-daily is one call per date, so
a today+prevDay pull is two calls spaced by the rate limit.
"""

from __future__ import annotations

import os
import time
from datetime import date as date_cls
from datetime import timedelta

import pandas as pd
import requests

from db_builder.config import RAW_TABLE

# 429 observed on back-to-back calls; 13s spacing is what the API tolerates.
MIN_REQUEST_INTERVAL = 13.0
REQUEST_TIMEOUT = 60

GROUPED_DAILY_URL = (
    "https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/{date}"
)

# Massive aggregated-bar field -> our us_equities column.
# `vw` (VWAP) and `n` (trade count) have no column in us_equities and are
# dropped; `t` is the bar timestamp and is validated, not stored.
FIELD_MAP = {
    "T": "ticker",
    "o": "open",
    "h": "high",
    "l": "low",
    "c": "close",
    "v": "volume",
}

_last_request_at = 0.0


def _api_key() -> str:
    key = os.getenv("MASSIVE_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "MASSIVE_API_KEY is not set; grouped-daily fetch is unavailable"
        )
    return key


def _throttle() -> None:
    """Enforce MIN_REQUEST_INTERVAL across every caller in this process."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def parse_grouped_bar(record: dict) -> dict | None:
    """Convert one Massive aggregated bar to a us_equities-shaped row."""
    ticker = record.get("T")
    if not ticker:
        return None
    row = {target: record.get(src) for src, target in FIELD_MAP.items()}
    row["ticker"] = str(row["ticker"]).upper()
    if row.get("close") is None:
        return None
    return row


def fetch_grouped_daily(
    day: str | date_cls,
    *,
    api_key: str | None = None,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """Fetch one session's full US market snapshot.

    Returns a frame shaped like us_equities minus the derived columns
    (name, prev_close, mkt_cap, pe_ttm, ytd_pct_chg), which the caller fills.
    """
    day_text = day.isoformat() if isinstance(day, date_cls) else str(day)
    url = GROUPED_DAILY_URL.format(date=day_text)

    _throttle()
    client = session or requests
    resp = client.get(
        url,
        params={"apiKey": api_key or _api_key(), "adjusted": "true"},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code == 429:
        raise RuntimeError(
            f"Massive grouped-daily rate limited for {day_text}; "
            f"wait {MIN_REQUEST_INTERVAL:.0f}s before retrying"
        )
    resp.raise_for_status()

    payload = resp.json()
    results = payload.get("results") or []
    if not results:
        raise RuntimeError(f"Massive grouped-daily returned no rows for {day_text}")

    rows = [parse_grouped_bar(rec) for rec in results]
    frame = pd.DataFrame([row for row in rows if row])
    if frame.empty:
        raise RuntimeError(f"Massive grouped-daily returned no usable rows for {day_text}")

    frame["date"] = pd.Timestamp(day_text).date()
    frame = frame.drop_duplicates(subset=["date", "ticker"], keep="first")
    return frame


def fetch_grouped_range(
    days: list[str] | list[date_cls],
    *,
    api_key: str | None = None,
) -> dict[str, pd.DataFrame]:
    """Fetch several sessions, honouring the one-call-per-minute rate limit."""
    out: dict[str, pd.DataFrame] = {}
    client = requests.Session()
    try:
        for day in days:
            day_text = day.isoformat() if isinstance(day, date_cls) else str(day)
            out[day_text] = fetch_grouped_daily(day_text, api_key=api_key, session=client)
    finally:
        client.close()
    return out


def grouped_bar_timestamp(record: dict) -> int | None:
    """Return the bar's millisecond epoch timestamp, if present."""
    value = record.get("t")
    return int(value) if value else None


def _prior_session(target) -> str:
    """Return the NYSE session immediately before ``target``."""
    import pandas_market_calendars as mcal

    target_date = (
        target if isinstance(target, date_cls) else pd.Timestamp(target).date()
    )
    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=target_date - timedelta(days=14),
        end_date=target_date,
    )
    prior = [d.date() for d in schedule.index if d.date() < target_date]
    if not prior:
        raise RuntimeError(f"No NYSE session precedes {target_date}")
    return max(prior).isoformat()


def build_session_frame(
    target_date: str | date_cls,
    *,
    api_key: str | None = None,
    include_prior: bool = True,
) -> pd.DataFrame:
    """Build a us_equities-shaped frame for one session from Massive.

    Fetches the target session's grouped daily bars, plus the prior session's
    when ``include_prior`` — the prior close is what ``prev_close`` and
    ``pct_chg`` derive from. Two calls, spaced by the rate limit.

    Columns not supplied by Massive (name, mkt_cap, pe_ttm, ytd_pct_chg) are
    left for the caller: ``replace_ytd_pct_chg_with_local_calculation`` fills
    YTD from the local prior session, and the rest stay NULL.
    """
    day_text = (
        target_date.isoformat() if isinstance(target_date, date_cls) else str(target_date)
    )
    today = fetch_grouped_daily(day_text, api_key=api_key)

    frame = today.copy()
    if include_prior:
        prev_day = _prior_session(day_text)
        prev = fetch_grouped_daily(prev_day, api_key=api_key)
        prev_close = prev[["ticker", "close"]].rename(columns={"close": "prev_close"})
        frame = frame.merge(prev_close, on="ticker", how="left")
        close = pd.to_numeric(frame["close"], errors="coerce")
        prior_close = pd.to_numeric(frame["prev_close"], errors="coerce")
        frame["pct_chg"] = ((close / prior_close) - 1.0) * 100.0
    else:
        frame["pct_chg"] = None

    frame["name"] = None
    return frame


def fetch_and_save_session(
    engine,
    target_date,
    *,
    table_name: str = RAW_TABLE,
    dry_run: bool = False,
    api_key: str | None = None,
    allow_non_trading_day: bool = False,
) -> dict:
    """Fetch one session's full market from Massive and upsert it.

    Mirrors the Eastmoney path's post-processing so downstream indicator and
    Neon-sync steps are provider-agnostic: trading-day rejection, local
    rolling YTD, then coverage-checked yfinance fill for the tickers Massive
    does not carry (warrants, units, preferreds, option contracts).
    """
    from db_builder.eastmoney import (
        MIN_FETCH_COVERAGE_RATIO,
        _load_target_table,
        _upsert_dataframe,
        replace_ytd_pct_chg_with_local_calculation,
    )
    from db_builder.trading_calendar import reject_non_trading_dates
    from db_builder.yfinance_equity_fallback import (
        fetch_missing_session_rows,
        fetch_prior_session_universe,
    )

    day_text = (
        target_date.isoformat() if isinstance(target_date, date_cls) else str(target_date)
    )
    print(f"[MASSIVE] Fetching grouped daily bars for {day_text}")
    frame = build_session_frame(day_text, api_key=api_key)
    fetched_tickers = set(frame["ticker"].astype(str))

    frame = reject_non_trading_dates(
        frame, allow_non_trading_day=allow_non_trading_day
    )
    frame = replace_ytd_pct_chg_with_local_calculation(
        engine, frame, table_name, target_date=day_text
    )

    if dry_run:
        print(f"[MASSIVE][dry-run] would upsert {len(frame):,} rows")
        print(
            frame[["date", "ticker", "close", "volume", "pct_chg", "ytd_pct_chg"]]
            .head(5)
            .to_string(index=False)
        )
        return {"tickers": len(frame), "fallback_recovered": 0, "coverage": 1.0}

    _upsert_dataframe(engine, _load_target_table(engine, table_name), frame)
    print(f"[MASSIVE] upserted {len(frame):,} rows for {day_text}")

    prior_universe = fetch_prior_session_universe(engine, day_text, table_name)
    expected_tickers = set(prior_universe["ticker"].astype(str).str.upper())
    unresolved = expected_tickers - fetched_tickers
    fallback_found: set[str] = set()
    if unresolved:
        print(
            f"[MASSIVE YF FALLBACK] unresolved_prior_session_tickers={len(unresolved):,}",
            flush=True,
        )
        try:
            fallback = fetch_missing_session_rows(
                prior_universe,
                unresolved,
                day_text,
                allow_non_trading_day=allow_non_trading_day,
            )
            if not fallback.frame.empty:
                fallback_frame = replace_ytd_pct_chg_with_local_calculation(
                    engine, fallback.frame, table_name, target_date=day_text
                )
                _upsert_dataframe(
                    engine, _load_target_table(engine, table_name), fallback_frame
                )
            fallback_found.update(fallback.found)
            print(
                f"[MASSIVE YF FALLBACK SUMMARY] requested={len(fallback.requested):,} "
                f"recovered={len(fallback.found):,} missing={len(fallback.missing):,}",
                flush=True,
            )
        except Exception as exc:
            print(f"[MASSIVE YF FALLBACK ERROR] {exc}", flush=True)

    combined = fetched_tickers | fallback_found
    denominator = max(len(expected_tickers), len(fetched_tickers))
    coverage = len(combined) / denominator if denominator else 0.0
    print(
        f"[MASSIVE FETCH SUMMARY] massive={len(fetched_tickers):,} "
        f"fallback_recovered={len(fallback_found):,} "
        f"combined={len(combined):,}/{denominator:,} coverage={coverage:.2%}"
    )
    if coverage < MIN_FETCH_COVERAGE_RATIO:
        raise RuntimeError(
            f"Massive fetch coverage {coverage:.2%} is below "
            f"required {MIN_FETCH_COVERAGE_RATIO:.0%}"
        )
    return {
        "tickers": len(fetched_tickers),
        "fallback_recovered": len(fallback_found),
        "coverage": coverage,
    }


__all__ = [
    "MIN_REQUEST_INTERVAL",
    "build_session_frame",
    "fetch_and_save_session",
    "fetch_grouped_daily",
    "fetch_grouped_range",
    "grouped_bar_timestamp",
    "parse_grouped_bar",
]
