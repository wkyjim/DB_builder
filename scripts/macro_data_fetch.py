import time
import random
import pandas as pd
import yfinance as yf

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import text

import _bootstrap  # noqa: F401
from db_builder.config import MACRO_TABLE, local_engine as make_local_engine
from db_builder.config import neon_engine as make_neon_engine

# ============================================================
# CONFIG
# ============================================================

TABLE_NAME = MACRO_TABLE

BATCH_SIZE = 50
GLOBAL_BUFFER_MINUTES = 30

# Target window to insert/update
LOOKBACK_DAYS = 5

# Extra days to fetch before the target window so the first target day
# can calculate prev_close/change/pct_chg/amplitude correctly.
CALC_BUFFER_DAYS = 5

local_engine = make_local_engine()
neon_engine = make_neon_engine()


# ============================================================
# ASSETS
# ============================================================

ASSETS = [
    ("^GSPC", "S&P 500", "stock_index"),
    ("^IXIC", "NASDAQ Composite", "stock_index"),
    ("^DJI", "Dow Jones Industrial Average", "stock_index"),
    ("^RUT", "Russell 2000 Index", "stock_index"),
    ("^VIX", "CBOE Volatility Index", "stock_index"),
    ("^HSI", "HANG SENG INDEX", "stock_index"),
    ("^N225", "Nikkei 225", "stock_index"),
    ("^KS11", "KOSPI Composite Index", "stock_index"),
    ("000001.SS", "SSE Composite Index", "stock_index"),
    ("^FTSE", "FTSE 100", "stock_index"),
    ("^GDAXI", "DAX", "stock_index"),
    ("^FCHI", "CAC 40", "stock_index"),

    ("NQ=F", "Nasdaq 100 Future", "futures"),
    ("ES=F", "E-mini S&P 500 Future", "futures"),
    ("YM=F", "Mini Dow Future", "futures"),
    ("RTY=F", "E-mini Russell 2000 Future", "futures"),
    ("GC=F", "Gold Future", "futures"),
    ("CL=F", "WTI Crude Oil Future", "futures"),
    ("BZ=F", "Brent Crude Oil Future", "futures"),
    ("HG=F", "Copper Future", "futures"),
    ("SI=F", "Silver Future", "futures"),
    ("NG=F", "Natural Gas Future", "futures"),

    ("^FVX", "Treasury Yield 5 Years", "ust_yield"),
    ("^TNX", "Treasury Yield 10 Years", "ust_yield"),
    ("^TYX", "Treasury Yield 30 Years", "ust_yield"),

    ("EURUSD=X", "EUR/USD", "fx"),
    ("JPY=X", "USD/JPY", "fx"),
    ("GBPUSD=X", "GBP/USD", "fx"),
    ("AUDUSD=X", "AUD/USD", "fx"),
    ("NZDUSD=X", "NZD/USD", "fx"),
    ("CNY=X", "USD/CNY", "fx"),
    ("HKD=X", "USD/HKD", "fx"),
    ("SGD=X", "USD/SGD", "fx"),

    ("BTC-USD", "Bitcoin USD", "crypto"),
    ("ETH-USD", "Ethereum USD", "crypto"),
]


# ============================================================
# CLOSE TIME MAP
# ============================================================

