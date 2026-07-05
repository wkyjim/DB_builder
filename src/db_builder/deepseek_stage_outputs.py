"""Deterministic stage outputs for the multistage DeepSeek CIO agent."""

from __future__ import annotations

from datetime import datetime, timezone


SCENARIOS = [
    "risk_on_expansion",
    "soft_landing",
    "growth_plus_inflation",
    "defensive_slowdown",
    "risk_off_shock",
]


def evidence_ids(snapshot: dict, prefixes: tuple[str, ...] | None = None) -> set[str]:
    ids = set()
    for row in snapshot.get("evidence_appendix", []):
        evidence_id = str(row.get("evidence_id") or "")
        if not evidence_id:
            continue
        if prefixes and not evidence_id.startswith(tuple(f"{prefix}_" for prefix in prefixes)):
            continue
        ids.add(evidence_id)
    return ids


def evidence_rows(snapshot: dict, prefixes: tuple[str, ...] | None = None, *, limit: int = 20) -> list[dict]:
    allowed = evidence_ids(snapshot, prefixes)
    rows = [row for row in snapshot.get("evidence_appendix", []) if row.get("evidence_id") in allowed]
    return rows[:limit]


def confidence_percent(snapshot: dict) -> float:
    confidence = (snapshot.get("market_state") or {}).get("confidence")
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return 30.0
    return value * 100 if value <= 1 else value


def _direction(value: float | None) -> str:
    if value is None:
        return "neutral"
    if value > 1:
        return "bullish"
    if value < -1:
        return "bearish"
    return "neutral"


def _as_float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _move_label(value) -> str:
    number = _as_float(value)
    if number is None:
        return "unavailable"
    if number > 0.25:
        return "up"
    if number < -0.25:
        return "down"
    return "stable"


def _strength_label(value) -> str:
    number = _as_float(value)
    if number is None:
        return "unavailable"
    if abs(number) >= 1:
        return "strong"
    if abs(number) >= 0.25:
        return "moderate"
    return "flat"


def _trend_label(row: dict) -> str:
    close = _as_float(row.get("close"))
    ma50 = _as_float(row.get("ma50"))
    ma200 = _as_float(row.get("ma200"))
    if close is None:
        return "unavailable"
    if ma50 is not None and ma200 is not None:
        if close > ma50 and close > ma200:
            return "above 50d/200d trend"
        if close < ma50 and close < ma200:
            return "below 50d/200d trend"
    pct_chg = _as_float(row.get("pct_chg"))
    return _move_label(pct_chg)


def _find_by_symbol(rows: list[dict], symbols: set[str]) -> dict | None:
    for row in rows:
        if str(row.get("symbol") or row.get("ticker") or "") in symbols:
            return row
    return None


def _macro_line(label: str, row: dict | None) -> str:
    if not row:
        return f"- {label}: unavailable from current PostgreSQL snapshot."
    evidence_id = row.get("evidence_id", "unavailable")
    pct_chg = row.get("pct_chg")
    date = row.get("date", "date unavailable")
    close = row.get("close", "close unavailable")
    return (
        f"- {label}: {_move_label(pct_chg)} / {_strength_label(pct_chg)}; "
        f"close={close}, pct_chg={pct_chg}, date={date} [{evidence_id}]."
    )


