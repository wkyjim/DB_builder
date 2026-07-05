"""Security classification and S&P 500 constituent mapping."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from io import StringIO

import pandas as pd
import requests
from sqlalchemy import text


SP500_CONSTITUENTS_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def normalize_ticker(ticker: str) -> str:
    return str(ticker).strip().upper().replace(".", "-")


def create_security_classification_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS public.security_classification (
                    ticker text PRIMARY KEY,
                    company_name text,
                    sector text,
                    industry text,
                    source text,
                    is_sp500 boolean NOT NULL DEFAULT false,
                    is_active boolean NOT NULL DEFAULT true,
                    date_added date,
                    cik text,
                    theme_tags text[] NOT NULL DEFAULT '{}',
                    first_seen_at timestamptz NOT NULL DEFAULT now(),
                    last_seen_at timestamptz NOT NULL DEFAULT now(),
                    removed_at timestamptz,
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    raw_payload jsonb
                )
                """
            )
        )


def fetch_sp500_constituents() -> list[dict]:
    response = requests.get(SP500_CONSTITUENTS_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    response.raise_for_status()
    tables = pd.read_html(StringIO(response.text))
    if not tables:
        return []
    df = tables[0]
    rows = []
    for item in df.to_dict(orient="records"):
        ticker = normalize_ticker(item.get("Symbol", ""))
        if not ticker:
            continue
        rows.append(
            {
                "ticker": ticker,
                "company_name": item.get("Security") or "",
                "sector": item.get("GICS Sector") or "",
                "industry": item.get("GICS Sub-Industry") or "",
                "source": "Wikipedia S&P 500",
                "is_sp500": True,
                "is_active": True,
                "date_added": _parse_date(item.get("Date added")),
                "cik": str(item.get("CIK") or ""),
                "theme_tags": [],
                "raw_payload": json.dumps(item, default=str, allow_nan=False),
            }
        )
    return rows


def _parse_date(value):
    if value is None or pd.isna(value):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def upsert_sp500_constituents(engine, rows: list[dict], *, mark_removed: bool = True) -> dict:
    create_security_classification_table(engine)
    if not rows:
        return {"upserted": 0, "marked_inactive": 0}
    now = datetime.now(timezone.utc)
    tickers = [row["ticker"] for row in rows]
    payload = [{**row, "updated_at": now, "last_seen_at": now} for row in rows]
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO public.security_classification (
                    ticker,
                    company_name,
                    sector,
                    industry,
                    source,
                    is_sp500,
                    is_active,
                    date_added,
                    cik,
                    theme_tags,
                    last_seen_at,
                    updated_at,
                    raw_payload
                )
                VALUES (
                    :ticker,
                    :company_name,
                    :sector,
                    :industry,
                    :source,
                    :is_sp500,
                    :is_active,
                    :date_added,
                    :cik,
                    :theme_tags,
                    :last_seen_at,
                    :updated_at,
                    CAST(:raw_payload AS jsonb)
                )
                ON CONFLICT (ticker)
                DO UPDATE SET
                    company_name = EXCLUDED.company_name,
                    sector = EXCLUDED.sector,
                    industry = EXCLUDED.industry,
                    source = EXCLUDED.source,
                    is_sp500 = true,
                    is_active = true,
                    date_added = EXCLUDED.date_added,
                    cik = EXCLUDED.cik,
                    last_seen_at = EXCLUDED.last_seen_at,
                    removed_at = NULL,
                    updated_at = now(),
                    raw_payload = EXCLUDED.raw_payload
                """
            ),
            payload,
        )
        marked_inactive = 0
        if mark_removed:
            result = conn.execute(
                text(
                    """
                    UPDATE public.security_classification
                    SET is_active = false,
                        removed_at = now(),
                        updated_at = now()
                    WHERE is_sp500 = true
                      AND is_active = true
                      AND ticker <> ALL(:tickers)
                    """
                ),
                {"tickers": tickers},
            )
            marked_inactive = result.rowcount or 0
    return {"upserted": len(rows), "marked_inactive": marked_inactive}


def sp500_classification_summary(engine) -> pd.DataFrame:
    create_security_classification_table(engine)
    sql = """
        SELECT
            sector,
            count(*) FILTER (WHERE is_active) AS active_count,
            count(*) FILTER (WHERE NOT is_active) AS inactive_count,
            max(updated_at) AS updated_at
        FROM public.security_classification
        WHERE is_sp500 = true
        GROUP BY sector
        ORDER BY active_count DESC, sector
    """
    return pd.read_sql(text(sql), engine)
