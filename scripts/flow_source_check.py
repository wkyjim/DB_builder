from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.flow_sources import run_source_probe, setup_flow_source_health_schema, source_probe_dataframe
from db_builder.positioning_flow_signals import setup_flow_tables
from db_builder.sec_13f_holdings import setup_sec_13f_schema
from db_builder.ici_flows import setup_ici_flows_schema
from db_builder.etf_holdings import setup_etf_holdings_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check positioning/flow source availability.")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--setup-tables", action="store_true", help="Create flow schemas without ingesting data.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.setup_tables:
        engine = local_engine(use_insertmanyvalues=True)
        setup_flow_source_health_schema(engine)
        setup_flow_tables(engine)
        setup_sec_13f_schema(engine)
        setup_ici_flows_schema(engine)
        setup_etf_holdings_schema(engine)
        print("[SETUP] flow tables are ready")
    results = run_source_probe(timeout=args.timeout)
    print(source_probe_dataframe(results).to_string(index=False))


if __name__ == "__main__":
    main()

