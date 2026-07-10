"""CFTC Commitments of Traders ingestion and feature generation."""

from __future__ import annotations

import io
import time
import zipfile
from datetime import date, datetime, timezone

import pandas as pd
import requests
from sqlalchemy import text

from db_builder.flow_sources import http_session, record_flow_source_health


CFTC_TFF_FUTURES_URL_TEMPLATE = "https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip"
CFTC_DISAGG_FUTURES_URL_TEMPLATE = "https://www.cftc.gov/files/dea/history/dea_fut_txt_{year}.zip"

COT_MARKET_MAP = [
    ("SPX", "equity_index", ["S&P 500 STOCK INDEX", "E-MINI S&P 500", "MICRO E-MINI S&P 500"]),
    ("NDX", "equity_index", ["NASDAQ-100", "NASDAQ 100", "E-MINI NASDAQ"]),
    ("RUT", "equity_index", ["RUSSELL 2000"]),
    ("UST2Y", "rates", ["2-YEAR U.S. TREASURY", "2 YEAR U.S. TREASURY"]),
    ("UST5Y", "rates", ["5-YEAR U.S. TREASURY", "5 YEAR U.S. TREASURY"]),
    ("UST10Y", "rates", ["10-YEAR U.S. TREASURY", "10 YEAR U.S. TREASURY"]),
    ("UST30Y", "rates", ["U.S. TREASURY BONDS", "30-YEAR U.S. TREASURY", "ULTRA U.S. TREASURY BOND"]),
    ("SOFR", "rates", ["SOFR"]),
    ("DXY", "fx", ["U.S. DOLLAR INDEX", "USD INDEX"]),
    ("EUR", "fx", ["EURO FX"]),
    ("JPY", "fx", ["JAPANESE YEN"]),
    ("GBP", "fx", ["BRITISH POUND"]),
    ("AUD", "fx", ["AUSTRALIAN DOLLAR"]),
    ("CAD", "fx", ["CANADIAN DOLLAR"]),
    ("GOLD", "commodity", ["GOLD"]),
    ("SILVER", "commodity", ["SILVER"]),
    ("COPPER", "commodity", ["COPPER"]),
    ("WTI", "commodity", ["CRUDE OIL", "LIGHT SWEET CRUDE"]),
    ("NATGAS", "commodity", ["NATURAL GAS"]),
]

EXCLUDED_MARKET_NAME_TERMS = ["DIVIDEND", "XRATE"]


def setup_cot_schema(engine) -> None:
    sql = text(
        """
        CREATE TABLE IF NOT EXISTS public.cot_positions (
            report_date date NOT NULL,
            market_name text NOT NULL,
            asset_class text,
            asset_id text,
            contract_code text NOT NULL,
            long_contracts numeric,
            short_contracts numeric,
            spread_contracts numeric,
            open_interest numeric,
            net_position numeric,
            net_position_pct_oi numeric,
            net_position_z_3y numeric,
            percentile_3y numeric,
            crowded_long boolean DEFAULT false,
            crowded_short boolean DEFAULT false,
            squeeze_risk boolean DEFAULT false,
            position_unwind_risk boolean DEFAULT false,
            raw_payload jsonb,
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now(),
            PRIMARY KEY (report_date, contract_code)
        );
        """
    )
    with engine.begin() as conn:
        conn.execute(sql)
        conn.execute(text("ALTER TABLE public.cot_positions ADD COLUMN IF NOT EXISTS asset_id text;"))


def cot_url(year: int, *, report_type: str = "financial") -> str:
    if report_type == "disaggregated":
        return CFTC_DISAGG_FUTURES_URL_TEMPLATE.format(year=year)
    return CFTC_TFF_FUTURES_URL_TEMPLATE.format(year=year)


def fetch_cot_zip(year: int, *, report_type: str = "financial", timeout: int = 60) -> bytes:
    url = cot_url(year, report_type=report_type)
    response = http_session().get(url, timeout=timeout)
    response.raise_for_status()
    return response.content


