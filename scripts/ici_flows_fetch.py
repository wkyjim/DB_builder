from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.ici_flows import setup_ici_flows_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare ICI flows schema. Full ingestion is deferred.")
    parser.add_argument("--setup-table", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.setup_table:
        setup_ici_flows_schema(local_engine(use_insertmanyvalues=True))
        print("[SETUP] public.ici_flows ready")
    else:
        print("ICI production ingestion is deferred. Run scripts/flow_source_check.py for availability.")


if __name__ == "__main__":
    main()