def macro_dashboard_lines(snapshot: dict | None) -> list[str]:
    snapshot = snapshot or {}
    macro = snapshot.get("cross_asset", []) or []
    technicals = snapshot.get("technicals", []) or []

    sp500 = _find_by_symbol(macro, {"^GSPC", "ES=F"}) or _find_by_symbol(technicals, {"SPY"})
    nasdaq = _find_by_symbol(macro, {"^IXIC", "NQ=F"}) or _find_by_symbol(technicals, {"QQQ"})
    russell = _find_by_symbol(macro, {"^RUT", "RTY=F"}) or _find_by_symbol(technicals, {"IWM"})
    five_year = _find_by_symbol(macro, {"^FVX"})
    ten_year = _find_by_symbol(macro, {"^TNX"})
    thirty_year = _find_by_symbol(macro, {"^TYX"})
    dollar = _find_by_symbol(macro, {"DXY", "DX-Y.NYB"})
    gold = _find_by_symbol(macro, {"GC=F"})
    oil = _find_by_symbol(macro, {"CL=F", "BZ=F"})
    copper = _find_by_symbol(macro, {"HG=F"})
    vix = _find_by_symbol(macro, {"^VIX"})

    curve = "unavailable from current PostgreSQL snapshot."
    ten = _as_float((ten_year or {}).get("close"))
    five = _as_float((five_year or {}).get("close"))
    thirty = _as_float((thirty_year or {}).get("close"))
    if five is not None and ten is not None and thirty is not None:
        curve = "inverted" if ten < five else "upward sloping"
        curve += f"; 5Y={five}, 10Y={ten}, 30Y={thirty}."
    elif ten is not None and thirty is not None:
        curve = "long-end curve available only; "
        curve += "inverted 10Y/30Y" if thirty < ten else "upward sloping 10Y/30Y"
        curve += f"; 10Y={ten}, 30Y={thirty}."

    return [
        "Equity Markets",
        _macro_line("S&P 500", sp500),
        _macro_line("Nasdaq", nasdaq),
        _macro_line("Russell 2000", russell),
        "Interest Rates",
        _macro_line("5Y Treasury", five_year),
        _macro_line("10Y Treasury", ten_year),
        _macro_line("30Y Treasury", thirty_year),
        "Yield Curve",
        f"- Yield Curve: {curve}",
        "Dollar",
        _macro_line("U.S. Dollar / DXY", dollar),
        "Commodities",
        _macro_line("Gold", gold),
        _macro_line("Oil", oil),
        _macro_line("Copper", copper),
        "Volatility",
        _macro_line("VIX", vix),
    ]


def fallback_evidence_extraction(snapshot: dict) -> dict:
    facts = []
    market = snapshot.get("market_state") or {}
    if market:
        facts.append(
            {
                "evidence_id": market.get("evidence_id", "REGIME_001"),
                "asset_or_theme": "market regime",
                "signal": f"{market.get('market_regime')} / {market.get('market_phase')} confidence={market.get('confidence')}",
                "direction": "mixed" if confidence_percent(snapshot) < 40 else "bullish",
                "strength": int(min(100, max(0, float(market.get("risk_on_score") or 50)))),
                "confidence": int(confidence_percent(snapshot)),
                "category": "macro",
            }
        )
    for row in snapshot.get("cross_asset", [])[:10]:
        facts.append(
            {
                "evidence_id": row["evidence_id"],
                "asset_or_theme": row.get("symbol"),
                "signal": f"{row.get('bucket')} pct_chg={row.get('pct_chg')}",
                "direction": _direction(row.get("pct_chg")),
                "strength": min(100, abs(int((row.get("pct_chg") or 0) * 10))),
                "confidence": 70,
                "category": "macro",
            }
        )
    for row in snapshot.get("technicals", [])[:8]:
        ret20 = row.get("return_20d")
        facts.append(
            {
                "evidence_id": row["evidence_id"],
                "asset_or_theme": row.get("ticker"),
                "signal": f"20d return={ret20}, close={row.get('close')}, rsi={row.get('rsi')}",
                "direction": _direction(ret20),
                "strength": min(100, abs(int((ret20 or 0) * 2))),
                "confidence": 70,
                "category": "technical",
            }
        )
    for row in snapshot.get("sector_data", [])[:8]:
        facts.append(
            {
                "evidence_id": row["evidence_id"],
                "asset_or_theme": row.get("sector"),
                "signal": f"{row.get('regime')} rank={row.get('rotation_rank')} score={row.get('final_score')}",
                "direction": "bullish" if row.get("allocation_bias") in {"overweight", "modest_overweight"} else "bearish" if row.get("allocation_bias") in {"underweight", "avoid"} else "neutral",
                "strength": int(row.get("final_score") or 50),
                "confidence": 65,
                "category": "sector",
            }
        )
    low_quality = []
    for row in snapshot.get("news", []):
        title = str(row.get("title") or "").lower()
        if any(token in title for token in ["spacex", "ipo hype", "fomo", "retirement", "social security", "plumber"]):
            low_quality.append({"evidence_id": row["evidence_id"], "reason": "speculative media / personal finance / low systemic relevance"})
    return {
        "stage": "evidence_extraction",
        "data_quality": {
            "overall": "moderate" if facts else "weak",
            "missing_data": [key for key, value in (snapshot.get("unavailable_fields") or {}).items() if value is None],
            "confidence_constraints": ["Market confidence below 40% requires cautious positioning"] if confidence_percent(snapshot) < 40 else [],
        },
        "facts": facts[:30],
        "low_quality_evidence": low_quality[:10],
    }


