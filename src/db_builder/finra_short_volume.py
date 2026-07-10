"""FINRA daily short-sale volume ingestion.

FINRA short-sale volume is not short interest. The signals here only describe
reported short-sale volume pressure and possible contested trading.
"""

from __future__ import annotations

import io
import time
from datetime import date

import pandas as pd
import numpy as np
from sqlalchemy import text

from db_builder.flow_sources import finra_short_volume_url, http_session, recent_business_dates, record_flow_source_health


def setup_finra_short_volume_schema(engine) -> None:
    sql = text(
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
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now(),
            PRIMARY KEY (trade_date, ticker)
        );
        """
    )
    with engine.begin() as conn:
        conn.execute(sql)


def parse_finra_short_volume_text(text_body: str, *, market: str = "CNMS") -> list[dict]:
    df = pd.read_csv(io.StringIO(text_body), sep="|")
    required = {"Date", "Symbol", "ShortVolume", "ShortExemptVolume", "TotalVolume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing FINRA columns: {sorted(missing)}")
    df["Date"] = pd.to_datetime(df["Date"], format="%Y%m%d", errors="coerce")
    rows = []
    for _, row in df.iterrows():
        if pd.isna(row["Date"]):
            continue
        total_volume = pd.to_numeric(row["TotalVolume"], errors="coerce")
        short_volume = pd.to_numeric(row["ShortVolume"], errors="coerce")
        short_exempt = pd.to_numeric(row["ShortExemptVolume"], errors="coerce")
        if pd.isna(total_volume) or total_volume == 0 or pd.isna(short_volume):
            continue
        rows.append(
            {
                "trade_date": row["Date"].date(),
                "ticker": str(row["Symbol"]).upper().strip(),
                "short_volume": float(short_volume),
                "short_exempt_volume": 0.0 if pd.isna(short_exempt) else float(short_exempt),
                "total_volume": float(total_volume),
                "short_volume_ratio": float(short_volume) / float(total_volume),
                "short_volume_z_60d": None,
                "short_pressure_flag": False,
                "short_covering_candidate": False,
                "bearish_pressure_flag": False,
                "market": market,
            }
        )
    return rows


def compute_finra_features(rows: list[dict], price_rows: list[dict] | None = None) -> list[dict]:
    if not rows:
        return []
    df = pd.DataFrame(rows).sort_values(["ticker", "trade_date"])
    df["short_volume_z_60d"] = df.groupby("ticker")["short_volume_ratio"].transform(
        lambda s: (s - s.rolling(60, min_periods=20).mean()) / s.rolling(60, min_periods=20).std().replace(0, pd.NA)
    )
    df["short_pressure_flag"] = df["short_volume_z_60d"] > 2
    if price_rows:
        price = pd.DataFrame(price_rows)
        if {"ticker", "date", "pct_chg"}.issubset(price.columns):
            price["trade_date"] = pd.to_datetime(price["date"]).dt.date
            df = df.merge(price[["ticker", "trade_date", "pct_chg"]], on=["ticker", "trade_date"], how="left")
            df["short_covering_candidate"] = (df["short_pressure_flag"]) & (pd.to_numeric(df["pct_chg"], errors="coerce") > 0)
            df["bearish_pressure_flag"] = (df["short_pressure_flag"]) & (pd.to_numeric(df["pct_chg"], errors="coerce") < 0)
    clean = df.replace({np.nan: None})
    return clean.to_dict(orient="records")


def fetch_finra_short_volume_rows(*, dates: list[date] | None = None, market: str = "CNMS", timeout: int = 20) -> list[dict]:
    session = http_session()
    selected_dates = dates or recent_business_dates(5)
    all_rows = []
    errors = []
    for trade_date in selected_dates:
        url = finra_short_volume_url(trade_date, market=market)
        try:
            response = session.get(url, timeout=timeout)
            if response.status_code == 404:
                errors.append(f"{trade_date}: 404")
                continue
            response.raise_for_status()
            all_rows.extend(parse_finra_short_volume_text(response.text, market=market))
        except Exception as exc:
            errors.append(f"{trade_date}: {exc}")
    if not all_rows and errors:
        raise RuntimeError("; ".join(errors[:5]))
    return compute_finra_features(all_rows)


def upsert_finra_short_volume(engine, rows: list[dict], *, chunk_size: int = 10000) -> int:
    if not rows:
        return 0
    setup_finra_short_volume_schema(engine)
    sql = text(
        """
        INSERT INTO public.finra_short_volume (
            trade_date, ticker, short_volume, short_exempt_volume, total_volume,
            short_volume_ratio, short_volume_z_60d, short_pressure_flag,
            short_covering_candidate, bearish_pressure_flag, market, updated_at
        )
        VALUES (
            :trade_date, :ticker, :short_volume, :short_exempt_volume, :total_volume,
            :short_volume_ratio, :short_volume_z_60d, :short_pressure_flag,
            :short_covering_candidate, :bearish_pressure_flag, :market, now()
        )
        ON CONFLICT (trade_date, ticker)
        DO UPDATE SET
            short_volume = EXCLUDED.short_volume,
            short_exempt_volume = EXCLUDED.short_exempt_volume,
            total_volume = EXCLUDED.total_volume,
            short_volume_ratio = EXCLUDED.short_volume_ratio,
            short_volume_z_60d = EXCLUDED.short_volume_z_60d,
            short_pressure_flag = EXCLUDED.short_pressure_flag,
            short_covering_candidate = EXCLUDED.short_covering_candidate,
            bearish_pressure_flag = EXCLUDED.bearish_pressure_flag,
            market = EXCLUDED.market,
            updated_at = now();
        """
    )
    with engine.begin() as conn:
        for start in range(0, len(rows), chunk_size):
            conn.execute(sql, rows[start : start + chunk_size])
    return len(rows)


def latest_finra_summary(engine, *, limit: int = 30) -> pd.DataFrame:
    sql = text(
        """
        SELECT *
        FROM public.finra_short_volume
        WHERE trade_date = (SELECT MAX(trade_date) FROM public.finra_short_volume)
          AND total_volume >= 100000
        ORDER BY short_volume_z_60d DESC NULLS LAST, short_volume_ratio DESC
        LIMIT :limit
        """
    )
    try:
        return pd.read_sql(sql, engine, params={"limit": limit})
    except Exception:
        return pd.DataFrame()


def run_finra_fetch(engine, *, dates: list[date] | None = None, market: str = "CNMS", dry_run: bool = False, timeout: int = 20) -> dict:
    started = time.perf_counter()
    try:
        rows = fetch_finra_short_volume_rows(dates=dates, market=market, timeout=timeout)
        latest = max((row["trade_date"] for row in rows), default=None)
        count = 0 if dry_run else upsert_finra_short_volume(engine, rows)
        record_flow_source_health(
            engine,
            source_name="FINRA short-sale volume",
            source_type="finra_short_volume",
            succeeded=True,
            fetch_seconds=time.perf_counter() - started,
            latest_available_date=latest,
        )
        return {"rows": len(rows), "upserted": count, "latest_available_date": latest}
    except Exception as exc:
        record_flow_source_health(
            engine,
            source_name="FINRA short-sale volume",
            source_type="finra_short_volume",
            succeeded=False,
            fetch_seconds=time.perf_counter() - started,
            error=str(exc),
        )
        raise
