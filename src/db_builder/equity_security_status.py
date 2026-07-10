"""Deterministic security-type and local price-history status classification."""

from __future__ import annotations

import re

import pandas as pd
from sqlalchemy import text

from db_builder.config import RAW_TABLE


NON_CORE_TYPES = {"warrant", "right", "unit", "preferred", "debt", "when_issued"}


def classify_security_type(ticker: str, name: str | None) -> str:
    symbol = str(ticker or "").upper().strip()
    label = str(name or "").upper().strip()
    if re.search(r"\b(WARRANT|WARRANTS|WT)\b", label):
        return "warrant"
    if re.search(r"\b(RIGHT|RIGHTS|RT)\b", label):
        return "right"
    if re.search(r"\bUNIT|UNIT CONS", label):
        return "unit"
    if "WHEN ISSUED" in label or re.search(r"\bWI\b", label):
        return "when_issued"
    if re.search(r"\b(PREFERRED|PREFERENCE|PFD)\b", label):
        return "preferred"
    if re.search(r"\b(NOTES? DUE|BOND|DEBENTURE)\b", label):
        return "debt"
    if " ETF" in f" {label}" or label.endswith("ETF"):
        return "etf"
    if re.search(r"\bADR\b", label):
        return "adr"
    if symbol.endswith("_U"):
        return "unit"
    return "common_or_fund"


def is_core_coverage_security(ticker: str, name: str | None, close) -> bool:
    numeric_close = pd.to_numeric(close, errors="coerce")
    return (
        pd.notna(numeric_close)
        and float(numeric_close) > 0
        and classify_security_type(ticker, name) not in NON_CORE_TYPES
    )


def filter_core_coverage(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    mask = frame.apply(
        lambda row: is_core_coverage_security(
            row.get("ticker"),
            row.get("name"),
            row.get("close"),
        ),
        axis=1,
    )
    return frame[mask].copy()


def create_equity_security_status_table(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS public.equity_security_status (
                    ticker text PRIMARY KEY,
                    name text,
                    security_type text NOT NULL,
                    lifecycle_status text NOT NULL,
                    coverage_eligible boolean NOT NULL,
                    first_valid_date date,
                    last_valid_date date,
                    valid_price_rows integer NOT NULL DEFAULT 0,
                    distinct_close_count integer NOT NULL DEFAULT 0,
                    status_reason text NOT NULL,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
        )


def build_security_status(frame: pd.DataFrame, latest_session) -> pd.DataFrame:
    latest = pd.Timestamp(latest_session).date()
    rows = []
    for item in frame.to_dict(orient="records"):
        security_type = classify_security_type(item.get("ticker"), item.get("name"))
        valid_rows = int(item.get("valid_price_rows") or 0)
        last_valid = item.get("last_valid_date")
        last_valid = None if pd.isna(last_valid) else pd.Timestamp(last_valid).date()
        if valid_rows == 0:
            lifecycle = "no_valid_price"
            reason = "No positive closing price exists in local history."
        elif last_valid == latest:
            lifecycle = "active"
            reason = "Positive closing price exists on the latest completed session."
        elif last_valid and (latest - last_valid).days > 14:
            lifecycle = "inactive_candidate"
            reason = "No positive closing price in the last 14 calendar days; not confirmed delisted."
        else:
            lifecycle = "intermittent"
            reason = "Recent price history exists, but not on the latest completed session."
        coverage_eligible = (
            security_type not in NON_CORE_TYPES
            and lifecycle not in {"no_valid_price", "inactive_candidate"}
        )
        rows.append(
            {
                **item,
                "security_type": security_type,
                "lifecycle_status": lifecycle,
                "coverage_eligible": coverage_eligible,
                "status_reason": reason,
            }
        )
    return pd.DataFrame(rows)


def refresh_equity_security_status(engine, latest_session) -> pd.DataFrame:
    create_equity_security_status_table(engine)
    history = pd.read_sql(
        text(
            f"""
            WITH latest_names AS (
                SELECT DISTINCT ON (ticker) ticker, name
                FROM {RAW_TABLE}
                ORDER BY ticker, date DESC
            )
            SELECT
                raw.ticker,
                names.name,
                MIN(raw.date) FILTER (WHERE raw.close > 0) AS first_valid_date,
                MAX(raw.date) FILTER (WHERE raw.close > 0) AS last_valid_date,
                COUNT(*) FILTER (WHERE raw.close > 0) AS valid_price_rows,
                COUNT(DISTINCT raw.close) FILTER (WHERE raw.close > 0) AS distinct_close_count
            FROM {RAW_TABLE} raw
            JOIN latest_names names USING (ticker)
            GROUP BY raw.ticker, names.name
            ORDER BY raw.ticker
            """
        ),
        engine,
    )
    status = build_security_status(history, latest_session)
    records = status.where(pd.notna(status), None).to_dict(orient="records")
    sql = text(
        """
        INSERT INTO public.equity_security_status (
            ticker, name, security_type, lifecycle_status, coverage_eligible,
            first_valid_date, last_valid_date, valid_price_rows,
            distinct_close_count, status_reason, updated_at
        )
        VALUES (
            :ticker, :name, :security_type, :lifecycle_status, :coverage_eligible,
            :first_valid_date, :last_valid_date, :valid_price_rows,
            :distinct_close_count, :status_reason, now()
        )
        ON CONFLICT (ticker) DO UPDATE SET
            name = EXCLUDED.name,
            security_type = EXCLUDED.security_type,
            lifecycle_status = EXCLUDED.lifecycle_status,
            coverage_eligible = EXCLUDED.coverage_eligible,
            first_valid_date = EXCLUDED.first_valid_date,
            last_valid_date = EXCLUDED.last_valid_date,
            valid_price_rows = EXCLUDED.valid_price_rows,
            distinct_close_count = EXCLUDED.distinct_close_count,
            status_reason = EXCLUDED.status_reason,
            updated_at = now()
        """
    )
    with engine.begin() as connection:
        connection.execute(sql, records)
    return status