def fallback_regime_scoring(snapshot: dict, stage1: dict | None = None) -> dict:
    market = snapshot.get("market_state") or {}
    confidence = int(confidence_percent(snapshot))
    risk_on = int(float(market.get("risk_on_score") or 50))
    risk_off = int(float(market.get("risk_off_score") or 50))
    growth = int((float(market.get("technical_score") or 50) + float(market.get("momentum_score") or 50)) / 2)
    inflation = 55 if any("inflation" in str(row).lower() for row in snapshot.get("news", [])) else 45
    probs = {
        "risk_on_expansion": max(10, min(45, risk_on // 2)),
        "soft_landing": 25,
        "growth_plus_inflation": 20 if inflation >= 50 else 15,
        "defensive_slowdown": max(10, risk_off // 3),
        "risk_off_shock": 10,
    }
    total = sum(probs.values()) or 100
    probs = {key: round(value * 100 / total) for key, value in probs.items()}
    delta = 100 - sum(probs.values())
    probs["soft_landing"] += delta
    dominant = max(probs, key=probs.get) if confidence >= 25 else "unclear"
    return {
        "stage": "regime_scoring",
        "scores": {
            "growth": growth,
            "inflation": inflation,
            "liquidity": 50,
            "risk_appetite": int(float(market.get("risk_appetite_score") or 50)),
            "breadth": int(float(market.get("breadth_score") or 50)),
            "rates_pressure": 55,
            "volatility_pressure": 35 if str(market.get("volatility_state")) in {"calm", "normal"} else 70,
            "policy_risk": 50,
        },
        "scenario_probabilities": probs,
        "dominant_regime": dominant,
        "confidence": confidence,
        "regime_summary": "Risk appetite and breadth are constructive, but confidence discipline prevents aggressive language.",
        "supporting_evidence": ["REGIME_001"],
        "conflicting_evidence": [row["evidence_id"] for row in (stage1 or {}).get("low_quality_evidence", [])[:3]],
    }


def fallback_cross_asset_validation(snapshot: dict, stage2: dict | None = None) -> dict:
    tests = []
    bucket_map: dict[str, list[dict]] = {}
    for row in snapshot.get("cross_asset", []):
        bucket_map.setdefault(str(row.get("bucket") or "other"), []).append(row)
    required = ["equities", "rates", "commodities", "volatility", "fx", "crypto"]
    for asset_class in required:
        rows = bucket_map.get(asset_class, [])
        if not rows:
            continue
        avg = sum(float(row.get("pct_chg") or 0) for row in rows) / len(rows)
        tests.append(
            {
                "asset_class": asset_class,
                "signal": f"Average pct_chg across available {asset_class} evidence is {round(avg, 2)}.",
                "confirms_regime": avg > 0 if asset_class != "volatility" else avg <= 0,
                "contradicts_regime": avg < 0 if asset_class != "volatility" else avg > 0,
                "alternative_explanation": "Mixed commodity or rates moves can indicate growth plus inflation rather than clean risk-on.",
                "evidence_ids": [row["evidence_id"] for row in rows[:4]],
            }
        )
    commodity_names = " ".join(str(row.get("symbol")) for row in bucket_map.get("commodities", []))
    contradictions = []
    if "GC=F" in commodity_names and ("HG=F" in commodity_names or "SI=F" in commodity_names):
        contradictions.append("Metals strength with equities can reflect growth, inflation, or geopolitical hedging rather than simple risk-on.")
    return {
        "stage": "cross_asset_validation",
        "dominant_cross_asset_story": "growth_plus_inflation" if contradictions else "growth_risk_on",
        "asset_class_tests": tests,
        "key_confirmations": [test["asset_class"] for test in tests if test["confirms_regime"]],
        "key_contradictions": contradictions,
        "confidence_adjustment": -5 if contradictions else 0,
        "summary": "Cross-asset evidence is constructive but not one-dimensional; rates and commodities should temper broad beta enthusiasm.",
    }


def _headline_category(row: dict) -> str:
    title = str(row.get("title") or "").lower()
    themes = " ".join(str(item).lower() for item in row.get("themes", []))
    text = f"{title} {themes}"
    if any(token in text for token in ["spacex", "fomo", "employee wealth", "plumber", "social security", "retirement"]):
        return "speculative_media" if "spacex" in text or "fomo" in text else "irrelevant"
    if any(token in text for token in ["inflation", "rates", "debt", "default", "oil", "gold", "iran"]):
        return "systemic_macro"
    if any(token in text for token in ["ai", "technology", "chip", "semiconductor"]):
        return "structural_theme"
    return "sector_specific"


def fallback_news_weighting(snapshot: dict, stage3: dict | None = None) -> dict:
    classifications = []
    noise = []
    systemic = []
    sector_risks = []
    for row in snapshot.get("news", [])[:15]:
        category = _headline_category(row)
        item = {
            "evidence_id": row["evidence_id"],
            "title": row.get("title"),
            "source": row.get("source"),
            "category": category,
            "portfolio_relevance": 80 if category == "systemic_macro" else 55 if category in {"sector_specific", "structural_theme"} else 15,
            "systemic_relevance": 80 if category == "systemic_macro" else 30,
            "tradability": 60 if category in {"systemic_macro", "structural_theme"} else 20,
            "include_in_report": category not in {"irrelevant", "speculative_media"},
            "reason": "Classified by systemic relevance, asset-class linkage, and portfolio tradability.",
        }
        classifications.append(item)
        if category in {"speculative_media", "irrelevant"}:
            noise.append(row["evidence_id"])
        elif category == "systemic_macro":
            systemic.append({"risk": row.get("themes", ["macro risk"])[0] if row.get("themes") else "macro risk", "evidence_ids": [row["evidence_id"]], "why_it_matters": row.get("why_it_matters") or "Potential macro relevance."})
        else:
            sector_risks.append({"risk": row.get("themes", ["sector risk"])[0] if row.get("themes") else "sector risk", "evidence_ids": [row["evidence_id"]], "why_it_matters": "Sector or theme relevance without broad systemic confirmation."})
    return {
        "stage": "news_weighting",
        "headline_classifications": classifications,
        "top_systemic_risks": systemic[:3],
        "top_sector_risks": sector_risks[:5],
        "narrative_noise": noise[:8],
        "risk_ranking_summary": "Speculative media and personal finance headlines are separated from systemic macro risks.",
    }


def fallback_portfolio_construction(snapshot: dict, stage2: dict | None = None, stage3: dict | None = None, stage4: dict | None = None) -> dict:
    stage_confidence = (stage2 or {}).get("confidence")
    confidence_values = [confidence_percent(snapshot)]
    if stage_confidence is not None:
        try:
            confidence_values.append(float(stage_confidence))
        except (TypeError, ValueError):
            pass
    confidence = int(min(confidence_values))
    low_conf = confidence < 40
    recommendations = []
    buy = []
    reduce = []
    for sector in snapshot.get("sector_data", [])[:14]:
        bias_raw = str(sector.get("allocation_bias") or "neutral")
        if bias_raw == "overweight" and low_conf:
            bias = "Modest OW"
            sizing = "modest_overweight"
        elif bias_raw == "overweight":
            bias = "OW"
            sizing = "core_overweight"
        elif bias_raw == "modest_overweight":
            bias = "Modest OW"
            sizing = "modest_overweight"
        elif bias_raw == "avoid":
            bias = "Avoid"
            sizing = "avoid_reduce"
        elif bias_raw == "underweight":
            bias = "Modest UW" if low_conf else "UW"
            sizing = "avoid_reduce"
        else:
            bias = "Neutral"
            sizing = "watchlist_only"
        item = {
            "sector": sector.get("sector"),
            "bias": bias,
            "sizing": sizing,
            "reason": f"{sector.get('regime')} regime, rotation rank {sector.get('rotation_rank')}, action {sector.get('recommended_action')}",
            "evidence_ids": [sector.get("evidence_id")],
            "key_risk": "Low confidence limits position size." if low_conf else "Monitor regime changes.",
        }
        recommendations.append(item)
        etfs = sector.get("related_etfs") or []
        if bias in {"OW", "Modest OW"} and etfs:
            buy.append({"asset": etfs[0], "action": "buy_pullback" if low_conf else "hold", "reason": item["reason"], "evidence_ids": item["evidence_ids"]})
        if bias in {"UW", "Modest UW", "Avoid"} and etfs:
            reduce.append({"asset": etfs[0], "action": "trim" if bias != "Avoid" else "avoid", "reason": item["reason"], "evidence_ids": item["evidence_ids"]})
    buy_assets = {row["asset"] for row in buy}
    reduce = [row for row in reduce if row["asset"] not in buy_assets]
    return {
        "stage": "portfolio_construction",
        "overall_stance": "selective_risk" if low_conf else "balanced",
        "equity_exposure_range": "55-70%" if low_conf else "60-80%",
        "cash_range": "15-25%" if low_conf else "10-20%",
        "hedge_guidance": ["Use hedges against rates, volatility, and commodity shocks when cross-asset contradictions rise."],
        "sector_recommendations": recommendations,
        "tactical_buy_list": buy[:8],
        "reduce_list": reduce[:8],
        "confidence_constraints": ["Confidence below 40% prevents aggressive overweight."] if low_conf else [],
    }


def fallback_final_report(stage_outputs: dict, snapshot: dict | None = None) -> str:
    regime = stage_outputs.get("regime_scoring", {})
    cross = stage_outputs.get("cross_asset_validation", {})
    news = stage_outputs.get("news_weighting", {})
    portfolio = stage_outputs.get("portfolio_construction", {})
    probs = regime.get("scenario_probabilities", {})
    bull = int(probs.get("risk_on_expansion") or 0)
    bear = int(probs.get("defensive_slowdown") or 0) + int(probs.get("risk_off_shock") or 0)
    base = max(0, 100 - bull - bear)
    recs = portfolio.get("sector_recommendations", [])[:8]
    buys = portfolio.get("tactical_buy_list", [])[:6]
    reduces = portfolio.get("reduce_list", [])[:6]
    headline_rows = news.get("headline_classifications", [])[:8]
    lines = [
        "# Qwen Multi-Stage CIO Report",
        "",
        "## Executive Summary",
        f"- Current market tone: `{regime.get('dominant_regime', 'unclear')}` with confidence `{regime.get('confidence', 0)}`.",
        f"- Portfolio stance: `{portfolio.get('overall_stance', 'balanced')}`.",
        f"- Macro interpretation: {regime.get('regime_summary', 'Regime evidence is mixed.')}",
        f"- Cross-asset check: {cross.get('summary', 'Cross-asset confirmation is mixed.')}",
        f"- News quality: {news.get('risk_ranking_summary', 'News weighted by relevance.')}",
        "- Recommendation discipline: no recommendation should be treated as high conviction unless it has multiple supporting evidence sources.",
        "",
        "## Macro Market Dashboard",
    ]
    if snapshot and snapshot.get("generated_at"):
        lines[2:2] = [f"Snapshot generated at: {snapshot.get('generated_at')}", ""]
    lines.extend(macro_dashboard_lines(snapshot))
    lines.extend([
        "",
        "## Macro Regime Classification",
        f"- Growth: {regime.get('scores', {}).get('growth', 'unavailable')} based on available technical and momentum evidence.",
        f"- Inflation: {regime.get('scores', {}).get('inflation', 'unavailable')} based on available news and macro evidence.",
        f"- Monetary Policy: unavailable unless explicit policy evidence is present.",
        f"- Risk Appetite: {regime.get('scores', {}).get('risk_appetite', 'unavailable')}.",
        f"- Liquidity: {regime.get('scores', {}).get('liquidity', 'unavailable')}.",
        "",
        "## News Analysis",
    ])
    for row in headline_rows:
        title = row.get("title") or "headline title unavailable"
        source = row.get("source") or "source unavailable"
        importance = "High" if row.get("systemic_relevance", 0) >= 70 else "Medium" if row.get("portfolio_relevance", 0) >= 50 else "Low"
        impact = "Mixed" if row.get("category") in {"systemic_macro", "structural_theme"} else "Low / unclear"
        lines.extend([
            f"### {row.get('evidence_id')}: {title}",
            f"- Source: {source}",
            f"- Importance: {importance}",
            f"- Market Impact: {impact}",
            "- Affected Assets: unavailable unless explicitly provided by classification.",
            "- Time Horizon: short to medium term unless supported otherwise.",
            f"- Investment Implication: {row.get('reason') or 'Classified by systemic relevance and portfolio tradability.'}",
        ])
    if not headline_rows:
        lines.append("No classified headlines were available from the news-weighting stage.")
    lines.extend([
        "",
        "## Evidence Table",
        "| Importance | Evidence | Interpretation |",
        "|---|---|---|",
        "| Very High | Central bank, inflation, and employment evidence | Unavailable unless present in the snapshot. |",
        f"| High | Cross-asset evidence | {cross.get('dominant_cross_asset_story', 'mixed')} |",
        f"| Medium | News evidence | {news.get('risk_ranking_summary', 'News weighted by relevance.')} |",
        "| Low | Speculative or opinion-like headlines | Excluded from top systemic risk unless confirmed by macro evidence. |",
        "",
        "## Cross-Asset Confirmation",
        cross.get("summary", "Cross-asset confirmation is mixed."),
        "",
        "## Market Narrative",
        f"- Markets appear to be pricing `{regime.get('dominant_regime', 'unclear')}` based on available evidence.",
        "- News themes are not sufficient by themselves; macro confirmation is required before raising conviction.",
        "- If evidence is mixed, the report should favor selective positioning rather than broad beta.",
        "",
        "## Scenario Analysis",
        "| Scenario | Probability | Source |",
        "|---|---:|---|",
        f"| Base Case | {base} | soft_landing + growth_plus_inflation |",
        f"| Bull Case | {bull} | risk_on_expansion |",
        f"| Bear Case | {bear} | defensive_slowdown + risk_off_shock |",
        "",
        "## Sector Analysis",
        "| Sector | Current View | Reason | Catalysts | Risks | Confidence |",
        "|---|---|---|---|---|---|",
    ])
    if recs:
        lines.extend(
            f"| {row.get('sector')} | {row.get('bias')} | {row.get('reason')} | unavailable unless provided | {row.get('key_risk')} | Medium |"
            for row in recs
        )
    else:
        lines.append("| unavailable | Neutral | No sector recommendations passed evidence filters. | unavailable | unavailable | Low |")
    lines.extend(["", "## Risk Register", "| Risk | Likelihood | Impact | Evidence |", "|---|---|---|---|"])
    for row in news.get("top_systemic_risks", [])[:5]:
        lines.append(f"| {row.get('risk')} | Medium | High if confirmed by macro data | {', '.join(row.get('evidence_ids', []))} |")
    if not news.get("top_systemic_risks"):
        lines.append("| Inflation / rates / growth shock | Unavailable | Unavailable | No top systemic risks passed evidence filters. |")
    lines.extend(["", "## Contradictions"])
    contradictions = []
    for item in cross.get("key_contradictions", []) or []:
        if isinstance(item, dict):
            contradictions.append(
                str(item.get("contradiction") or item.get("summary") or item.get("signal") or item.get("asset_class") or item)
            )
        else:
            contradictions.append(str(item))
    lines.append("; ".join(contradictions or ["No significant contradictions found from the available data."]))
    lines.extend(["", "## Investment Recommendations"])
    lines.extend(f"- {row.get('asset')}: {row.get('action')} ({row.get('reason')})" for row in buys)
    if not buys:
        lines.append("- No high-conviction tactical buys passed current evidence filters.")
    if reduces:
        lines.extend(["", "### Reduce / Avoid"])
        lines.extend(f"- {row.get('asset')}: {row.get('action')} ({row.get('reason')})" for row in reduces)
    lines.extend(["", "## Invalidators"])
    lines.append("The view weakens if cross-asset contradictions rise, scenario probabilities shift toward risk-off, or confidence remains low while positioning becomes aggressive.")
    lines.extend(["", "## Confidence Assessment"])
    lines.append(f"- Regime confidence: {'High' if float(regime.get('confidence') or 0) >= 70 else 'Medium' if float(regime.get('confidence') or 0) >= 40 else 'Low'}.")
    lines.append("- Recommendation confidence depends on evidence quality and cross-asset consistency; single-headline support is low confidence.")
    return "\n".join(lines).rstrip() + "\n"
