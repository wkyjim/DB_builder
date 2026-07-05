"""Free economic indicator fetching and storage.

The first implementation uses FRED's public CSV download endpoint. It does not
require an API key and is suitable for scheduled local PostgreSQL updates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
from uuid import NAMESPACE_URL, uuid5

import pandas as pd
import requests
from sqlalchemy import text


FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


@dataclass(frozen=True)
class EconomicSeries:
    series_id: str
    series_name: str
    source: str
    country: str
    region: str
    category: str
    frequency: str
    unit: str
    seasonal_adjustment: str = ""


DEFAULT_SERIES: tuple[EconomicSeries, ...] = (
    EconomicSeries("PAYEMS", "All Employees, Total Nonfarm", "FRED", "US", "United States", "labor", "monthly", "thousands", "seasonally adjusted"),
    EconomicSeries("UNRATE", "Unemployment Rate", "FRED", "US", "United States", "labor", "monthly", "percent", "seasonally adjusted"),
    EconomicSeries("ICSA", "Initial Claims", "FRED", "US", "United States", "labor", "weekly", "number", "seasonally adjusted"),
    EconomicSeries("CCSA", "Continued Claims", "FRED", "US", "United States", "labor", "weekly", "number", "seasonally adjusted"),
    EconomicSeries("CIVPART", "Labor Force Participation Rate", "FRED", "US", "United States", "labor", "monthly", "percent", "seasonally adjusted"),
    EconomicSeries("GDPC1", "Real Gross Domestic Product", "FRED", "US", "United States", "growth", "quarterly", "billions chained 2017 dollars", "seasonally adjusted annual rate"),
    EconomicSeries("INDPRO", "Industrial Production Index", "FRED", "US", "United States", "growth", "monthly", "index 2017=100", "seasonally adjusted"),
    EconomicSeries("RSAFS", "Advance Retail Sales", "FRED", "US", "United States", "growth", "monthly", "millions dollars", "seasonally adjusted"),
    EconomicSeries("CPIAUCSL", "Consumer Price Index for All Urban Consumers", "FRED", "US", "United States", "inflation", "monthly", "index 1982-1984=100", "seasonally adjusted"),
    EconomicSeries("CPILFESL", "Core CPI", "FRED", "US", "United States", "inflation", "monthly", "index 1982-1984=100", "seasonally adjusted"),
    EconomicSeries("CPIAUCNS", "Consumer Price Index for All Urban Consumers", "FRED", "US", "United States", "inflation", "monthly", "index 1982-1984=100", "not seasonally adjusted"),
    EconomicSeries("CPILFENS", "Core CPI", "FRED", "US", "United States", "inflation", "monthly", "index 1982-1984=100", "not seasonally adjusted"),
    EconomicSeries("PCEPI", "Personal Consumption Expenditures Price Index", "FRED", "US", "United States", "inflation", "monthly", "index 2017=100", "seasonally adjusted"),
    EconomicSeries("PCEPILFE", "Core PCE Price Index", "FRED", "US", "United States", "inflation", "monthly", "index 2017=100", "seasonally adjusted"),
    EconomicSeries("PPIFID", "Producer Price Index: Final Demand", "FRED", "US", "United States", "inflation", "monthly", "index Nov 2009=100", "not seasonally adjusted"),
    EconomicSeries("PPIFES", "Producer Price Index: Final Demand Less Foods and Energy", "FRED", "US", "United States", "inflation", "monthly", "index Apr 2010=100", "seasonally adjusted"),
    EconomicSeries("PPIACO", "Producer Price Index by Commodity: All Commodities", "FRED", "US", "United States", "inflation", "monthly", "index 1982=100", "not seasonally adjusted"),
    EconomicSeries("FEDFUNDS", "Effective Federal Funds Rate", "FRED", "US", "United States", "policy", "monthly", "percent", ""),
    EconomicSeries("DFF", "Federal Funds Effective Rate", "FRED", "US", "United States", "policy", "daily", "percent", ""),
    EconomicSeries("SOFR", "Secured Overnight Financing Rate", "FRED", "US", "United States", "policy", "daily", "percent", ""),
    EconomicSeries("WALCL", "Assets: Total Assets: Federal Reserve", "FRED", "US", "United States", "liquidity", "weekly", "millions dollars", ""),
    EconomicSeries("M1SL", "M1 Money Stock", "FRED", "US", "United States", "liquidity", "monthly", "billions dollars", "seasonally adjusted"),
    EconomicSeries("M2SL", "M2 Money Stock", "FRED", "US", "United States", "liquidity", "monthly", "billions dollars", "seasonally adjusted"),
    EconomicSeries("HOUST", "Housing Starts", "FRED", "US", "United States", "housing", "monthly", "thousands", "seasonally adjusted annual rate"),
    EconomicSeries("PERMIT", "New Privately-Owned Housing Units Authorized", "FRED", "US", "United States", "housing", "monthly", "thousands", "seasonally adjusted annual rate"),
    EconomicSeries("MORTGAGE30US", "30-Year Fixed Rate Mortgage Average", "FRED", "US", "United States", "housing", "weekly", "percent", ""),
    EconomicSeries("BAMLH0A0HYM2", "ICE BofA US High Yield Option-Adjusted Spread", "FRED", "US", "United States", "credit", "daily", "percent", ""),
    EconomicSeries("BAMLC0A0CM", "ICE BofA US Corporate Option-Adjusted Spread", "FRED", "US", "United States", "credit", "daily", "percent", ""),
    EconomicSeries("UMCSENT", "University of Michigan Consumer Sentiment", "FRED", "US", "United States", "sentiment", "monthly", "index 1966:Q1=100", ""),
)


DERIVED_INFLATION_SERIES = {
    "CPIAUCSL": ("CPI_HEADLINE_SA", "Headline CPI", "seasonally adjusted"),
    "CPILFESL": ("CPI_CORE_SA", "Core CPI", "seasonally adjusted"),
    "CPIAUCNS": ("CPI_HEADLINE_NSA", "Headline CPI", "not seasonally adjusted"),
    "CPILFENS": ("CPI_CORE_NSA", "Core CPI", "not seasonally adjusted"),
    "PPIFID": ("PPI_HEADLINE", "Headline PPI Final Demand", "not seasonally adjusted"),
    "PPIFES": ("PPI_CORE", "Core PPI Final Demand Less Foods and Energy", "seasonally adjusted"),
    "PCEPI": ("PCE_HEADLINE", "Headline PCE Price Index", "seasonally adjusted"),
    "PCEPILFE": ("PCE_CORE", "Core PCE Price Index", "seasonally adjusted"),
}


def series_registry() -> dict[str, EconomicSeries]:
    return {series.series_id: series for series in DEFAULT_SERIES}


def create_economic_indicators_table(engine) -> None:
    indicator_sql = """
        CREATE TABLE IF NOT EXISTS public.economic_indicators (
            date date NOT NULL,
            series_id text NOT NULL,
            series_name text,
            source text,
            country text,
            region text,
            category text,
            frequency text,
            value numeric,
            unit text,
            seasonal_adjustment text,
            realtime_start date NOT NULL DEFAULT CURRENT_DATE,
            realtime_end date NOT NULL DEFAULT DATE '9999-12-31',
            updated_at timestamptz NOT NULL DEFAULT now(),
            raw_payload jsonb,
            PRIMARY KEY (series_id, date, realtime_start)
        )
    """
    release_sql = """
        CREATE TABLE IF NOT EXISTS public.economic_release_calendar (
            release_id uuid PRIMARY KEY,
            source text NOT NULL,
            release_name text NOT NULL,
            series_ids text[] NOT NULL,
            category text,
            country text,
            period_covered text,
            release_datetime timestamptz NOT NULL,
            release_timezone text,
            importance text,
            status text,
            source_url text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            raw_payload jsonb
        )
    """
    index_sql = """
        CREATE INDEX IF NOT EXISTS idx_economic_release_calendar_series_time
        ON public.economic_release_calendar USING gin (series_ids)
    """
    with engine.begin() as conn:
        conn.execute(text(indicator_sql))
        conn.execute(text(release_sql))
        conn.execute(text(index_sql))


def _utc_datetime(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def default_release_calendar(now: datetime | None = None) -> list[dict]:
    """Return known/reasonable next release dates for the registered series.

    High-impact monthly and quarterly dates come from official July 2026 BLS,
    BEA, and Census calendars. Daily and weekly series use conservative
    scheduled-release approximations so the fetcher avoids unnecessary network
    calls before a new observation is plausibly available.
    """
    now = now or datetime.now(timezone.utc)
    releases = [
        {
            "source": "BLS",
            "release_name": "Employment Situation",
            "series_ids": ["PAYEMS", "UNRATE", "CIVPART"],
            "category": "labor",
            "country": "US",
            "period_covered": "June 2026",
            "release_datetime": _utc_datetime(2026, 7, 2, 12, 30),
            "release_timezone": "America/New_York",
            "importance": "high",
            "status": "scheduled",
            "source_url": "https://www.bls.gov/schedule/news_release/",
        },
        {
            "source": "BLS",
            "release_name": "Consumer Price Index",
            "series_ids": ["CPIAUCSL", "CPILFESL", "CPIAUCNS", "CPILFENS"],
            "category": "inflation",
            "country": "US",
            "period_covered": "June 2026",
            "release_datetime": _utc_datetime(2026, 7, 14, 12, 30),
            "release_timezone": "America/New_York",
            "importance": "high",
            "status": "scheduled",
            "source_url": "https://www.bls.gov/schedule/news_release/",
        },
        {
            "source": "BLS",
            "release_name": "Producer Price Index",
            "series_ids": ["PPIACO", "PPIFID", "PPIFES"],
            "category": "inflation",
            "country": "US",
            "period_covered": "June 2026",
            "release_datetime": _utc_datetime(2026, 7, 15, 12, 30),
            "release_timezone": "America/New_York",
            "importance": "high",
            "status": "scheduled",
            "source_url": "https://www.bls.gov/schedule/news_release/",
        },
        {
            "source": "Census",
            "release_name": "Advance Monthly Retail Sales",
            "series_ids": ["RSAFS"],
            "category": "growth",
            "country": "US",
            "period_covered": "June 2026",
            "release_datetime": _utc_datetime(2026, 7, 16, 12, 30),
            "release_timezone": "America/New_York",
            "importance": "high",
            "status": "scheduled",
            "source_url": "https://www.census.gov/economic-indicators/calendar-listview.html",
        },
        {
            "source": "Census",
            "release_name": "New Residential Construction",
            "series_ids": ["HOUST", "PERMIT"],
            "category": "housing",
            "country": "US",
            "period_covered": "June 2026",
            "release_datetime": _utc_datetime(2026, 7, 17, 12, 30),
            "release_timezone": "America/New_York",
            "importance": "medium",
            "status": "scheduled",
            "source_url": "https://www.census.gov/economic-indicators/calendar-listview.html",
        },
        {
            "source": "BEA",
            "release_name": "GDP Advance Estimate and Personal Income and Outlays",
            "series_ids": ["GDPC1", "PCEPI", "PCEPILFE"],
            "category": "growth_inflation",
            "country": "US",
            "period_covered": "Q2 2026 / June 2026",
            "release_datetime": _utc_datetime(2026, 7, 30, 12, 30),
            "release_timezone": "America/New_York",
            "importance": "high",
            "status": "scheduled",
            "source_url": "https://www.bea.gov/news/schedule",
        },
    ]
    weekly_rules = {
        "ICSA": ("BLS/FRED", "Initial Claims", "labor", "high", 3, time(12, 30)),
        "CCSA": ("BLS/FRED", "Continued Claims", "labor", "medium", 3, time(12, 30)),
        "WALCL": ("Federal Reserve", "H.4.1 Federal Reserve Balance Sheet", "liquidity", "medium", 3, time(20, 30)),
        "MORTGAGE30US": ("Freddie Mac/FRED", "30-Year Fixed Mortgage Rate", "housing", "medium", 3, time(16, 0)),
    }
    daily_rules = {
        "DFF": ("Federal Reserve/FRED", "Daily Federal Funds Effective Rate", "policy", "medium"),
        "SOFR": ("Federal Reserve/FRED", "Secured Overnight Financing Rate", "policy", "medium"),
        "BAMLH0A0HYM2": ("ICE BofA/FRED", "US High Yield Option-Adjusted Spread", "credit", "medium"),
        "BAMLC0A0CM": ("ICE BofA/FRED", "US Corporate Option-Adjusted Spread", "credit", "medium"),
    }
    monthly_rules = {
        "INDPRO": ("Federal Reserve/FRED", "Industrial Production", "growth", "medium", 15),
        "M1SL": ("Federal Reserve/FRED", "M1 Money Stock", "liquidity", "medium", 22),
        "M2SL": ("Federal Reserve/FRED", "M2 Money Stock", "liquidity", "medium", 22),
        "FEDFUNDS": ("Federal Reserve/FRED", "Monthly Effective Federal Funds Rate", "policy", "medium", 1),
        "UMCSENT": ("University of Michigan/FRED", "Consumer Sentiment", "sentiment", "medium", 12),
    }
    for series_id, (source, name, category, importance, weekday, release_time) in weekly_rules.items():
        release_dt = _next_weekday_release(now, weekday, release_time)
        releases.append(_release_row(source, name, [series_id], category, release_dt, importance, "rule_based_weekly"))
    for series_id, (source, name, category, importance) in daily_rules.items():
        release_dt = _next_business_day_release(now, time(21, 0))
        releases.append(_release_row(source, name, [series_id], category, release_dt, importance, "rule_based_daily"))
    for series_id, (source, name, category, importance, day) in monthly_rules.items():
        release_dt = _next_monthly_release(now, day, time(21, 0))
        releases.append(_release_row(source, name, [series_id], category, release_dt, importance, "rule_based_monthly"))
    return releases


def _release_row(source: str, name: str, series_ids: list[str], category: str, release_dt: datetime, importance: str, status: str) -> dict:
    return {
        "source": source,
        "release_name": name,
        "series_ids": series_ids,
        "category": category,
        "country": "US",
        "period_covered": "",
        "release_datetime": release_dt,
        "release_timezone": "UTC",
        "importance": importance,
        "status": status,
        "source_url": "https://fred.stlouisfed.org/",
    }


def _next_business_day_release(now: datetime, release_time: time) -> datetime:
    candidate = datetime.combine(now.date(), release_time, tzinfo=timezone.utc)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _next_weekday_release(now: datetime, weekday: int, release_time: time) -> datetime:
    days = (weekday - now.weekday()) % 7
    candidate = datetime.combine(now.date() + timedelta(days=days), release_time, tzinfo=timezone.utc)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _next_monthly_release(now: datetime, day: int, release_time: time) -> datetime:
    year = now.year
    month = now.month
    while True:
        candidate = datetime(year, month, min(day, 28), release_time.hour, release_time.minute, tzinfo=timezone.utc)
        if candidate > now:
            return candidate
        month += 1
        if month == 13:
            month = 1
            year += 1


def upsert_release_calendar(engine, releases: list[dict]) -> int:
    if not releases:
        return 0
    sql = """
        INSERT INTO public.economic_release_calendar (
            release_id, source, release_name, series_ids, category, country,
            period_covered, release_datetime, release_timezone, importance,
            status, source_url, updated_at, raw_payload
        )
        VALUES (
            :release_id, :source, :release_name, :series_ids, :category, :country,
            :period_covered, :release_datetime, :release_timezone, :importance,
            :status, :source_url, now(), CAST(:raw_payload AS jsonb)
        )
        ON CONFLICT (release_id)
        DO UPDATE SET
            source = EXCLUDED.source,
            release_name = EXCLUDED.release_name,
            series_ids = EXCLUDED.series_ids,
            category = EXCLUDED.category,
            country = EXCLUDED.country,
            period_covered = EXCLUDED.period_covered,
            release_datetime = EXCLUDED.release_datetime,
            release_timezone = EXCLUDED.release_timezone,
            importance = EXCLUDED.importance,
            status = EXCLUDED.status,
            source_url = EXCLUDED.source_url,
            updated_at = now(),
            raw_payload = EXCLUDED.raw_payload
    """
    serializable = []
    for release in releases:
        natural_id = f"{release['source']}|{release['release_name']}|{release['release_datetime'].isoformat()}"
        row = {
            **release,
            "release_id": str(uuid5(NAMESPACE_URL, natural_id)) if release.get("release_id") is None else release["release_id"],
            "raw_payload": json.dumps({"natural_id": natural_id, **release}, default=str),
        }
        serializable.append(row)
    with engine.begin() as conn:
        conn.execute(text(sql), serializable)
    return len(serializable)


def replace_upcoming_release_calendar(engine, releases: list[dict], *, now: datetime | None = None) -> int:
    """Refresh known upcoming rows using idempotent upserts only."""
    return upsert_release_calendar(engine, releases)


def next_release_for_series(engine, series_id: str, *, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    sql = """
        SELECT release_name, source, category, period_covered, release_datetime,
               importance, status, source_url
        FROM public.economic_release_calendar
        WHERE :series_id = ANY(series_ids)
          AND release_datetime >= :now
        ORDER BY release_datetime ASC
        LIMIT 1
    """
    df = pd.read_sql(text(sql), engine, params={"series_id": series_id, "now": now})
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def should_fetch_series(engine, series_id: str, *, now: datetime | None = None, force_refresh: bool = False) -> tuple[bool, str]:
    if force_refresh:
        return True, "force-refresh"
    now = now or datetime.now(timezone.utc)
    next_release = next_release_for_series(engine, series_id, now=now)
    if not next_release:
        return True, "no release calendar row"
    release_at = _coerce_utc(next_release["release_datetime"])
    if is_release_due(release_at, now=now):
        return True, f"release passed: {release_at.isoformat()}"
    return False, f"next release not due until {release_at.isoformat()}"


def _coerce_utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_release_due(release_at: datetime, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return _coerce_utc(now) >= _coerce_utc(release_at)


def fetch_fred_csv(series: EconomicSeries, *, start_date: str | date = "2025-01-01", timeout: int = 30) -> list[dict]:
    response = requests.get(FRED_CSV_URL, params={"id": series.series_id}, timeout=timeout)
    response.raise_for_status()
    df = pd.read_csv(StringIO(response.text))
    if "observation_date" not in df.columns or series.series_id not in df.columns:
        raise ValueError(f"Unexpected FRED CSV shape for {series.series_id}: {list(df.columns)}")

    df["date"] = pd.to_datetime(df["observation_date"], errors="coerce").dt.date
    df["value"] = pd.to_numeric(df[series.series_id].replace(".", pd.NA), errors="coerce")
    start = pd.to_datetime(start_date).date()
    df = df[(df["date"] >= start) & df["value"].notna()]
    today = date.today()
    rows = []
    for item in df.to_dict(orient="records"):
        rows.append(
            {
                "date": item["date"],
                "series_id": series.series_id,
                "series_name": series.series_name,
                "source": series.source,
                "country": series.country,
                "region": series.region,
                "category": series.category,
                "frequency": series.frequency,
                "value": float(item["value"]),
                "unit": series.unit,
                "seasonal_adjustment": series.seasonal_adjustment,
                "realtime_start": today,
                "realtime_end": date(9999, 12, 31),
                "updated_at": datetime.now(timezone.utc),
                "raw_payload": {"observation_date": str(item["date"]), "value": item["value"]},
            }
        )
    return rows


def upsert_economic_indicators(engine, rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO public.economic_indicators (
            date, series_id, series_name, source, country, region, category,
            frequency, value, unit, seasonal_adjustment, realtime_start,
            realtime_end, updated_at, raw_payload
        )
        VALUES (
            :date, :series_id, :series_name, :source, :country, :region, :category,
            :frequency, :value, :unit, :seasonal_adjustment, :realtime_start,
            :realtime_end, :updated_at, CAST(:raw_payload AS jsonb)
        )
        ON CONFLICT (series_id, date, realtime_start)
        DO UPDATE SET
            series_name = EXCLUDED.series_name,
            source = EXCLUDED.source,
            country = EXCLUDED.country,
            region = EXCLUDED.region,
            category = EXCLUDED.category,
            frequency = EXCLUDED.frequency,
            value = EXCLUDED.value,
            unit = EXCLUDED.unit,
            seasonal_adjustment = EXCLUDED.seasonal_adjustment,
            realtime_end = EXCLUDED.realtime_end,
            updated_at = now(),
            raw_payload = EXCLUDED.raw_payload
    """
    serializable = [{**row, "raw_payload": json.dumps(_json_safe(row["raw_payload"]), default=str, allow_nan=False)} for row in rows]
    with engine.begin() as conn:
        conn.execute(text(sql), serializable)
    return len(rows)


