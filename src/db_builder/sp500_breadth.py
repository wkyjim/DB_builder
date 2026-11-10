"""Point-in-time S&P 500 breadth from historical constituent membership.

This is a standalone research dataset. It does not modify or feed the current
production market-regime model.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

TABLE = "public.sp500_breadth_daily"
DOC = Path("docs/sp500_breadth.md")
COVERAGE_JSON = Path("data/sp500_breadth_coverage.json")


def setup_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                date date PRIMARY KEY,
                constituents integer,
                price_rows integer,
                indicator_rows_50 integer,
                indicator_rows_200 integer,
                indicator_rows_20 integer,
                above_50 integer,
                above_200 integer,
                coverage_pct numeric,
                indicator_coverage_pct numeric,
                pct_above_50d numeric,
                pct_above_200d numeric,
                pct_positive_20d numeric,
                advancers integer,
                decliners integer,
                advance_decline_ratio numeric,
                new_highs integer,
                new_lows integer,
                pct_new_highs numeric,
                pct_new_lows numeric,
                spx_close numeric,
                spx_return_20d_pct numeric,
                breadth_50d_change_5d numeric,
                breadth_50d_change_20d numeric,
                breadth_200d_change_5d numeric,
                breadth_200d_change_20d numeric,
                price_breadth_divergence numeric,
                negative_divergence boolean,
                updated_at timestamptz NOT NULL DEFAULT now()
            )
        """))
        conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS indicator_rows_20 integer"))
        conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS above_50 integer"))
        conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS above_200 integer"))
        conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS positive_20d integer"))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS idx_sp500_breadth_date ON {TABLE} (date)"))


def active_counts(engine, start: date, end: date) -> pd.DataFrame:
    sql = text("""
        SELECT d::date AS date, count(*) AS constituents
        FROM generate_series(CAST(:start AS date), CAST(:end AS date), INTERVAL '1 day') d
        JOIN public.sp500_membership_history m
          ON m.start_date <= d::date
         AND (m.end_date IS NULL OR m.end_date > d::date)
        GROUP BY d::date
        ORDER BY d::date
    """)
    return pd.read_sql(sql, engine, params={"start": start, "end": end})


def constituent_metrics(engine, start: date, end: date) -> pd.DataFrame:
    sql = text("""
        SELECT
            e.date,
            count(*) FILTER (WHERE e.close IS NOT NULL) AS price_rows,
            count(*) FILTER (WHERE i.ma_50 IS NOT NULL AND i.ma_50::text <> 'NaN') AS indicator_rows_50,
            count(*) FILTER (WHERE i.ma_200 IS NOT NULL AND i.ma_200::text <> 'NaN') AS indicator_rows_200,
            count(*) FILTER (WHERE i.ma_50 IS NOT NULL AND i.ma_50::text <> 'NaN' AND e.close >= i.ma_50) AS above_50,
            count(*) FILTER (WHERE i.ma_200 IS NOT NULL AND i.ma_200::text <> 'NaN' AND e.close >= i.ma_200) AS above_200,
            count(*) FILTER (WHERE i.return_20d IS NOT NULL AND i.return_20d::text <> 'NaN') AS indicator_rows_20,
            count(*) FILTER (WHERE i.return_20d IS NOT NULL AND i.return_20d::text <> 'NaN' AND i.return_20d > 0) AS positive_20d,
            count(*) FILTER (WHERE e.pct_chg > 0) AS advancers,
            count(*) FILTER (WHERE e.pct_chg < 0) AS decliners,
            count(*) FILTER (WHERE i.high_52w IS NOT NULL AND i.high_52w::text <> 'NaN' AND e.close >= i.high_52w) AS new_highs,
            count(*) FILTER (WHERE i.low_52w IS NOT NULL AND i.low_52w::text <> 'NaN' AND e.close <= i.low_52w) AS new_lows
        FROM public.sp500_membership_history m
        JOIN public.us_equities e
          ON e.ticker = regexp_replace(m.ticker, '\\s*\\|$', '')
         AND e.date BETWEEN m.start_date AND COALESCE(m.end_date - INTERVAL '1 day', CAST(:end AS date))
        LEFT JOIN public.us_equities_indicators i
          ON i.ticker = e.ticker AND i.date = e.date
        WHERE e.date BETWEEN CAST(:start AS date) AND CAST(:end AS date)
        GROUP BY e.date
        ORDER BY e.date
    """)
    return pd.read_sql(sql, engine, params={"start": start, "end": end})


