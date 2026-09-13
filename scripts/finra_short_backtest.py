from __future__ import annotations

import json

import _bootstrap  # noqa: F401

import pandas as pd

from db_builder.config import local_engine
from db_builder.short_backtest import conditional_si_change_table, evaluate_interval_predictiveness


def main() -> None:
    engine = local_engine()
    intervals = pd.read_sql(
        "SELECT * FROM public.finra_short_interest_intervals ORDER BY end_publication_date, ticker",
        engine,
    )
    result = evaluate_interval_predictiveness(intervals)
    result["conditional_si_change"] = conditional_si_change_table(intervals)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
