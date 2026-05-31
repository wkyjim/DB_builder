import os
import time
import random
import pandas as pd
import yfinance as yf
from datetime import timedelta
from sqlalchemy import text

import _bootstrap
from db_builder.config import RAW_TABLE, local_engine as make_local_engine


# ============================================================
# CONFIG
# ============================================================

CUSTOM_START_DATE = "2026-05-20"
CUSTOM_END_DATE = "2026-05-30"

FETCH_BUFFER_DAYS = 3
BATCH_SIZE = 100

LOG_FILE = _bootstrap.LOGS_PATH / f"completed_range_{CUSTOM_START_DATE}_{CUSTOM_END_DATE}.log"

TABLE_NAME = RAW_TABLE

engine = make_local_engine()

# ============================================================
# DATE HELPERS
# ============================================================

def date_to_str(d) -> str:
    return pd.to_datetime(d).strftime("%Y-%m-%d")


def date_obj(d):
    return pd.to_datetime(d).date()


def calendar_days_before(date_str: str, days: int) -> str:
    return (
        pd.to_datetime(date_str) - timedelta(days=days)
    ).strftime("%Y-%m-%d")


def next_calendar_day(date_str: str) -> str:
    return (
        pd.to_datetime(date_str) + timedelta(days=1)
    ).strftime("%Y-%m-%d")


# ============================================================
# LOG HELPERS
# ============================================================

def get_completed_tickers() -> set:
    if not os.path.exists(LOG_FILE):
        return set()

    with open(LOG_FILE, "r", encoding="utf-8") as f:
        return set(
            line.strip().upper()
            for line in f
            if line.strip()
        )


def mark_completed(ticker: str):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{ticker.upper()}\n")


# ============================================================
# ERROR HELPER
# ============================================================

def is_no_data_error(error: Exception) -> bool:
    msg = str(error).lower()

    no_data_keywords = [
        "no data",
        "possibly delisted",
        "delisted",
        "no price data found",
        "symbol may be delisted",
        "empty",
        "not found",
        "404",
    ]

    return any(keyword in msg for keyword in no_data_keywords)


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_tickers_from_db() -> pd.DataFrame:
    sql = text(f"""
        SELECT DISTINCT ticker, market
        FROM {TABLE_NAME}
        WHERE ticker IS NOT NULL
        ORDER BY ticker
    """)

    with engine.begin() as conn:
        return pd.read_sql(sql, conn)


def get_existing_dates_for_ticker(
    ticker: str,
    start_date: str,
    end_date: str
) -> set:
    sql = text(f"""
        SELECT date
        FROM {TABLE_NAME}
        WHERE ticker = :ticker
          AND date BETWEEN :start_date AND :end_date
    """)

    with engine.begin() as conn:
        df = pd.read_sql(
            sql,
            conn,
            params={
                "ticker": ticker.upper(),
                "start_date": start_date,
                "end_date": end_date,
            },
        )

    if df.empty:
        return set()

    return set(pd.to_datetime(df["date"]).dt.date)


def get_reference_metrics_from_db(ticker: str, start_date: str):
    """
    Get latest available previous row before the target range.
    Used to estimate mkt_cap and pe_ttm.
    """
    sql = text(f"""
        SELECT date, close, mkt_cap, pe_ttm
        FROM {TABLE_NAME}
        WHERE ticker = :ticker
          AND date < :start_date
          AND close IS NOT NULL
        ORDER BY date DESC
        LIMIT 1
    """)

    with engine.begin() as conn:
        row = conn.execute(
            sql,
            {
                "ticker": ticker.upper(),
                "start_date": start_date,
            },
        ).fetchone()

    return row


def get_ytd_base_close_from_db(ticker: str, custom_start_date: str):
    year_start = f"{pd.to_datetime(custom_start_date).year}-01-01"

    sql = text(f"""
        SELECT date, close
        FROM {TABLE_NAME}
        WHERE ticker = :ticker
          AND date >= :year_start
          AND date < :custom_start_date
          AND close IS NOT NULL
        ORDER BY date ASC
        LIMIT 1
    """)

    with engine.begin() as conn:
        row = conn.execute(
            sql,
            {
                "ticker": ticker.upper(),
                "year_start": year_start,
                "custom_start_date": custom_start_date,
            },
        ).fetchone()

    if row is None:
        return None

    return row.close


# ============================================================
# YFINANCE BATCH FETCHER
# ============================================================

