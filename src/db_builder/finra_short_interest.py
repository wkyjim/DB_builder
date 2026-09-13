"""FINRA twice-monthly short-interest ingestion and feature engineering."""

from __future__ import annotations

import io
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal
from sqlalchemy import bindparam, text

from db_builder.finra_short_analytics import _rolling_percentile, setup_short_analytics_schema
from db_builder.flow_sources import finra_short_interest_url, http_session, record_flow_source_health
from db_builder.short_analytics_config import load_short_analytics_config


LOGGER = logging.getLogger(__name__)
FINRA_REPORTING_CALENDAR_URL = "https://www.finra.org/filing-reporting/regulatory-filing-systems/short-interest"
MASSIVE_SHORT_INTEREST_URL = "https://api.massive.com/stocks/v1/short-interest"
MASSIVE_API_KEY_ENV = "MASSIVE_API_KEY"
NEW_YORK = ZoneInfo("America/New_York")
Progress = Callable[[str], None]

RAW_COLUMNS = {
    "accountingYearMonthNumber",
    "symbolCode",
    "issueName",
    "issuerServicesGroupExchangeCode",
    "marketClassCode",
    "currentShortPositionQuantity",
    "previousShortPositionQuantity",
    "stockSplitFlag",
    "averageDailyVolumeQuantity",
    "daysToCoverQuantity",
    "revisionFlag",
    "changePercent",
    "changePreviousNumber",
    "settlementDate",
}


@dataclass
class ShortInterestFetchResult:
    rows: list[dict] = field(default_factory=list)
    successful_dates: list[date] = field(default_factory=list)
    missing_dates: list[date] = field(default_factory=list)
    failed_dates: dict[date, str] = field(default_factory=dict)
    skipped_dates: list[date] = field(default_factory=list)


def massive_fallback_is_due(
    settlement_date: date,
    publication_date: date,
    *,
    as_of_date: date,
    data_already_present: bool,
) -> bool:
    """Return true only after publication when the settlement is absent locally."""
    return not data_already_present and settlement_date <= publication_date <= as_of_date


def parse_massive_short_interest_payload(
    payload: dict,
    *,
    settlement_date: date,
    publication_date: date,
    publication_date_source: str,
    previous_short_interest: dict[str, float] | None = None,
    fetched_at: datetime | None = None,
    minimum_rows: int = 100,
) -> list[dict]:
    """Normalize one complete Massive settlement-date response to the FINRA schema."""
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise ValueError("Massive short-interest response does not contain a results list")
    if payload.get("next_url"):
        raise ValueError("Massive short-interest response exceeds the one-call 50,000-row limit")
    if len(results) < minimum_rows:
        raise ValueError(f"Massive short-interest response has {len(results):,} rows; expected at least {minimum_rows:,}")

    previous = previous_short_interest or {}
    timestamp = fetched_at or datetime.now(timezone.utc)
    source_file = f"massive:stocks/v1/short-interest?settlement_date={settlement_date.isoformat()}"
    normalized: dict[str, dict] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").upper().strip().replace(" ", "")
        item_settlement = pd.to_datetime(item.get("settlement_date"), errors="coerce")
        current = pd.to_numeric(item.get("short_interest"), errors="coerce")
        if not ticker or pd.isna(item_settlement) or item_settlement.date() != settlement_date or pd.isna(current) or current < 0:
            continue
        prior = previous.get(ticker)
        change = None if prior is None else float(current) - float(prior)
        change_percent = None if prior in (None, 0) else change / float(prior) * 100.0
        average_volume = pd.to_numeric(item.get("avg_daily_volume"), errors="coerce")
        days_to_cover = pd.to_numeric(item.get("days_to_cover"), errors="coerce")
        normalized[ticker] = {
            "settlement_date": settlement_date,
            "publication_date": publication_date,
            "publication_date_source": publication_date_source,
            "ticker": ticker,
            "issue_name": None,
            "issuer_services_group_exchange_code": None,
            "market_class_code": None,
            "current_short_position_quantity": float(current),
            "previous_short_position_quantity": None if prior is None else float(prior),
            "change_previous_number": change,
            "change_percent": change_percent,
            "average_daily_volume_quantity": None if pd.isna(average_volume) else float(average_volume),
            "days_to_cover_quantity": None if pd.isna(days_to_cover) else float(days_to_cover),
            "stock_split_flag": None,
            "revision_flag": None,
            "source_file": source_file,
            "fetched_at": timestamp,
            "data_quality_status": "valid_massive_fallback",
        }
    if len(normalized) < minimum_rows:
        raise ValueError(f"Massive short-interest response contains only {len(normalized):,} valid rows")
    return list(normalized.values())


