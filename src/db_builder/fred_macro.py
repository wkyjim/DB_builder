"""FRED daily macro series ingestion for public.macro.

Adds the 5-year and 10-year breakeven inflation rates (T5YIE, T10YIE) to the
same macro table used by the market-regime and report pipelines.

Daily cadence: FRED publishes business-day observations around 16:03-16:04
America/Chicago. The fetcher only calls FRED when the newest expected business
day is not yet stored, so an hourly scheduler does not hammer the endpoint.
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import date, datetime, time, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

import pandas as pd
import urllib.request
import subprocess
from sqlalchemy import text

from db_builder.config import local_engine, neon_engine, NEON_MIRROR_SINCE

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
CHICAGO = ZoneInfo("America/Chicago")
RELEASE_TIME = time(16, 5)  # FRED posts ~16:03-16:04 CT; wait one minute.
SERIES = {
    "T5YIE": "5-Year Breakeven Inflation Rate",
    "T10YIE": "10-Year Breakeven Inflation Rate",
}
SOURCE = "FRED"
DEFINITION = "{series_id}: {name}, percent, daily, not seasonally adjusted"


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def us_federal_holidays(year: int) -> set[date]:
    """Federal Reserve / FRED holiday closures (observed dates)."""
    out = {
        date(year, 1, 1),
        _nth_weekday(year, 1, 0, 3),   # MLK
        _nth_weekday(year, 2, 0, 3),   # Presidents
        _last_weekday(year, 5, 0),      # Memorial
        date(year, 6, 19),              # Juneteenth
        date(year, 7, 4),
        _nth_weekday(year, 9, 0, 1),   # Labor
        _nth_weekday(year, 10, 0, 2),  # Columbus
        date(year, 11, 11),             # Veterans
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        date(year, 12, 25),
    }
    # Observed: Saturday -> Friday, Sunday -> Monday.
    fixed = {date(year, 1, 1), date(year, 6, 19), date(year, 7, 4),
             date(year, 11, 11), date(year, 12, 25)}
    for d in list(fixed):
        if d.weekday() == 5:
            out.add(d - timedelta(days=1))
        elif d.weekday() == 6:
            out.add(d + timedelta(days=1))
    return out


def is_business_day(d: date) -> bool:
    holidays = set()
    for year in (d.year - 1, d.year, d.year + 1):
        holidays |= us_federal_holidays(year)
    return d.weekday() < 5 and d not in holidays


def expected_latest(now: datetime | None = None) -> date:
    now = (now or datetime.now(CHICAGO)).astimezone(CHICAGO)
    d = now.date()
    if is_business_day(d) and now.time() >= RELEASE_TIME:
        return d
    d -= timedelta(days=1)
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def fetch_series(series_id: str, start: date) -> pd.Series:
    url = f"{FRED_CSV_URL}?id={series_id}&cosd={start.isoformat()}"
    body = None
    for attempt in range(3):
        try:
            proc = subprocess.run(
                ["curl.exe" if sys.platform == "win32" else "curl", "-sS", "--max-time", "45", url],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode == 0 and "observation_date" in proc.stdout[:200]:
                body = proc.stdout
                break
            req = urllib.request.Request(url, headers={"User-Agent": "db-builder/1.0"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = resp.read().decode("utf-8")
            break
        except Exception:
            if attempt == 2:
                raise
            _time.sleep(2 * (attempt + 1))
    if body is None or "observation_date" not in body[:200]:
        raise RuntimeError(f"FRED returned no CSV for {series_id}")
    df = pd.read_csv(StringIO(body))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"]).drop_duplicates("date").sort_values("date")
    return df.set_index("date")["value"]


def upsert_macro(engine, series_id: str, values: pd.Series) -> int:
    if values.empty:
        return 0
    name = SERIES[series_id]
    rows = []
    prev = None
    for d, value in values.items():
        value = float(value)
        change = None if prev is None else round(value - prev, 6)
        pct = None if prev in (None, 0) else round((value - prev) / prev * 100, 6)
        rows.append({
            "date": d, "symbol": series_id, "name": name, "asset_type": "macro_yield",
            "open": value, "high": value, "low": value, "close": value,
            "adj_close": value, "volume": None, "prev_close": prev,
            "change": change, "pct_chg": pct, "amplitude": 0,
            "source": SOURCE,
            "source_definition": DEFINITION.format(series_id=series_id, name=name),
        })
        prev = value
    sql = text("""
        INSERT INTO public.macro (
            date, symbol, name, asset_type, open, high, low, close, adj_close,
            volume, prev_close, change, pct_chg, amplitude, source, source_definition
        ) VALUES (
            :date, :symbol, :name, :asset_type, :open, :high, :low, :close, :adj_close,
            :volume, :prev_close, :change, :pct_chg, :amplitude, :source, :source_definition
        )
        ON CONFLICT (date, symbol) DO UPDATE SET
            name = EXCLUDED.name, asset_type = EXCLUDED.asset_type,
            open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
            close = EXCLUDED.close, adj_close = EXCLUDED.adj_close,
            prev_close = EXCLUDED.prev_close, change = EXCLUDED.change,
            pct_chg = EXCLUDED.pct_chg, amplitude = EXCLUDED.amplitude,
            source = EXCLUDED.source, source_definition = EXCLUDED.source_definition
    """)
    with engine.begin() as conn:
        conn.execute(sql, rows)
    return len(rows)


def stored_latest(engine, series_id: str) -> date | None:
    with engine.connect() as conn:
        return conn.execute(text(
            "select max(date) from public.macro where symbol = :s"
        ), {"s": series_id}).scalar()



def sync_neon(series_id: str, since: str = NEON_MIRROR_SINCE) -> int:
    """Mirror the canonical local rows on/after ``since`` into Neon."""
    local = local_engine()
    with local.connect() as conn:
        rows = conn.execute(text("""
            SELECT date, symbol, name, asset_type, open, high, low, close,
                   adj_close, volume, prev_close, change, pct_chg, amplitude,
                   source, source_definition
            FROM public.macro
            WHERE symbol = :s AND date >= :since
            ORDER BY date
        """), {"s": series_id, "since": since}).mappings().all()
    if not rows:
        return 0
    neon = neon_engine()
    sql = text("""
        INSERT INTO public.macro (
            date, symbol, name, asset_type, open, high, low, close, adj_close,
            volume, prev_close, change, pct_chg, amplitude, source, source_definition
        ) VALUES (
            :date, :symbol, :name, :asset_type, :open, :high, :low, :close, :adj_close,
            :volume, :prev_close, :change, :pct_chg, :amplitude, :source, :source_definition
        )
        ON CONFLICT (date, symbol) DO UPDATE SET
            name = EXCLUDED.name, asset_type = EXCLUDED.asset_type,
            open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
            close = EXCLUDED.close, adj_close = EXCLUDED.adj_close,
            prev_close = EXCLUDED.prev_close, change = EXCLUDED.change,
            pct_chg = EXCLUDED.pct_chg, amplitude = EXCLUDED.amplitude,
            source = EXCLUDED.source, source_definition = EXCLUDED.source_definition
    """)
    with neon.begin() as conn:
        conn.execute(sql, [dict(r) for r in rows])
        conn.execute(text("DELETE FROM public.macro WHERE symbol = :s AND date < :since"),
                     {"s": series_id, "since": since})
    neon.dispose()
    return len(rows)


def neon_latest(series_id: str) -> date | None:
    eng = neon_engine()
    try:
        with eng.connect() as conn:
            return conn.execute(text(
                "select max(date) from public.macro where symbol = :s"
            ), {"s": series_id}).scalar()
    finally:
        eng.dispose()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-since", help="force a full backfill from YYYY-MM-DD")
    ap.add_argument("--daily", action="store_true", help="release-gated daily sync")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-neon", action="store_true", help="local only")
    args = ap.parse_args()
    if not args.backfill_since and not args.daily:
        ap.error("choose --backfill-since YYYY-MM-DD or --daily")

    engine = local_engine()
    target = expected_latest()
    for series_id in SERIES:
        latest = stored_latest(engine, series_id)
        if args.backfill_since:
            start = date.fromisoformat(args.backfill_since)
        elif args.force or latest is None:
            start = date(2025, 1, 1)
        else:
            if latest >= target:
                if not args.no_neon and not args.dry_run:
                    remote = neon_latest(series_id)
                    if remote is None or remote < latest:
                        m = sync_neon(series_id)
                        print(f"{series_id}: neon converged {m} rows (was {remote})")
                print(f"{series_id}: up to date ({latest} >= {target}); skip")
                continue
            start = date(2025, 1, 1)
        print(f"{series_id}: fetching {start} .. {target} (stored latest {latest})")
        if args.dry_run:
            continue
        values = fetch_series(series_id, start)
        n = upsert_macro(engine, series_id, values)
        print(f"{series_id}: upserted {n} rows; latest={values.index.max()}")
        if not args.no_neon:
            m = sync_neon(series_id)
            print(f"{series_id}: neon mirrored {m} rows since {NEON_MIRROR_SINCE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