SYMBOL_CLOSE_MAP = {
    "^DJI": {"tz": "America/New_York", "close_time": "16:00"},
    "^FCHI": {"tz": "Europe/Paris", "close_time": "17:30"},
    "^FTSE": {"tz": "Europe/London", "close_time": "16:30"},
    "^GDAXI": {"tz": "Europe/Berlin", "close_time": "17:30"},
    "^GSPC": {"tz": "America/New_York", "close_time": "16:00"},
    "^HSI": {"tz": "Asia/Hong_Kong", "close_time": "16:10"},
    "^IXIC": {"tz": "America/New_York", "close_time": "16:00"},
    "^KS11": {"tz": "Asia/Seoul", "close_time": "15:30"},
    "^N225": {"tz": "Asia/Tokyo", "close_time": "15:30"},
    "^RUT": {"tz": "America/New_York", "close_time": "16:00"},
    "^VIX": {"tz": "America/Chicago", "close_time": "15:15"},
    "000001.SS": {"tz": "Asia/Shanghai", "close_time": "15:00"},

    "NQ=F": {"tz": "America/New_York", "close_time": "17:00"},
    "ES=F": {"tz": "America/New_York", "close_time": "17:00"},
    "YM=F": {"tz": "America/New_York", "close_time": "17:00"},
    "RTY=F": {"tz": "America/New_York", "close_time": "17:00"},
    "GC=F": {"tz": "America/New_York", "close_time": "17:00"},
    "CL=F": {"tz": "America/New_York", "close_time": "17:00"},
    "BZ=F": {"tz": "America/New_York", "close_time": "17:00"},
    "HG=F": {"tz": "America/New_York", "close_time": "17:00"},
    "SI=F": {"tz": "America/New_York", "close_time": "17:00"},
    "NG=F": {"tz": "America/New_York", "close_time": "17:00"},

    "^FVX": {"tz": "America/New_York", "close_time": "16:00"},
    "^TNX": {"tz": "America/New_York", "close_time": "16:00"},
    "^TYX": {"tz": "America/New_York", "close_time": "16:00"},

    "EURUSD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "JPY=X": {"tz": "America/New_York", "close_time": "17:00"},
    "GBPUSD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "AUDUSD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "NZDUSD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "CNY=X": {"tz": "America/New_York", "close_time": "17:00"},
    "HKD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "SGD=X": {"tz": "America/New_York", "close_time": "17:00"},

    "BTC-USD": {"tz": "UTC", "close_time": "23:59"},
    "ETH-USD": {"tz": "UTC", "close_time": "23:59"},
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


# ============================================================
# YFINANCE
# ============================================================

def download_recent_batch(symbols, max_retries=5):
    end_date = datetime.now().date() + timedelta(days=1)

    target_start_date = datetime.now().date() - timedelta(days=LOOKBACK_DAYS)
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


# ============================================================
# DAILY UPDATE
# ============================================================

def run_daily_update():
    create_macro_table(local_engine)
    create_macro_table(neon_engine)

    asset_map = {
        symbol: {
            "name": name,
            "asset_type": asset_type,
        }
        for symbol, name, asset_type in ASSETS
    }

    symbols = list(asset_map.keys())

    print(
        f"Running macro update for {len(symbols)} symbols "
        f"in batches of {BATCH_SIZE}..."
    )

    for batch in chunk_list(symbols, BATCH_SIZE):
        print(f"\n[BATCH] {batch[0]} -> {batch[-1]} | {len(batch)} symbols")

        try:
            batch_df, target_start_date = download_recent_batch(batch)
        except Exception as e:
            print(f"[BATCH ERROR] {e}")
            time.sleep(random.uniform(5.0, 15.0))
            continue

        all_yf_rows = []

        for symbol in batch:
            try:
                meta = asset_map[symbol]
                print(f"[PROCESS YF] {symbol}")

                symbol_df = extract_symbol_df_from_batch(batch_df, symbol)

                if symbol_df.empty:
                    print(f"[NO DATA] {symbol}")
                    continue

                print(
                    f"[YF DATES RAW] {symbol} | "
                    f"{pd.to_datetime(symbol_df['Date']).dt.date.tolist()}"
                )

                symbol_df = remove_unfinished_bars(symbol_df, symbol)

                if symbol_df.empty:
                    print(f"[NO FINISHED BARS] {symbol}")
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
                    continue

                print(
                    f"[YF ROW DATES TARGET] {symbol} | "
                    f"{[row['date'] for row in rows]}"
                )

                all_yf_rows.extend(rows)

            except Exception as e:
                print(f"[PROCESS ERROR] {symbol}: {e}")
                time.sleep(random.uniform(2.0, 5.0))

        if not all_yf_rows:
            print("[BATCH NO YF ROWS]")
            continue

        yf_df = pd.DataFrame(all_yf_rows)
        min_date = yf_df["date"].min()
        max_date = yf_df["date"].max()

        existing_df = fetch_existing_symbol_dates(
            local_engine,
            symbols=batch,
            min_date=min_date,
            max_date=max_date,
        )

        print(
            f"[EXISTING LOCAL] rows={len(existing_df):,} "
            f"range={min_date} -> {max_date}"
        )

        missing_rows = filter_duplicate_symbol_dates(
            rows=all_yf_rows,
            existing_df=existing_df,
        )

        if not missing_rows:
            print("[BATCH NO NEW ROWS AFTER DEDUPE]")
            time.sleep(random.uniform(1.0, 3.0))
            continue

        print(f"[NEW ROWS] {len(missing_rows):,}")

        debug_missing = pd.DataFrame(missing_rows)
        print("[MISSING ROWS BY SYMBOL]")
        print(
            debug_missing.groupby("symbol")["date"]
            .apply(lambda x: sorted(list(x)))
            .to_string()
        )

        print(f"[UPSERT LOCAL] rows={len(missing_rows):,}")
        upsert_macro(local_engine, missing_rows)

        print(f"[UPSERT NEON] rows={len(missing_rows):,}")
        upsert_macro(neon_engine, missing_rows)

        print("[BATCH DONE]")

        time.sleep(random.uniform(1.0, 3.0))

    print("\n[DAILY UPDATE DONE]")


if __name__ == "__main__":
    run_daily_update()