def fetch_massive_short_interest(
    settlement_date: date,
    *,
    publication_date: date,
    publication_date_source: str,
    api_key: str,
    previous_short_interest: dict[str, float] | None = None,
    timeout: int = 60,
    session=None,
    minimum_rows: int = 100,
) -> list[dict]:
    """Fetch all tickers for one settlement date in a single Massive API call."""
    if not api_key or not api_key.strip():
        raise ValueError(f"{MASSIVE_API_KEY_ENV} is not configured")
    client = session or http_session()
    response = client.get(
        MASSIVE_SHORT_INTEREST_URL,
        params={
            "settlement_date": settlement_date.isoformat(),
            "limit": 50000,
            "sort": "ticker.asc",
            "apiKey": api_key.strip(),
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Massive short-interest request failed with HTTP {response.status_code}")
    try:
        payload = response.json()
    except Exception as exc:
        raise ValueError("Massive short-interest response is not valid JSON") from exc
    return parse_massive_short_interest_payload(
        payload,
        settlement_date=settlement_date,
        publication_date=publication_date,
        publication_date_source=publication_date_source,
        previous_short_interest=previous_short_interest,
        minimum_rows=minimum_rows,
    )


def _nyse_sessions(start_date: date, end_date: date) -> pd.DatetimeIndex:
    schedule = mcal.get_calendar("NYSE").schedule(start_date=start_date, end_date=end_date)
    return pd.DatetimeIndex(schedule.index).tz_localize(None)


def discover_short_interest_settlement_dates(start_date: date, end_date: date) -> list[date]:
    """Generate FINRA reporting candidates from actual U.S. settlement sessions."""
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    sessions = _nyse_sessions(date(start_date.year, start_date.month, 1), end_date)
    frame = pd.DataFrame({"session": sessions})
    frame["month"] = frame["session"].dt.to_period("M")
    results: set[date] = set()
    for _, group in frame.groupby("month"):
        mid = group[group["session"].dt.day <= 15]
        if not mid.empty:
            results.add(mid["session"].max().date())
        results.add(group["session"].max().date())
    return sorted(item for item in results if start_date <= item <= end_date)


def _parse_calendar_date(value: object, year: int) -> date | None:
    cleaned = re.sub(r"\s*\([^)]*\)\s*", "", str(value)).strip()
    parsed = pd.to_datetime(f"{cleaned} {year}", errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def parse_reporting_calendar_tables(tables: list[pd.DataFrame], *, current_year: int) -> dict[date, date]:
    mapping: dict[date, date] = {}
    for table in tables:
        if table.empty or len(table.columns) < 3:
            continue
        columns = [str(item).lower() for item in table.columns]
        if not any("settlement" in item for item in columns) or not any("publication" in item for item in columns):
            continue
        # FINRA's page currently shows the last rows of the prior year first,
        # followed by the complete current-year schedule.
        year = current_year if len(table) >= 20 else current_year - 1
        settlement_column = next(item for item in table.columns if "settlement" in str(item).lower())
        publication_column = next(item for item in table.columns if "publication" in str(item).lower())
        for row in table.to_dict(orient="records"):
            settlement = _parse_calendar_date(row.get(settlement_column), year)
            publication_year = year
            publication_text = str(row.get(publication_column) or "")
            if settlement and settlement.month == 12 and "january" in publication_text.lower():
                publication_year += 1
            publication = _parse_calendar_date(publication_text, publication_year)
            if settlement and publication:
                mapping[settlement] = publication
    return mapping


def fetch_reporting_calendar(*, timeout: int = 30, today: date | None = None) -> dict[date, date]:
    current = today or datetime.now(timezone.utc).date()
    response = http_session().get(FINRA_REPORTING_CALENDAR_URL, timeout=timeout)
    response.raise_for_status()
    return parse_reporting_calendar_tables(pd.read_html(io.StringIO(response.text)), current_year=current.year)


def derive_publication_date(settlement_date: date) -> date:
    sessions = _nyse_sessions(settlement_date, settlement_date + pd.Timedelta(days=20))
    future = [item.date() for item in sessions if item.date() > settlement_date]
    if len(future) < 7:
        raise ValueError(f"Unable to derive publication date for {settlement_date}")
    return future[6]


def publication_metadata(settlement_date: date, official_calendar: dict[date, date] | None = None) -> tuple[date, str]:
    if official_calendar and settlement_date in official_calendar:
        return official_calendar[settlement_date], "finra_calendar"
    return derive_publication_date(settlement_date), "derived_7_business_days"


def parse_finra_short_interest_text(
    text_body: str,
    *,
    source_file: str | None = None,
    publication_date: date | None = None,
    publication_date_source: str | None = None,
    fetched_at: datetime | None = None,
    minimum_rows: int = 100,
) -> list[dict]:
    if not text_body or len(text_body.strip()) < 10:
        raise ValueError("FINRA short-interest file is empty or unexpectedly small")
    try:
        frame = pd.read_csv(io.StringIO(text_body), sep="|", dtype=str)
    except Exception as exc:
        raise ValueError("FINRA short-interest file is not valid pipe-delimited text") from exc
    missing = RAW_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"Missing FINRA short-interest columns: {sorted(missing)}")
    if len(frame) < minimum_rows:
        raise ValueError(f"FINRA short-interest file has {len(frame):,} rows; expected at least {minimum_rows:,}")

    numeric_columns = [
        "currentShortPositionQuantity",
        "previousShortPositionQuantity",
        "changePreviousNumber",
        "changePercent",
        "averageDailyVolumeQuantity",
        "daysToCoverQuantity",
    ]
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["settlementDate"] = pd.to_datetime(frame["settlementDate"], errors="coerce")
    frame["ticker"] = frame["symbolCode"].fillna("").astype(str).str.upper().str.strip().str.replace(" ", "", regex=False)
    valid = (
        frame["settlementDate"].notna()
        & frame["ticker"].ne("")
        & frame["currentShortPositionQuantity"].notna()
        & frame["currentShortPositionQuantity"].ge(0)
    )
    frame = frame.loc[valid].copy()
    if frame.empty:
        raise ValueError("FINRA short-interest file contains no valid rows")
    if frame["settlementDate"].dt.date.nunique() != 1:
        raise ValueError("FINRA short-interest file contains multiple settlement dates")
    timestamp = fetched_at or datetime.now(timezone.utc)
    rows = []
    for row in frame.itertuples(index=False):
        rows.append(
            {
                "settlement_date": row.settlementDate.date(),
                "publication_date": publication_date,
                "publication_date_source": publication_date_source,
                "ticker": row.ticker,
                "issue_name": row.issueName,
                "issuer_services_group_exchange_code": row.issuerServicesGroupExchangeCode,
                "market_class_code": row.marketClassCode,
                "current_short_position_quantity": None if pd.isna(row.currentShortPositionQuantity) else float(row.currentShortPositionQuantity),
                "previous_short_position_quantity": None if pd.isna(row.previousShortPositionQuantity) else float(row.previousShortPositionQuantity),
                "change_previous_number": None if pd.isna(row.changePreviousNumber) else float(row.changePreviousNumber),
                "change_percent": None if pd.isna(row.changePercent) else float(row.changePercent),
                "average_daily_volume_quantity": None if pd.isna(row.averageDailyVolumeQuantity) else float(row.averageDailyVolumeQuantity),
                "days_to_cover_quantity": None if pd.isna(row.daysToCoverQuantity) else float(row.daysToCoverQuantity),
                "stock_split_flag": None if pd.isna(row.stockSplitFlag) or not str(row.stockSplitFlag).strip() else str(row.stockSplitFlag).strip(),
                "revision_flag": None if pd.isna(row.revisionFlag) or not str(row.revisionFlag).strip() else str(row.revisionFlag).strip(),
                "source_file": source_file,
                "fetched_at": timestamp,
                "data_quality_status": "valid",
            }
        )
    return rows


def _existing_success_dates(engine, dates: list[date]) -> set[date]:
    if not dates:
        return set()
    setup_short_analytics_schema(engine)
    query = text(
        """
        SELECT source_date FROM public.finra_ingestion_runs
        WHERE source_type = 'short_interest' AND status = 'success' AND source_date IN :dates
        """
    ).bindparams(bindparam("dates", expanding=True))
    with engine.connect() as conn:
        recorded = {row[0] for row in conn.execute(query, {"dates": dates})}
        data_query = text(
            """
            SELECT DISTINCT settlement_date
            FROM public.finra_short_interest
            WHERE settlement_date IN :dates
            """
        ).bindparams(bindparam("dates", expanding=True))
        populated = {row[0] for row in conn.execute(data_query, {"dates": dates})}
    return recorded | populated


def _existing_data_dates(engine, dates: list[date]) -> set[date]:
    if not dates:
        return set()
    setup_short_analytics_schema(engine)
    query = text(
        """
        SELECT DISTINCT settlement_date
        FROM public.finra_short_interest
        WHERE settlement_date IN :dates
        """
    ).bindparams(bindparam("dates", expanding=True))
    with engine.connect() as conn:
        return {row[0] for row in conn.execute(query, {"dates": dates})}


def _previous_short_interest_map(engine, settlement_date: date) -> dict[str, float]:
    query = text(
        """
        SELECT DISTINCT ON (ticker) ticker, current_short_position_quantity
        FROM public.finra_short_interest
        WHERE settlement_date < :settlement_date
          AND current_short_position_quantity IS NOT NULL
        ORDER BY ticker, settlement_date DESC
        """
    )
    with engine.connect() as conn:
        return {str(row[0]).upper(): float(row[1]) for row in conn.execute(query, {"settlement_date": settlement_date})}


def _record_run(engine, source_date: date, source_file: str, status: str, row_count: int = 0, error: str | None = None) -> None:
    query = text(
        """
        INSERT INTO public.finra_ingestion_runs (
            source_type, source_date, source_file, status, row_count, error_message, fetched_at
        ) VALUES ('short_interest', :source_date, :source_file, :status, :row_count, :error, now())
        ON CONFLICT (source_type, source_date) DO UPDATE SET
            source_file = EXCLUDED.source_file, status = EXCLUDED.status,
            row_count = EXCLUDED.row_count, error_message = EXCLUDED.error_message, fetched_at = now()
        """
    )
    with engine.begin() as conn:
        conn.execute(query, {"source_date": source_date, "source_file": source_file, "status": status, "row_count": row_count, "error": error})


def fetch_finra_short_interest_files(
    settlement_dates: list[date],
    *,
    official_calendar: dict[date, date] | None = None,
    timeout: int = 30,
    progress: Progress | None = None,
    session=None,
) -> ShortInterestFetchResult:
    session = session or http_session()
    result = ShortInterestFetchResult()
    for index, settlement_date in enumerate(dict.fromkeys(settlement_dates), start=1):
        url = finra_short_interest_url(settlement_date)
        source_file = PurePosixPath(url).name
        if progress:
            progress(f"FINRA short interest {index}/{len(settlement_dates)} {settlement_date} requesting {source_file}")
        try:
            response = session.get(url, timeout=timeout)
            if response.status_code in {403, 404}:
                result.missing_dates.append(settlement_date)
                if progress:
                    progress(f"FINRA short interest {settlement_date} unavailable ({response.status_code})")
                continue
            response.raise_for_status()
            publication_date, source = publication_metadata(settlement_date, official_calendar)
            rows = parse_finra_short_interest_text(
                response.text,
                source_file=source_file,
                publication_date=publication_date,
                publication_date_source=source,
            )
            if any(row["settlement_date"] != settlement_date for row in rows):
                raise ValueError("settlement date does not match requested filename")
            result.rows.extend(rows)
            result.successful_dates.append(settlement_date)
            if progress:
                progress(f"FINRA short interest {settlement_date} validated rows={len(rows):,}")
        except Exception as exc:
            result.failed_dates[settlement_date] = str(exc)
            if progress:
                progress(f"FINRA short interest {settlement_date} failed: {exc}")
    return result


def upsert_finra_short_interest(engine, rows: list[dict], *, chunk_size: int = 10000, ensure_schema: bool = True) -> int:
    if not rows:
        return 0
    if ensure_schema:
        setup_short_analytics_schema(engine)
    columns = list(rows[0])
    query = text(
        f"""
        INSERT INTO public.finra_short_interest ({', '.join(columns)})
        VALUES ({', '.join(':' + item for item in columns)})
        ON CONFLICT (settlement_date, ticker) DO UPDATE SET
        {', '.join(f'{item} = EXCLUDED.{item}' for item in columns if item not in {'settlement_date', 'ticker'})}
        """
    )
    with engine.begin() as conn:
        for start in range(0, len(rows), chunk_size):
            conn.execute(query, rows[start : start + chunk_size])
    return len(rows)


def run_finra_short_interest_fetch(
    engine,
    *,
    settlement_dates: list[date],
    dry_run: bool = False,
    skip_existing: bool = True,
    timeout: int = 30,
    massive_timeout: int = 60,
    massive_fallback: bool = True,
    massive_api_key: str | None = None,
    as_of_date: date | None = None,
    progress: Progress | None = print,
) -> dict:
    started = time.perf_counter()
    setup_short_analytics_schema(engine)
    selected = list(dict.fromkeys(settlement_dates))
    populated_dates = _existing_data_dates(engine, selected)
    skipped: list[date] = []
    if skip_existing:
        existing = _existing_success_dates(engine, selected)
        skipped = [item for item in selected if item in existing]
        selected = [item for item in selected if item not in existing]
    try:
        calendar = fetch_reporting_calendar(timeout=timeout)
    except Exception as exc:
        LOGGER.warning("FINRA reporting calendar unavailable; deriving publication dates: %s", exc)
        calendar = {}
    successful_dates: list[date] = []
    fallback_dates: list[date] = []
    fallback_row_count = 0
    fallback_failures: dict[date, str] = {}
    missing_dates: list[date] = []
    failed_dates: dict[date, str] = {}
    total_rows = 0
    upserted = 0
    session = http_session()
    fallback_key = massive_api_key if massive_api_key is not None else os.getenv(MASSIVE_API_KEY_ENV, "")
    current_date = as_of_date or datetime.now(NEW_YORK).date()
    for source_date in selected:
        publication_date, publication_source = publication_metadata(source_date, calendar)
        fetched = fetch_finra_short_interest_files(
            [source_date], official_calendar=calendar, timeout=timeout, progress=progress, session=session
        )
        source_file = PurePosixPath(finra_short_interest_url(source_date)).name
        if fetched.successful_dates:
            row_count = len(fetched.rows)
            total_rows += row_count
            if not dry_run:
                upserted += upsert_finra_short_interest(engine, fetched.rows, ensure_schema=False)
                _record_run(engine, source_date, source_file, "success", row_count)
            successful_dates.append(source_date)
            continue

        finra_error = "HTTP 403/404" if fetched.missing_dates else fetched.failed_dates.get(source_date)
        fallback_due = massive_fallback_is_due(
            source_date,
            publication_date,
            as_of_date=current_date,
            data_already_present=source_date in populated_dates,
        )
        if massive_fallback and fallback_due and fallback_key:
            fallback_started = time.perf_counter()
            try:
                if progress:
                    progress(f"Massive short interest fallback {source_date} requesting one 50,000-row call")
                fallback_rows = fetch_massive_short_interest(
                    source_date,
                    publication_date=publication_date,
                    publication_date_source=publication_source,
                    api_key=fallback_key,
                    previous_short_interest=_previous_short_interest_map(engine, source_date),
                    timeout=massive_timeout,
                    session=session,
                )
                row_count = len(fallback_rows)
                total_rows += row_count
                fallback_row_count += row_count
                if not dry_run:
                    upserted += upsert_finra_short_interest(engine, fallback_rows, ensure_schema=False)
                    _record_run(engine, source_date, fallback_rows[0]["source_file"], "success", row_count)
                successful_dates.append(source_date)
                fallback_dates.append(source_date)
                if progress:
                    progress(f"Massive short interest fallback {source_date} validated rows={row_count:,}")
                record_flow_source_health(
                    engine,
                    source_name="Massive short interest fallback",
                    source_type="massive_short_interest",
                    succeeded=True,
                    fetch_seconds=time.perf_counter() - fallback_started,
                    latest_available_date=source_date,
                )
                continue
            except Exception as exc:
                fallback_failures[source_date] = str(exc)
                if progress:
                    progress(f"Massive short interest fallback {source_date} failed: {exc}")
                record_flow_source_health(
                    engine,
                    source_name="Massive short interest fallback",
                    source_type="massive_short_interest",
                    succeeded=False,
                    fetch_seconds=time.perf_counter() - fallback_started,
                    error=str(exc),
                )
        elif massive_fallback and fallback_due and not fallback_key and progress:
            progress(f"Massive fallback due for {source_date}, but {MASSIVE_API_KEY_ENV} is not configured")

        if fetched.missing_dates:
            missing_dates.append(source_date)
            if not dry_run:
                _record_run(engine, source_date, source_file, "missing", error="HTTP 403/404")
        elif fetched.failed_dates:
            error = finra_error or "FINRA fetch failed"
            failed_dates[source_date] = error
            if not dry_run:
                _record_run(engine, source_date, source_file, "failed", error=error)
    latest = max(successful_dates, default=max(skipped, default=None))
    succeeded = bool(successful_dates or skipped) and not failed_dates
    record_flow_source_health(
        engine,
        source_name="FINRA short interest",
        source_type="finra_short_interest",
        succeeded=succeeded,
        fetch_seconds=time.perf_counter() - started,
        latest_available_date=latest,
        error=None if succeeded else "; ".join(f"{key}: {value}" for key, value in list(failed_dates.items())[:5]),
    )
    return {
        "rows": total_rows,
        "upserted": upserted,
        "latest_available_date": latest,
        "successful_dates": successful_dates,
        "fallback_dates": fallback_dates,
        "fallback_rows": fallback_row_count,
        "fallback_failures": fallback_failures,
        "missing_dates": missing_dates,
        "failed_dates": failed_dates,
        "skipped_dates": skipped,
    }


def _rolling_regression(group: pd.DataFrame, window: int, min_periods: int) -> tuple[pd.Series, pd.Series]:
    y = np.log(pd.to_numeric(group["short_interest"], errors="coerce").where(lambda value: value > 0))
    x = pd.Series(np.arange(len(group), dtype=float), index=group.index)
    sx = x.rolling(window, min_periods=min_periods).sum()
    sy = y.rolling(window, min_periods=min_periods).sum()
    sxx = (x * x).rolling(window, min_periods=min_periods).sum()
    syy = (y * y).rolling(window, min_periods=min_periods).sum()
    sxy = (x * y).rolling(window, min_periods=min_periods).sum()
    n = y.rolling(window, min_periods=min_periods).count()
    covariance = n * sxy - sx * sy
    variance_x = n * sxx - sx * sx
    variance_y = n * syy - sy * sy
    slope = covariance / variance_x.replace(0, np.nan)
    r2 = (covariance * covariance) / (variance_x * variance_y).replace(0, np.nan)
    return slope, r2.clip(0, 1)


def _own_percentile(values: pd.Series, window: int, minimum: int) -> pd.Series:
    return _rolling_percentile(values, window, minimum)


def _short_interest_ticker_features(group: pd.DataFrame, config: dict) -> pd.DataFrame:
    ticker = group.name
    group = group.sort_values("settlement_date").copy()
    group["ticker"] = ticker
    si = pd.to_numeric(group["short_interest"], errors="coerce")
    group["si_observation_count"] = np.arange(1, len(group) + 1)
    split_threshold = float(config["quality"]["split_change_threshold"])
    log_si = np.log(si.where(si > 0))
    log_change = log_si.diff()
    split_flag = group["stock_split_flag"].fillna("").astype(str).str.strip().ne("")
    suspicious = log_change.abs().gt(split_threshold)
    group["corporate_action_flag"] = split_flag | suspicious
    clean_change = si.pct_change(fill_method=None).mask(group["corporate_action_flag"])
    for observations in (1, 2, 4, 8, 12, 24, 48):
        group[f"si_change_{observations}obs"] = si.pct_change(observations, fill_method=None).mask(
            group["corporate_action_flag"].rolling(observations + 1, min_periods=1).max().astype(bool)
        )

    windows = {"3m": 6, "6m": 12, "12m": 24, "24m": 48}
    for label, window in windows.items():
        slope, r2 = _rolling_regression(group.assign(short_interest=si), window, max(4, window // 2))
        group[f"si_slope_{label}"] = slope
        group[f"si_r2_{label}"] = r2
    group["si_trend_quality"] = (
        (group["si_slope_12m"].abs() * 24 * 100).clip(0, 100)
        * group["si_r2_12m"].fillna(0)
    ).clip(0, 100)
    group["si_acceleration"] = group["si_slope_3m"] - group["si_slope_6m"]
    group["own_si_percentile_1y"] = _own_percentile(si, 24, 12)
    group["own_si_percentile_3y"] = _own_percentile(si, 72, 24)
    group["own_si_percentile_5y"] = _own_percentile(si, 120, 40)
    for label, window in (("6m", 12), ("12m", 24), ("24m", 48)):
        minimum = max(4, window // 2)
        group[f"median_si_{label}"] = si.rolling(window, min_periods=minimum).median()
        group[f"si_volatility_{label}"] = clean_change.rolling(window, min_periods=minimum).std()
    group["coefficient_variation_12m"] = si.rolling(24, min_periods=12).std() / si.rolling(24, min_periods=12).mean().replace(0, np.nan)
    return group


def compute_short_interest_features(
    rows: pd.DataFrame | list[dict],
    *,
    classifications: pd.DataFrame | None = None,
    config: dict | None = None,
) -> pd.DataFrame:
    frame = pd.DataFrame(rows).copy()
    if frame.empty:
        return frame
    required = {"settlement_date", "publication_date", "ticker", "current_short_position_quantity"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Missing short-interest fields: {sorted(missing)}")
    frame["settlement_date"] = pd.to_datetime(frame["settlement_date"], errors="coerce")
    frame["publication_date"] = pd.to_datetime(frame["publication_date"], errors="coerce")
    frame["ticker"] = frame["ticker"].astype(str).str.upper().str.strip()
    frame["short_interest"] = pd.to_numeric(frame["current_short_position_quantity"], errors="coerce")
    frame["days_to_cover"] = pd.to_numeric(frame.get("days_to_cover_quantity"), errors="coerce")
    frame = frame.dropna(subset=["settlement_date", "ticker", "short_interest"])
    frame = frame.sort_values(["ticker", "settlement_date"]).drop_duplicates(["ticker", "settlement_date"], keep="last")
    cfg = config or load_short_analytics_config()
    featured = frame.groupby("ticker", group_keys=False, sort=False).apply(
        _short_interest_ticker_features, config=cfg, include_groups=False
    )
    if "ticker" not in featured.columns:
        featured = featured.reset_index(level=0)

    if classifications is not None and not classifications.empty:
        classes = classifications[[item for item in ("ticker", "sector", "industry") if item in classifications.columns]].drop_duplicates("ticker")
        featured = featured.merge(classes, on="ticker", how="left")
        peer = featured["industry"].fillna(featured.get("sector"))
        featured["market_si_percentile"] = featured.groupby("settlement_date")["short_interest"].rank(pct=True)
        featured["sector_si_percentile"] = featured.groupby([featured["settlement_date"], featured["sector"]], dropna=False)["short_interest"].rank(pct=True).where(featured["sector"].notna())
        featured["industry_si_percentile"] = featured.groupby([featured["settlement_date"], peer])["short_interest"].rank(pct=True)
    else:
        featured["market_si_percentile"] = np.nan
        featured["sector_si_percentile"] = np.nan
        featured["industry_si_percentile"] = np.nan
    accumulation = featured.groupby("ticker")["short_interest"].pct_change(fill_method=None).gt(0).astype(float)
    featured["_si_accumulation"] = accumulation
    for label, window in (("6m", 12), ("12m", 24), ("24m", 48)):
        featured[f"si_persistence_{label}"] = featured.groupby("ticker")["_si_accumulation"].transform(
            lambda values, window=window: values.rolling(window, min_periods=max(4, window // 2)).mean()
        )
    featured["settlement_date"] = pd.to_datetime(featured["settlement_date"]).dt.date
    featured["publication_date"] = pd.to_datetime(featured["publication_date"]).dt.date
    return featured.drop(columns=["_si_accumulation"], errors="ignore").replace({np.nan: None})


SI_FEATURE_COLUMNS = [
    "settlement_date", "publication_date", "ticker", "short_interest", "days_to_cover",
    "si_observation_count",
    "si_change_1obs", "si_change_2obs", "si_change_4obs", "si_change_8obs", "si_change_12obs",
    "si_change_24obs", "si_change_48obs", "si_slope_3m", "si_slope_6m", "si_slope_12m",
    "si_slope_24m", "si_r2_3m", "si_r2_6m", "si_r2_12m", "si_r2_24m",
    "si_trend_quality", "si_acceleration", "own_si_percentile_1y", "own_si_percentile_3y",
    "own_si_percentile_5y", "market_si_percentile", "sector_si_percentile", "industry_si_percentile",
    "median_si_6m", "median_si_12m", "median_si_24m",
    "si_persistence_6m", "si_persistence_12m", "si_persistence_24m", "si_volatility_6m",
    "si_volatility_12m", "si_volatility_24m", "coefficient_variation_12m", "corporate_action_flag",
]


def upsert_short_interest_features(engine, features: pd.DataFrame, *, chunk_size: int = 10000) -> int:
    if features.empty:
        return 0
    setup_short_analytics_schema(engine)
    frame = features.reindex(columns=SI_FEATURE_COLUMNS).replace({np.nan: None})
    query = text(
        f"""
        INSERT INTO public.finra_short_interest_features ({', '.join(SI_FEATURE_COLUMNS)}, calculated_at)
        VALUES ({', '.join(':' + item for item in SI_FEATURE_COLUMNS)}, now())
        ON CONFLICT (settlement_date, ticker) DO UPDATE SET
        {', '.join(f'{item} = EXCLUDED.{item}' for item in SI_FEATURE_COLUMNS if item not in {'settlement_date', 'ticker'})},
        calculated_at = now()
        """
    )
    rows = frame.to_dict(orient="records")
    with engine.begin() as conn:
        for start in range(0, len(rows), chunk_size):
            conn.execute(query, rows[start : start + chunk_size])
    return len(rows)


def refresh_short_interest_features(engine, *, progress=print) -> dict:
    setup_short_analytics_schema(engine)
    try:
        classifications = pd.read_sql("SELECT ticker, sector, industry FROM public.security_classification", engine)
    except Exception:
        classifications = pd.DataFrame()
    with engine.connect() as conn:
        tickers = [
            row[0]
            for row in conn.execute(
                text(
                    """
                    SELECT DISTINCT si.ticker
                    FROM public.finra_short_interest si
                    JOIN (
                        SELECT DISTINCT ticker
                        FROM public.finra_short_volume
                        WHERE raw_symbol IS NOT NULL
                          AND normalized_ticker IS NOT NULL
                          AND normalized_ticker = ticker
                    ) eligible ON eligible.ticker = si.ticker
                    ORDER BY si.ticker
                    """
                )
            )
        ]
    raw_count = 0
    feature_count = 0
    upserted = 0
    batch_size = 500
    for start in range(0, len(tickers), batch_size):
        batch = tickers[start : start + batch_size]
        query = text("SELECT * FROM public.finra_short_interest WHERE ticker IN :tickers ORDER BY ticker, settlement_date").bindparams(
            bindparam("tickers", expanding=True)
        )
        raw = pd.read_sql(query, engine, params={"tickers": batch})
        raw_count += len(raw)
        features = compute_short_interest_features(raw, classifications=classifications)
        feature_count += len(features)
        upserted += upsert_short_interest_features(engine, features)
        if progress:
            progress(f"FINRA short-interest analytics batch={start // batch_size + 1} tickers={len(batch)} rows={len(features):,}")
    # Batch processing keeps memory bounded, but peer percentiles must use the
    # complete settlement-date cross section rather than an alphabetical batch.
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE public.finra_short_interest_features f
                SET sector_si_percentile = NULL,
                    industry_si_percentile = NULL
                WHERE NOT EXISTS (
                    SELECT 1 FROM public.security_classification c
                    WHERE c.ticker = f.ticker AND COALESCE(c.industry, c.sector) IS NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                WITH ranked AS (
                    SELECT f.settlement_date,
                           f.ticker,
                           percent_rank() OVER (
                               PARTITION BY f.settlement_date
                               ORDER BY f.short_interest
                           ) AS market_percentile,
                           percent_rank() OVER (
                               PARTITION BY f.settlement_date, COALESCE(c.sector, 'UNCLASSIFIED')
                               ORDER BY f.short_interest
                           ) AS sector_percentile,
                           percent_rank() OVER (
                               PARTITION BY f.settlement_date, COALESCE(c.industry, c.sector)
                               ORDER BY f.short_interest
                           ) AS peer_percentile
                    FROM public.finra_short_interest_features f
                    JOIN public.security_classification c ON c.ticker = f.ticker
                    WHERE COALESCE(c.industry, c.sector) IS NOT NULL
                )
                UPDATE public.finra_short_interest_features target
                SET market_si_percentile = ranked.market_percentile,
                    sector_si_percentile = ranked.sector_percentile,
                    industry_si_percentile = ranked.peer_percentile,
                    calculated_at = now()
                FROM ranked
                WHERE target.settlement_date = ranked.settlement_date
                  AND target.ticker = ranked.ticker
                """
            )
        )
        conn.execute(
            text(
                """
                WITH persistence AS (
                    SELECT settlement_date,
                           ticker,
                           avg((si_change_1obs > 0)::int) OVER (
                               PARTITION BY ticker ORDER BY settlement_date
                               ROWS BETWEEN 11 PRECEDING AND CURRENT ROW
                           ) AS persistence_6m,
                           avg((si_change_1obs > 0)::int) OVER (
                               PARTITION BY ticker ORDER BY settlement_date
                               ROWS BETWEEN 23 PRECEDING AND CURRENT ROW
                           ) AS persistence_12m,
                           avg((si_change_1obs > 0)::int) OVER (
                               PARTITION BY ticker ORDER BY settlement_date
                               ROWS BETWEEN 47 PRECEDING AND CURRENT ROW
                           ) AS persistence_24m
                    FROM public.finra_short_interest_features
                    WHERE si_change_1obs IS NOT NULL
                )
                UPDATE public.finra_short_interest_features target
                SET si_persistence_6m = persistence.persistence_6m,
                    si_persistence_12m = persistence.persistence_12m,
                    si_persistence_24m = persistence.persistence_24m,
                    calculated_at = now()
                FROM persistence
                WHERE target.settlement_date = persistence.settlement_date
                  AND target.ticker = persistence.ticker
                """
            )
        )
    if progress:
        progress(f"FINRA short-interest analytics upserted rows={upserted:,}")
    return {"raw_rows": raw_count, "feature_rows": feature_count, "upserted": upserted, "ticker_count": len(tickers)}