def spx_series(engine, start: date, end: date) -> pd.DataFrame:
    sql = text("""
        SELECT date, close FROM public.macro
        WHERE symbol = '^GSPC' AND date BETWEEN CAST(:start AS date) AND CAST(:end AS date)
        ORDER BY date
    """)
    return pd.read_sql(sql, engine, params={"start": start, "end": end})


def build(engine, *, start: date, end: date) -> pd.DataFrame:
    setup_schema(engine)
    active = active_counts(engine, start, end)
    metrics = constituent_metrics(engine, start, end)
    spx = spx_series(engine, start, end).rename(columns={"close": "spx_close"})
    df = active.merge(metrics, on="date", how="left").merge(spx, on="date", how="left")
    df = df[df["date"].isin(metrics["date"])].reset_index(drop=True)
    for col in ("price_rows", "indicator_rows_50", "indicator_rows_200", "indicator_rows_20",
                "above_50", "above_200", "positive_20d", "advancers",
                "decliners", "new_highs", "new_lows"):
        df[col] = df[col].fillna(0).astype(int)
    df["coverage_pct"] = (df["price_rows"] / df["constituents"] * 100).round(2)
    df["indicator_coverage_pct"] = (
        df["indicator_rows_50"] / df["constituents"] * 100
    ).round(2)
    df["pct_above_50d"] = (df["above_50"] / df["indicator_rows_50"].replace(0, np.nan) * 100).round(2)
    df["pct_above_200d"] = (df["above_200"] / df["indicator_rows_200"].replace(0, np.nan) * 100).round(2)
    df["pct_positive_20d"] = (df["positive_20d"] / df["indicator_rows_20"].replace(0, np.nan) * 100).round(2)
    df["advance_decline_ratio"] = (
        df["advancers"] / df["decliners"].replace(0, np.nan)
    ).round(3)
    df["pct_new_highs"] = (df["new_highs"] / df["indicator_rows_200"].replace(0, np.nan) * 100).round(2)
    df["pct_new_lows"] = (df["new_lows"] / df["indicator_rows_200"].replace(0, np.nan) * 100).round(2)
    df["spx_return_20d_pct"] = (df["spx_close"] / df["spx_close"].shift(20) - 1).mul(100).round(2)

    df["breadth_50d_change_5d"] = df["pct_above_50d"].diff(5).round(2)
    df["breadth_50d_change_20d"] = df["pct_above_50d"].diff(20).round(2)
    df["breadth_200d_change_5d"] = df["pct_above_200d"].diff(5).round(2)
    df["breadth_200d_change_20d"] = df["pct_above_200d"].diff(20).round(2)
    df["price_breadth_divergence"] = (
        df["spx_return_20d_pct"] - df["breadth_50d_change_20d"]
    ).round(2)
    df["negative_divergence"] = (
        (df["spx_return_20d_pct"] > 0) & (df["breadth_50d_change_20d"] < 0)
    )
    return df


def write(engine, df: pd.DataFrame) -> None:
    cols = [c for c in df.columns if c != "updated_at"]
    rows = []
    for rec in df[cols].to_dict(orient="records"):
        clean = {}
        for k, v in rec.items():
            if v is None:
                clean[k] = None
            elif isinstance(v, float) and pd.isna(v):
                clean[k] = None
            elif not isinstance(v, (str, bool)) and pd.isna(v):
                clean[k] = None
            else:
                clean[k] = v
        rows.append(clean)
    sql = text(
        f"INSERT INTO {TABLE} ({', '.join(cols)}) VALUES "
        f"({', '.join(':' + c for c in cols)}) "
        f"ON CONFLICT (date) DO UPDATE SET "
        + ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "date")
        + ", updated_at = now()"
    )
    with engine.begin() as conn:
        for i in range(0, len(rows), 500):
            conn.execute(sql, rows[i:i + 500])


