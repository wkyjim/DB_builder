"""Prompt builders for the multistage DeepSeek CIO pipeline."""

from __future__ import annotations

import json

from db_builder.deepseek_report_validator import allowed_evidence_ids
from db_builder.knowledge_retriever import find_knowledge_file, retrieve_knowledge


STAGE_ORDER = [
    "evidence_extraction",
    "regime_scoring",
    "cross_asset_validation",
    "news_weighting",
    "portfolio_construction",
    "final_cio_report",
]

STAGE_TITLES = {
    "evidence_extraction": "Stage 1: Evidence Extraction",
    "regime_scoring": "Stage 2: Regime Scoring",
    "cross_asset_validation": "Stage 3: Cross-Asset Validation",
    "news_weighting": "Stage 4: News Weighting and Risk Ranking",
    "portfolio_construction": "Stage 5: Portfolio Construction",
    "final_cio_report": "Stage 6: Final CIO Report and Audit",
}

FINAL_REPORT_HEADINGS = [
    "# Qwen Multi-Stage CIO Report",
    "## Executive Summary",
    "## Macro Market Dashboard",
    "## Macro Regime Classification",
    "## News Analysis",
    "## Evidence Table",
    "## Cross-Asset Confirmation",
    "## Market Narrative",
    "## Scenario Analysis",
    "## Sector Analysis",
    "## Risk Register",
    "## Contradictions",
    "## Investment Recommendations",
    "## Invalidators",
    "## Confidence Assessment",
]


def read_system_prompt() -> str:
    path = find_knowledge_file("deepseek_agent_system_prompt.md")
    if path:
        return path.read_text(encoding="utf-8", errors="ignore").replace("DeepSeek", "Qwen")
    return (
        "You are an institutional CIO analyst. Do not summarize mechanically. "
        "Infer regime, test cross-asset consistency, identify contradictions, assign probabilities, "
        "and produce portfolio implications using only supplied evidence."
    )


def _short(value, *, max_chars: int = 14000) -> str:
    text = json.dumps(value, ensure_ascii=True, indent=2, default=str) if not isinstance(value, str) else value
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]..."


def _allowed_evidence_block(snapshot: dict, *, limit: int = 140) -> str:
    ids = sorted(allowed_evidence_ids(snapshot))
    shown = ids[:limit]
    suffix = "" if len(ids) <= limit else f"\n... {len(ids) - limit} more omitted ..."
    return "\n".join(shown) + suffix


def _scenario_probability_table(stage_outputs: dict) -> str:
    probabilities = (stage_outputs.get("regime_scoring") or {}).get("scenario_probabilities") or {}
    if not probabilities:
        return "| Scenario | Probability | Source |\n|---|---:|---|"
    bull = int(probabilities.get("risk_on_expansion") or 0)
    bear = int(probabilities.get("defensive_slowdown") or 0) + int(probabilities.get("risk_off_shock") or 0)
    base = max(0, 100 - bull - bear)
    lines = ["| Scenario | Probability | Source |", "|---|---:|---|"]
    lines.append(f"| Base Case | {base} | soft_landing + growth_plus_inflation |")
    lines.append(f"| Bull Case | {bull} | risk_on_expansion |")
    lines.append(f"| Bear Case | {bear} | defensive_slowdown + risk_off_shock |")
    return "\n".join(lines)


def _top_headlines_block(snapshot: dict, *, limit: int = 10) -> str:
    rows = []
    for row in snapshot.get("news", [])[:limit]:
        rows.append(
            {
                "evidence_id": row.get("evidence_id"),
                "title": row.get("title"),
                "source": row.get("source"),
                "source_priority": row.get("source_priority"),
                "published_at": row.get("published_at"),
                "themes": row.get("themes"),
                "affected_tickers": row.get("affected_tickers"),
                "sentiment": row.get("sentiment"),
                "impact": row.get("impact"),
                "confidence": row.get("confidence"),
                "why_it_matters": row.get("reasoning"),
            }
        )
    return _short(rows, max_chars=7000)


