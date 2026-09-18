import time
import random
import pandas as pd
import yfinance as yf
import argparse
import sys

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import text

import _bootstrap  # noqa: F401
from db_builder.config import MACRO_TABLE, local_engine as make_local_engine
from db_builder.config import neon_engine as make_neon_engine

# ============================================================
# CONFIG
# ============================================================

TABLE_NAME = MACRO_TABLE
LIVE_TABLE_NAME = "public.macro_live"

BATCH_SIZE = 50
GLOBAL_BUFFER_MINUTES = 30

# Target window to insert/update
LOOKBACK_DAYS = 5

# Extra days to fetch before the target window so the first target day
# can calculate prev_close/change/pct_chg/amplitude correctly.
CALC_BUFFER_DAYS = 5

# A refresh is operationally acceptable only when every core market series is
# available and at least 90% of the requested registry returns a usable daily
# observation. All other configured symbols are supplementary: isolated
# failures may be accepted, but are reported as PARTIAL FAILURE rather than
# SUCCESS. Provider fallback is intentionally outside this policy.
MIN_COVERAGE_PCT = 90.0
REQUIRED_SYMBOLS = frozenset(
    {
        "^GSPC",
        "^NDX",
        "^DJI",
        "^VIX",
        "^HSI",
        "^N225",
        "^KS11",
        "GC=F",
        "CL=F",
        "BZ=F",
        "^FVX",
        "^TNX",
        "^TYX",
        "US2YT=X",
        "US10YT=X",
        "US30YT=X",
        "DX-Y.NYB",
        "EURUSD=X",
        "JPY=X",
        "CNY=X",
    }
)

local_engine = make_local_engine()
neon_engine = make_neon_engine()


# ============================================================
# ASSETS
# ============================================================

ASSETS = [
    ("^GSPC", "S&P 500", "stock_index"),
    ("^NDX", "NASDAQ 100", "stock_index"),
    ("^IXIC", "NASDAQ Composite", "stock_index"),
    ("^DJI", "Dow Jones Industrial Average", "stock_index"),
    ("^RUT", "Russell 2000 Index", "stock_index"),
    ("^VIX", "CBOE Volatility Index", "stock_index"),
    ("^SKEW", "CBOE SKEW Index", "volatility"),
    ("^MOVE", "ICE BofA MOVE Index", "volatility"),
    ("^HSI", "HANG SENG INDEX", "stock_index"),
    ("^N225", "Nikkei 225", "stock_index"),
    ("^KS11", "KOSPI Composite Index", "stock_index"),
    ("^KS200", "KOSPI 200 Index", "stock_index"),
    ("000001.SS", "SSE Composite Index", "stock_index"),
    ("^FTSE", "FTSE 100", "stock_index"),
    ("^GDAXI", "DAX", "stock_index"),
    ("^FCHI", "CAC 40", "stock_index"),

    ("NQ=F", "Nasdaq 100 Future", "futures"),
    ("ES=F", "E-mini S&P 500 Future", "futures"),
    ("YM=F", "Mini Dow Future", "futures"),
    ("RTY=F", "E-mini Russell 2000 Future", "futures"),
    ("NIY=F", "Nikkei 225 Future", "futures"),
    ("KOR200c1", "KOSPI 200 Futures", "futures"),
    ("HK50", "Hang Seng Futures", "futures"),
    ("CIHc1", "SSE 50 Futures", "futures"),
    ("GC=F", "Gold Future", "futures"),
    ("CL=F", "WTI Crude Oil Future", "futures"),
    ("BZ=F", "Brent Crude Oil Future", "futures"),
    ("HG=F", "Copper Future", "futures"),
    ("SI=F", "Silver Future", "futures"),
    ("NG=F", "Natural Gas Future", "futures"),

    ("^FVX", "Treasury Yield 5 Years", "ust_yield"),
    ("^TNX", "Treasury Yield 10 Years", "ust_yield"),
    ("^TYX", "Treasury Yield 30 Years", "ust_yield"),
    ("US2YT=X", "United States 2-Year Treasury Yield", "ust_yield"),
    ("US3YT=X", "United States 3-Year Treasury Yield", "ust_yield"),
    ("US5YT=X", "United States 5-Year Treasury Yield", "ust_yield"),
    ("US7YT=X", "United States 7-Year Treasury Yield", "ust_yield"),
    ("US10YT=X", "United States 10-Year Treasury Yield", "ust_yield"),
    ("US20YT=X", "United States 20-Year Treasury Yield", "ust_yield"),
    ("US30YT=X", "United States 30-Year Treasury Yield", "ust_yield"),

    ("EURUSD=X", "EUR/USD", "fx"),
    ("DX-Y.NYB", "US Dollar Index", "fx"),
    ("JPY=X", "USD/JPY", "fx"),
    ("GBPUSD=X", "GBP/USD", "fx"),
    ("AUDUSD=X", "AUD/USD", "fx"),
    ("NZDUSD=X", "NZD/USD", "fx"),
    ("CNY=X", "USD/CNY", "fx"),
    ("HKD=X", "USD/HKD", "fx"),
    ("SGD=X", "USD/SGD", "fx"),
    ("CHFUSD=X", "CHF/USD", "fx"),

    ("BTC-USD", "Bitcoin USD", "crypto"),
    ("ETH-USD", "Ethereum USD", "crypto"),

    ("HYG", "iShares iBoxx High Yield Corporate Bond ETF", "credit_proxy"),
    ("LQD", "iShares iBoxx Investment Grade Corporate Bond ETF", "credit_proxy"),
    ("JNK", "SPDR Bloomberg High Yield Bond ETF", "credit_proxy"),
    ("RSP", "Invesco S&P 500 Equal Weight ETF", "equity_style"),
    ("IWF", "iShares Russell 1000 Growth ETF", "equity_style"),
    ("IWD", "iShares Russell 1000 Value ETF", "equity_style"),
    ("TLT", "iShares 20+ Year Treasury Bond ETF", "duration_rates"),
    ("IEF", "iShares 7-10 Year Treasury Bond ETF", "duration_rates"),
    ("SHY", "iShares 1-3 Year Treasury Bond ETF", "duration_rates"),
]


@dataclass(frozen=True)
class SymbolFetchOutcome:
    symbol: str
    name: str
    provider: str
    required: bool
    success: bool
    rows_fetched: int
    latest_observation: date | None
    failure_reason: str | None = None


