"""Eastmoney raw equity fetcher."""

from __future__ import annotations

import random
import time
from math import ceil

import pandas as pd
import requests
from sqlalchemy import MetaData, Table, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db_builder.config import RAW_TABLE

# Provider-level circuit breaker
MAX_NO_DATA_PAGES = 5  # If 5+ consecutive pages return no data, abort the fetch
MAX_PAGE_RETRIES = 3   # Reduced from 5 to 3 per page

from db_builder.trading_calendar import (
    latest_completed_nyse_session_date,
    reject_non_trading_dates,
)
from db_builder.yfinance_equity_fallback import (
    fetch_missing_session_rows,
    fetch_prior_session_universe,
)


FIELDS_MAP = {
    "f12": "ticker", "f14": "name", "f17": "open", "f15": "high",
    "f16": "low", "f2": "close", "f4": "change", "f3": "pct_chg",
    "f18": "prev_close", "f6": "turnover", "f5": "volume",
    "f20": "mkt_cap", "f13": "market", "f24": "ytd_pct_chg", "f115": "pe_ttm",
}
PAGE_SIZE = 100
MIN_FETCH_COVERAGE_RATIO = 0.98
# The prior-session YTD lookup is issued once per call, so a whole-universe
# frame would scan every hypertable chunk for every ticker. Keep each query
# bounded; 500 is well inside the planner's comfortable range.
PRIOR_LOOKUP_BATCH_SIZE = 500


def _to_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _calculate_ytd_pct_chg_from_prior(
    frame: pd.DataFrame,
    prior_rows: pd.DataFrame,
) -> pd.Series:
    """Calculate split-safe YTD from prior local YTD plus today's daily return."""
    if frame.empty:
        return pd.Series(dtype="float64")

    result = frame.copy()
    result["ticker"] = result["ticker"].astype(str).str.upper()
    result["close_num"] = _to_numeric(result.get("close"))
    result["pct_chg_num"] = _to_numeric(result.get("pct_chg"))
    result["prev_close_num"] = _to_numeric(result.get("prev_close"))

    prior = prior_rows.copy()
    if prior.empty:
        prior = pd.DataFrame(columns=["ticker", "prior_ytd_pct_chg", "prior_close"])
    prior["ticker"] = prior["ticker"].astype(str).str.upper()
    prior["prior_ytd_pct_chg"] = _to_numeric(prior.get("prior_ytd_pct_chg"))
    prior["prior_close"] = _to_numeric(prior.get("prior_close"))

    result = result.merge(prior, on="ticker", how="left")
    daily_return = result["pct_chg_num"] / 100.0
    close_return = (result["close_num"] / result["prev_close_num"]) - 1.0
    daily_return = daily_return.where(daily_return.notna(), close_return)
    daily_return = daily_return.where(daily_return.notna(), 0.0)

    prior_ytd = result["prior_ytd_pct_chg"] / 100.0
    prior_ytd = prior_ytd.where(prior_ytd.notna(), 0.0)
    ytd = ((1.0 + prior_ytd) * (1.0 + daily_return) - 1.0) * 100.0
    return ytd.round(4)