def _macro_dashboard_block(snapshot: dict) -> str:
    rows = {
        "market_state": snapshot.get("market_state"),
        "cross_asset": snapshot.get("cross_asset", [])[:35],
        "technicals": snapshot.get("technicals", [])[:14],
        "unavailable_fields": snapshot.get("unavailable_fields") or {},
    }
    return _short(rows, max_chars=10000)


def compact_snapshot(snapshot: dict) -> dict:
    return {
        "generated_at": snapshot.get("generated_at"),
        "window_hours": snapshot.get("window_hours"),
        "market_state": snapshot.get("market_state"),
        "cross_asset": snapshot.get("cross_asset", [])[:35],
        "technicals": snapshot.get("technicals", [])[:14],
        "news": snapshot.get("news", [])[:15],
        "sector_data": snapshot.get("sector_data", [])[:14],
        "secular_themes": snapshot.get("secular_themes", [])[:12],
        "opportunity_risk": {
            "top_opportunities": snapshot.get("opportunity_risk", {}).get("top_opportunities", [])[:8],
            "top_risks": snapshot.get("opportunity_risk", {}).get("top_risks", [])[:10],
            "no_opportunity_warning": snapshot.get("opportunity_risk", {}).get("no_opportunity_warning"),
        },
        "confidence_inputs": snapshot.get("confidence_inputs", []),
        "approved_ticker_universe": snapshot.get("approved_ticker_universe", []),
        "approved_etf_label_map": snapshot.get("approved_etf_label_map", {}),
    }


def stage_schema(stage_name: str) -> str:
    schemas = {
        "evidence_extraction": {
            "stage": "evidence_extraction",
            "data_quality": {"overall": "strong|moderate|weak", "missing_data": [], "confidence_constraints": []},
            "facts": [
                {
                    "evidence_id": "TECH_001",
                    "asset_or_theme": "SMH",
                    "signal": "observable fact only",
                    "direction": "bullish|bearish|neutral|mixed",
                    "strength": 0,
                    "confidence": 0,
                    "category": "technical|macro|sector|theme|news|risk",
                }
            ],
            "low_quality_evidence": [{"evidence_id": "NEWS_003", "reason": "speculative media / low systemic relevance"}],
        },
        "regime_scoring": {
            "stage": "regime_scoring",
            "scores": {
                "growth": 0,
                "inflation": 0,
                "liquidity": 0,
                "risk_appetite": 0,
                "breadth": 0,
                "rates_pressure": 0,
                "volatility_pressure": 0,
                "policy_risk": 0,
            },
            "scenario_probabilities": {
                "risk_on_expansion": 0,
                "soft_landing": 0,
                "growth_plus_inflation": 0,
                "defensive_slowdown": 0,
                "risk_off_shock": 0,
            },
            "dominant_regime": "risk_on_expansion|soft_landing|growth_plus_inflation|defensive_slowdown|risk_off_shock|unclear",
            "confidence": 0,
            "regime_summary": "short explanation",
            "supporting_evidence": [],
            "conflicting_evidence": [],
        },
        "cross_asset_validation": {
            "stage": "cross_asset_validation",
            "dominant_cross_asset_story": "growth_risk_on|growth_plus_inflation|disinflationary_risk_on|stagflation_pressure|risk_off_deflation|mixed",
            "asset_class_tests": [
                {
                    "asset_class": "equities",
                    "signal": "text",
                    "confirms_regime": True,
                    "contradicts_regime": False,
                    "alternative_explanation": "text",
                    "evidence_ids": [],
                }
            ],
            "key_confirmations": [],
            "key_contradictions": [],
            "confidence_adjustment": -10,
            "summary": "text",
        },
        "news_weighting": {
            "stage": "news_weighting",
            "headline_classifications": [
                {
                    "evidence_id": "NEWS_001",
                    "title": "headline title from snapshot",
                    "source": "source name from snapshot",
                    "category": "systemic_macro|sector_specific|structural_theme|sentiment_noise|speculative_media|irrelevant",
                    "portfolio_relevance": 0,
                    "systemic_relevance": 0,
                    "tradability": 0,
                    "include_in_report": True,
                    "reason": "text",
                }
            ],
            "top_systemic_risks": [{"risk": "inflation reacceleration", "evidence_ids": ["NEWS_001"], "why_it_matters": "text"}],
            "top_sector_risks": [{"risk": "AI capex risk", "evidence_ids": ["NEWS_002"], "why_it_matters": "text"}],
            "narrative_noise": [{"evidence_id": "NEWS_003", "reason": "speculative media"}],
            "risk_ranking_summary": "text",
        },
        "portfolio_construction": {
            "stage": "portfolio_construction",
            "overall_stance": "aggressive_risk_on|selective_risk|balanced|defensive|risk_off",
            "equity_exposure_range": "text",
            "cash_range": "text",
            "hedge_guidance": [],
            "sector_recommendations": [
                {
                    "sector": "Semiconductors",
                    "bias": "OW|Modest OW|Neutral|Modest UW|UW|Avoid",
                    "sizing": "core_overweight|modest_overweight|watchlist_only|avoid_reduce",
                    "reason": "text",
                    "evidence_ids": ["SECTOR_001", "TECH_001"],
                    "key_risk": "text",
                }
            ],
            "tactical_buy_list": [{"asset": "SMH", "action": "buy_pullback|hold|watch|avoid_chasing", "reason": "text", "evidence_ids": ["SECTOR_001"]}],
            "reduce_list": [{"asset": "XLY", "action": "trim|avoid|reduce_into_strength", "reason": "text", "evidence_ids": ["SECTOR_002"]}],
            "confidence_constraints": [],
        },
    }
    return json.dumps(schemas[stage_name], ensure_ascii=True, indent=2)