def read_cot_zip(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = [name for name in zf.namelist() if name.lower().endswith((".txt", ".csv"))]
        if not names:
            raise ValueError("CFTC zip did not contain a text/csv file")
        with zf.open(names[0]) as handle:
            return pd.read_csv(handle)


def _col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    lower = {column.lower(): column for column in df.columns}
    for candidate in candidates:
        if candidate.lower() in lower:
            return lower[candidate.lower()]
    for column in df.columns:
        compact = column.lower().replace("_", "").replace(" ", "")
        for candidate in candidates:
            if compact == candidate.lower().replace("_", "").replace(" ", ""):
                return column
    return None


def _market_asset(name: str) -> tuple[str, str] | None:
    upper = name.upper()
    if any(term in upper for term in EXCLUDED_MARKET_NAME_TERMS):
        return None
    for code, asset_class, patterns in COT_MARKET_MAP:
        if any(pattern.upper() in upper for pattern in patterns):
            return code, asset_class
    return None


def normalize_cot_dataframe(df: pd.DataFrame) -> list[dict]:
    market_col = _col(df, ["Market_and_Exchange_Names", "Market and Exchange Names"])
    contract_col = _col(df, ["CFTC_Contract_Market_Code", "CFTC Contract Market Code"])
    date_col = _col(df, ["Report_Date_as_YYYY-MM-DD", "Report Date as YYYY-MM-DD", "As_of_Date_In_Form_YYMMDD"])
    oi_col = _col(df, ["Open_Interest_All", "Open Interest All"])
    long_col = _col(
        df,
        [
            "Asset_Mgr_Positions_Long_All",
            "M_Money_Positions_Long_All",
            "NonComm_Positions_Long_All",
        ],
    )
    short_col = _col(
        df,
        [
            "Asset_Mgr_Positions_Short_All",
            "M_Money_Positions_Short_All",
            "NonComm_Positions_Short_All",
        ],
    )
    spread_col = _col(
        df,
        [
            "Asset_Mgr_Positions_Spread_All",
            "M_Money_Positions_Spread_All",
            "NonComm_Postions_Spread_All",
            "NonComm_Positions_Spread_All",
        ],
    )
    required = [market_col, contract_col, date_col, oi_col, long_col, short_col]
    if any(column is None for column in required):
        raise ValueError(f"Missing required COT columns. Available: {list(df.columns)[:20]}")

    rows = []
    for _, item in df.iterrows():
        market_name = str(item[market_col])
        mapped = _market_asset(market_name)
        if not mapped:
            continue
        asset_id, asset_class = mapped
        contract_code = str(item[contract_col]).strip()
        report_date = pd.to_datetime(item[date_col], errors="coerce")
        if pd.isna(report_date):
            continue
        long_contracts = pd.to_numeric(item[long_col], errors="coerce")
        short_contracts = pd.to_numeric(item[short_col], errors="coerce")
        spread_contracts = pd.to_numeric(item[spread_col], errors="coerce") if spread_col else 0
        open_interest = pd.to_numeric(item[oi_col], errors="coerce")
        if pd.isna(long_contracts) or pd.isna(short_contracts) or pd.isna(open_interest) or open_interest == 0:
            continue
        net_position = float(long_contracts) - float(short_contracts)
        rows.append(
            {
                "report_date": report_date.date(),
                "market_name": market_name,
                "asset_class": asset_class,
                "asset_id": asset_id,
                "contract_code": contract_code,
                "long_contracts": float(long_contracts),
                "short_contracts": float(short_contracts),
                "spread_contracts": float(spread_contracts) if not pd.isna(spread_contracts) else 0.0,
                "open_interest": float(open_interest),
                "net_position": net_position,
                "net_position_pct_oi": net_position / float(open_interest),
                "raw_payload": {str(k): None if pd.isna(v) else v for k, v in item.to_dict().items()},
            }
        )
    return rows


def compute_cot_features(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    df = pd.DataFrame(rows).sort_values(["contract_code", "report_date"])
    grouped = df.groupby("contract_code", group_keys=False)
    rolling_mean = grouped["net_position_pct_oi"].transform(lambda s: s.rolling(156, min_periods=20).mean())
    rolling_std = grouped["net_position_pct_oi"].transform(lambda s: s.rolling(156, min_periods=20).std())
    df["net_position_z_3y"] = (df["net_position_pct_oi"] - rolling_mean) / rolling_std.replace(0, pd.NA)
    df["percentile_3y"] = grouped["net_position_pct_oi"].transform(lambda s: s.rolling(156, min_periods=20).rank(pct=True))
    df["crowded_long"] = df["net_position_z_3y"] >= 2
    df["crowded_short"] = df["net_position_z_3y"] <= -2
    df["squeeze_risk"] = df["crowded_short"]
    df["position_unwind_risk"] = df["crowded_long"]
    return df.where(pd.notnull(df), None).to_dict(orient="records")


def upsert_cot_positions(engine, rows: list[dict]) -> int:
    if not rows:
        return 0
    setup_cot_schema(engine)
    sql = text(
        """
        INSERT INTO public.cot_positions (
            report_date, market_name, asset_class, asset_id, contract_code,
            long_contracts, short_contracts, spread_contracts, open_interest,
            net_position, net_position_pct_oi, net_position_z_3y, percentile_3y,
            crowded_long, crowded_short, squeeze_risk, position_unwind_risk,
            raw_payload, updated_at
        )
        VALUES (
            :report_date, :market_name, :asset_class, :asset_id, :contract_code,
            :long_contracts, :short_contracts, :spread_contracts, :open_interest,
            :net_position, :net_position_pct_oi, :net_position_z_3y, :percentile_3y,
            :crowded_long, :crowded_short, :squeeze_risk, :position_unwind_risk,
            CAST(:raw_payload AS jsonb), now()
        )
        ON CONFLICT (report_date, contract_code)
        DO UPDATE SET
            market_name = EXCLUDED.market_name,
            asset_class = EXCLUDED.asset_class,
            asset_id = EXCLUDED.asset_id,
            long_contracts = EXCLUDED.long_contracts,
            short_contracts = EXCLUDED.short_contracts,
            spread_contracts = EXCLUDED.spread_contracts,
            open_interest = EXCLUDED.open_interest,
            net_position = EXCLUDED.net_position,
            net_position_pct_oi = EXCLUDED.net_position_pct_oi,
            net_position_z_3y = EXCLUDED.net_position_z_3y,
            percentile_3y = EXCLUDED.percentile_3y,
            crowded_long = EXCLUDED.crowded_long,
            crowded_short = EXCLUDED.crowded_short,
            squeeze_risk = EXCLUDED.squeeze_risk,
            position_unwind_risk = EXCLUDED.position_unwind_risk,
            raw_payload = EXCLUDED.raw_payload,
            updated_at = now();
        """
    )
    import json

    payloads = []
    for row in rows:
        item = row.copy()
        item["raw_payload"] = json.dumps(item.get("raw_payload") or {}, default=str)
        payloads.append(item)
    with engine.begin() as conn:
        conn.execute(sql, payloads)
    return len(payloads)


def fetch_normalized_cot_rows(
    *,
    years: list[int] | None = None,
    report_type: str = "financial",
    timeout: int = 60,
) -> list[dict]:
    selected_years = years or [datetime.now(timezone.utc).year]
    all_rows = []
    for year in selected_years:
        content = fetch_cot_zip(year, report_type=report_type, timeout=timeout)
        df = read_cot_zip(content)
        all_rows.extend(normalize_cot_dataframe(df))
    return compute_cot_features(all_rows)


def latest_cot_summary(engine, *, limit: int = 20) -> pd.DataFrame:
    sql = text(
        """
        WITH latest AS (
            SELECT *
            FROM public.cot_positions
            WHERE report_date = (SELECT MAX(report_date) FROM public.cot_positions)
              AND asset_id IS NOT NULL
              AND market_name NOT ILIKE '%DIVIDEND%'
              AND market_name NOT ILIKE '%XRATE%'
        ),
        ranked AS (
            SELECT
                latest.*,
                ROW_NUMBER() OVER (PARTITION BY asset_id ORDER BY open_interest DESC NULLS LAST) AS rn
            FROM latest
        )
        SELECT
            report_date, market_name, asset_class, asset_id, contract_code,
            long_contracts, short_contracts, spread_contracts, open_interest,
            net_position, net_position_pct_oi, net_position_z_3y, percentile_3y,
            crowded_long, crowded_short, squeeze_risk, position_unwind_risk,
            created_at, updated_at
        FROM ranked
        WHERE rn = 1
        ORDER BY ABS(COALESCE(net_position_z_3y, 0)) DESC, contract_code
        LIMIT :limit
        """
    )
    try:
        return pd.read_sql(sql, engine, params={"limit": limit})
    except Exception:
        return pd.DataFrame()


def run_cot_fetch(engine, *, years: list[int] | None = None, report_type: str = "financial", dry_run: bool = False, timeout: int = 60) -> dict:
    started = time.perf_counter()
    try:
        rows = fetch_normalized_cot_rows(years=years, report_type=report_type, timeout=timeout)
        latest = max((row["report_date"] for row in rows), default=None)
        count = 0 if dry_run else upsert_cot_positions(engine, rows)
        record_flow_source_health(
            engine,
            source_name="CFTC COT",
            source_type="cftc_cot",
            succeeded=True,
            fetch_seconds=time.perf_counter() - started,
            latest_available_date=latest,
        )
        return {"rows": len(rows), "upserted": count, "latest_available_date": latest}
    except Exception as exc:
        record_flow_source_health(
            engine,
            source_name="CFTC COT",
            source_type="cftc_cot",
            succeeded=False,
            fetch_seconds=time.perf_counter() - started,
            error=str(exc),
        )
        raise
