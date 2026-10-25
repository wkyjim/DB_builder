"""Deterministic security-type and local price-history status classification."""

from __future__ import annotations

import re

import pandas as pd
from sqlalchemy import text

from db_builder.config import RAW_TABLE


NON_CORE_TYPES = {
    "warrant", "right", "unit", "preferred", "debt", "when_issued",
    "etf", "etn", "closed_end_fund", "trust", "spac",
    "fund",
}


PLAIN_EQUITY_TYPES = {"common_stock", "adr"}

# A "Series X" share is a preferred; a "Class X" share is a voting/second class
# of the same common stock (BRK_B) and stays plain equity.
_SERIES_RE = re.compile(r"\bSERIES\s+[A-Z]\b", re.IGNORECASE)
_CLASS_RE = re.compile(r"\bCLASS\s+[A-Z]\b", re.IGNORECASE)
# Option chains use a bare ticker with the expiry year appended: AAPL26,
# AAPL2026, and the full OCC form AAPL260119C00150000.
_OPTION_RE = re.compile(r"^[A-Z]{1,6}\d{2}(\d{4})?[CP]?\d{0,8}$")
_OPTION_TAIL_RE = re.compile(r"^[A-Z]{1,6}\d{6,}$")


def is_option_symbol(ticker: str) -> bool:
    """True when the ticker is an option contract rather than a security."""
    symbol = str(ticker or "").upper().strip()
    if not symbol:
        return False
    if _OPTION_TAIL_RE.match(symbol):
        return True
    return bool(_OPTION_RE.match(symbol)) and any(ch.isdigit() for ch in symbol)


def classify_security_type(ticker: str, name: str | None) -> str:
    symbol = str(ticker or "").upper().strip()
    label = str(name or "").upper().strip()
    # Option chains carry no security name at all, so test the symbol first.
    if is_option_symbol(symbol) and not label:
        return "option"
    if re.search(r"\b(WARRANT|WARRANTS|WT)\b", label):
        return "warrant"
    if re.search(r"\b(RIGHT|RIGHTS|RT)\b", label):
        return "right"
    if re.search(r"\bUNIT|UNIT CONS", label):
        return "unit"
    if "WHEN ISSUED" in label or re.search(r"\bWI\b", label):
        return "when_issued"
    if re.search(r"\b(NOTES? DUE|BOND|DEBENTURE)\b", label):
        return "debt"
    if re.search(r"\b(ETN|EXCHANGE TRADED NOTE)\b", label):
        return "etn"
    if re.search(r"\b(CLOSED END|CLOSED-END)\b", label):
        return "closed_end_fund"
    if " ETF" in f" {label}" or label.endswith("ETF") or re.search(
        r"\b(ISHARES|SPDR|VANECK|VANGUARD|FIRST TRUST|INVESCO)\b", label
    ):
        return "etf"
    if re.search(r"\b(2X|3X|ULTRASHORT|DAILY INVERSE|CLO)\b", label):
        return "fund"
    if re.search(r"\bFUND\b", label):
        return "fund"
    if re.search(r"\b(UNIT INVESTMENT TRUST|COMMODITY TRUST|BITCOIN TRUST)\b", label):
        return "trust"
    if re.search(r"\b(ACQUISITION CORP|ACQUISITION CO|BLANK CHECK)\b", label):
        return "spac"
    # Preferred before generic "Series": a Series share is a preferred unless
    # the name calls it a class of common stock.
    if re.search(r"\b(PREFERRED|PREFERENCE|PFD)\b", label) or _SERIES_RE.search(label):
        if not _CLASS_RE.search(label):
            return "preferred"
    if re.search(r"\bADR\b", label):
        return "adr"
    if symbol.endswith("_U"):
        return "unit"
    return "common_stock"


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


def is_plain_equity(security_type: str, lifecycle_status: str) -> bool:
    """True only for a live common stock or ADR.

    This is the single flag for "is this a real equity we want". False for
    warrants, units, preferreds, options, rights, debt, ETFs/ETNs/funds/trusts/
    SPACs, and for instruments with no usable price history or that have gone
    quiet -- the classes a non-Eastmoney source such as the Polygon grouped
    daily bars will not carry.
    """
    return (
        security_type in PLAIN_EQUITY_TYPES
        and lifecycle_status not in {"no_valid_price", "inactive_candidate"}
    )


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
                    is_plain_equity boolean NOT NULL DEFAULT true,
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
        # Deployed tables predate the column; add it in place.
        connection.execute(
            text(
                """
                ALTER TABLE public.equity_security_status
                    ADD COLUMN IF NOT EXISTS is_plain_equity boolean
                        NOT NULL DEFAULT true
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
                "is_plain_equity": is_plain_equity(security_type, lifecycle),
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
                -- The newest row per ticker is not necessarily named: the
                -- Massive grouped-daily source stores no name, so picking
                -- strictly by date would blank every name and break
                -- name-driven classification (ETF, warrant, preferred).
                -- Take the most recent row that actually carries one.
                SELECT DISTINCT ON (ticker) ticker, name
                FROM {RAW_TABLE}
                WHERE name IS NOT NULL AND name <> ''
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
            is_plain_equity,
            first_valid_date, last_valid_date, valid_price_rows,
            distinct_close_count, status_reason, updated_at
        )
        VALUES (
            :ticker, :name, :security_type, :lifecycle_status, :coverage_eligible,
            :is_plain_equity,
            :first_valid_date, :last_valid_date, :valid_price_rows,
            :distinct_close_count, :status_reason, now()
        )
        ON CONFLICT (ticker) DO UPDATE SET
            name = EXCLUDED.name,
            security_type = EXCLUDED.security_type,
            lifecycle_status = EXCLUDED.lifecycle_status,
            coverage_eligible = EXCLUDED.coverage_eligible,
            is_plain_equity = EXCLUDED.is_plain_equity,
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