def build_derived_inflation_rows_from_base_rows(base_rows: list[dict]) -> list[dict]:
    if not base_rows:
        return []
    df = pd.DataFrame(base_rows)
    required = {"series_id", "date", "value"}
    if not required <= set(df.columns):
        return []
    registry = series_registry()
    today = date.today()
    derived_rows = []
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df[df["series_id"].isin(DERIVED_INFLATION_SERIES) & df["value"].notna()]
    for source_series_id, group in df.groupby("series_id"):
        slug, label, seasonal_adjustment = DERIVED_INFLATION_SERIES[source_series_id]
        source_meta = registry.get(source_series_id)
        group = group.sort_values("date").drop_duplicates("date", keep="last").copy()
        group["mom"] = group["value"].pct_change(periods=1) * 100
        group["yoy"] = group["value"].pct_change(periods=12) * 100
        for metric, column, metric_label in [
            ("MOM", "mom", "month-over-month"),
            ("YOY", "yoy", "year-over-year"),
        ]:
            series_id = f"DERIVED:{slug}:{metric}"
            for item in group[group[column].notna()].to_dict(orient="records"):
                derived_rows.append(
                    {
                        "date": item["date"],
                        "series_id": series_id,
                        "series_name": f"{label} {metric_label} inflation rate",
                        "source": "Derived/FRED",
                        "country": source_meta.country if source_meta else "US",
                        "region": source_meta.region if source_meta else "United States",
                        "category": "inflation",
                        "frequency": "monthly",
                        "value": round(float(item[column]), 6),
                        "unit": "percent",
                        "seasonal_adjustment": seasonal_adjustment,
                        "realtime_start": today,
                        "realtime_end": date(9999, 12, 31),
                        "updated_at": datetime.now(timezone.utc),
                        "raw_payload": {
                            "source_series_id": source_series_id,
                            "source_index_value": float(item["value"]),
                            "formula": (
                                "100 * (current_index / previous_month_index - 1)"
                                if metric == "MOM"
                                else "100 * (current_index / index_12_months_ago - 1)"
                            ),
                        },
                    }
                )
    return derived_rows


