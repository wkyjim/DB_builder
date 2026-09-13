"""Availability probes and shared schema for positioning/flow data sources."""

from __future__ import annotations

import io
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pandas_market_calendars as mcal
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from sqlalchemy import text
from urllib3.util.retry import Retry


CFTC_HISTORICAL_COMPRESSED_URL = "https://www.cftc.gov/MarketReports/CommitmentsofTraders/HistoricalCompressed/index.htm"
FINRA_SHORT_VOLUME_PAGE = "https://www.finra.org/finra-data/browse-catalog/short-sale-volume-data"
SEC_13F_DATASETS_URL = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
ICI_STATS_URL = "https://www.ici.org/research/stats"

ETF_HOLDING_PROBES = {
    "SPDR_XLK": "https://www.ssga.com/us/en/intermediary/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-xlk.xlsx",
    "iShares_HYG": "https://www.ishares.com/us/products/239565/ishares-iboxx-high-yield-corporate-bond-etf/1467271812596.ajax?fileType=csv&fileName=HYG_holdings&dataType=fund",
    "Invesco_QQQ": "https://www.invesco.com/us/financial-products/etfs/holdings/main/holdings/0?audienceType=Investor&action=download&ticker=QQQ",
}


@dataclass
class SourceProbeResult:
    source: str
    status: str
    latest_available_date: date | None
    fetch_url: str
    row_count_sample: int | None
    parser_ready: bool
    last_error: str | None = None

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "status": self.status,
            "latest_available_date": self.latest_available_date,
            "fetch_url": self.fetch_url,
            "row_count_sample": self.row_count_sample,
            "parser_ready": self.parser_ready,
            "last_error": self.last_error,
        }


def http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "DBBuilder/1.0 contact: local-research",
            "Accept": "text/html,text/csv,application/zip,application/octet-stream,*/*",
        }
    )
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.75,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def setup_flow_source_health_schema(engine) -> None:
    sql = text(
        """
        CREATE TABLE IF NOT EXISTS public.flow_source_health (
            source_name text PRIMARY KEY,
            source_type text,
            last_success_at timestamptz,
            last_failure_at timestamptz,
            success_count integer DEFAULT 0,
            failure_count integer DEFAULT 0,
            latest_available_date date,
            last_error text,
            avg_fetch_seconds numeric,
            enabled_recommendation boolean DEFAULT true,
            updated_at timestamptz DEFAULT now()
        );
        """
    )
    with engine.begin() as conn:
        conn.execute(sql)


def record_flow_source_health(
    engine,
    *,
    source_name: str,
    source_type: str,
    succeeded: bool,
    fetch_seconds: float,
    latest_available_date: date | None = None,
    error: str | None = None,
) -> None:
    setup_flow_source_health_schema(engine)
    sql = text(
        """
        INSERT INTO public.flow_source_health (
            source_name, source_type, last_success_at, last_failure_at,
            success_count, failure_count, latest_available_date, last_error,
            avg_fetch_seconds, enabled_recommendation, updated_at
        )
        VALUES (
            :source_name, :source_type,
            CASE WHEN :succeeded THEN now() ELSE NULL END,
            CASE WHEN :succeeded THEN NULL ELSE now() END,
            CASE WHEN :succeeded THEN 1 ELSE 0 END,
            CASE WHEN :succeeded THEN 0 ELSE 1 END,
            :latest_available_date,
            :error,
            :fetch_seconds,
            true,
            now()
        )
        ON CONFLICT (source_name)
        DO UPDATE SET
            source_type = EXCLUDED.source_type,
            last_success_at = CASE WHEN :succeeded THEN now() ELSE public.flow_source_health.last_success_at END,
            last_failure_at = CASE WHEN :succeeded THEN public.flow_source_health.last_failure_at ELSE now() END,
            success_count = public.flow_source_health.success_count + CASE WHEN :succeeded THEN 1 ELSE 0 END,
            failure_count = public.flow_source_health.failure_count + CASE WHEN :succeeded THEN 0 ELSE 1 END,
            latest_available_date = COALESCE(:latest_available_date, public.flow_source_health.latest_available_date),
            last_error = CASE WHEN :succeeded THEN NULL ELSE :error END,
            avg_fetch_seconds = CASE
                WHEN public.flow_source_health.avg_fetch_seconds IS NULL THEN :fetch_seconds
                ELSE ((public.flow_source_health.avg_fetch_seconds * GREATEST(public.flow_source_health.success_count + public.flow_source_health.failure_count, 1)) + :fetch_seconds)
                    / GREATEST(public.flow_source_health.success_count + public.flow_source_health.failure_count + 1, 1)
            END,
            enabled_recommendation = CASE
                WHEN :succeeded THEN true
                WHEN public.flow_source_health.failure_count + 1 >= 5 THEN false
                ELSE public.flow_source_health.enabled_recommendation
            END,
            updated_at = now();
        """
    )
    with engine.begin() as conn:
        conn.execute(
            sql,
            {
                "source_name": source_name,
                "source_type": source_type,
                "succeeded": succeeded,
                "fetch_seconds": fetch_seconds,
                "latest_available_date": latest_available_date,
                "error": error,
            },
        )


