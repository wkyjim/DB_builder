"""FINRA consolidated daily short-sale volume ingestion.

The daily file measures reported short-sale transaction activity. It is not
short interest and is never accumulated as an estimate of outstanding shorts.
"""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from typing import Callable, Iterable

import numpy as np
import pandas as pd
from sqlalchemy import bindparam, text

from db_builder.flow_sources import finra_short_volume_url, http_session, recent_business_dates, record_flow_source_health
from db_builder.short_analytics_config import load_short_analytics_config


LOGGER = logging.getLogger(__name__)
Progress = Callable[[str], None]


@dataclass
class DailyFetchResult:
    rows: list[dict] = field(default_factory=list)
    successful_dates: list[date] = field(default_factory=list)
    missing_dates: list[date] = field(default_factory=list)
    failed_dates: dict[date, str] = field(default_factory=dict)
    skipped_dates: list[date] = field(default_factory=list)


def setup_finra_short_volume_schema(engine) -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS public.finra_short_volume (
            trade_date date NOT NULL,
            ticker text NOT NULL,
            short_volume numeric,
            short_exempt_volume numeric,
            total_volume numeric,
            short_volume_ratio numeric,
            short_volume_z_60d numeric,
            short_pressure_flag boolean DEFAULT false,
            short_covering_candidate boolean DEFAULT false,
            bearish_pressure_flag boolean DEFAULT false,
            market text,
            raw_symbol text,
            normalized_ticker text,
            source_file text,
            fetched_at timestamptz,
            data_quality_status text DEFAULT 'valid',
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now(),
            PRIMARY KEY (trade_date, ticker)
        )
        """,
        "ALTER TABLE public.finra_short_volume ADD COLUMN IF NOT EXISTS source_file text",
        "ALTER TABLE public.finra_short_volume ADD COLUMN IF NOT EXISTS fetched_at timestamptz",
        "ALTER TABLE public.finra_short_volume ADD COLUMN IF NOT EXISTS data_quality_status text DEFAULT 'valid'",
        "ALTER TABLE public.finra_short_volume ADD COLUMN IF NOT EXISTS raw_symbol text",
        "ALTER TABLE public.finra_short_volume ADD COLUMN IF NOT EXISTS normalized_ticker text",
        "CREATE INDEX IF NOT EXISTS idx_finra_short_volume_ticker_date ON public.finra_short_volume (ticker, trade_date)",
        """
        CREATE TABLE IF NOT EXISTS public.finra_ingestion_runs (
            source_type text NOT NULL,
            source_date date NOT NULL,
            source_file text,
            status text NOT NULL,
            row_count integer,
            error_message text,
            fetched_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (source_type, source_date)
        )
        """,
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def _raw_symbol(value: object) -> str:
    return str(value or "").strip().replace(" ", "")


def _normalized_ticker(raw_symbol: str) -> str | None:
    # FINRA mixed-case symbology distinguishes preferreds, rights, and other
    # variants. Only already-uppercase symbols are safe common ticker keys.
    return raw_symbol if raw_symbol and raw_symbol == raw_symbol.upper() else None


def parse_finra_short_volume_text(
    text_body: str,
    *,
    market: str = "CNMS",
    source_file: str | None = None,
    fetched_at: datetime | None = None,
    minimum_rows: int = 1,
) -> list[dict]:
    if not text_body or len(text_body.strip()) < 10:
        raise ValueError("FINRA daily file is empty or unexpectedly small")
    try:
        df = pd.read_csv(io.StringIO(text_body), sep="|", dtype=str)
    except Exception as exc:
        raise ValueError("FINRA daily file is not valid pipe-delimited text") from exc
    required = {"Date", "Symbol", "ShortVolume", "ShortExemptVolume", "TotalVolume", "Market"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing FINRA columns: {sorted(missing)}")
    if len(df) < minimum_rows:
        raise ValueError(f"FINRA daily file has {len(df):,} rows; expected at least {minimum_rows:,}")

    footer_mask = (
        df["Symbol"].isna()
        & df[["ShortVolume", "ShortExemptVolume", "TotalVolume", "Market"]].isna().all(axis=1)
        & df["Date"].astype(str).str.fullmatch(r"\d+")
        & df["Date"].astype(str).str.len().lt(8)
    )
    footer_counts = pd.to_numeric(df.loc[footer_mask, "Date"], errors="coerce").dropna()
    if not footer_counts.empty and int(footer_counts.iloc[-1]) != len(df) - int(footer_mask.sum()):
        raise ValueError("FINRA daily footer row count does not match parsed source rows")
    df = df.loc[~footer_mask].copy()
    df["Date"] = pd.to_datetime(df["Date"], format="%Y%m%d", errors="coerce")
    for column in ("ShortVolume", "ShortExemptVolume", "TotalVolume"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["raw_symbol"] = df["Symbol"].map(_raw_symbol)
    df["ticker"] = df["raw_symbol"]
    df["normalized_ticker"] = df["raw_symbol"].map(_normalized_ticker)
    valid = (
        df["Date"].notna()
        & df["ticker"].ne("")
        & df["ShortVolume"].notna()
        & df["TotalVolume"].notna()
        & df["ShortVolume"].ge(0)
        & df["TotalVolume"].gt(0)
    )
    rejected = int((~valid).sum())
    df = df.loc[valid].copy()
    if df.empty:
        raise ValueError("FINRA daily file contains no valid data rows")
    if df["Date"].dt.date.nunique() != 1:
        raise ValueError("FINRA daily file contains multiple trade dates")

    df["short_volume_ratio"] = df["ShortVolume"] / df["TotalVolume"]
    maximum_ratio = float(load_short_analytics_config()["quality"]["maximum_short_volume_ratio"])
    df["data_quality_status"] = np.where(df["short_volume_ratio"] > maximum_ratio, "ratio_outlier", "valid")
    timestamp = fetched_at or datetime.now(timezone.utc)
    rows = []
    for row in df.itertuples(index=False):
        rows.append(
            {
                "trade_date": row.Date.date(),
                "ticker": row.ticker,
                "raw_symbol": row.raw_symbol,
                "normalized_ticker": None if pd.isna(row.normalized_ticker) else row.normalized_ticker,
                "short_volume": float(row.ShortVolume),
                "short_exempt_volume": None if pd.isna(row.ShortExemptVolume) else float(row.ShortExemptVolume),
                "total_volume": float(row.TotalVolume),
                "short_volume_ratio": float(row.short_volume_ratio),
                "short_volume_z_60d": None,
                "short_pressure_flag": False,
                "short_covering_candidate": False,
                "bearish_pressure_flag": False,
                "market": str(row.Market).strip() or market,
                "source_file": source_file,
                "fetched_at": timestamp,
                "data_quality_status": row.data_quality_status,
            }
        )
    if rejected:
        LOGGER.warning("Rejected %s malformed FINRA daily rows from %s", rejected, source_file or "input")
    return rows


def compute_finra_features(rows: list[dict], price_rows: list[dict] | None = None) -> list[dict]:
    """Compatibility helper for small in-memory callers.

    Production rolling analytics are calculated from complete database history
    by ``finra_short_analytics.compute_daily_short_volume_features``.
    """
    if not rows:
        return []
    df = pd.DataFrame(rows).sort_values(["ticker", "trade_date"])
    df["short_volume_z_60d"] = df.groupby("ticker")["short_volume_ratio"].transform(
        lambda values: (values - values.rolling(60, min_periods=20).mean())
        / values.rolling(60, min_periods=20).std().replace(0, np.nan)
    )
    df["short_pressure_flag"] = df["short_volume_z_60d"] > 2
    if price_rows:
        price = pd.DataFrame(price_rows)
        if {"ticker", "date", "pct_chg"}.issubset(price.columns):
            price["trade_date"] = pd.to_datetime(price["date"]).dt.date
            df = df.merge(price[["ticker", "trade_date", "pct_chg"]], on=["ticker", "trade_date"], how="left")
            move = pd.to_numeric(df["pct_chg"], errors="coerce")
            df["short_covering_candidate"] = df["short_pressure_flag"] & move.gt(0)
            df["bearish_pressure_flag"] = df["short_pressure_flag"] & move.lt(0)
    return df.replace({np.nan: None}).to_dict(orient="records")


def _existing_success_dates(engine, dates: Iterable[date]) -> set[date]:
    selected = list(dict.fromkeys(dates))
    if not selected:
        return set()
    setup_finra_short_volume_schema(engine)
    query = text(
        """
        SELECT source_date
        FROM public.finra_ingestion_runs
        WHERE source_type = 'daily_short_volume'
          AND status = 'success'
          AND source_date IN :dates
        """
    ).bindparams(bindparam("dates", expanding=True))
    with engine.connect() as conn:
        return {row[0] for row in conn.execute(query, {"dates": selected})}


def _record_ingestion_run(engine, *, source_date: date, source_file: str, status: str, row_count: int = 0, error: str | None = None) -> None:
    query = text(
        """
        INSERT INTO public.finra_ingestion_runs (
            source_type, source_date, source_file, status, row_count, error_message, fetched_at
        ) VALUES (
            'daily_short_volume', :source_date, :source_file, :status, :row_count, :error_message, now()
        )
        ON CONFLICT (source_type, source_date) DO UPDATE SET
            source_file = EXCLUDED.source_file,
            status = EXCLUDED.status,
            row_count = EXCLUDED.row_count,
            error_message = EXCLUDED.error_message,
            fetched_at = now()
        """
    )
    with engine.begin() as conn:
        conn.execute(
            query,
            {
                "source_date": source_date,
                "source_file": source_file,
                "status": status,
                "row_count": row_count,
                "error_message": error,
            },
        )


def fetch_finra_short_volume_files(
    *,
    dates: list[date] | None = None,
    market: str = "CNMS",
    timeout: int = 30,
    minimum_rows: int | None = None,
    progress: Progress | None = None,
    session=None,
) -> DailyFetchResult:
    selected_dates = list(dict.fromkeys(dates or recent_business_dates(5)))
    session = session or http_session()
    minimum = minimum_rows or int(load_short_analytics_config()["quality"]["minimum_daily_file_rows"])
    result = DailyFetchResult()
    for index, trade_date in enumerate(selected_dates, start=1):
        url = finra_short_volume_url(trade_date, market=market)
        source_file = PurePosixPath(url).name
        if progress:
            progress(f"FINRA daily {index}/{len(selected_dates)} {trade_date} requesting {source_file}")
        try:
            response = session.get(url, timeout=timeout)
            if response.status_code == 404:
                result.missing_dates.append(trade_date)
                if progress:
                    progress(f"FINRA daily {trade_date} unavailable (404)")
                continue
            response.raise_for_status()
            rows = parse_finra_short_volume_text(
                response.text,
                market=market,
                source_file=source_file,
                minimum_rows=minimum,
            )
            if any(row["trade_date"] != trade_date for row in rows):
                raise ValueError("source trade date does not match requested filename")
            result.rows.extend(rows)
            result.successful_dates.append(trade_date)
            if progress:
                progress(f"FINRA daily {trade_date} validated rows={len(rows):,}")
        except Exception as exc:
            result.failed_dates[trade_date] = str(exc)
            if progress:
                progress(f"FINRA daily {trade_date} failed: {exc}")
    return result


def fetch_finra_short_volume_rows(*, dates: list[date] | None = None, market: str = "CNMS", timeout: int = 20) -> list[dict]:
    result = fetch_finra_short_volume_files(dates=dates, market=market, timeout=timeout)
    if not result.rows and result.failed_dates:
        errors = [f"{key}: {value}" for key, value in result.failed_dates.items()]
        raise RuntimeError("; ".join(errors[:5]))
    return result.rows


def upsert_finra_short_volume(engine, rows: list[dict], *, chunk_size: int = 10000, ensure_schema: bool = True) -> int:
    if not rows:
        return 0
    if ensure_schema:
        setup_finra_short_volume_schema(engine)
    query = text(
        """
        INSERT INTO public.finra_short_volume (
            trade_date, ticker, short_volume, short_exempt_volume, total_volume,
            short_volume_ratio, short_volume_z_60d, short_pressure_flag,
            short_covering_candidate, bearish_pressure_flag, market, source_file,
            raw_symbol, normalized_ticker, fetched_at, data_quality_status, updated_at
        ) VALUES (
            :trade_date, :ticker, :short_volume, :short_exempt_volume, :total_volume,
            :short_volume_ratio, :short_volume_z_60d, :short_pressure_flag,
            :short_covering_candidate, :bearish_pressure_flag, :market, :source_file,
            :raw_symbol, :normalized_ticker, :fetched_at, :data_quality_status, now()
        )
        ON CONFLICT (trade_date, ticker) DO UPDATE SET
            short_volume = EXCLUDED.short_volume,
            short_exempt_volume = EXCLUDED.short_exempt_volume,
            total_volume = EXCLUDED.total_volume,
            short_volume_ratio = EXCLUDED.short_volume_ratio,
            market = EXCLUDED.market,
            raw_symbol = EXCLUDED.raw_symbol,
            normalized_ticker = EXCLUDED.normalized_ticker,
            source_file = EXCLUDED.source_file,
            fetched_at = EXCLUDED.fetched_at,
            data_quality_status = EXCLUDED.data_quality_status,
            updated_at = now()
        """
    )
    with engine.begin() as conn:
        for start in range(0, len(rows), chunk_size):
            conn.execute(query, rows[start : start + chunk_size])
    return len(rows)


def latest_finra_summary(engine, *, limit: int = 30) -> pd.DataFrame:
    query = text(
        """
        SELECT raw.*, features.svr_5d, features.svr_20d, features.svr_60d,
               features.svr_z20, features.svr_z60, features.short_activity_score
        FROM public.finra_short_volume raw
        LEFT JOIN public.finra_short_volume_daily_features features
          ON features.trade_date = raw.trade_date AND features.ticker = raw.ticker
        WHERE raw.trade_date = (SELECT MAX(trade_date) FROM public.finra_short_volume)
          AND raw.raw_symbol IS NOT NULL
          AND raw.normalized_ticker IS NOT NULL
          AND raw.normalized_ticker = raw.ticker
          AND raw.total_volume >= 100000
        ORDER BY features.short_activity_score DESC NULLS LAST, raw.short_volume_ratio DESC
        LIMIT :limit
        """
    )
    try:
        return pd.read_sql(query, engine, params={"limit": limit})
    except Exception:
        return pd.DataFrame()


def run_finra_fetch(
    engine,
    *,
    dates: list[date] | None = None,
    market: str = "CNMS",
    dry_run: bool = False,
    timeout: int = 20,
    skip_existing: bool = True,
    progress: Progress | None = print,
) -> dict:
    started = time.perf_counter()
    setup_finra_short_volume_schema(engine)
    selected = list(dict.fromkeys(dates or recent_business_dates(5)))
    skipped: list[date] = []
    if skip_existing and not dry_run:
        existing = _existing_success_dates(engine, selected)
        skipped = [item for item in selected if item in existing]
        selected = [item for item in selected if item not in existing]
    successful_dates: list[date] = []
    missing_dates: list[date] = []
    failed_dates: dict[date, str] = {}
    total_rows = 0
    upserted = 0
    session = http_session()
    # Flush each date independently. This bounds memory and makes a long
    # backfill resumable after every successfully committed source file.
    for source_date in selected:
        fetched = fetch_finra_short_volume_files(
            dates=[source_date], market=market, timeout=timeout, progress=progress, session=session
        )
        source_file = PurePosixPath(finra_short_volume_url(source_date, market=market)).name
        if fetched.successful_dates:
            row_count = len(fetched.rows)
            total_rows += row_count
            if not dry_run:
                upserted += upsert_finra_short_volume(engine, fetched.rows, ensure_schema=False)
                _record_ingestion_run(
                    engine,
                    source_date=source_date,
                    source_file=source_file,
                    status="success",
                    row_count=row_count,
                )
            successful_dates.append(source_date)
        elif fetched.missing_dates:
            missing_dates.append(source_date)
            if not dry_run:
                _record_ingestion_run(
                    engine,
                    source_date=source_date,
                    source_file=source_file,
                    status="missing",
                    error="HTTP 404",
                )
        elif fetched.failed_dates:
            error = fetched.failed_dates[source_date]
            failed_dates[source_date] = error
            if not dry_run:
                _record_ingestion_run(
                    engine,
                    source_date=source_date,
                    source_file=source_file,
                    status="failed",
                    error=error,
                )
    latest = max(successful_dates, default=None)
    succeeded = bool(successful_dates or skipped) and not failed_dates
    record_flow_source_health(
        engine,
        source_name="FINRA short-sale volume",
        source_type="finra_short_volume",
        succeeded=succeeded,
        fetch_seconds=time.perf_counter() - started,
        latest_available_date=latest or max(skipped, default=None),
        error=None if succeeded else "; ".join(f"{key}: {value}" for key, value in list(failed_dates.items())[:5]),
    )
    if not total_rows and failed_dates and not skipped:
        raise RuntimeError("; ".join(f"{key}: {value}" for key, value in list(failed_dates.items())[:5]))
    return {
        "rows": total_rows,
        "upserted": upserted,
        "latest_available_date": latest or max(skipped, default=None),
        "successful_dates": successful_dates,
        "missing_dates": missing_dates,
        "failed_dates": failed_dates,
        "skipped_dates": skipped,
    }