def build_derived_inflation_rows(engine, *, start_date: str | date = "2025-01-01") -> list[dict]:
    source_ids = list(DERIVED_INFLATION_SERIES)
    sql = """
        SELECT DISTINCT ON (series_id, date)
            series_id, date, value
        FROM public.economic_indicators
        WHERE series_id = ANY(:series_ids)
          AND date >= :start_date
        ORDER BY series_id, date, realtime_start DESC
    """
    base = pd.read_sql(text(sql), engine, params={"series_ids": source_ids, "start_date": pd.to_datetime(start_date).date()})
    return build_derived_inflation_rows_from_base_rows(base.to_dict(orient="records"))


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def latest_economic_summary(engine, *, series_ids: list[str] | None = None) -> pd.DataFrame:
    selected = series_ids or list(series_registry())
    sql = """
        SELECT DISTINCT ON (series_id)
            series_id, series_name, category, frequency, date, value, unit, updated_at
        FROM public.economic_indicators
        WHERE series_id = ANY(:series_ids)
        ORDER BY series_id, date DESC, realtime_start DESC
    """
    return pd.read_sql(text(sql), engine, params={"series_ids": selected})


def upcoming_release_summary(engine, *, series_ids: list[str] | None = None, now: datetime | None = None) -> pd.DataFrame:
    selected = series_ids or list(series_registry())
    now = now or datetime.now(timezone.utc)
    sql = """
        SELECT release_datetime, release_name, source, category, importance,
               period_covered, series_ids, status
        FROM public.economic_release_calendar
        WHERE release_datetime >= :now
          AND series_ids && CAST(:series_ids AS text[])
        ORDER BY release_datetime ASC
    """
    return pd.read_sql(text(sql), engine, params={"now": now, "series_ids": selected})