@dataclass(frozen=True)
class MacroCoverageSummary:
    outcomes: tuple[SymbolFetchOutcome, ...]
    configured_count: int
    successful_count: int
    failed_count: int
    coverage_pct: float
    required_failed_symbols: tuple[str, ...]
    provider_failure_counts: tuple[tuple[str, int], ...]
    coverage_satisfied: bool

    @property
    def exit_code(self):
        return 0 if self.coverage_satisfied else 1


class SymbolProcessingError(RuntimeError):
    def __init__(self, message, *, rows_fetched=0, latest_observation=None):
        super().__init__(message)
        self.rows_fetched = rows_fetched
        self.latest_observation = latest_observation

INVESTINY_ASSETS = {
    "US2YT=X": 23701,
    "US3YT=X": 23702,
    "US5YT=X": 23703,
    "US7YT=X": 23704,
    "US10YT=X": 23705,
    "US20YT=X": 1161827,
    "US30YT=X": 23706,
    "KOR200c1": 8987,
    "HK50": 8984,
    "CIHc1": 1115062,
    # Investing.com continuous front-contract instruments. The public source
    # labels currently display as LCOV6 (Brent) and OIL (WTI), while the
    # canonical database symbols remain BZ=F and CL=F for API compatibility.
    "BZ=F": 8833,
    "CL=F": 8849,
}

INVESTING_SOURCE_SYMBOLS = {
    "BZ=F": "LCOV6",
    "CL=F": "OIL",
}


# ============================================================
# CLOSE TIME MAP
# ============================================================

SYMBOL_CLOSE_MAP = {
    "^DJI": {"tz": "America/New_York", "close_time": "16:00"},
    "^FCHI": {"tz": "Europe/Paris", "close_time": "17:30"},
    "^FTSE": {"tz": "Europe/London", "close_time": "16:30"},
    "^GDAXI": {"tz": "Europe/Berlin", "close_time": "17:30"},
    "^GSPC": {"tz": "America/New_York", "close_time": "16:00"},
    "^NDX": {"tz": "America/New_York", "close_time": "16:00"},
    "^HSI": {"tz": "Asia/Hong_Kong", "close_time": "16:10"},
    "^IXIC": {"tz": "America/New_York", "close_time": "16:00"},
    "^KS11": {"tz": "Asia/Seoul", "close_time": "15:30"},
    "^KS200": {"tz": "Asia/Seoul", "close_time": "15:30"},
    "^N225": {"tz": "Asia/Tokyo", "close_time": "15:30"},
    "^RUT": {"tz": "America/New_York", "close_time": "16:00"},
    "^VIX": {"tz": "America/Chicago", "close_time": "15:15"},
    "^SKEW": {"tz": "America/Chicago", "close_time": "15:15"},
    "^MOVE": {"tz": "America/New_York", "close_time": "16:00"},
    "000001.SS": {"tz": "Asia/Shanghai", "close_time": "15:00"},

    "NQ=F": {"tz": "America/New_York", "close_time": "17:00"},
    "ES=F": {"tz": "America/New_York", "close_time": "17:00"},
    "YM=F": {"tz": "America/New_York", "close_time": "17:00"},
    "RTY=F": {"tz": "America/New_York", "close_time": "17:00"},
    "NIY=F": {"tz": "America/Chicago", "close_time": "16:00"},
    "KOR200c1": {"tz": "Asia/Seoul", "close_time": "15:45"},
    "HK50": {"tz": "Asia/Hong_Kong", "close_time": "16:30"},
    "CIHc1": {"tz": "Asia/Shanghai", "close_time": "15:15"},
    "GC=F": {"tz": "America/New_York", "close_time": "17:00"},
    "CL=F": {"tz": "America/New_York", "close_time": "17:00"},
    "BZ=F": {"tz": "America/New_York", "close_time": "17:00"},
    "HG=F": {"tz": "America/New_York", "close_time": "17:00"},
    "SI=F": {"tz": "America/New_York", "close_time": "17:00"},
    "NG=F": {"tz": "America/New_York", "close_time": "17:00"},

    "^FVX": {"tz": "America/New_York", "close_time": "16:00"},
    "^TNX": {"tz": "America/New_York", "close_time": "16:00"},
    "^TYX": {"tz": "America/New_York", "close_time": "16:00"},
    "US2YT=X": {"tz": "America/New_York", "close_time": "16:00"},
    "US3YT=X": {"tz": "America/New_York", "close_time": "16:00"},
    "US5YT=X": {"tz": "America/New_York", "close_time": "16:00"},
    "US7YT=X": {"tz": "America/New_York", "close_time": "16:00"},
    "US10YT=X": {"tz": "America/New_York", "close_time": "16:00"},
    "US20YT=X": {"tz": "America/New_York", "close_time": "16:00"},
    "US30YT=X": {"tz": "America/New_York", "close_time": "16:00"},

    "EURUSD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "DX-Y.NYB": {"tz": "America/New_York", "close_time": "17:00"},
    "JPY=X": {"tz": "America/New_York", "close_time": "17:00"},
    "GBPUSD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "AUDUSD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "NZDUSD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "CNY=X": {"tz": "America/New_York", "close_time": "17:00"},
    "HKD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "SGD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "CHFUSD=X": {"tz": "America/New_York", "close_time": "17:00"},

    "BTC-USD": {"tz": "UTC", "close_time": "23:59"},
    "ETH-USD": {"tz": "UTC", "close_time": "23:59"},

    "HYG": {"tz": "America/New_York", "close_time": "16:00"},
    "LQD": {"tz": "America/New_York", "close_time": "16:00"},
    "JNK": {"tz": "America/New_York", "close_time": "16:00"},
    "RSP": {"tz": "America/New_York", "close_time": "16:00"},
    "IWF": {"tz": "America/New_York", "close_time": "16:00"},
    "IWD": {"tz": "America/New_York", "close_time": "16:00"},
    "TLT": {"tz": "America/New_York", "close_time": "16:00"},
    "IEF": {"tz": "America/New_York", "close_time": "16:00"},
    "SHY": {"tz": "America/New_York", "close_time": "16:00"},
}

# ============================================================
# DATABASE
# ============================================================

def create_macro_table(engine):
    sql = text("""
        CREATE TABLE IF NOT EXISTS public.macro (
            date DATE NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            asset_type TEXT,

            open NUMERIC(20,4),
            high NUMERIC(20,4),
            low NUMERIC(20,4),
            close NUMERIC(20,4),
            adj_close NUMERIC(20,4),
            volume NUMERIC(20,4),

            prev_close NUMERIC(20,4),
            change NUMERIC(20,4),
            pct_chg NUMERIC(20,4),
            amplitude NUMERIC(20,4),

            PRIMARY KEY (date, symbol)
        );
    """)

    with engine.begin() as conn:
        conn.execute(sql)


