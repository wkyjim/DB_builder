"""Optional local Ollama analyst overlay for deterministic investment reports."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from db_builder.news_classifier import (
    ClassificationError,
    _ollama_chat,
    extract_json_object,
    ollama_deep_model,
    ollama_model,
    ollama_url,
)


LLM_ANALYST_TABLE = "public.llm_analyst_outputs"
QWEN_DEFAULTS = {
    "market_view": "unclear",
    "positioning": "selective_risk",
    "risk_level": "moderate",
    "confidence": 0.0,
    "summary": "Local analyst overlay unavailable.",
    "top_risk": "No model-generated risk summary is available.",
    "top_opportunity": "No model-generated opportunity summary is available.",
    "preferred_sectors": [],
    "avoid_sectors": [],
}


def setup_llm_analyst_schema(engine) -> None:
    sql = f"""
    CREATE TABLE IF NOT EXISTS {LLM_ANALYST_TABLE} (
        output_id uuid PRIMARY KEY,
        run_time timestamptz,
        window_hours integer,
        analysis_type text,
        model_name text,
        model_role text,
        input_hash text,
        source_snapshot jsonb,
        market_regime_view text,
        positioning_bias text,
        risk_level text,
        confidence numeric,
        top_risks jsonb,
        top_opportunities jsonb,
        sector_views jsonb,
        theme_views jsonb,
        watchlist_views jsonb,
        summary_markdown text,
        raw_response jsonb,
        created_at timestamptz DEFAULT now()
    );
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


def _as_records(df: pd.DataFrame, limit: int | None = None) -> list[dict]:
    if df.empty:
        return []
    selected = df.head(limit) if limit else df
    return json.loads(json.dumps(selected.to_dict(orient="records"), default=str))


def _safe_read_sql(engine, sql: str, params: dict | None = None) -> pd.DataFrame:
    try:
        return pd.read_sql(text(sql), engine, params=params or {})
    except Exception:
        return pd.DataFrame()


def collect_overlay_snapshot(engine, *, window_hours: int) -> dict:
    snapshot = {
        "market_regime_v2": _as_records(
            _safe_read_sql(
                engine,
                """
                SELECT *
                FROM public.market_regime_v2
                WHERE window_hours = :window_hours
                ORDER BY run_time DESC
                LIMIT 1
                """,
                {"window_hours": window_hours},
            )
        ),
        "sector_regimes": _as_records(
            _safe_read_sql(
                engine,
                """
                WITH latest_run AS (
                    SELECT MAX(run_time) AS run_time FROM public.sector_regimes WHERE window_hours = :window_hours
                )
                SELECT sector_name, related_etfs, sector_regime, cycle_phase, confidence,
                       final_score, trend_score, momentum_score, relative_strength_score, risk_score
                FROM public.sector_regimes s
                JOIN latest_run r ON r.run_time = s.run_time
                WHERE s.window_hours = :window_hours
                ORDER BY final_score DESC
                """,
                {"window_hours": window_hours},
            )
            ,
            limit=5,
        ),
        "sector_rotation": _as_records(
            _safe_read_sql(
                engine,
                """
                WITH latest_run AS (
                    SELECT MAX(run_time) AS run_time FROM public.sector_rotation_signals WHERE window_hours = :window_hours
                )
                SELECT sector_name, related_etfs, rotation_rank, rotation_score,
                       allocation_bias, recommended_action
                FROM public.sector_rotation_signals s
                JOIN latest_run r ON r.run_time = s.run_time
                WHERE s.window_hours = :window_hours
                ORDER BY rotation_rank
                """,
                {"window_hours": window_hours},
            )
            ,
            limit=5,
        ),
        "secular_themes": _as_records(
            _safe_read_sql(
                engine,
                """
                WITH latest_run AS (
                    SELECT MAX(run_time) AS run_time FROM public.secular_theme_signals WHERE window_hours = :window_hours
                )
                SELECT theme_name, parent_theme, related_etfs, mention_count_7d, mention_count_30d,
                       secular_score, tactical_score, theme_phase, confidence, top_subthemes
                FROM public.secular_theme_signals s
                JOIN latest_run r ON r.run_time = s.run_time
                WHERE s.window_hours = :window_hours
                ORDER BY secular_score DESC
                LIMIT 5
                """,
                {"window_hours": window_hours},
            )
        ),
        "news_signals": _as_records(
            _safe_read_sql(
                engine,
                """
                WITH latest_run AS (
                    SELECT MAX(run_time) AS run_time FROM public.news_signals WHERE window_hours = :window_hours
                )
                SELECT dimension_type, dimension_value, article_count, weighted_sentiment_score,
                       opportunity_score, risk_score
                FROM public.news_signals s
                JOIN latest_run r ON r.run_time = s.run_time
                WHERE s.window_hours = :window_hours
                ORDER BY risk_score DESC, article_count DESC
                LIMIT 5
                """,
                {"window_hours": window_hours},
            )
        ),
        "opportunity_signals": _as_records(
            _safe_read_sql(
                engine,
                """
                WITH latest_run AS (
                    SELECT MAX(run_time) AS run_time FROM public.opportunity_signals WHERE window_hours = :window_hours
                )
                SELECT *
                FROM public.opportunity_signals o
                JOIN latest_run r ON r.run_time = o.run_time
                WHERE o.window_hours = :window_hours
                ORDER BY opportunity_score DESC
                LIMIT 5
                """,
                {"window_hours": window_hours},
            )
        ),
        "sector_rotation_bottom": _as_records(
            _safe_read_sql(
                engine,
                """
                WITH latest_run AS (
                    SELECT MAX(run_time) AS run_time FROM public.sector_rotation_signals WHERE window_hours = :window_hours
                )
                SELECT sector_name, related_etfs, rotation_rank, rotation_score,
                       allocation_bias, recommended_action
                FROM public.sector_rotation_signals s
                JOIN latest_run r ON r.run_time = s.run_time
                WHERE s.window_hours = :window_hours
                ORDER BY rotation_rank DESC
                LIMIT 3
                """,
                {"window_hours": window_hours},
            )
        ),
    }
    return snapshot


