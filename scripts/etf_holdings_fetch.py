from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.etf_holdings import setup_etf_holdings_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare ETF holdings schema. Full ingestion is adapter-by-adapter.")
    parser.add_argument("--setup-table", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.setup_table:
        setup_etf_holdings_schema(local_engine(use_insertmanyvalues=True))
        print("[SETUP] public.etf_holdings ready")
    else:
        print("ETF issuer holdings ingestion is deferred. Run scripts/flow_source_check.py for availability.")


if __name__ == "__main__":
    main()