def create_macro_live_table(engine):
    sql = text("""
        CREATE TABLE IF NOT EXISTS public.macro_live (
            symbol TEXT PRIMARY KEY,
            market_date DATE NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            name TEXT,
            asset_type TEXT,

            open NUMERIC(20,4),
            high NUMERIC(20,4),
            low NUMERIC(20,4),
            close NUMERIC(20,4),
            adj_close NUMERIC(20,4),
            volume NUMERIC(20,4),

            prev_close NUMERIC(20,4),
            change NUMERIC(20,4),
            pct_chg NUMERIC(20,4),
            amplitude NUMERIC(20,4),
            is_market_closed BOOLEAN NOT NULL DEFAULT FALSE,
            source TEXT NOT NULL DEFAULT 'yfinance'
        );
    """)

    with engine.begin() as conn:
        conn.execute(sql)


def _prepare_macro_live_rows(rows):
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.where(pd.notna(df), None)
        duplicate_count = int(df.duplicated(subset=["symbol"], keep="last").sum())
        if duplicate_count:
            duplicate_symbols = sorted(
                df.loc[df.duplicated(subset=["symbol"], keep=False), "symbol"]
                .astype(str)
                .unique()
                .tolist()
            )
            print(
                "[LIVE SNAPSHOT DEDUPLICATED] "
                f"rows={duplicate_count:,} symbols={duplicate_symbols}"
            )
            df = df.drop_duplicates(subset=["symbol"], keep="last")
    return df


def _macro_live_upsert_sql():
    return text("""
        INSERT INTO public.macro_live (
            symbol, market_date, observed_at, name, asset_type,
            open, high, low, close, adj_close, volume,
            prev_close, change, pct_chg, amplitude,
            is_market_closed, source
        )
        VALUES (
            :symbol, :market_date, :observed_at, :name, :asset_type,
            :open, :high, :low, :close, :adj_close, :volume,
            :prev_close, :change, :pct_chg, :amplitude,
            :is_market_closed, :source
        )
        ON CONFLICT (symbol) DO UPDATE SET
            market_date = EXCLUDED.market_date,
            observed_at = EXCLUDED.observed_at,
            name = EXCLUDED.name,
            asset_type = EXCLUDED.asset_type,
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            adj_close = EXCLUDED.adj_close,
            volume = EXCLUDED.volume,
            prev_close = EXCLUDED.prev_close,
            change = EXCLUDED.change,
            pct_chg = EXCLUDED.pct_chg,
            amplitude = EXCLUDED.amplitude,
            is_market_closed = EXCLUDED.is_market_closed,
            source = EXCLUDED.source;
    """)


def replace_macro_live(engine, rows):
    """Atomically replace a complete transient intraday snapshot."""
    df = _prepare_macro_live_rows(rows)

    with engine.begin() as conn:
        # Serialize overlapping hourly/manual refreshes within each database.
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('macro_live_replace'))"))
        # This table intentionally contains only the latest fetch snapshot.
        conn.execute(text("DELETE FROM public.macro_live"))
        if not df.empty:
            conn.execute(_macro_live_upsert_sql(), df.to_dict(orient="records"))


def merge_macro_live(engine, rows, successful_symbols):
    """Refresh successful symbols while retaining failed symbols unchanged."""
    symbols = sorted(set(successful_symbols))
    if not symbols:
        return

    df = _prepare_macro_live_rows(rows)
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('macro_live_replace'))"))
        # Only successful symbols are cleared. Failed symbols keep their prior
        # observed_at, making retained stale observations distinguishable.
        conn.execute(
            text("DELETE FROM public.macro_live WHERE symbol = ANY(:symbols)"),
            {"symbols": symbols},
        )
        if not df.empty:
            conn.execute(_macro_live_upsert_sql(), df.to_dict(orient="records"))


def fetch_existing_symbol_dates(engine, symbols, min_date, max_date):
    sql = text("""
        SELECT symbol, date
        FROM public.macro
        WHERE symbol = ANY(:symbols)
          AND date BETWEEN :min_date AND :max_date
    """)

    with engine.begin() as conn:
        df = pd.read_sql(
            sql,
            conn,
            params={
                "symbols": symbols,
                "min_date": min_date,
                "max_date": max_date,
            },
        )

    if df.empty:
        return pd.DataFrame(columns=["symbol", "date"])

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["symbol"] = df["symbol"].astype(str)

    return df


def upsert_macro(engine, rows):
    if not rows:
        return

    df = pd.DataFrame(rows)
    df = df.where(pd.notna(df), None)

    sql = text("""
        INSERT INTO public.macro (
            date, symbol, name, asset_type,
            open, high, low, close, adj_close, volume,
            prev_close, change, pct_chg, amplitude
        )
        VALUES (
            :date, :symbol, :name, :asset_type,
            :open, :high, :low, :close, :adj_close, :volume,
            :prev_close, :change, :pct_chg, :amplitude
        )
        ON CONFLICT (date, symbol)
        DO UPDATE SET
            name = EXCLUDED.name,
            asset_type = EXCLUDED.asset_type,
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            adj_close = EXCLUDED.adj_close,
            volume = EXCLUDED.volume,
            prev_close = EXCLUDED.prev_close,
            change = EXCLUDED.change,
            pct_chg = EXCLUDED.pct_chg,
            amplitude = EXCLUDED.amplitude;
    """)

    with engine.begin() as conn:
        conn.execute(sql, df.to_dict(orient="records"))


# ============================================================
# HELPERS
# ============================================================