def stage_task(stage_name: str) -> str:
    tasks = {
        "evidence_extraction": (
            "Extract observable facts only. No interpretation, no regime call, no portfolio recommendation. "
            "Mark low-quality or speculative headlines separately."
        ),
        "regime_scoring": (
            "Using only Stage 1 facts and market regime data, classify the regime, score factors, and assign scenario probabilities that sum to 100."
        ),
        "cross_asset_validation": (
            "Test whether equities, rates, commodities, gold, copper/silver, oil, volatility, FX/dollar, crypto, and breadth confirm the dominant regime."
        ),
        "news_weighting": (
            "Classify headlines by systemic relevance and prevent speculative or personal-finance noise from becoming top portfolio risks."
        ),
        "portfolio_construction": (
            "Convert regime, cross-asset, news, and sector evidence into disciplined portfolio bias. No aggressive overweight if confidence is below 40."
        ),
        "final_cio_report": (
            "Write a concise final CIO report using only prior stage outputs and the supplied top headline block. "
            "Do not introduce new facts, numbers, tickers, or risks."
        ),
    }
    return tasks[stage_name]


def build_stage_prompt(stage_name: str, snapshot: dict, stage_outputs: dict, *, max_knowledge_chunks: int = 4) -> list[dict]:
    system = (
        f"{read_system_prompt()}\n\n"
        "You must separate observable facts, interpretation, recommendation, risks, and invalidators. "
        "Use only the supplied snapshot and previous stage outputs."
    )
    knowledge = retrieve_knowledge(f"deepseek multistage {stage_name} macro cross asset portfolio risk", snapshot, max_chunks=max_knowledge_chunks)
    knowledge_text = "\n\n".join(
        f"### {chunk['file_name']} / {chunk['heading']}\n{chunk['content'][:1400]}"
        for chunk in knowledge
    )
    if stage_name == "final_cio_report":
        locked_probabilities = _scenario_probability_table(stage_outputs)
        user = (
            f"{STAGE_TITLES[stage_name]}\n\n"
            f"TASK:\n{stage_task(stage_name)}\n\n"
            "PRIOR STAGE OUTPUTS:\n"
            f"{_short(stage_outputs, max_chars=22000)}\n\n"
            "LOCKED BASE/BULL/BEAR SCENARIO TABLE:\n"
            f"{locked_probabilities}\n\n"
            "You must copy the locked scenario table exactly in the Scenario Analysis section. "
            "Do not recalculate, round, omit, rename, or reorder scenario probabilities.\n\n"
            "REQUIRED MARKDOWN HEADINGS:\n"
            f"{chr(10).join(FINAL_REPORT_HEADINGS)}\n\n"
            "Use each required heading exactly as written, once, as its own line. "
            "Do not combine, rename, skip, or replace headings.\n\n"
            "MACRO MARKET DASHBOARD INPUTS:\n"
            f"{_macro_dashboard_block(snapshot)}\n\n"
            "TOP NEWS HEADLINES FROM SNAPSHOT:\n"
            f"{_top_headlines_block(snapshot)}\n\n"
            "REPORT QUALITY RULES:\n"
            "- Facts before opinions. Separate observations, interpretation, and investment implication.\n"
            "- Do not make broad conclusions from a single news article.\n"
            "- Every recommendation must cite supporting evidence IDs or explicitly say evidence is unavailable.\n"
            "- If Macro API data is unavailable for a requested item, say unavailable instead of guessing.\n"
            "- Macro Market Dashboard must cover equities, rates, yield curve, dollar, commodities, and volatility where available.\n"
            "- News Analysis must include headline, importance, market impact, affected assets, time horizon, and investment implication.\n"
            "- Evidence Table must rank evidence quality as Very High, High, Medium, or Low.\n"
            "- Cross-Asset Confirmation must state whether macro data confirms or contradicts the news narrative.\n"
            "- Scenario Analysis must include Base Case, Bull Case, Bear Case, probabilities, supporting evidence, risks, and portfolio implication.\n"
            "- Investment Recommendations must include recommendation, reason, supporting evidence, confidence, and time horizon.\n"
            "- Confidence Assessment must assign High, Medium, or Low to each major conclusion.\n"
            "STRICT FACT RULES:\n"
            "- Output markdown only.\n"
            "- Do not invent evidence IDs. Use only IDs already present in prior stage outputs.\n"
            "- You may use NEWS evidence IDs and headline text from TOP NEWS HEADLINES FROM SNAPSHOT.\n"
            "- Include the Top News Headlines section with actual headline titles, source names when available, why each matters, and the evidence ID.\n"
            "- Keep each section concise. One short paragraph or 2 bullets is enough.\n"
            "- Do not invent exact market numbers, prices, yields, percentages, scores, or dates.\n"
            "- The only probability numbers allowed are the locked Stage 2 scenario probabilities above.\n"
            "- Do not write exposure ranges, thresholds, or numbered claims unless those numbers appear in prior stage outputs.\n"
            "- Prefer qualitative wording such as low, moderate, elevated, selective, neutral, or defensive.\n"
            "- Do not introduce new tickers, ETFs, sectors, risks, or recommendations not present in prior stage outputs."
        )
    else:
        allowed_ids = _allowed_evidence_block(snapshot)
        user = (
            f"{STAGE_TITLES[stage_name]}\n\n"
            f"TASK:\n{stage_task(stage_name)}\n\n"
            "RELEVANT KNOWLEDGE:\n"
            f"{knowledge_text}\n\n"
            "SNAPSHOT OR PRIOR OUTPUTS:\n"
            f"{_short(compact_snapshot(snapshot) if stage_name == 'evidence_extraction' else stage_outputs, max_chars=22000)}\n\n"
            "ALLOWED EVIDENCE IDS:\n"
            f"{allowed_ids}\n\n"
            "STRICT JSON SCHEMA:\n"
            f"{stage_schema(stage_name)}\n\n"
            "STRICT FACT RULES:\n"
            "- Return one valid JSON object only. No markdown. No prose outside JSON.\n"
            "- Use only evidence IDs from ALLOWED EVIDENCE IDS. Never invent IDs such as EVIDENCE_001.\n"
            "- Do not invent exact market numbers, prices, yields, percentages, dates, or tickers.\n"
            "- Numeric scores and probabilities are allowed only in the requested scoring fields.\n"
            "- If a top risk has no valid evidence ID, do not include it as a top risk."
        )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def prompts_preview(prompts: dict[str, list[dict]], *, max_chars: int = 4000) -> str:
    blocks = []
    for stage_name, messages in prompts.items():
        combined = "\n\n".join(f"{message['role'].upper()}:\n{message['content']}" for message in messages)
        if len(combined) > max_chars:
            combined = combined[:max_chars].rstrip() + "\n...[truncated]..."
        blocks.append(f"# {STAGE_TITLES.get(stage_name, stage_name)}\n\n{combined}")
    return "\n\n".join(blocks)