def finra_short_volume_url(for_date: date, *, market: str = "CNMS") -> str:
    return f"https://cdn.finra.org/equity/regsho/daily/{market}shvol{for_date:%Y%m%d}.txt"


def finra_short_interest_url(settlement_date: date) -> str:
    return f"https://cdn.finra.org/equity/otcmarket/biweekly/shrt{settlement_date:%Y%m%d}.csv"


def recent_business_dates(days: int = 10, *, today: date | None = None) -> list[date]:
    current = today or datetime.now(timezone.utc).date()
    dates = []
    cursor = current
    while len(dates) < days:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor -= timedelta(days=1)
    return dates


def business_dates_between(start_date: date, end_date: date) -> list[date]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    dates = []
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor += timedelta(days=1)
    return dates


def market_session_dates_between(start_date: date, end_date: date) -> list[date]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    schedule = mcal.get_calendar("NYSE").schedule(start_date=start_date, end_date=end_date)
    return [timestamp.date() for timestamp in schedule.index]


def probe_cftc(timeout: int = 20) -> SourceProbeResult:
    started = time.perf_counter()
    try:
        session = http_session()
        response = session.get(CFTC_HISTORICAL_COMPRESSED_URL, timeout=timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        links = [a.get("href", "") for a in soup.find_all("a")]
        text_links = [link for link in links if "history" in link.lower() and link.lower().endswith(".zip")]
        latest_year = datetime.now(timezone.utc).year
        latest_links = [link for link in text_links if str(latest_year) in link]
        fetch_url = latest_links[0] if latest_links else (text_links[0] if text_links else CFTC_HISTORICAL_COMPRESSED_URL)
        if fetch_url.startswith("/"):
            fetch_url = "https://www.cftc.gov" + fetch_url
        return SourceProbeResult("CFTC COT", "ok", None, fetch_url, len(text_links), bool(text_links)).as_dict()
    except Exception as exc:
        return SourceProbeResult("CFTC COT", "error", None, CFTC_HISTORICAL_COMPRESSED_URL, None, False, str(exc)).as_dict()
    finally:
        _ = time.perf_counter() - started


def probe_finra(timeout: int = 12) -> dict:
    session = http_session()
    last_error = None
    for candidate in recent_business_dates(12):
        url = finra_short_volume_url(candidate)
        try:
            response = session.get(url, timeout=timeout)
            if response.status_code != 200 or "Symbol" not in response.text[:300]:
                last_error = f"{candidate}: HTTP {response.status_code}"
                continue
            df = pd.read_csv(io.StringIO(response.text), sep="|")
            return SourceProbeResult(
                "FINRA short-sale volume",
                "ok",
                candidate,
                url,
                len(df.head(1000)),
                {"Date", "Symbol", "ShortVolume", "TotalVolume"}.issubset(df.columns),
            ).as_dict()
        except Exception as exc:
            last_error = str(exc)
    return SourceProbeResult("FINRA short-sale volume", "error", None, FINRA_SHORT_VOLUME_PAGE, None, False, last_error).as_dict()


def probe_simple_page(source: str, source_type: str, url: str, marker: str, timeout: int = 15) -> dict:
    try:
        response = http_session().get(url, timeout=timeout)
        ok = response.status_code == 200 and marker.lower() in response.text.lower()
        return SourceProbeResult(source, "ok" if ok else "error", None, url, None, ok, None if ok else f"HTTP {response.status_code} or marker not found").as_dict()
    except Exception as exc:
        return SourceProbeResult(source, "error", None, url, None, False, str(exc)).as_dict()


def probe_issuer_holdings(timeout: int = 15) -> dict:
    session = http_session()
    results = []
    for name, url in ETF_HOLDING_PROBES.items():
        try:
            response = session.get(url, timeout=timeout, stream=True)
            results.append((name, response.status_code, int(response.headers.get("content-length") or 0)))
        except Exception as exc:
            results.append((name, "error", str(exc)))
    ok = any(status == 200 for _, status, _ in results)
    return SourceProbeResult("ETF issuer holdings", "ok" if ok else "error", None, "; ".join(ETF_HOLDING_PROBES.values()), len(results), ok, None if ok else str(results)).as_dict()


def run_source_probe(timeout: int = 20) -> list[dict]:
    return [
        probe_cftc(timeout=timeout),
        probe_finra(timeout=min(timeout, 12)),
        probe_simple_page("SEC 13F datasets", "sec_13f", SEC_13F_DATASETS_URL, "13F", timeout=timeout),
        probe_simple_page("ICI statistics", "ici_flows", ICI_STATS_URL, "Statistics", timeout=timeout),
        probe_issuer_holdings(timeout=timeout),
    ]


def source_probe_dataframe(results: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(results)[
        ["source", "status", "latest_available_date", "fetch_url", "row_count_sample", "parser_ready", "last_error"]
    ]