def coverage_summary(df: pd.DataFrame) -> dict:
    return {
        "start": str(df["date"].min()),
        "end": str(df["date"].max()),
        "trading_dates": int(len(df)),
        "constituents_min": int(df["constituents"].min()),
        "constituents_max": int(df["constituents"].max()),
        "coverage_avg_pct": round(float(df["coverage_pct"].mean()), 2),
        "coverage_min_pct": round(float(df["coverage_pct"].min()), 2),
        "dates_coverage_below_90_pct": int((df["coverage_pct"] < 90).sum()),
        "missing_observations_total": int((df["constituents"] - df["price_rows"]).sum()),
        "missing_first_date": int(df.iloc[0]["constituents"] - df.iloc[0]["price_rows"]),
        "missing_last_date": int(df.iloc[-1]["constituents"] - df.iloc[-1]["price_rows"]),
        "first_pct_above_50d": str(df.loc[df["pct_above_50d"].notna(), "date"].min()),
        "first_pct_above_200d": str(df.loc[df["pct_above_200d"].notna(), "date"].min()),
        "first_pct_positive_20d": str(df.loc[df["pct_positive_20d"].notna(), "date"].min()),
    }


def write_docs(df: pd.DataFrame) -> None:
    summary = coverage_summary(df)
    DOC.parent.mkdir(parents=True, exist_ok=True)
    COVERAGE_JSON.parent.mkdir(parents=True, exist_ok=True)
    COVERAGE_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    lines = [
        "# S&P 500 Point-in-Time Breadth",
        "",
        "Standalone Phase 2 research dataset. It does not modify or feed the production regime model.",
        "",
        "## Sources",
        "",
        "- Membership: `public.sp500_membership_history` (Wikipedia historical changes + current list).",
        "- Prices: `public.us_equities` (split-adjusted close/volume).",
        "- Indicators: `public.us_equities_indicators` (MA, RSI, 52-week high/low, returns).",
        "",
        "## Coverage",
        "",
        f"- Window: **{summary['start']} → {summary['end']}**",
        f"- Trading dates: **{summary['trading_dates']}**",
        f"- Constituents per date: **{summary['constituents_min']}–{summary['constituents_max']}**",
        f"- Average price coverage: **{summary['coverage_avg_pct']}%**",
        f"- Minimum price coverage: **{summary['coverage_min_pct']}%**",
        f"- Dates below 90% coverage: **{summary['dates_coverage_below_90_pct']}**",
        f"- Missing constituent observations: **{summary['missing_observations_total']}**",
        f"- Missing at first date ({summary['start']}): **{summary['missing_first_date']}**; at last date ({summary['end']}): **{summary['missing_last_date']}**",
        f"- First valid 50D breadth: **{summary['first_pct_above_50d']}**",
        f"- First valid 200D breadth: **{summary['first_pct_above_200d']}**",
        f"- First valid 20D positive breadth: **{summary['first_pct_positive_20d']}**",
        "",
        "## Limitations",
        "",
        "- Membership history is sourced from Wikipedia's historical changes table and current constituents. The table is not an official S&P DJI point-in-time feed.",
        "- Removed/delisted names with no price history inherited from the local database are counted as active constituents but excluded from each breadth denominator; see coverage columns.",
        "- Tickers with a trailing Wikipedia footnote marker (for example `ALLE |`) are normalized by stripping the marker before the price join.",
        "- Historical membership before the first reliable change record may contain unknown start dates (`1900-01-01`).",
        "- Breadth is computed on split-adjusted close and stored indicator values; no separate survivorship-bias-free price database exists.",
        "- MA50/MA200/52-week-high-low windows require warm-up history. Early 2025 rows can have `indicator_rows_50 = 0` and NULL breadth ratios; these are missing observations, not zero breadth.",
        "- Broad-market breadth (all tracked equities) is intentionally separate and not part of this table.",
        "",
        "## Metric Definitions",
        "",
        "- `pct_above_50d` / `pct_above_200d`: valid constituents with close ≥ MA50/MA200.",
        "- `pct_positive_20d`: valid constituents with positive 20-day return.",
        "- `advance_decline_ratio`: advancers / decliners using same-day pct_chg.",
        "- `new_highs` / `new_lows`: close ≥ 52-week high / ≤ 52-week low.",
        "- `breadth_50d_change_5d/20d`: change in `pct_above_50d` over 5/20 trading rows.",
        "- `price_breadth_divergence`: `spx_return_20d_pct` − `breadth_50d_change_20d`.",
        "- `negative_divergence`: SPX 20-day return positive while 50D breadth change is negative.",
    ]
    DOC.write_text("\n".join(lines), encoding="utf-8")