def yf_download_batch_with_retry(
    tickers: list[str],
    start_date: str,
    end_date: str,
    max_retries: int = 5,
) -> pd.DataFrame:

    fetch_start = calendar_days_before(start_date, FETCH_BUFFER_DAYS)
    fetch_end = next_calendar_day(end_date)

    ticker_str = " ".join([t.upper() for t in tickers])

    print(
        f"[YF FETCH] {fetch_start} -> {fetch_end} "
        f"| tickers={len(tickers)}"
    )

    for attempt in range(1, max_retries + 1):
        try:
            time.sleep(random.uniform(1.0, 3.0))

            df = yf.download(
                tickers=ticker_str,
                start=fetch_start,
                end=fetch_end,
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=False,
                timeout=30,
                repair=False,
            )

            if df.empty:
                print("[EMPTY BATCH]")
                return pd.DataFrame()

            return df

        except Exception as e:
            if is_no_data_error(e):
                raise

            print(
                f"[BATCH RETRY {attempt}/{max_retries}] "
                f"{len(tickers)} tickers: {e}"
            )

            if attempt == max_retries:
                raise

            time.sleep(random.uniform(5.0, 15.0))


def normalize_yf_columns_for_ticker(
    batch_download_df: pd.DataFrame,
    ticker: str,
) -> pd.DataFrame:
    """
    Handles both yfinance column formats:

    Case 1:
        ('AA', 'Close')

    Case 2:
        ('Close', 'AA')

    Also handles normal single-index columns.
    """

    ticker = ticker.upper()

    if batch_download_df.empty:
        return pd.DataFrame()

    df = batch_download_df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        level0 = [
            str(x).upper()
            for x in df.columns.get_level_values(0).unique()
        ]

        level1 = [
            str(x).upper()
            for x in df.columns.get_level_values(1).unique()
        ]

        # Case 1: level 0 = ticker, level 1 = fields
        # Example: ('AA', 'Close')
        if ticker in level0:
            df = df[ticker].copy()

        # Case 2: level 0 = fields, level 1 = ticker
        # Example: ('Close', 'AA')
        elif ticker in level1:
            df = df.xs(ticker, axis=1, level=1).copy()

        else:
            print(f"[TICKER NOT FOUND IN YF BATCH] {ticker}")
            print(f"Level 0 sample: {level0[:10]}")
            print(f"Level 1 sample: {level1[:10]}")
            return pd.DataFrame()

    return df


def convert_batch_download_to_ticker_df(
    batch_download_df: pd.DataFrame,
    ticker: str,
    market: str,
) -> pd.DataFrame:

    ticker = ticker.upper()

    try:
        if batch_download_df.empty:
            print(f"[DEBUG EMPTY BATCH DF] {ticker}")
            return pd.DataFrame()

        raw = normalize_yf_columns_for_ticker(
            batch_download_df=batch_download_df,
            ticker=ticker,
        )

        if raw.empty:
            print(f"[DEBUG EMPTY RAW AFTER NORMALIZE] {ticker}")
            return pd.DataFrame()

        raw = raw.dropna(how="all")

        if raw.empty:
            print(f"[DEBUG RAW EMPTY AFTER DROPNA] {ticker}")
            return pd.DataFrame()

        raw = raw.reset_index()

        # Find date column safely
        date_col = None
        for col in raw.columns:
            if str(col).lower() in ["date", "datetime"]:
                date_col = col
                break

        if date_col is None:
            print(f"[NO DATE COLUMN] {ticker}")
            print(f"Columns: {raw.columns.tolist()}")
            return pd.DataFrame()

        # Build robust column map
        col_map = {}

        for col in raw.columns:
            col_name = str(col).lower().strip()

            if col_name == "open":
                col_map["Open"] = col
            elif col_name == "high":
                col_map["High"] = col
            elif col_name == "low":
                col_map["Low"] = col
            elif col_name == "close":
                col_map["Close"] = col
            elif col_name == "adj close":
                col_map["Adj Close"] = col
            elif col_name == "volume":
                col_map["Volume"] = col

        if "Close" not in col_map:
            print(f"[NO CLOSE COLUMN] {ticker}")
            print(f"Columns: {raw.columns.tolist()}")
            return pd.DataFrame()

        rows = []

        for _, row in raw.iterrows():
            close_price = row.get(col_map["Close"])

            if pd.isna(close_price):
                continue

            open_price = row.get(col_map["Open"]) if "Open" in col_map else None
            high_price = row.get(col_map["High"]) if "High" in col_map else None
            low_price = row.get(col_map["Low"]) if "Low" in col_map else None
            adj_close = row.get(col_map["Adj Close"]) if "Adj Close" in col_map else None
            volume = row.get(col_map["Volume"]) if "Volume" in col_map else None

            turnover = None

            if pd.notna(close_price) and pd.notna(volume):
                turnover = close_price * volume

            rows.append({
                "date": pd.to_datetime(row[date_col]).date(),
                "ticker": ticker,
                "name": ticker,
                "market": str(market) if pd.notna(market) else "105",

                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,

                "change": None,
                "pct_chg": None,
                "prev_close": None,

                "turnover": turnover,
                "volume": volume,

                "mkt_cap": None,
                "ytd_pct_chg": None,
                "pe_ttm": None,

                "amplitude": None,
                "turnover_rate": None,
            })

        if not rows:
            print(f"[NO VALID CLOSE ROWS] {ticker}")
            return pd.DataFrame()

        result = pd.DataFrame(rows).sort_values("date")

        print(
            f"[CONVERT OK] {ticker} | "
            f"rows={len(result)} | "
            f"dates={result['date'].tolist()}"
        )

        return result

    except Exception as e:
        print(f"[CONVERT ERROR] {ticker}: {e}")
        return pd.DataFrame()

