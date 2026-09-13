"""Read-only verification for the latest short-positioning snapshot."""

from __future__ import annotations

import _bootstrap  # noqa: F401
import pandas as pd
from sqlalchemy import text

from db_builder.config import local_engine, neon_engine
from db_builder.short_snapshot import LATEST_TABLE


SUMMARY_SQL = text(
    f"""
    SELECT count(*) AS rows,
           count(DISTINCT ticker) AS tickers,
           max(analytics_date) AS latest_analytics_date,
           max(latest_si_settlement_date) AS latest_si_settlement_date,
           max(latest_si_publication_date) AS latest_si_publication_date,
           count(*) FILTER (WHERE latest_si_publication_date > analytics_date) AS publication_leaks,
           count(*) FILTER (WHERE short_interest = 0) AS zero_short_interest_rows,
           count(*) FILTER (WHERE security_type = 'common_stock') AS common_stocks,
           count(*) FILTER (WHERE security_type = 'etf') AS etfs,
           min(snapshot_updated_at) AS earliest_snapshot_update,
           max(snapshot_updated_at) AS latest_snapshot_update
    FROM {LATEST_TABLE}
    """
)


def verify(engine) -> dict:
    summary = pd.read_sql(SUMMARY_SQL, engine).iloc[0].to_dict()
    summary["short_analytics_tables"] = pd.read_sql(
        text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name LIKE 'us_equities_short_analytics%'
            ORDER BY table_name
            """
        ),
        engine,
    )["table_name"].tolist()
    return summary


if __name__ == "__main__":
    print("Local:", verify(local_engine()))
    try:
        print("Neon:", verify(neon_engine()))
    except Exception as exc:
        print("Neon: unavailable", type(exc).__name__)
