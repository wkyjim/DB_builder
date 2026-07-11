"""Orchestrator for ETF flow analytics."""

from __future__ import annotations

from datetime import date
from typing import Any

from db_builder.etf_flow.aggregation import build_etf_flow_analytics
from db_builder.etf_flow.config import ETFAnalyticsConfig
from db_builder.etf_flow.repository import (
    fetch_raw_etf_flow_source,
    setup_etf_flow_analytics_schema,
    upsert_daily,
    upsert_features,
    upsert_raw_from_source,
    upsert_regime,
    upsert_segments,
    upsert_simple_table,
)


def run_etf_flow_analytics(
    engine,
    *,
    as_of_date: date | None = None,
    start_date: date | None = None,
    existing_regime_score: float | None = None,
    dry_run: bool = True,
    write_report_output: bool = False,
    config: ETFAnalyticsConfig | None = None,
) -> dict[str, Any]:
    setup_etf_flow_analytics_schema(engine)
    raw = fetch_raw_etf_flow_source(engine, start_date=start_date, as_of_date=as_of_date)
    output, tables = build_etf_flow_analytics(raw, existing_regime_score=existing_regime_score, config=config)
    writes = {
        "raw": 0,
        "daily": 0,
        "features": 0,
        "segments": 0,
        "consensus": 0,
        "rotation": 0,
        "forward": 0,
        "audits": 0,
        "regime": 0,
    }
    if not dry_run:
        writes["raw"] = upsert_raw_from_source(engine, raw)
        writes["daily"] = upsert_daily(engine, tables["daily"])
        writes["features"] = upsert_features(engine, tables["features"])
        writes["segments"] = upsert_segments(engine, tables["segments"])
        writes["consensus"] = upsert_simple_table(
            engine,
            tables["consensus"],
            "etf_flow_consensus_daily",
            ["date", "segment"],
        )
        writes["rotation"] = upsert_simple_table(
            engine,
            tables["rotation"],
            "etf_flow_rotation_daily",
            ["date", "segment_type", "segment"],
        )
        writes["forward"] = upsert_simple_table(
            engine,
            tables["forward"],
            "etf_flow_forward_signals",
            ["date", "segment_type", "segment"],
        )
        writes["audits"] = upsert_simple_table(
            engine,
            tables["audits"],
            "etf_flow_audit_flags",
            ["date", "flag_type", "segment", "description"],
        )
        writes["regime"] = upsert_regime(engine, output.flow_regime, output.as_of_date)
    return {
        "dry_run": dry_run,
        "write_report_output": write_report_output,
        "raw_rows": len(raw),
        "daily_rows": len(tables["daily"]),
        "feature_rows": len(tables["features"]),
        "segment_rows": len(tables["segments"]),
        "as_of_date": output.as_of_date.isoformat() if output.as_of_date else None,
        "writes": writes,
        "output": output.to_dict(),
    }