# ============================================================
# ENRICH RANGE DATA
# ============================================================

def enrich_range_with_prev_close_and_metrics(
    price_df: pd.DataFrame,
    ticker: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:

    if price_df.empty:
        return price_df

    df = price_df.copy()
    df = df.sort_values("date").reset_index(drop=True)

    start_dt = date_obj(start_date)
    end_dt = date_obj(end_date)

    ytd_base_close = get_ytd_base_close_from_db(
        ticker=ticker,
        custom_start_date=start_date,
    )

    ref_row = get_reference_metrics_from_db(
        ticker=ticker,
        start_date=start_date,
    )

    implied_shares = None
    implied_eps_ttm = None

    if ref_row is not None:
        if (
            ref_row.mkt_cap is not None
            and ref_row.close is not None
            and ref_row.close != 0
        ):
            implied_shares = ref_row.mkt_cap / ref_row.close

        if (
            ref_row.pe_ttm is not None
            and ref_row.close is not None
            and ref_row.pe_ttm != 0
        ):
            implied_eps_ttm = ref_row.close / ref_row.pe_ttm

    enriched_rows = []

    for i in range(len(df)):
        row = df.iloc[i].copy()
        row_date = row["date"]

        # Keep only target range rows
        if row_date < start_dt or row_date > end_dt:
            continue

        # Previous close from previous available YF row
        if i > 0:
            prev_close = df.iloc[i - 1]["close"]
        else:
            prev_close = None

        close_price = row["close"]

        if pd.notna(prev_close) and prev_close != 0:
            row["prev_close"] = prev_close
            row["change"] = close_price - prev_close
            row["pct_chg"] = ((close_price - prev_close) / prev_close) * 100

            high_price = row["high"]
            low_price = row["low"]

            if pd.notna(high_price) and pd.notna(low_price):
                row["amplitude"] = (
                    (high_price - low_price) / prev_close
                ) * 100

        if implied_shares is not None and pd.notna(close_price):
            row["mkt_cap"] = close_price * implied_shares

        if implied_eps_ttm is not None and implied_eps_ttm != 0:
            row["pe_ttm"] = close_price / implied_eps_ttm

        if (
            ytd_base_close is not None
            and ytd_base_close != 0
            and pd.notna(close_price)
        ):
            row["ytd_pct_chg"] = (
                (close_price - ytd_base_close) / ytd_base_close
            ) * 100

        enriched_rows.append(row.to_dict())

    if not enriched_rows:
        return pd.DataFrame()

    return pd.DataFrame(enriched_rows)


def drop_existing_dates(
    df: pd.DataFrame,
    existing_dates: set
) -> pd.DataFrame:

    if df.empty:
        return df

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date

    return df[
        ~df["date"].isin(existing_dates)
    ].copy()


# ============================================================
# UPSERT
# ============================================================

def upsert_to_postgres(df: pd.DataFrame):
    if df.empty:
        return

    upsert_cols = [
        "date", "ticker", "name", "market",
        "open", "high", "low", "close",
        "change", "pct_chg", "prev_close",
        "turnover", "volume",
        "mkt_cap", "ytd_pct_chg", "pe_ttm",
        "amplitude", "turnover_rate",
    ]

    df = df[upsert_cols].copy()

    numeric_cols = [
        "open", "high", "low", "close",
        "change", "pct_chg", "prev_close",
        "turnover", "volume",
        "mkt_cap", "ytd_pct_chg", "pe_ttm",
        "amplitude", "turnover_rate",
    ]

    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.where(pd.notna(df), None)

    records = df.to_dict(orient="records")

    sql = text(f"""
        INSERT INTO {TABLE_NAME} (
            date, ticker, name, market,
            open, high, low, close,
            change, pct_chg, prev_close,
            turnover, volume,
            mkt_cap, ytd_pct_chg, pe_ttm,
            amplitude, turnover_rate
        )
        VALUES (
            :date, :ticker, :name, :market,
            :open, :high, :low, :close,
            :change, :pct_chg, :prev_close,
            :turnover, :volume,
            :mkt_cap, :ytd_pct_chg, :pe_ttm,
            :amplitude, :turnover_rate
        )
        ON CONFLICT (date, ticker)
        DO UPDATE SET
            name = EXCLUDED.name,
            market = EXCLUDED.market,
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            change = EXCLUDED.change,
            pct_chg = EXCLUDED.pct_chg,
            prev_close = EXCLUDED.prev_close,
            turnover = EXCLUDED.turnover,
            volume = EXCLUDED.volume,
            mkt_cap = EXCLUDED.mkt_cap,
            ytd_pct_chg = EXCLUDED.ytd_pct_chg,
            pe_ttm = EXCLUDED.pe_ttm,
            amplitude = EXCLUDED.amplitude,
            turnover_rate = EXCLUDED.turnover_rate
    """)

    with engine.begin() as conn:
        conn.execute(sql, records)


# ============================================================
# MAIN
# ============================================================

def run():
    completed = get_completed_tickers()
    print(f"Loaded {len(completed)} completed tickers from log.")

    ticker_df = get_tickers_from_db()
    print(f"Found {len(ticker_df)} tickers.")
    print(f"Already completed: {len(completed)}")

    ticker_df["ticker"] = ticker_df["ticker"].str.upper()

    ticker_df = ticker_df[
        ~ticker_df["ticker"].isin(completed)
    ].copy()

    print(f"Remaining tickers: {len(ticker_df)}")
    print(f"Date range: {CUSTOM_START_DATE} -> {CUSTOM_END_DATE}")

    for batch_start in range(0, len(ticker_df), BATCH_SIZE):
        batch_df = ticker_df.iloc[
            batch_start:batch_start + BATCH_SIZE
        ].copy()

        batch_tickers = batch_df["ticker"].str.upper().tolist()

        print(
            f"\n[BATCH] {batch_start + 1}-"
            f"{batch_start + len(batch_tickers)} "
            f"| {len(batch_tickers)} tickers"
        )

        try:
            yf_batch_df = yf_download_batch_with_retry(
                tickers=batch_tickers,
                start_date=CUSTOM_START_DATE,
                end_date=CUSTOM_END_DATE,
            )

        except Exception as e:
            print(f"[BATCH FAILED] {e}")
            time.sleep(random.uniform(5.0, 15.0))
            continue

        batch_final_rows = []

        for _, row in batch_df.iterrows():
            ticker = row["ticker"].upper()
            market = row["market"] if pd.notna(row["market"]) else "105"

            if ticker in completed:
                print(f"[SKIP LOGGED] {ticker}")
                continue

            try:
                print(f"[PROCESS] {ticker}")

                price_df = convert_batch_download_to_ticker_df(
                    batch_download_df=yf_batch_df,
                    ticker=ticker,
                    market=market,
                )

                if price_df.empty:
                    print(f"[NO DATA / RETRY LATER] {ticker}")
                    continue

                print(
                    f"[YF DATES] {ticker}: "
                    f"{price_df['date'].tolist()}"
                )

                enriched_df = enrich_range_with_prev_close_and_metrics(
                    price_df=price_df,
                    ticker=ticker,
                    start_date=CUSTOM_START_DATE,
                    end_date=CUSTOM_END_DATE,
                )

                if enriched_df.empty:
                    print(f"[NO TARGET RANGE ROWS] {ticker}")
                    mark_completed(ticker)
                    completed.add(ticker)
                    continue

                existing_dates = get_existing_dates_for_ticker(
                    ticker=ticker,
                    start_date=CUSTOM_START_DATE,
                    end_date=CUSTOM_END_DATE,
                )

                final_df = drop_existing_dates(
                    enriched_df,
                    existing_dates
                )

                if final_df.empty:
                    print(f"[NO NEW ROWS AFTER DEDUPE] {ticker}")
                    mark_completed(ticker)
                    completed.add(ticker)
                    continue

                batch_final_rows.append(final_df)

                print(
                    f"[READY] {ticker} | rows={len(final_df)} | "
                    f"dates={final_df['date'].tolist()}"
                )

                mark_completed(ticker)
                completed.add(ticker)

            except Exception as e:
                if is_no_data_error(e):
                    print(f"[DELISTED / NO DATA / SKIP] {ticker}: {e}")
                    mark_completed(ticker)
                    completed.add(ticker)
                    continue

                print(f"[PROCESS ERROR] {ticker}: {e}")
                time.sleep(random.uniform(5.0, 15.0))

        if batch_final_rows:
            upload_df = pd.concat(
                batch_final_rows,
                ignore_index=True
            )

            print(f"[UPSERT] batch rows={len(upload_df)}")
            upsert_to_postgres(upload_df)

            print("[BATCH DONE]")
        else:
            print("[BATCH NO NEW ROWS]")

        time.sleep(random.uniform(1.0, 3.0))


if __name__ == "__main__":
    run()