def snapshot_hash(snapshot: dict) -> str:
    payload = json.dumps(snapshot, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def qwen_input_snapshot(snapshot: dict, critical_pm_view: dict | None = None) -> dict:
    market_rows = snapshot.get("market_regime_v2") or []
    market_summary = {}
    if market_rows:
        row = market_rows[0]
        market_summary = {
            "market_regime": row.get("market_regime"),
            "market_phase": row.get("market_phase"),
            "confidence": row.get("confidence"),
            "market_strength": row.get("market_strength"),
            "trend_state": row.get("trend_state"),
            "momentum_state": row.get("momentum_state"),
            "volatility_state": row.get("volatility_state"),
            "breadth_state": row.get("breadth_state"),
            "risk_appetite_state": row.get("risk_appetite_state"),
        }
    return {
        "market_regime_v2": market_summary,
        "top_sector_rotation": (snapshot.get("sector_rotation") or [])[:3],
        "bottom_sector_rotation": (snapshot.get("sector_rotation_bottom") or [])[:3],
        "top_secular_themes": (snapshot.get("secular_themes") or [])[:5],
        "top_risk_signals": (snapshot.get("news_signals") or [])[:5],
        "critical_pm_view": {
            "market_view": (critical_pm_view or {}).get("market_view"),
            "positioning_bias": (critical_pm_view or {}).get("positioning_bias"),
            "final_house_view": (critical_pm_view or {}).get("final_house_view"),
            "contradiction_flags": (critical_pm_view or {}).get("contradiction_flags", [])[:3],
        },
    }


def qwen_prompt(snapshot: dict, critical_pm_view: dict | None = None) -> list[dict]:
    system = (
        "Return ONE JSON object only. No markdown. No prose. No code fence. "
        "Use only provided data. If unsure, use unclear and empty arrays. "
        "Return exactly the requested keys and do not copy the input data. "
        "The deterministic Critical PM View is primary; summarize it, do not override it."
    )
    example = {
        "market_view": "neutral",
        "positioning": "selective_risk",
        "risk_level": "moderate",
        "confidence": 0.6,
        "summary": "Market structure is mixed with selective opportunities.",
        "top_risk": "Weak breadth and elevated volatility remain the main risk.",
        "top_opportunity": "Cybersecurity shows relative leadership.",
        "preferred_sectors": ["Cybersecurity", "Healthcare"],
        "avoid_sectors": ["Energy", "Utilities"],
    }
    user = (
        "Write a compact analyst overlay from deterministic investment signals. Do not invent facts.\n"
        "If you disagree with the Critical PM View, include 'LLM disagreement noted' in the summary, but keep the deterministic view primary.\n"
        "Return exactly these keys and no other keys:\n"
        "market_view, positioning, risk_level, confidence, summary, top_risk, top_opportunity, preferred_sectors, avoid_sectors.\n"
        "Allowed market_view: risk_on, neutral, risk_off, correction_in_bull, unclear.\n"
        "Allowed positioning: aggressive, selective_risk, defensive, cash_heavy.\n"
        "Allowed risk_level: low, moderate, high, stressed.\n"
        "preferred_sectors and avoid_sectors must be arrays of strings.\n"
        "Valid example JSON:\n"
        f"{json.dumps(example, ensure_ascii=True)}\n"
        "Input data, for evidence only. Do not copy these keys into the output:\n"
        f"{json.dumps(qwen_input_snapshot(snapshot, critical_pm_view), ensure_ascii=True)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def fallback_qwen_overlay(reason: str, snapshot: dict | None = None) -> dict:
    return {
        "market_view": "unclear",
        "positioning": "selective_risk",
        "risk_level": "moderate",
        "confidence": 0.0,
        "summary": "Local analyst overlay unavailable.",
        "top_risk": f"LLM overlay unavailable: {reason}",
        "top_opportunity": "No model-generated opportunity summary is available.",
        "preferred_sectors": [],
        "avoid_sectors": [],
        "summary_markdown": f"## LLM Analyst Overlay\n\n- Fallback: {reason}\n",
        "raw_response": {},
        "source_snapshot": snapshot or {},
    }


def validate_qwen_overlay(payload: dict) -> dict:
    payload = {**QWEN_DEFAULTS, **(payload or {})}
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
        raise ClassificationError("confidence must be numeric from 0 to 1")
    normalized = payload.copy()
    for key in ["preferred_sectors", "avoid_sectors"]:
        if not isinstance(normalized.get(key), list):
            normalized[key] = []
    for key in ["market_view", "positioning", "risk_level", "summary", "top_risk", "top_opportunity"]:
        normalized[key] = str(normalized.get(key) or QWEN_DEFAULTS[key]).strip()
    normalized.setdefault("summary_markdown", render_qwen_overlay_markdown(normalized))
    return normalized


def generate_qwen_overlay(
    engine,
    *,
    window_hours: int,
    timeout: int = 120,
    model: str | None = None,
    chat_fn=_ollama_chat,
    critical_pm_view: dict | None = None,
) -> dict:
    snapshot = collect_overlay_snapshot(engine, window_hours=window_hours)
    selected_model = model or ollama_model()
    try:
        body = chat_fn(
            qwen_prompt(snapshot, critical_pm_view),
            url=ollama_url(),
            model=selected_model,
            timeout=timeout,
            options={"temperature": 0, "num_predict": 700},
        )
        payload = validate_qwen_overlay(extract_json_object(body.get("message", {}).get("content", "")))
        payload["raw_response"] = body
        payload["source_snapshot"] = snapshot
        payload["model_name"] = selected_model
        return payload
    except Exception as exc:
        fallback = fallback_qwen_overlay(str(exc), snapshot)
        fallback["model_name"] = selected_model
        return fallback


def render_qwen_overlay_markdown(overlay: dict) -> str:
    lines = [
        "## LLM Analyst Overlay",
        "",
        f"- Market view: **{overlay.get('market_view', 'unclear')}**",
        f"- Positioning: `{overlay.get('positioning', 'selective_risk')}`",
        f"- Risk level: `{overlay.get('risk_level', 'moderate')}`",
        f"- Confidence: `{overlay.get('confidence', 0)}`",
        f"- Summary: {overlay.get('summary', '')}",
        f"- Top risk: {overlay.get('top_risk', '')}",
        f"- Top opportunity: {overlay.get('top_opportunity', '')}",
    ]
    preferred = ", ".join(str(item) for item in overlay.get("preferred_sectors", []) if item) or "n/a"
    avoid = ", ".join(str(item) for item in overlay.get("avoid_sectors", []) if item) or "n/a"
    lines.append(f"- Preferred sectors: `{preferred}`")
    lines.append(f"- Avoid sectors: `{avoid}`")
    return "\n".join(lines).rstrip() + "\n"


def deepseek_prompt(report_markdown: str, qwen_overlay: dict | None, snapshot: dict) -> list[dict]:
    system = (
        "Return markdown only. Start exactly with: ## CIO Commentary. "
        "Write at least 3 bullet points. Do not return JSON. Deterministic database signals remain the source of truth."
    )
    user = {
        "task": "Write concise CIO Commentary.",
        "deterministic_report_summary": report_markdown[:6000],
        "qwen_overlay": qwen_overlay or {},
    }
    return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user, ensure_ascii=True)}]


