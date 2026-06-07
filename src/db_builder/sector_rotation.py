"""Sector rotation ranking from sector regime signals."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from db_builder.sector_intelligence import _as_list, _clamp, _float


SECTOR_ROTATION_TABLE = "public.sector_rotation_signals"


def setup_sector_rotation_schema(engine) -> None:
    sql = f"""
    CREATE TABLE IF NOT EXISTS {SECTOR_ROTATION_TABLE} (
        signal_id uuid PRIMARY KEY,
        run_time timestamptz,
        window_hours integer,
        sector_name text,
        related_etfs text[],
        rotation_rank integer,
        rotation_score numeric,
        relative_strength_rank integer,
        momentum_rank integer,
        news_rank integer,
        risk_rank integer,
        allocation_bias text,
        recommended_action text,
        drivers jsonb,
        created_at timestamptz DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_sector_rotation_run_sector
    ON {SECTOR_ROTATION_TABLE} (run_time, window_hours, sector_name);
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


def fetch_latest_sector_regimes(engine, *, window_hours: int) -> pd.DataFrame:
    sql = text("""
        WITH latest_run AS (
            SELECT MAX(run_time) AS run_time
            FROM public.sector_regimes
            WHERE window_hours = :window_hours
        )
        SELECT r.*
        FROM public.sector_regimes r
        JOIN latest_run lr ON lr.run_time = r.run_time
        WHERE r.window_hours = :window_hours
    """)
    try:
        return pd.read_sql(sql, engine, params={"window_hours": window_hours})
    except Exception:
        return pd.DataFrame()


def rotation_score(signal: dict) -> float:
    return round(
        (0.30 * _float(signal.get("relative_strength_score"), 50))
        + (0.25 * _float(signal.get("momentum_score"), 50))
        + (0.20 * _float(signal.get("trend_score"), 50))
        + (0.15 * _float(signal.get("news_score"), 50))
        - (0.10 * _float(signal.get("risk_score"), 20)),
        4,
    )


def classify_allocation(signal: dict, rank: int, total: int) -> tuple[str, str]:
    regime = str(signal.get("sector_regime") or "")
    phase = str(signal.get("cycle_phase") or "")
    risk = _float(signal.get("risk_score"))
    score = _float(signal.get("rotation_score"))
    top_quartile = rank <= max(1, int(total * 0.25))
    if regime == "strong_bear" or risk >= 80:
        return "avoid", "avoid"
    if regime == "bear" or _float(signal.get("relative_strength_score"), 50) < 40:
        return "underweight", "trim"
    if top_quartile and regime in {"bull", "strong_bull"} and risk < 70:
        return "overweight", "add"
    if score > 50 and phase in {"early_bull", "mid_bull", "emerging_bull"}:
        return "modest_overweight", "hold"
    if phase in {"capitulation", "correction"}:
        return "neutral", "watch_for_reversal"
    return "neutral", "hold"


def _rank_map(rows: list[dict], key: str, *, reverse: bool = True) -> dict[str, int]:
    ordered = sorted(rows, key=lambda row: _float(row.get(key), 50), reverse=reverse)
    return {str(row.get("sector_name")): index for index, row in enumerate(ordered, start=1)}


def build_sector_rotation_signals(
    sector_regimes_df: pd.DataFrame,
    *,
    window_hours: int,
    run_time: datetime | None = None,
) -> list[dict]:
    selected_run_time = run_time or datetime.now(timezone.utc)
    rows = sector_regimes_df.to_dict(orient="records") if not sector_regimes_df.empty else []
    if not rows:
        return []
    for row in rows:
        row["rotation_score"] = rotation_score(row)
    relative_ranks = _rank_map(rows, "relative_strength_score")
    momentum_ranks = _rank_map(rows, "momentum_score")
    news_ranks = _rank_map(rows, "news_score")
    risk_ranks = _rank_map(rows, "risk_score", reverse=False)
    ranked = sorted(rows, key=lambda row: _float(row.get("rotation_score")), reverse=True)
    signals = []
    total = len(ranked)
    for rank, row in enumerate(ranked, start=1):
        bias, action = classify_allocation(row, rank, total)
        sector = str(row.get("sector_name"))
        key = f"{selected_run_time.isoformat()}|{window_hours}|{sector}"
        signals.append(
            {
                "signal_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"db_builder.sector_rotation:{key}")),
                "run_time": selected_run_time,
                "window_hours": window_hours,
                "sector_name": sector,
                "related_etfs": _as_list(row.get("related_etfs")),
                "rotation_rank": rank,
                "rotation_score": round(_float(row.get("rotation_score")), 4),
                "relative_strength_rank": relative_ranks[sector],
                "momentum_rank": momentum_ranks[sector],
                "news_rank": news_ranks[sector],
                "risk_rank": risk_ranks[sector],
                "allocation_bias": bias,
                "recommended_action": action,
                "drivers": {
                    "sector_regime": row.get("sector_regime"),
                    "cycle_phase": row.get("cycle_phase"),
                    "relative_strength_score": row.get("relative_strength_score"),
                    "momentum_score": row.get("momentum_score"),
                    "trend_score": row.get("trend_score"),
                    "news_score": row.get("news_score"),
                    "risk_score": row.get("risk_score"),
                },
            }
        )
    return signals


def upsert_sector_rotation_signals(engine, signals: list[dict]) -> None:
    if not signals:
        return
    sql = text(f"""
        INSERT INTO {SECTOR_ROTATION_TABLE} (
            signal_id, run_time, window_hours, sector_name, related_etfs,
            rotation_rank, rotation_score, relative_strength_rank, momentum_rank,
            news_rank, risk_rank, allocation_bias, recommended_action, drivers, created_at
        )
        VALUES (
            :signal_id, :run_time, :window_hours, :sector_name, CAST(:related_etfs AS text[]),
            :rotation_rank, :rotation_score, :relative_strength_rank, :momentum_rank,
            :news_rank, :risk_rank, :allocation_bias, :recommended_action, CAST(:drivers AS jsonb), now()
        )
        ON CONFLICT (signal_id)
        DO UPDATE SET
            rotation_rank = EXCLUDED.rotation_rank,
            rotation_score = EXCLUDED.rotation_score,
            relative_strength_rank = EXCLUDED.relative_strength_rank,
            momentum_rank = EXCLUDED.momentum_rank,
            news_rank = EXCLUDED.news_rank,
            risk_rank = EXCLUDED.risk_rank,
            allocation_bias = EXCLUDED.allocation_bias,
            recommended_action = EXCLUDED.recommended_action,
            drivers = EXCLUDED.drivers,
            created_at = now();
    """)
    rows = []
    for signal in signals:
        row = signal.copy()
        row["drivers"] = json.dumps(row.get("drivers") or {}, default=str)
        rows.append(row)
    with engine.begin() as conn:
        conn.execute(sql, rows)


def generate_sector_rotation(engine, *, window_hours: int, dry_run: bool = True) -> list[dict]:
    run_time = datetime.now(timezone.utc)
    regimes_df = fetch_latest_sector_regimes(engine, window_hours=window_hours)
    if dry_run and regimes_df.empty:
        from db_builder.sector_regime import build_sector_regimes, fetch_sector_market_data, fetch_sector_news_inputs

        regimes = build_sector_regimes(
            fetch_sector_market_data(engine),
            fetch_sector_news_inputs(engine, window_hours=window_hours),
            window_hours=window_hours,
            run_time=run_time,
        )
        regimes_df = pd.DataFrame(regimes)
    signals = build_sector_rotation_signals(regimes_df, window_hours=window_hours, run_time=run_time)
    if not dry_run:
        setup_sector_rotation_schema(engine)
        upsert_sector_rotation_signals(engine, signals)
    return signals
