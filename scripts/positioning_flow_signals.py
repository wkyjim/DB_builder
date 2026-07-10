from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.positioning_flow_signals import (
    fetch_positioning_flow_dashboard,
    run_positioning_flow_signal_update,
    setup_flow_tables,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build unified positioning/flow signals.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--upsert-local", action="store_true")
    parser.add_argument("--setup-tables", action="store_true")
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    if args.setup_tables:
        setup_flow_tables(engine)
        print("[SETUP] flow tables are ready")
    if args.summary:
        rows = fetch_positioning_flow_dashboard(engine)
        if not rows:
            print("No positioning/flow signals found.")
        else:
            import pandas as pd

            print(pd.DataFrame(rows).to_string(index=False))
        return
    result = run_positioning_flow_signal_update(engine, dry_run=args.dry_run or not args.upsert_local)
    if args.dry_run or not args.upsert_local:
        print(f"[DRY RUN] signals={result['rows']:,} no database writes")
    else:
        print(f"[UPSERT LOCAL] signals={result['upserted']:,}")


if __name__ == "__main__":
    main()