def replace_ytd_pct_chg_with_local_calculation(
    engine,
    frame: pd.DataFrame,
    table_name: str = RAW_TABLE,
    *,
    target_date=None,
) -> pd.DataFrame:
    """Replace Eastmoney YTD with local rolling calculation before upsert."""
    if frame.empty:
        return frame

    tickers = sorted(set(frame["ticker"].astype(str).str.upper()))
    if not tickers:
        return frame

    date_value = target_date or frame["date"].iloc[0]
    query = text(
        f"""
        SELECT DISTINCT ON (ticker)
            ticker,
            ytd_pct_chg AS prior_ytd_pct_chg,
            close AS prior_close
        FROM {table_name}
        WHERE ticker = ANY(:tickers)
          AND date < :target_date
        ORDER BY ticker, date DESC
        """
    )
    # The universe can hold ~11,000 tickers. One ANY(:tickers) query over the
    # TimescaleDB hypertable fans out to every chunk (894 at the time of
    # writing) and took 26+ minutes; the Eastmoney path never noticed because
    # it calls this per 100-row page. Batch the lookup so each query is
    # bounded regardless of how the caller assembled the frame.
    prior_frames = []
    for start in range(0, len(tickers), PRIOR_LOOKUP_BATCH_SIZE):
        prior_frames.append(
            pd.read_sql_query(
                query,
                engine,
                params={
                    "tickers": tickers[start:start + PRIOR_LOOKUP_BATCH_SIZE],
                    "target_date": date_value,
                },
            )
        )
    prior_rows = pd.concat(prior_frames, ignore_index=True) if prior_frames else pd.DataFrame()

    result = frame.copy()
    result["ytd_pct_chg"] = _calculate_ytd_pct_chg_from_prior(result, prior_rows)
    return result


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
            raise RuntimeError(f"Cannot load database table {table_name}") from exc
    else:
        target_table = None

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Connection": "close",
    }

    # Provider-level state initialization
    provider_circuit_breaker_open = False
    provider_breaker_reason: str | None = None
    provider_403_count = 0
    provider_429_count = 0
    provider_5xx_count = 0
    provider_connection_errors = 0

    try:
        init_resp = requests.get(get_api_url(pn=1, pz=1), headers=headers, timeout=15)
        init_resp.raise_for_status()
        total_count = init_resp.json()["data"]["total"]
        pz = min(max(limit or PAGE_SIZE, 1), PAGE_SIZE) if dry_run else PAGE_SIZE
        total_pages = ceil(total_count / pz)
        print(f"Total equities: {total_count} | pages: {total_pages} | date: {target_date}")
    except Exception as exc:
        # --- Provider breaker opens on initial-request failure ---
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code is not None:
            if status_code == 403:
                provider_403_count = 1
                provider_breaker_reason = f"initial request HTTP 403"
            elif status_code == 429:
                provider_429_count = 1
                provider_breaker_reason = f"initial request HTTP 429"
            elif status_code >= 500:
                provider_5xx_count = 1
                provider_breaker_reason = f"initial request HTTP {status_code}"
            else:
                provider_breaker_reason = f"initial request HTTP {status_code}"
        else:
            error_str = str(exc)
            if any(code in error_str for code in ["403", "429", "500", "502", "503", "504"]):
                provider_5xx_count = 1
                provider_breaker_reason = f"initial request {error_str[:80]}"
            else:
                provider_connection_errors = 1
                provider_breaker_reason = f"initial request connection/timeout error"

        provider_circuit_breaker_open = True
        print(f"[EASTMONEY] Initial request {provider_breaker_reason} — provider breaker opened")
        print(f"[EASTMONEY] Invoking yfinance fallback for latest session {target_date}")

        # --- Invoke configured single-stock fallback path ---
        try:
            prior_universe = fetch_prior_session_universe(engine, target_date, table_name)
            expected_tickers = set(prior_universe["ticker"].astype(str).str.upper())
            if not expected_tickers:
                raise RuntimeError("No prior-session universe available for fallback")
            fallback = fetch_missing_session_rows(
                prior_universe,
                expected_tickers,
                target_date,
                allow_non_trading_day=allow_non_trading_day,
            )
            if not fallback.frame.empty:
                fallback_frame = replace_ytd_pct_chg_with_local_calculation(
                    engine, fallback.frame, table_name, target_date=target_date
                )
                _upsert_dataframe(engine, target_table, fallback_frame)
            print(f"[FALLBACK] Latest session {target_date} stored via yfinance fallback")
            print(f"[FALLBACK] Recovered {len(fallback.found):,} of {len(expected_tickers):,} tickers")
        except Exception as fallback_exc:
            print(f"[FALLBACK ERROR] {fallback_exc}")
            raise RuntimeError(
                f"Eastmoney unavailable and fallback failed for session {target_date}"
            ) from fallback_exc

        return  # Branch A continues: pgSQL_equities_auto.py will run indicators

    pages_to_fetch = 1 if dry_run else total_pages
    successful_pages = 0
    failed_pages: list[int] = []
    fetched_tickers: set[str] = set()

    no_data_pages = 0
    for pn in range(1, pages_to_fetch + 1):
        success = False
        retries = 0

        while not success and retries < MAX_PAGE_RETRIES:
            try:
                resp = requests.get(get_api_url(pn, pz), headers=headers, timeout=20)
                resp.raise_for_status()

                stocks = resp.json().get("data", {}).get("diff", [])
                if not stocks:
                    print(f"[page {pn}] no data; retrying")
                    retries += 1
                    no_data_pages += 1
                    if no_data_pages >= MAX_NO_DATA_PAGES or provider_circuit_breaker_open:
                        print(f"Provider circuit breaker tripped: {no_data_pages} consecutive no-data pages")
                        return []
                    time.sleep(3)
                    continue

                df = pd.DataFrame(stocks)
                df = df[FIELDS_MAP.keys()].rename(columns=FIELDS_MAP)
                df["ticker"] = df["ticker"].astype(str).str.upper()
                df["date"] = target_date
                df = df.replace("-", None)
                df = df.drop_duplicates(subset=["date", "ticker"], keep="first")
                df = reject_non_trading_dates(
                    df,
                    allow_non_trading_day=allow_non_trading_day,
                )
                df = replace_ytd_pct_chg_with_local_calculation(
                    engine,
                    df,
                    table_name,
                    target_date=target_date,
                )

                if dry_run:
                    print(f"[dry-run] page {pn}: would upsert {len(df):,} rows")
                    print(
                        df[["date", "ticker", "name", "close", "ytd_pct_chg"]]
                        .head(limit or 5)
                        .to_string(index=False)
                    )
                else:
                    _upsert_dataframe(engine, target_table, df)
                    print(f"[page {pn}/{total_pages}] synced")
                fetched_tickers.update(df["ticker"].tolist())
                successful_pages += 1
                success = True
                time.sleep(random.uniform(0.6, 1.5))

            except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError, requests.exceptions.Timeout):
                retries += 1
                provider_connection_errors += 1
                wait_time = retries * 10
                print(f"Connection interrupted; waiting {wait_time} seconds before retry")
                time.sleep(wait_time)
                # Check if connection errors indicate provider-wide issue
                if provider_connection_errors >= 3:
                    provider_circuit_breaker_open = True
                    provider_breaker_reason = f"{provider_connection_errors} connection errors"
                    return []
            except requests.exceptions.HTTPError as exc:
                retries += 1
                status_code = getattr(exc, "response", None)
                if status_code is not None:
                    status_code = status_code.status_code
                    if status_code == 403:
                        provider_403_count += 1
                    elif status_code == 429:
                        provider_429_count += 1
                    elif status_code >= 500:
                        provider_5xx_count += 1
                print(f"Eastmoney HTTP error ({exc}): page {pn}")
                time.sleep(3)
                # Check if HTTP errors indicate provider-wide issue
                total_http_failures = provider_403_count + provider_429_count + provider_5xx_count
                if total_http_failures >= 3:
                    provider_circuit_breaker_open = True
                    provider_breaker_reason = f"{total_http_failures} HTTP errors (403:{provider_403_count} 429:{provider_429_count} 5xx:{provider_5xx_count})"
                    return []
            except Exception as exc:
                retries += 1
                print(f"Eastmoney page error: {exc}")
                time.sleep(3)
                # Check for provider-wide error indicators in message
                error_str = str(exc)
                if any(code in error_str for code in ["403", "429", "500", "502", "503", "504"]):
                    total_http_failures = provider_403_count + provider_429_count + provider_5xx_count + 1
                    if total_http_failures >= 3:
                        provider_circuit_breaker_open = True
                        provider_breaker_reason = f"HTTP error in exception: {error_str}"
                        return []

        if not success:
            failed_pages.append(pn)
            print(f"[page {pn}/{total_pages}] failed after {retries} attempts")

    if not dry_run:
        prior_universe = fetch_prior_session_universe(engine, target_date, table_name)
        expected_tickers = set(prior_universe["ticker"].astype(str).str.upper())
        unresolved_tickers = expected_tickers - fetched_tickers
        fallback_found: set[str] = set()
        if unresolved_tickers:
            print(
                f"[YF FALLBACK] unresolved_prior_session_tickers={len(unresolved_tickers):,}",
                flush=True,
            )
            try:
                fallback = fetch_missing_session_rows(
                    prior_universe,
                    unresolved_tickers,
                    target_date,
                    allow_non_trading_day=allow_non_trading_day,
                )
                if not fallback.frame.empty:
                    fallback_frame = replace_ytd_pct_chg_with_local_calculation(
                        engine,
                        fallback.frame,
                        table_name,
                        target_date=target_date,
                    )
                    _upsert_dataframe(engine, target_table, fallback_frame)
                fallback_found.update(fallback.found)
                print(
                    f"[YF FALLBACK SUMMARY] requested={len(fallback.requested):,} "
                    f"recovered={len(fallback.found):,} "
                    f"missing={len(fallback.missing):,}",
                    flush=True,
                )
            except Exception as exc:
                print(f"[YF FALLBACK ERROR] {exc}", flush=True)

        combined_tickers = fetched_tickers | fallback_found
        coverage_denominator = max(total_count, len(expected_tickers))
        coverage_ratio = (
            len(combined_tickers) / coverage_denominator
            if coverage_denominator
            else 0.0
        )
        print(
            f"[FETCH SUMMARY] pages={successful_pages}/{total_pages} "
            f"eastmoney_unique={len(fetched_tickers):,} "
            f"fallback_recovered={len(fallback_found):,} "
            f"combined={len(combined_tickers):,}/{coverage_denominator:,} "
            f"coverage={coverage_ratio:.2%}"
        )
        if failed_pages:
            raise RuntimeError(
                f"Eastmoney fetch incomplete; failed pages after retries: {failed_pages}"
            )
        if coverage_ratio < MIN_FETCH_COVERAGE_RATIO:
            raise RuntimeError(
                f"Eastmoney fetch coverage {coverage_ratio:.2%} is below "
                f"required {MIN_FETCH_COVERAGE_RATIO:.0%}"
            )

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
    total_pages = ceil(total_count / page_size)

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
        df = replace_ytd_pct_chg_with_local_calculation(
            engine,
            df,
            table_name,
            target_date=target_date,
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
    print(result[["date", "ticker", "name", "close", "ytd_pct_chg"]].to_string(index=False))

    missing = sorted(target_tickers - found_tickers)
    if missing:
        print(f"Requested tickers not found: {missing}")

    if dry_run:
        print(f"[dry-run] would upsert {len(result):,} local raw rows")
    else:
        _upsert_dataframe(engine, target_table, result)
        print(f"Upserted {len(result):,} local raw rows for {sorted(found_tickers)}")

    return result
