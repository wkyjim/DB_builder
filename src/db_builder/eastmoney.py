"""Eastmoney raw equity fetcher."""

from __future__ import annotations

import random
import time

import pandas as pd
import requests
from sqlalchemy import MetaData, Table
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db_builder.config import RAW_TABLE
from db_builder.trading_calendar import (
    latest_completed_nyse_session_date,
    reject_non_trading_dates,
)


FIELDS_MAP = {
    "f12": "ticker", "f14": "name", "f17": "open", "f15": "high",
    "f16": "low", "f2": "close", "f4": "change", "f3": "pct_chg",
    "f18": "prev_close", "f6": "turnover", "f5": "volume",
    "f20": "mkt_cap", "f13": "market", "f24": "ytd_pct_chg", "f115": "pe_ttm",
}


def _load_target_table(engine, table_name: str):
    metadata = MetaData()
    return Table(
        table_name.split(".")[-1],
        metadata,
        schema="public",
        autoload_with=engine,
    )


def _upsert_dataframe(engine, target_table, df: pd.DataFrame) -> None:
    if df.empty:
        return

    data_list = df.to_dict(orient="records")
    with engine.begin() as conn:
        stmt = pg_insert(target_table).values(data_list)
        update_dict = {
            c.name: c for c in stmt.excluded
            if c.name not in ["date", "ticker"]
        }
        upsert_stmt = stmt.on_conflict_do_update(
            index_elements=["date", "ticker"],
            set_=update_dict,
        )
        conn.execute(upsert_stmt)


def get_api_url(pn: int, pz: int = 100) -> str:
    fields = ",".join(FIELDS_MAP.keys())
    return (
        "https://62.push2delay.eastmoney.com/api/qt/clist/get?"
        f"pn={pn}&pz={pz}&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
        f"&fltt=2&invt=2&fid=f3&fs=m:105,m:106,m:107&fields={fields}"
    )


def fetch_and_save_all(
    engine=None,
    table_name: str = RAW_TABLE,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    expected_session_date=None,
    allow_non_trading_day: bool = False,
) -> None:
    target_date = expected_session_date or latest_completed_nyse_session_date()
    if not allow_non_trading_day and not target_date:
        raise ValueError("Missing expected NYSE session date")

    if not dry_run:
        try:
            target_table = _load_target_table(engine, table_name)
        except Exception as exc:
            print(f"Cannot load database table {table_name}: {exc}")
            return
    else:
        target_table = None

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Connection": "close",
    }

    try:
        init_resp = requests.get(get_api_url(pn=1, pz=1), headers=headers, timeout=15)
        init_resp.raise_for_status()
        total_count = init_resp.json()["data"]["total"]
        pz = limit or 100
        total_pages = (total_count // pz) + (1 if total_count % pz > 0 else 0)
        print(f"Total equities: {total_count} | pages: {total_pages} | date: {target_date}")
    except Exception as exc:
        print(f"Initial Eastmoney request failed: {exc}")
        return

    pages_to_fetch = 1 if dry_run else total_pages

    for pn in range(1, pages_to_fetch + 1):
        success = False
        retries = 0

        while not success and retries < 5:
            try:
                resp = requests.get(get_api_url(pn, pz), headers=headers, timeout=20)
                resp.raise_for_status()

                stocks = resp.json().get("data", {}).get("diff", [])
                if not stocks:
                    print(f"[page {pn}] no data; retrying")
                    retries += 1
                    time.sleep(3)
                    continue

                df = pd.DataFrame(stocks)
                df = df[FIELDS_MAP.keys()].rename(columns=FIELDS_MAP)
                df["date"] = target_date
                df = df.replace("-", None)
                df = df.drop_duplicates(subset=["date", "ticker"], keep="first")
                df = reject_non_trading_dates(
                    df,
                    allow_non_trading_day=allow_non_trading_day,
                )

                if dry_run:
                    print(f"[dry-run] page {pn}: would upsert {len(df):,} rows")
                    print(df[["date", "ticker", "name", "close"]].head(limit or 5).to_string(index=False))
                else:
                    _upsert_dataframe(engine, target_table, df)
                    print(f"[page {pn}/{total_pages}] synced")
                success = True
                time.sleep(random.uniform(0.6, 1.5))

            except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError):
                retries += 1
                wait_time = retries * 10
                print(f"Connection interrupted; waiting {wait_time} seconds before retry")
                time.sleep(wait_time)
            except Exception as exc:
                retries += 1
                print(f"Eastmoney page error: {exc}")
                time.sleep(3)

    print("Eastmoney equity dry-run finished." if dry_run else "Eastmoney equity sync finished.")


def fetch_and_save_tickers(
    engine,
    tickers: list[str],
    table_name: str = RAW_TABLE,
    *,
    dry_run: bool = False,
    page_size: int = 100,
    expected_session_date=None,
    allow_non_trading_day: bool = False,
) -> pd.DataFrame:
    target_date = expected_session_date or latest_completed_nyse_session_date()
    target_tickers = {ticker.strip().upper() for ticker in tickers if ticker.strip()}
    found_frames = []
    found_tickers: set[str] = set()

    if not target_tickers:
        print("No tickers requested.")
        return pd.DataFrame()

    target_table = None if dry_run else _load_target_table(engine, table_name)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Connection": "close",
    }

    init_resp = requests.get(get_api_url(pn=1, pz=1), headers=headers, timeout=15)
    init_resp.raise_for_status()
    total_count = init_resp.json()["data"]["total"]
    total_pages = (total_count // page_size) + (1 if total_count % page_size > 0 else 0)

    print(
        f"Looking for {sorted(target_tickers)} in Eastmoney "
        f"across {total_pages} pages | date: {target_date}"
    )

    for pn in range(1, total_pages + 1):
        resp = requests.get(get_api_url(pn, page_size), headers=headers, timeout=20)
        resp.raise_for_status()

        stocks = resp.json().get("data", {}).get("diff", [])
        if not stocks:
            continue

        df = pd.DataFrame(stocks)
        df = df[FIELDS_MAP.keys()].rename(columns=FIELDS_MAP)
        df["ticker"] = df["ticker"].astype(str).str.upper()
        df = df[df["ticker"].isin(target_tickers - found_tickers)].copy()

        if df.empty:
            continue

        df["date"] = target_date
        df = df.replace("-", None)
        df = df.drop_duplicates(subset=["date", "ticker"], keep="first")
        df = reject_non_trading_dates(
            df,
            allow_non_trading_day=allow_non_trading_day,
        )

        found_frames.append(df)
        found_tickers.update(df["ticker"].tolist())
        print(f"[page {pn}/{total_pages}] found {sorted(found_tickers)}")

        if found_tickers == target_tickers:
            break

        time.sleep(random.uniform(0.2, 0.8))

    if not found_frames:
        print("No requested tickers found in Eastmoney response.")
        return pd.DataFrame()

    result = pd.concat(found_frames, ignore_index=True)
    print(result[["date", "ticker", "name", "close"]].to_string(index=False))

    missing = sorted(target_tickers - found_tickers)
    if missing:
        print(f"Requested tickers not found: {missing}")

    if dry_run:
        print(f"[dry-run] would upsert {len(result):,} local raw rows")
    else:
        _upsert_dataframe(engine, target_table, result)
        print(f"Upserted {len(result):,} local raw rows for {sorted(found_tickers)}")

    return result
