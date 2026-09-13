from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.finra_short_analytics import refresh_daily_short_volume_features, setup_short_analytics_schema
from db_builder.finra_short_interest import (
    discover_short_interest_settlement_dates,
    fetch_reporting_calendar,
    publication_metadata,
    refresh_short_interest_features,
    run_finra_short_interest_fetch,
)
from db_builder.finra_short_volume import run_finra_fetch
from db_builder.flow_sources import market_session_dates_between
from db_builder.short_pipeline import refresh_short_analytics


NEW_YORK = ZoneInfo("America/New_York")


def expected_daily_sessions(now_et: datetime) -> list[date]:
    start = now_et.date() - timedelta(days=20)
    sessions = market_session_dates_between(start, now_et.date())
    if now_et.time() < time(18, 15):
        sessions = [item for item in sessions if item < now_et.date()]
    return sessions[-10:]


def published_si_candidates(now_et: datetime) -> list[date]:
    candidates = discover_short_interest_settlement_dates(now_et.date() - timedelta(days=100), now_et.date())
    try:
        official = fetch_reporting_calendar()
    except Exception:
        official = {}
    return [item for item in candidates if publication_metadata(item, official)[0] <= now_et.date()]


def main() -> None:
    now_et = datetime.now(NEW_YORK)
    engine = local_engine(use_insertmanyvalues=True)
    setup_short_analytics_schema(engine)
    print(f"[FINRA INCREMENTAL] now_et={now_et.isoformat()}")

    daily_dates = expected_daily_sessions(now_et)
    daily = run_finra_fetch(engine, dates=daily_dates, skip_existing=True, progress=print)
    si_dates = published_si_candidates(now_et)
    short_interest = run_finra_short_interest_fetch(
        engine,
        settlement_dates=si_dates,
        skip_existing=True,
        progress=print,
    )

    daily_changed = daily["upserted"] > 0
    si_changed = short_interest["upserted"] > 0
    if not daily_changed and not si_changed:
        print("[FINRA INCREMENTAL] no newly available files; analytics unchanged")
        return
    if daily_changed:
        analytics_start = min(daily["successful_dates"])
        refresh_daily_short_volume_features(engine, output_start_date=analytics_start)
    else:
        analytics_start = max(daily_dates)
    if si_changed:
        refresh_short_interest_features(engine)
    result = refresh_short_analytics(
        engine,
        start_date=analytics_start,
        end_date=max(daily_dates),
        batch_size=200,
        build_intervals=si_changed,
    )
    print(f"[FINRA INCREMENTAL] complete {result}")


if __name__ == "__main__":
    main()