def fallback_cio_commentary(reason: str) -> str:
    return (
        "## CIO Commentary\n\n"
        f"- Deep CIO commentary unavailable: {reason}\n"
        "- Deterministic market, sector, secular theme, and opportunity sections remain the source of truth.\n"
    )


def json_like_to_cio_markdown(content: str) -> str | None:
    try:
        payload = extract_json_object(content)
    except Exception:
        return None
    lines = ["## CIO Commentary", ""]
    summary = payload.get("summary") if isinstance(payload, dict) else None
    if isinstance(summary, list):
        for item in summary:
            if isinstance(item, dict):
                label = item.get("type") or "Observation"
                description = item.get("description") or item.get("reason") or ""
                lines.append(f"- **{label}**: {description}")
            else:
                lines.append(f"- {item}")
    else:
        lines.append("- Model returned structured content rather than markdown; useful fields are summarized here.")
    lines.append(f"- {payload.get('positioning_bias', 'Use deterministic positioning sections as source of truth.') if isinstance(payload, dict) else 'Use deterministic sections.'}")
    lines.append("- Breadth, volatility, sector rotation, and secular/tactical divergence should be monitored in deterministic sections.")
    return "\n".join(lines).rstrip() + "\n"


def validate_cio_commentary(content: str) -> str:
    cleaned = (content or "").strip()
    if not cleaned:
        raise ClassificationError("empty model response")
    if cleaned.startswith("{"):
        converted = json_like_to_cio_markdown(cleaned)
        if converted:
            return converted
        raise ClassificationError("DeepSeek response was not markdown CIO commentary")
    if "## CIO Commentary" not in cleaned:
        raise ClassificationError("DeepSeek commentary missing ## CIO Commentary heading")
    bullet_count = sum(1 for line in cleaned.splitlines() if line.strip().startswith(("-", "*")))
    if bullet_count < 3:
        raise ClassificationError("DeepSeek commentary has fewer than 3 bullet points")
    return cleaned