def chunk_list(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def round_4(value):
    if pd.isna(value):
        return None
    return round(float(value), 4)


def get_close_config(symbol):
    if symbol not in SYMBOL_CLOSE_MAP:
        raise ValueError(f"Missing SYMBOL_CLOSE_MAP config for: {symbol}")

    return SYMBOL_CLOSE_MAP[symbol]


def symbol_date_has_closed(symbol, bar_date):
    cfg = get_close_config(symbol)

    tz = ZoneInfo(cfg["tz"])
    now_local = datetime.now(tz)

    close_hour, close_minute = map(int, cfg["close_time"].split(":"))

    bar_close_dt = datetime.combine(
        bar_date,
        datetime.min.time(),
        tzinfo=tz,
    ).replace(
        hour=close_hour,
        minute=close_minute,
        second=0,
        microsecond=0,
    )

    bar_close_dt += timedelta(minutes=GLOBAL_BUFFER_MINUTES)

    return now_local >= bar_close_dt


def remove_unfinished_bars(df, symbol):
    if df.empty:
        return df

    df = df.sort_values("Date").copy()
    keep_mask = []

    for _, row in df.iterrows():
        bar_date = pd.to_datetime(row["Date"]).date()
        keep_mask.append(symbol_date_has_closed(symbol, bar_date))

    return df[keep_mask].copy()


def extract_latest_unfinished_row(df, symbol, name, asset_type, observed_at=None):
    """Return the latest incomplete daily bar as a transient live snapshot."""
    if df.empty or len(df) < 2:
        return None

    ordered = df.sort_values("Date").copy()
    latest_date = pd.to_datetime(ordered.iloc[-1]["Date"]).date()
    if symbol_date_has_closed(symbol, latest_date):
        return None

    calculated = calculate_rows_for_symbol(
        df=ordered,
        symbol=symbol,
        name=name,
        asset_type=asset_type,
    )
    if not calculated:
        return None

    latest = calculated[-1]
    if latest["date"] != latest_date:
        return None

    latest["market_date"] = latest.pop("date")
    latest["observed_at"] = observed_at or datetime.now(ZoneInfo("UTC"))
    latest["is_market_closed"] = False
    latest["source"] = "yfinance"
    return latest


def calculate_rows_for_symbol(df, symbol, name, asset_type):
    if df.empty or len(df) < 2:
        return []

    df = df.sort_values("Date").copy()
    rows = []

    for i in range(1, len(df)):
        latest = df.iloc[i]
        previous = df.iloc[i - 1]

        open_price = latest.get("Open")
        high = latest.get("High")
        low = latest.get("Low")
        close = latest.get("Close")
        adj_close = latest.get("Adj Close")
        volume = latest.get("Volume")
        prev_close = previous.get("Close")

        change = None
        pct_chg = None
        amplitude = None

        if pd.notna(close) and pd.notna(prev_close) and prev_close != 0:
            change = close - prev_close
            pct_chg = (change / prev_close) * 100

            if pd.notna(high) and pd.notna(low):
                amplitude = ((high - low) / prev_close) * 100

        rows.append({
            "date": pd.to_datetime(latest["Date"]).date(),
            "symbol": symbol,
            "name": name,
            "asset_type": asset_type,
            "open": round_4(open_price),
            "high": round_4(high),
            "low": round_4(low),
            "close": round_4(close),
            "adj_close": round_4(adj_close),
            "volume": round_4(volume),
            "prev_close": round_4(prev_close),
            "change": round_4(change),
            "pct_chg": round_4(pct_chg),
            "amplitude": round_4(amplitude),
        })

    return rows


def filter_rows_to_target_window(rows, target_start_date):
    return [
        row for row in rows
        if row["date"] >= target_start_date
    ]


def filter_duplicate_symbol_dates(rows, existing_df):
    if not rows:
        return []

    df_new = pd.DataFrame(rows)
    df_new["date"] = pd.to_datetime(df_new["date"]).dt.date
    df_new["symbol"] = df_new["symbol"].astype(str)

    if existing_df.empty:
        return df_new.to_dict(orient="records")

    existing_df = existing_df.copy()
    existing_df["date"] = pd.to_datetime(existing_df["date"]).dt.date
    existing_df["symbol"] = existing_df["symbol"].astype(str)

    df_new["_key"] = df_new["symbol"] + "|" + df_new["date"].astype(str)
    existing_df["_key"] = existing_df["symbol"] + "|" + existing_df["date"].astype(str)

    df_missing = df_new[~df_new["_key"].isin(existing_df["_key"])].copy()
    df_missing = df_missing.drop(columns=["_key"])

    return df_missing.to_dict(orient="records")


def upsert_new_macro_rows(rows, symbols, *, upsert_neon=True, dry_run=False, provider_label="macro"):
    if not rows:
        return 0

    rows_df = pd.DataFrame(rows)
    min_date = rows_df["date"].min()
    max_date = rows_df["date"].max()

    local_existing_df = fetch_existing_symbol_dates(
        local_engine,
        symbols=symbols,
        min_date=min_date,
        max_date=max_date,
    )

    print(
        f"[EXISTING LOCAL {provider_label}] rows={len(local_existing_df):,} "
        f"range={min_date} -> {max_date}"
    )

    local_missing_rows = filter_duplicate_symbol_dates(
        rows=rows,
        existing_df=local_existing_df,
    )

    if local_missing_rows:
        print(f"[{provider_label} LOCAL NEW ROWS] {len(local_missing_rows):,}")
        if dry_run:
            print(f"[DRY RUN SKIP LOCAL UPSERT {provider_label}] rows={len(local_missing_rows):,}")
        else:
            print(f"[UPSERT LOCAL {provider_label}] rows={len(local_missing_rows):,}")
            upsert_macro(local_engine, local_missing_rows)
    else:
        print(f"[{provider_label} LOCAL NO NEW ROWS AFTER DEDUPE]")

    neon_missing_rows = []
    if upsert_neon:
        neon_existing_df = fetch_existing_symbol_dates(
            neon_engine,
            symbols=symbols,
            min_date=min_date,
            max_date=max_date,
        )
        print(
            f"[EXISTING NEON {provider_label}] rows={len(neon_existing_df):,} "
            f"range={min_date} -> {max_date}"
        )
        neon_missing_rows = filter_duplicate_symbol_dates(
            rows=rows,
            existing_df=neon_existing_df,
        )
        if neon_missing_rows:
            print(f"[{provider_label} NEON NEW ROWS] {len(neon_missing_rows):,}")
            if dry_run:
                print(f"[DRY RUN SKIP NEON UPSERT {provider_label}] rows={len(neon_missing_rows):,}")
            else:
                print(f"[UPSERT NEON {provider_label}] rows={len(neon_missing_rows):,}")
                upsert_macro(neon_engine, neon_missing_rows)
        else:
            print(f"[{provider_label} NEON NO NEW ROWS AFTER DEDUPE]")

    return max(len(local_missing_rows), len(neon_missing_rows))


# ============================================================
# INVESTINY
# ============================================================

def _investiny_date(value):
    return pd.to_datetime(value, format="%m/%d/%Y", errors="coerce")


def _investiny_retry_delay(error, attempt):
    """Return a longer cooldown when Investing.com throttles with HTTP 403."""
    if "403" in str(error):
        return min(60.0, 8.0 * (2 ** (attempt - 1))) + random.uniform(0.0, 3.0)
    return random.uniform(3.0, 8.0)


def fetch_investiny_symbol_df(symbol, *, start_date=None, end_date=None, max_retries=5):
    investing_id = INVESTINY_ASSETS[symbol]
    source_symbol = INVESTING_SOURCE_SYMBOLS.get(symbol, symbol)
    target_start_date = start_date or (datetime.now().date() - timedelta(days=LOOKBACK_DAYS))
    fetch_start_date = target_start_date - timedelta(days=CALC_BUFFER_DAYS)
    fetch_end_date = end_date or (datetime.now().date() + timedelta(days=1))

    from_date = fetch_start_date.strftime("%m/%d/%Y")
    to_date = fetch_end_date.strftime("%m/%d/%Y")
    print(
        f"[INVESTING.COM FETCH WINDOW] {symbol} source_symbol={source_symbol} "
        f"id={investing_id} "
        f"fetch_start={fetch_start_date} | target_start={target_start_date} | end={fetch_end_date}"
    )

    for attempt in range(1, max_retries + 1):
        try:
            from investiny import historical_data

            time.sleep(random.uniform(0.8, 2.0))
            payload = historical_data(
                investing_id=investing_id,
                from_date=from_date,
                to_date=to_date,
                interval="D",
            )
            df = pd.DataFrame(payload)
            if df.empty:
                return pd.DataFrame(), target_start_date

            df = df.rename(
                columns={
                    "date": "Date",
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume",
                }
            )
            df["Date"] = df["Date"].map(_investiny_date)
            df["Adj Close"] = df["Close"]
            if "Volume" not in df.columns:
                df["Volume"] = None
            df = df.dropna(subset=["Date", "Close"]).sort_values("Date")
            df = df[df["Date"].dt.weekday < 5].copy()
            return df[["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"]], target_start_date
        except Exception as e:
            print(f"[INVESTINY RETRY {attempt}/{max_retries}] {symbol}: {e}")
            if attempt == max_retries:
                raise
            delay = _investiny_retry_delay(e, attempt)
            print(f"[INVESTINY COOLDOWN] {symbol}: {delay:.1f}s")
            time.sleep(delay)


def process_investiny_symbol(symbol, meta, *, start_date=None, observed_at=None):
    symbol_df, target_start_date = fetch_investiny_symbol_df(symbol, start_date=start_date)
    rows_fetched = len(symbol_df)
    latest_observation = None
    try:
        rows_fetched, latest_observation = observation_metadata(symbol_df)

        print(
            f"[INVESTINY DATES RAW] {symbol} | "
            f"{pd.to_datetime(symbol_df['Date']).dt.date.tolist()}"
        )

        live_row = extract_latest_unfinished_row(
            df=symbol_df,
            symbol=symbol,
            name=meta["name"],
            asset_type=meta["asset_type"],
            observed_at=observed_at,
        )
        if live_row:
            live_row["source"] = "investing.com"

        symbol_df = remove_unfinished_bars(symbol_df, symbol)
        if symbol_df.empty:
            print(f"[INVESTINY NO FINISHED BARS] {symbol}")
            return [], live_row, rows_fetched, latest_observation

        rows = calculate_rows_for_symbol(
            df=symbol_df,
            symbol=symbol,
            name=meta["name"],
            asset_type=meta["asset_type"],
        )
        rows = filter_rows_to_target_window(rows=rows, target_start_date=target_start_date)
        if rows:
            print(f"[INVESTINY ROW DATES TARGET] {symbol} | {[row['date'] for row in rows]}")
        else:
            print(f"[INVESTINY NO VALID ROWS IN TARGET WINDOW] {symbol}")
        return rows, live_row, rows_fetched, latest_observation
    except Exception as exc:
        raise SymbolProcessingError(
            f"{type(exc).__name__}: {exc}",
            rows_fetched=rows_fetched,
            latest_observation=latest_observation,
        ) from exc


# ============================================================
# YFINANCE
# ============================================================

def download_recent_batch(symbols, max_retries=5, start_date=None):
    end_date = datetime.now().date() + timedelta(days=1)

    target_start_date = start_date or (datetime.now().date() - timedelta(days=LOOKBACK_DAYS))
    fetch_start_date = target_start_date - timedelta(days=CALC_BUFFER_DAYS)

    print(
        f"[YF FETCH WINDOW] fetch_start={fetch_start_date} | "
        f"target_start={target_start_date} | end={end_date}"
    )

    for attempt in range(1, max_retries + 1):
        try:
            time.sleep(random.uniform(1.0, 3.0))

            df = yf.download(
                tickers=" ".join(symbols),
                start=fetch_start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=False,
                repair=False,
                timeout=30,
            )

            if df.empty:
                print("[EMPTY BATCH]")
                return pd.DataFrame(), target_start_date

            return df, target_start_date

        except Exception as e:
            print(f"[BATCH RETRY {attempt}/{max_retries}] {e}")

            if attempt == max_retries:
                raise

            time.sleep(random.uniform(5.0, 15.0))


def extract_symbol_df_from_batch(batch_df, symbol):
    if batch_df.empty:
        return pd.DataFrame()

    if isinstance(batch_df.columns, pd.MultiIndex):
        symbols_in_df = batch_df.columns.get_level_values(0)

        if symbol not in symbols_in_df:
            print(f"[MISSING IN BATCH] {symbol}")
            return pd.DataFrame()

        df = batch_df[symbol].copy()
    else:
        df = batch_df.copy()

    if df.empty:
        return pd.DataFrame()

    df = df.reset_index()

    close_col = None
    for col in df.columns:
        if str(col).lower() == "close":
            close_col = col
            break

    if close_col is None:
        print(f"[NO CLOSE COLUMN] {symbol}")
        return pd.DataFrame()

    df = df.dropna(subset=[close_col], how="all")

    return df


def provider_for_symbol(symbol):
    return "investing.com" if symbol in INVESTINY_ASSETS else "yfinance"


def observation_metadata(symbol_df):
    """Return usable row count and latest daily observation date."""
    if symbol_df is None or symbol_df.empty:
        raise ValueError("empty provider response")
    if "Date" not in symbol_df.columns or "Close" not in symbol_df.columns:
        raise ValueError("malformed provider response: Date/Close columns required")

    dates = pd.to_datetime(symbol_df["Date"], errors="coerce")
    valid_rows = symbol_df.loc[dates.notna() & symbol_df["Close"].notna()]
    if valid_rows.empty:
        raise ValueError("malformed provider response: no valid Date/Close observations")
    if len(valid_rows) < 2:
        raise ValueError("insufficient provider observations: at least 2 required")
    return len(valid_rows), pd.to_datetime(valid_rows["Date"]).max().date()


def summarize_outcomes(outcomes, minimum_coverage_pct=MIN_COVERAGE_PCT):
    ordered = tuple(sorted(outcomes, key=lambda item: item.symbol))
    configured_count = len(ordered)
    successful_count = sum(item.success for item in ordered)
    failed = tuple(item for item in ordered if not item.success)
    raw_coverage_pct = (
        (successful_count / configured_count) * 100.0 if configured_count else 0.0
    )
    coverage_pct = round(raw_coverage_pct, 2)
    required_failed = tuple(item.symbol for item in failed if item.required)
    providers = sorted({item.provider for item in ordered})
    provider_failure_counts = tuple(
        (provider, sum(not item.success for item in ordered if item.provider == provider))
        for provider in providers
    )
    coverage_satisfied = (
        configured_count > 0
        and not required_failed
        and raw_coverage_pct >= minimum_coverage_pct
    )
    return MacroCoverageSummary(
        outcomes=ordered,
        configured_count=configured_count,
        successful_count=successful_count,
        failed_count=len(failed),
        coverage_pct=coverage_pct,
        required_failed_symbols=required_failed,
        provider_failure_counts=provider_failure_counts,
        coverage_satisfied=coverage_satisfied,
    )


def log_coverage_summary(summary):
    for outcome in summary.outcomes:
        status = "OK" if outcome.success else "FAILED"
        latest = outcome.latest_observation.isoformat() if outcome.latest_observation else "none"
        reason = outcome.failure_reason or "none"
        print(
            f"[MACRO SYMBOL {status}] symbol={outcome.symbol} name={outcome.name} "
            f"provider={outcome.provider} "
            f"required={'yes' if outcome.required else 'no'} rows_fetched={outcome.rows_fetched} "
            f"latest_observation={latest} reason={reason}"
        )

    failed_symbols = [
        f"{item.symbol}({item.name})" for item in summary.outcomes if not item.success
    ]
    print(
        f"[MACRO COVERAGE] configured={summary.configured_count} "
        f"successful={summary.successful_count} failed={summary.failed_count} "
        f"coverage={summary.coverage_pct:.2f}% threshold={MIN_COVERAGE_PCT:.2f}%"
    )
    print(
        "[MACRO FAILED SYMBOLS] "
        + (",".join(failed_symbols) if failed_symbols else "none")
    )
    print(
        "[MACRO REQUIRED FAILURES] "
        + (
            ",".join(summary.required_failed_symbols)
            if summary.required_failed_symbols
            else "none"
        )
    )
    for provider, failure_count in summary.provider_failure_counts:
        print(f"[MACRO PROVIDER FAILURES] provider={provider} failed={failure_count}")


# ============================================================
# DAILY UPDATE
# ============================================================

def run_etf_flow_update_after_macro(*, start_date=None, tickers=None, dry_run=False):
    """Refresh local ETF daily flow data after the macro market refresh.

    ETF daily flow data is maintained locally because the Neon sync contract
    currently covers macro/live market data separately. The derived positioning
    signals are refreshed from the latest local ETF rows.
    """
    from db_builder.etf_flow.run import run_etf_flow_analytics
    from db_builder.etf_flows import ETF_FLOW_UNIVERSE, plan_etf_flow_missing_fetch, run_etf_flow_fetch
    from db_builder.positioning_flow_signals import run_positioning_flow_signal_update, setup_flow_tables

    print("\n[ETF FLOW UPDATE]")
    selected_tickers = [ticker.strip().upper() for ticker in tickers if ticker.strip()] if tickers else None
    print(f"[ETF FLOW UNIVERSE] tickers={len(selected_tickers) if selected_tickers else len(ETF_FLOW_UNIVERSE)}")
    if selected_tickers:
        print(f"[ETF FLOW TICKERS] {','.join(selected_tickers)}")
    if start_date:
        print(f"[ETF FLOW START DATE] {start_date}")
    if dry_run:
        print("[ETF FLOW DRY RUN] No rows will be upserted.")

    if not dry_run:
        setup_flow_tables(local_engine)

    plan = plan_etf_flow_missing_fetch(
        local_engine,
        tickers=selected_tickers,
        start_date=start_date,
    )
    print(
        f"[ETF FLOW TARGET] session={plan['target_date']} "
        f"missing_tickers={len(plan['tickers'])} skipped_current={len(plan['skipped_tickers'])}"
    )
    if plan.get("unsupported_tickers"):
        print(f"[ETF FLOW UNSUPPORTED ISSUER HISTORY] {','.join(plan['unsupported_tickers'])}")
    if plan["latest_dates"]:
        latest_preview = sorted(plan["latest_dates"].items())[:8]
        print(f"[ETF FLOW LATEST PREVIEW] {latest_preview}")
    if not plan["tickers"]:
        print("[ETF FLOW SKIP] Issuer-backed ETF flow data already covers the target session.")
        return {
            "rows": 0,
            "upserted": 0,
            "recomputed": 0,
            "sample": [],
            "plan": plan,
            "skipped": True,
        }

    result = run_etf_flow_fetch(
        local_engine,
        tickers=plan["tickers"],
        dry_run=dry_run,
        start_date=plan["start_date"],
        end_date=plan["target_date"],
        allow_yfinance_fallback=False,
    )
    result["plan"] = plan
    print(
        f"[ETF FLOW RESULT] snapshots={result['rows']:,} "
        f"upserted={result['upserted']:,} recomputed={result['recomputed']:,}"
    )
    if not dry_run:
        analytics_result = run_etf_flow_analytics(
            local_engine,
            as_of_date=plan["target_date"],
            # Analytics needs prior rows for lag/rolling fields. The issuer
            # fetch above remains incremental, but the analytics refresh must
            # include full available history so Monday values can use Friday
            # as the previous session and rolling windows are not truncated.
            start_date=None,
            dry_run=False,
            write_report_output=True,
        )
        print(
            f"[ETF FLOW ANALYTICS] as_of={analytics_result['as_of_date']} "
            f"raw={analytics_result['raw_rows']:,} features={analytics_result['feature_rows']:,} "
            f"signals={analytics_result['representative_signal_rows']:,}"
        )
        signal_result = run_positioning_flow_signal_update(local_engine, dry_run=False)
        print(f"[ETF FLOW SIGNALS] positioning_flow_signals upserted={signal_result['upserted']:,}")
    return result


def run_daily_update(
    symbol_filter=None,
    start_date=None,
    upsert_neon=True,
    dry_run=False,
    update_etf_flows=True,
    etf_flow_start_date=None,
    etf_flow_tickers=None,
    update_live=True,
):
    if not dry_run:
        create_macro_table(local_engine)
        create_macro_live_table(local_engine)
        if upsert_neon:
            create_macro_table(neon_engine)
            create_macro_live_table(neon_engine)

    asset_map = {
        symbol: {
            "name": name,
            "asset_type": asset_type,
        }
        for symbol, name, asset_type in ASSETS
    }

    symbols = list(asset_map.keys())
    if symbol_filter:
        requested = set(symbol_filter)
        unknown = sorted(requested - set(symbols))
        if unknown:
            raise ValueError(f"Unknown macro symbols requested: {unknown}")
        symbols = [symbol for symbol in symbols if symbol in requested]

    yfinance_symbols = [symbol for symbol in symbols if symbol not in INVESTINY_ASSETS]
    investiny_symbols = [symbol for symbol in symbols if symbol in INVESTINY_ASSETS]

    print(
        f"Running macro update for {len(symbols)} symbols "
        f"({len(yfinance_symbols)} yfinance, {len(investiny_symbols)} Investing.com) "
        f"in yfinance batches of {BATCH_SIZE}..."
    )
    if start_date:
        print(f"[START DATE OVERRIDE] target_start={start_date}")
    if dry_run:
        print("[DRY RUN] No rows will be upserted.")
    if not upsert_neon:
        print("[LOCAL ONLY] Neon upsert disabled.")

    all_live_rows = []
    outcomes = {}
    observed_at = datetime.now(ZoneInfo("UTC"))

    for batch in chunk_list(yfinance_symbols, BATCH_SIZE):
        print(f"\n[BATCH] {batch[0]} -> {batch[-1]} | {len(batch)} symbols")

        try:
            batch_df, target_start_date = download_recent_batch(batch, start_date=start_date)
        except Exception as e:
            print(f"[BATCH ERROR] {e}")
            for symbol in batch:
                meta = asset_map[symbol]
                outcomes[symbol] = SymbolFetchOutcome(
                    symbol=symbol,
                    name=meta["name"],
                    provider="yfinance",
                    required=symbol in REQUIRED_SYMBOLS,
                    success=False,
                    rows_fetched=0,
                    latest_observation=None,
                    failure_reason=f"{type(e).__name__}: {e}",
                )
            time.sleep(random.uniform(5.0, 15.0))
            continue

        all_yf_rows = []

        for symbol in batch:
            rows_fetched = 0
            latest_observation = None
            try:
                meta = asset_map[symbol]
                print(f"[PROCESS YF] {symbol}")

                symbol_df = extract_symbol_df_from_batch(batch_df, symbol)
                rows_fetched = len(symbol_df)
                rows_fetched, latest_observation = observation_metadata(symbol_df)

                print(
                    f"[YF DATES RAW] {symbol} | "
                    f"{pd.to_datetime(symbol_df['Date']).dt.date.tolist()}"
                )

                live_row = extract_latest_unfinished_row(
                    df=symbol_df,
                    symbol=symbol,
                    name=meta["name"],
                    asset_type=meta["asset_type"],
                    observed_at=observed_at,
                )
                if live_row:
                    all_live_rows.append(live_row)
                    print(
                        f"[LIVE SNAPSHOT] {symbol} | "
                        f"market_date={live_row['market_date']} "
                        f"close={live_row['close']}"
                    )

                symbol_df = remove_unfinished_bars(symbol_df, symbol)

                if symbol_df.empty:
                    print(f"[NO FINISHED BARS] {symbol}")
                    outcomes[symbol] = SymbolFetchOutcome(
                        symbol=symbol,
                        name=meta["name"],
                        provider="yfinance",
                        required=symbol in REQUIRED_SYMBOLS,
                        success=True,
                        rows_fetched=rows_fetched,
                        latest_observation=latest_observation,
                    )
                    continue

                rows = calculate_rows_for_symbol(
                    df=symbol_df,
                    symbol=symbol,
                    name=meta["name"],
                    asset_type=meta["asset_type"],
                )

                rows = filter_rows_to_target_window(
                    rows=rows,
                    target_start_date=target_start_date,
                )

                if not rows:
                    print(f"[NO VALID ROWS IN TARGET WINDOW] {symbol}")
                    outcomes[symbol] = SymbolFetchOutcome(
                        symbol=symbol,
                        name=meta["name"],
                        provider="yfinance",
                        required=symbol in REQUIRED_SYMBOLS,
                        success=True,
                        rows_fetched=rows_fetched,
                        latest_observation=latest_observation,
                    )
                    continue

                print(
                    f"[YF ROW DATES TARGET] {symbol} | "
                    f"{[row['date'] for row in rows]}"
                )

                all_yf_rows.extend(rows)
                outcomes[symbol] = SymbolFetchOutcome(
                    symbol=symbol,
                    name=meta["name"],
                    provider="yfinance",
                    required=symbol in REQUIRED_SYMBOLS,
                    success=True,
                    rows_fetched=rows_fetched,
                    latest_observation=latest_observation,
                )

            except Exception as e:
                print(f"[PROCESS ERROR] {symbol}: {e}")
                meta = asset_map[symbol]
                outcomes[symbol] = SymbolFetchOutcome(
                    symbol=symbol,
                    name=meta["name"],
                    provider="yfinance",
                    required=symbol in REQUIRED_SYMBOLS,
                    success=False,
                    rows_fetched=rows_fetched,
                    latest_observation=latest_observation,
                    failure_reason=f"{type(e).__name__}: {e}",
                )
                time.sleep(random.uniform(2.0, 5.0))

        if not all_yf_rows:
            print("[BATCH NO YF ROWS]")
            continue

        upsert_new_macro_rows(
            all_yf_rows,
            batch,
            upsert_neon=upsert_neon,
            dry_run=dry_run,
            provider_label="YF",
        )

        print("[BATCH DONE]")

        time.sleep(random.uniform(1.0, 3.0))

    if investiny_symbols:
        print(f"\n[INVESTING.COM SYMBOLS] {investiny_symbols}")
    for symbol in investiny_symbols:
        meta = asset_map[symbol]
        try:
            print(f"[PROCESS INVESTING.COM] {symbol}")
            rows, live_row, rows_fetched, latest_observation = process_investiny_symbol(
                symbol,
                meta,
                start_date=start_date,
                observed_at=observed_at,
            )
        except Exception as e:
            print(f"[INVESTINY PROCESS ERROR] {symbol}: {e}")
            rows_fetched = getattr(e, "rows_fetched", 0)
            latest_observation = getattr(e, "latest_observation", None)
            failure_reason = (
                str(e)
                if isinstance(e, SymbolProcessingError)
                else f"{type(e).__name__}: {e}"
            )
            outcomes[symbol] = SymbolFetchOutcome(
                symbol=symbol,
                name=meta["name"],
                provider="investing.com",
                required=symbol in REQUIRED_SYMBOLS,
                success=False,
                rows_fetched=rows_fetched,
                latest_observation=latest_observation,
                failure_reason=failure_reason,
            )
            time.sleep(random.uniform(2.0, 5.0))
            continue

        if live_row:
            all_live_rows.append(live_row)
            print(
                f"[INVESTING.COM LIVE SNAPSHOT] {symbol} | "
                f"market_date={live_row['market_date']} close={live_row['close']}"
            )
        # Database write errors are fatal and must not be reclassified as an
        # accepted supplementary provider failure.
        upsert_new_macro_rows(
            rows,
            [symbol],
            upsert_neon=upsert_neon,
            dry_run=dry_run,
            provider_label="INVESTING.COM",
        )
        outcomes[symbol] = SymbolFetchOutcome(
            symbol=symbol,
            name=meta["name"],
            provider="investing.com",
            required=symbol in REQUIRED_SYMBOLS,
            success=True,
            rows_fetched=rows_fetched,
            latest_observation=latest_observation,
        )
        # Investing.com intermittently returns 403 when symbols are fetched
        # back-to-back, even though each individual instrument is valid.
        time.sleep(random.uniform(3.0, 6.0))

    for symbol in symbols:
        if symbol not in outcomes:
            meta = asset_map[symbol]
            outcomes[symbol] = SymbolFetchOutcome(
                symbol=symbol,
                name=meta["name"],
                provider=provider_for_symbol(symbol),
                required=symbol in REQUIRED_SYMBOLS,
                success=False,
                rows_fetched=0,
                latest_observation=None,
                failure_reason="symbol was not attempted",
            )

    summary = summarize_outcomes(outcomes.values())
    log_coverage_summary(summary)

    successful_symbols = {
        outcome.symbol for outcome in summary.outcomes if outcome.success
    }
    is_complete_registry_refresh = (
        summary.failed_count == 0 and set(symbols) == set(asset_map)
    )

    print(f"\n[LIVE SNAPSHOT TOTAL] rows={len(all_live_rows):,}")
    if not update_live:
        print("[LIVE REPLACE SKIP] Historical backfill mode.")
    elif dry_run:
        print("[DRY RUN SKIP LIVE REPLACE]")
    elif is_complete_registry_refresh:
        print(f"[REPLACE LOCAL LIVE] rows={len(all_live_rows):,}")
        replace_macro_live(local_engine, all_live_rows)
        if upsert_neon:
            print(f"[REPLACE NEON LIVE] rows={len(all_live_rows):,}")
            replace_macro_live(neon_engine, all_live_rows)
    else:
        print(
            f"[MERGE LOCAL LIVE] rows={len(all_live_rows):,} "
            f"successful_symbols={len(successful_symbols):,} retained_failed={summary.failed_count:,}"
        )
        merge_macro_live(local_engine, all_live_rows, successful_symbols)
        if upsert_neon:
            print(
                f"[MERGE NEON LIVE] rows={len(all_live_rows):,} "
                f"successful_symbols={len(successful_symbols):,} retained_failed={summary.failed_count:,}"
            )
            merge_macro_live(neon_engine, all_live_rows, successful_symbols)

    if update_etf_flows:
        try:
            run_etf_flow_update_after_macro(
                start_date=etf_flow_start_date,
                tickers=etf_flow_tickers,
                dry_run=dry_run,
            )
        except Exception as e:
            print(f"[ETF FLOW ERROR] {e}")
            raise
    else:
        print("[ETF FLOW SKIP] ETF daily flow refresh disabled.")

    if summary.failed_count == 0:
        print("[MACRO UPDATE SUCCESS] Required coverage satisfied; all configured symbols succeeded.")
    elif summary.coverage_satisfied:
        print(
            "[MACRO UPDATE PARTIAL FAILURE] Required coverage satisfied; "
            "supplementary failures retained and reported."
        )
    else:
        print(
            "[MACRO UPDATE FAILURE] Required coverage not satisfied; "
            "returning non-zero."
        )
    return summary


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Fetch macro/index/ETF data into public.macro.")
    parser.add_argument("--symbols", default=None, help="Comma-separated symbol subset.")
    parser.add_argument("--start-date", default=None, help="Target start date in YYYY-MM-DD format.")
    parser.add_argument("--local-only", action="store_true", help="Upsert local PostgreSQL only.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and calculate rows without upserting.")
    parser.add_argument("--skip-etf-flows", action="store_true", help="Skip local ETF daily flow refresh after macro update.")
    parser.add_argument("--skip-live-replace", action="store_true", help="Do not replace public.macro_live; useful for historical symbol-subset backfills.")
    parser.add_argument("--etf-flow-start-date", default=None, help="Optional ETF flow backfill start date in YYYY-MM-DD format.")
    parser.add_argument("--etf-flow-tickers", default=None, help="Optional comma-separated ETF subset for ETF flow refresh.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    requested_symbols = [symbol.strip() for symbol in args.symbols.split(",") if symbol.strip()] if args.symbols else None
    start = datetime.strptime(args.start_date, "%Y-%m-%d").date() if args.start_date else None
    etf_flow_start = datetime.strptime(args.etf_flow_start_date, "%Y-%m-%d").date() if args.etf_flow_start_date else None
    etf_flow_tickers = [ticker.strip().upper() for ticker in args.etf_flow_tickers.split(",") if ticker.strip()] if args.etf_flow_tickers else None
    summary = run_daily_update(
        symbol_filter=requested_symbols,
        start_date=start,
        upsert_neon=not args.local_only,
        dry_run=args.dry_run,
        update_etf_flows=not args.skip_etf_flows,
        etf_flow_start_date=etf_flow_start,
        etf_flow_tickers=etf_flow_tickers,
        update_live=not args.skip_live_replace,
    )
    return summary.exit_code


if __name__ == "__main__":
    sys.exit(main())
