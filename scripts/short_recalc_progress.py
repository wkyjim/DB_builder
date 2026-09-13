"""Read-only progress summary for long FINRA recalculation runs."""

from __future__ import annotations

import _bootstrap  # noqa: F401
import pandas as pd
from sqlalchemy import text

from db_builder.config import local_engine


SQL = text(
    """
    SELECT 'daily_features' AS stage, count(*) FILTER (WHERE calculated_at >= now() - interval '2 hours') AS recently_updated,
           max(calculated_at) AS latest_update FROM public.finra_short_volume_daily_features
    UNION ALL
    SELECT 'si_features', count(*) FILTER (WHERE calculated_at >= now() - interval '2 hours'), max(calculated_at)
    FROM public.finra_short_interest_features
    UNION ALL
    SELECT 'final_analytics', count(*) FILTER (WHERE calculated_at >= now() - interval '2 hours'), max(calculated_at)
    FROM public.us_equities_short_analytics
    """
)


if __name__ == "__main__":
    print(pd.read_sql(SQL, local_engine()).to_string(index=False))
