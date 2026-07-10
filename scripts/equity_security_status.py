from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401
from db_builder.config import local_engine
from db_builder.equity_security_status import refresh_equity_security_status
from db_builder.trading_calendar import latest_completed_nyse_session_date


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh local equity security lifecycle/status flags.")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    status = refresh_equity_security_status(
        local_engine(),
        latest_completed_nyse_session_date(),
    )
    print(f"[STATUS REFRESH] tickers={len(status):,}")
    if args.summary:
        print(
            status.groupby(
                ["security_type", "lifecycle_status", "coverage_eligible"],
                dropna=False,
            ).size().rename("count").reset_index().to_string(index=False)
        )


if __name__ == "__main__":
    main()