def generate_deepseek_cio_commentary(
    report_markdown: str,
    *,
    engine,
    window_hours: int,
    qwen_overlay: dict | None = None,
    timeout: int = 300,
    model: str | None = None,
    chat_fn=_ollama_chat,
) -> str:
    snapshot = collect_overlay_snapshot(engine, window_hours=window_hours)
    selected_model = model or ollama_deep_model()
    try:
        body = chat_fn(
            deepseek_prompt(report_markdown, qwen_overlay, snapshot),
            url=ollama_url(),
            model=selected_model,
            timeout=timeout,
            options={"temperature": 0.2, "num_predict": 1400},
            response_format=None,
        )
        content = body.get("message", {}).get("content", "").strip()
        return validate_cio_commentary(content)
    except Exception as exc:
        return fallback_cio_commentary(str(exc))


def upsert_llm_output(engine, *, window_hours: int, analysis_type: str, model_name: str, model_role: str, payload: dict) -> None:
    snapshot = payload.get("source_snapshot") or {}
    row = {
        "output_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"db_builder.llm:{analysis_type}:{snapshot_hash(snapshot)}:{datetime.now(timezone.utc).isoformat()}")),
        "run_time": datetime.now(timezone.utc),
        "window_hours": window_hours,
        "analysis_type": analysis_type,
        "model_name": model_name,
        "model_role": model_role,
        "input_hash": snapshot_hash(snapshot),
        "source_snapshot": json.dumps(snapshot, default=str),
        "market_regime_view": payload.get("market_view") or payload.get("market_regime_view"),
        "positioning_bias": payload.get("positioning") or payload.get("positioning_bias"),
        "risk_level": payload.get("risk_level"),
        "confidence": payload.get("confidence"),
        "top_risks": json.dumps([payload.get("top_risk")] if payload.get("top_risk") else payload.get("top_risks") or [], default=str),
        "top_opportunities": json.dumps([payload.get("top_opportunity")] if payload.get("top_opportunity") else payload.get("top_opportunities") or [], default=str),
        "sector_views": json.dumps(payload.get("sector_views") or [], default=str),
        "theme_views": json.dumps(payload.get("theme_views") or [], default=str),
        "watchlist_views": json.dumps(payload.get("watchlist_views") or [], default=str),
        "summary_markdown": payload.get("summary_markdown"),
        "raw_response": json.dumps(payload.get("raw_response") or {}, default=str),
    }
    sql = text(f"""
        INSERT INTO {LLM_ANALYST_TABLE} (
            output_id, run_time, window_hours, analysis_type, model_name, model_role,
            input_hash, source_snapshot, market_regime_view, positioning_bias, risk_level,
            confidence, top_risks, top_opportunities, sector_views, theme_views,
            watchlist_views, summary_markdown, raw_response, created_at
        )
        VALUES (
            :output_id, :run_time, :window_hours, :analysis_type, :model_name, :model_role,
            :input_hash, CAST(:source_snapshot AS jsonb), :market_regime_view, :positioning_bias, :risk_level,
            :confidence, CAST(:top_risks AS jsonb), CAST(:top_opportunities AS jsonb),
            CAST(:sector_views AS jsonb), CAST(:theme_views AS jsonb), CAST(:watchlist_views AS jsonb),
            :summary_markdown, CAST(:raw_response AS jsonb), now()
        )
    """)
    setup_llm_analyst_schema(engine)
    with engine.begin() as conn:
        conn.execute(sql, row)
