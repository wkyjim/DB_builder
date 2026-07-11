from __future__ import annotations

import argparse
from datetime import datetime

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.etf_flows import ETF_FLOW_UNIVERSE, fetch_latest_etf_flow_rows, run_etf_flow_fetch, setup_etf_flow_schema
from db_builder.positioning_flow_signals import run_positioning_flow_signal_update, setup_flow_tables


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch free ETF NAV/AUM/shares data and estimate ETF net fund flows.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--upsert-local", action="store_true")
    parser.add_argument("--setup-tables", action="store_true")
    parser.add_argument("--tickers", help="Comma-separated ETF tickers. Defaults to the curated ETF flow universe.")
    parser.add_argument("--start-date", help="Backfill issuer history from YYYY-MM-DD where issuer history is available.")
    parser.add_argument("--refresh-signals", action="store_true", help="Refresh public.positioning_flow_signals after upsert.")
    parser.add_argument("--summary", action="store_true", help="Print latest ETF daily data rows from positioning_flow_signals.")
    return parser.parse_args()


def _parse_tickers(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    if args.setup_tables:
        setup_flow_tables(engine)
        setup_etf_flow_schema(engine)
        print("[SETUP] ETF flow tables are ready")
    if args.summary:
        import pandas as pd

        rows = pd.DataFrame(fetch_latest_etf_flow_rows(engine))
        if not rows.empty:
            rows = rows[
                [
                    "snapshot_date",
                    "etf_ticker",
                    "category",
                    "source",
                    "net_fund_flow_1d",
                    "net_fund_flow_5d",
                    "flow_method",
                ]
            ].head(25)
        print(rows.to_string(index=False) if not rows.empty else "No ETF daily data signals found.")
        return

    tickers = _parse_tickers(args.tickers)
    start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date() if args.start_date else None
    result = run_etf_flow_fetch(
        engine,
        tickers=tickers,
        dry_run=args.dry_run or not args.upsert_local,
        start_date=start_date,
    )
    mode = "DRY RUN" if args.dry_run or not args.upsert_local else "UPSERT LOCAL"
    print(f"[{mode}] ETF snapshots={result['rows']:,} upserted={result['upserted']:,} recomputed={result['recomputed']:,}")
    if args.dry_run or not args.upsert_local:
        print("Sample:")
        for row in result["sample"]:
            print(
                f"- {row['etf_ticker']} {row.get('issuer')} source={row.get('source')} "
                f"date={row.get('date')} nav={row.get('nav')} assets={row.get('aum')} shares={row.get('shares_outstanding')}"
            )
        print(f"Default universe: {', '.join(ETF_FLOW_UNIVERSE)}")
    elif args.refresh_signals:
        signal_result = run_positioning_flow_signal_update(engine, dry_run=False)
        print(f"[SIGNALS] positioning_flow_signals upserted={signal_result['upserted']:,}")


if __name__ == "__main__":
    main()
