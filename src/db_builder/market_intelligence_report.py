"""Clean market intelligence report formats."""

from __future__ import annotations

from datetime import datetime, timezone

from db_builder.critical_house_view import build_critical_house_view
from db_builder.news_importance import annotate_article_importance
from db_builder.report_sections import as_list, comma, first, flt, fmt, generated_header, heading, table


def ensure_critical_view(data: dict) -> dict:
    view = data.get("critical_house_view")
    if not view:
        view = build_critical_house_view(data)
        data["critical_house_view"] = view
    return view


def confidence_label(value: float) -> str:
    if value < 0.15:
        return f"Low ({fmt(value, 4)})"
    if value <= 0.35:
        return f"Moderate-low ({fmt(value, 4)})"
    if value > 0.50:
        return f"High ({fmt(value, 4)})"
    return f"Moderate ({fmt(value, 4)})"


def market_character(data: dict) -> str:
    regime_v2 = first(data.get("regime_v2") or [])
    parts = [
        f"market strength {regime_v2.get('market_strength', 'unknown')}",
        f"trend {regime_v2.get('trend_state', 'unknown')}",
        f"momentum {regime_v2.get('momentum_state', 'unknown')}",
        f"volatility {regime_v2.get('volatility_state', 'unknown')}",
        f"breadth {regime_v2.get('breadth_state', 'unknown')}",
        f"risk appetite {regime_v2.get('risk_appetite_state', 'unknown')}",
    ]
    return "; ".join(parts)


def bullishness_mix(data: dict) -> dict:
    regime = first(data.get("regime_v2") or [])
    technical = flt(regime.get("technical_score"), 50)
    momentum = flt(regime.get("momentum_score"), 50)
    breadth = flt(regime.get("breadth_score"), 50)
    risk_appetite = flt(regime.get("risk_appetite_score"), 50)
    news = flt(regime.get("news_score"), 50)
    bullish = max(0, min(100, round((technical + momentum + breadth + risk_appetite + news) / 5, 1)))
    bearish = max(0, min(100, round((100 - bullish) * 0.65, 1)))
    neutral = max(0, round(100 - bullish - bearish, 1))
    return {"bullish": bullish, "neutral": neutral, "bearish": bearish}


def rank_sector_views(data: dict) -> list[dict]:
    critical = ensure_critical_view(data)
    rotation_by_sector = {row.get("sector_name"): row for row in data.get("sector_rotation", [])}
    regimes_by_sector = {row.get("sector_name"): row for row in data.get("sector_regimes", [])}
    themes = data.get("secular_themes", [])
    rows = []
    for item in critical.get("sector_views", []):
        sector = item["sector"]
        rotation = rotation_by_sector.get(sector, {})
        regime = regimes_by_sector.get(sector, {})
        related_etfs = as_list(rotation.get("related_etfs")) or as_list(regime.get("related_etfs"))
        secular = [
            theme for theme in themes
            if sector.lower() in str(theme.get("theme_name", "") + " " + theme.get("parent_theme", "")).lower()
        ]
        secular_support = "High" if any(flt(row.get("secular_score")) > 70 for row in secular) else "Mixed"
        rank = int(flt(rotation.get("rotation_rank"), 999))
        rows.append(
            {
                **item,
                "rank": rank,
                "related_etfs": related_etfs,
                "tactical_regime": regime.get("sector_regime", "unknown"),
                "cycle_phase": regime.get("cycle_phase", "unknown"),
                "secular_support": secular_support,
                "rotation_score": flt(rotation.get("rotation_score")),
            }
        )
    return sorted(rows, key=lambda row: (row["rank"], -row["rotation_score"], row["sector"]))


def top_news_articles(data: dict, *, limit: int) -> list[dict]:
    rows = []
    for article in data.get("articles", []):
        annotated = annotate_article_importance(article)
        annotated["impact_score"] = flt(article.get("impact_score"))
        annotated["source_priority"] = flt(article.get("source_priority"), 75)
        rows.append(annotated)
    return sorted(
        rows,
        key=lambda row: (
            row.get("source_priority", 0),
            row.get("importance_score", 0),
            row.get("impact_score", 0),
            row.get("published_at") is not None,
            row.get("published_at"),
        ),
        reverse=True,
    )[:limit]


def article_reason(article: dict) -> str:
    reasons = as_list(article.get("importance_reasons"))
    if reasons:
        return str(reasons[0]).replace("title ", "").replace("summary ", "")
    themes = comma(as_list(article.get("themes")), "market relevance")
    return f"Impacts {themes}"


def render_top_news(data: dict, *, limit: int) -> list[str]:
    lines = heading("Top News Intelligence")
    articles = top_news_articles(data, limit=limit)
    if not articles:
        return lines + ["No recent classified headlines are available."]
    for article in articles:
        affected = comma(as_list(article.get("affected_tickers")) or as_list(article.get("themes")), "broad market")
        lines.append(
            f"- **{article.get('title', 'Untitled')}** ({article.get('source_name', 'unknown source')}, "
            f"{article.get('source_category', 'news')}): {article_reason(article)}; affected `{affected}`; "
            f"impact `{fmt(article.get('impact_score'))}`"
        )
    return lines


def _find_symbol(rows: list[dict], symbols: set[str], *, key: str = "symbol") -> dict:
    for row in rows:
        if str(row.get(key) or "") in symbols:
            return row
    return {}


def _move_state(value) -> str:
    number = flt(value, None)
    if number is None:
        return "unavailable"
    if number > 0.25:
        return "up"
    if number < -0.25:
        return "down"
    return "stable"


def _macro_snapshot_line(label: str, row: dict) -> str:
    if not row:
        return f"- {label}: unavailable from local PostgreSQL snapshot."
    return (
        f"- {label}: **{_move_state(row.get('pct_chg'))}**; "
        f"close `{fmt(row.get('close'))}`, pct_chg `{fmt(row.get('pct_chg'))}%`, "
        f"date `{row.get('date', 'n/a')}`"
    )


def _watchlist_snapshot_line(label: str, row: dict) -> str:
    if not row:
        return f"- {label}: unavailable from local PostgreSQL watchlist snapshot."
    return (
        f"- {label}: **{_move_state(row.get('pct_chg'))}**; "
        f"close `{fmt(row.get('close'))}`, pct_chg `{fmt(row.get('pct_chg'))}%`, "
        f"RSI `{fmt(row.get('rsi_14'))}`"
    )


def render_market_snapshot(data: dict) -> list[str]:
    macro = data.get("macro") or []
    watchlist = data.get("watchlist") or []
    five_year = _find_symbol(macro, {"^FVX"})
    ten_year = _find_symbol(macro, {"^TNX"})
    thirty_year = _find_symbol(macro, {"^TYX"})
    curve = "unavailable from local PostgreSQL snapshot."
    five = flt(five_year.get("close"), None)
    ten = flt(ten_year.get("close"), None)
    thirty = flt(thirty_year.get("close"), None)
    if five is not None and ten is not None and thirty is not None:
        curve_state = "inverted" if ten < five else "upward sloping"
        curve = f"{curve_state}; 5Y `{fmt(five)}`, 10Y `{fmt(ten)}`, 30Y `{fmt(thirty)}`"
    elif ten is not None and thirty is not None:
        curve_state = "inverted 10Y/30Y" if thirty < ten else "upward sloping 10Y/30Y"
        curve = f"{curve_state}; 10Y `{fmt(ten)}`, 30Y `{fmt(thirty)}`"
    return heading("Market Snapshot") + [
        "### Equity Markets",
        _macro_snapshot_line("S&P 500", _find_symbol(macro, {"^GSPC", "ES=F"})),
        _macro_snapshot_line("Nasdaq", _find_symbol(macro, {"^IXIC", "NQ=F"})),
        _macro_snapshot_line("Russell 2000", _find_symbol(macro, {"^RUT", "RTY=F"})),
        _watchlist_snapshot_line("SPY", _find_symbol(watchlist, {"SPY"}, key="ticker")),
        _watchlist_snapshot_line("QQQ", _find_symbol(watchlist, {"QQQ"}, key="ticker")),
        "",
        "### Interest Rates",
        _macro_snapshot_line("5Y Treasury", five_year),
        _macro_snapshot_line("10Y Treasury", ten_year),
        _macro_snapshot_line("30Y Treasury", thirty_year),
        f"- Yield Curve: {curve}",
        "",
        "### Dollar, Commodities, Volatility",
        _macro_snapshot_line("U.S. Dollar / DXY", _find_symbol(macro, {"DXY", "DX-Y.NYB"})),
        _macro_snapshot_line("Gold", _find_symbol(macro, {"GC=F"})),
        _macro_snapshot_line("Oil", _find_symbol(macro, {"CL=F", "BZ=F"})),
        _macro_snapshot_line("Copper", _find_symbol(macro, {"HG=F"})),
        _macro_snapshot_line("VIX", _find_symbol(macro, {"^VIX"})),
    ]


def render_sector_ranking(data: dict, *, limit: int | None = None) -> list[str]:
    rows = rank_sector_views(data)
    if limit:
        rows = rows[:limit]
    return heading("Sector Strength Ranking") + table(
        ["Rank", "Sector", "Bias", "ETFs", "Tactical Regime", "Secular Support"],
        [
            [
                row["rank"] if row["rank"] < 999 else "n/a",
                row["sector"],
                row["portfolio_bias"],
                comma(row["related_etfs"]),
                row["tactical_regime"],
                row["secular_support"],
            ]
            for row in rows
        ],
    )


def allocation_groups(data: dict) -> dict[str, list[dict]]:
    groups = {"overweight": [], "neutral": [], "underweight": []}
    for row in rank_sector_views(data):
        bias = row["portfolio_bias"].lower()
        if "overweight" in bias:
            groups["overweight"].append(row)
        elif "underweight" in bias or "avoid" in bias:
            groups["underweight"].append(row)
        else:
            groups["neutral"].append(row)
    return groups


def render_tactical_positioning(data: dict) -> list[str]:
    groups = allocation_groups(data)
    lines = heading("Tactical Positioning")
    for label, rows in [
        ("OVERWEIGHT", groups["overweight"]),
        ("NEUTRAL", groups["neutral"]),
        ("UNDERWEIGHT", groups["underweight"]),
    ]:
        lines.extend([f"### {label}", ""])
        if not rows:
            lines.append("- None.")
        for row in rows[:6]:
            lines.append(f"- **{row['sector']}**: {row['critical_view']}")
        lines.append("")
    return lines


def render_opportunities(data: dict) -> list[str]:
    critical = ensure_critical_view(data)
    lines = heading("Highest Conviction Opportunities")
    if not data.get("opportunities"):
        lines.append(
            "No high-conviction single-name opportunities passed strict filters. "
            "Thematic opportunities remain available."
        )
    else:
        for signal in data.get("opportunities", [])[:8]:
            lines.append(
                f"- **{signal.get('ticker')}**: opportunity `{fmt(signal.get('opportunity_score'))}`, "
                f"risk `{fmt(signal.get('risk_score'))}`"
            )
    lines.extend(
        [
            "",
            "### Tier 1",
            "- Cybersecurity",
            "- Healthcare",
            "- Selective Semiconductors",
            "",
            "### Tier 2",
            "- AI Infrastructure on pullbacks",
            "- Defense",
            "- Grid Infrastructure",
            "",
            "### Tier 3",
            "- Nuclear Power",
            "- Energy Security",
            "- Crypto Infrastructure tactical only",
            "",
            f"Decision note: {critical['opportunity_view']}",
        ]
    )
    return lines


def render_risks(data: dict, *, limit: int = 8) -> list[str]:
    lines = heading("Key Risks")
    macro_risks = [
        f"{row.get('symbol')} move `{fmt(row.get('pct_chg'))}%`"
        for row in data.get("macro", [])[:4]
    ]
    signal_risks = [
        f"{row.get('dimension_value')} risk `{fmt(row.get('risk_score'))}`"
        for row in sorted(data.get("news_signals", []), key=lambda row: flt(row.get("risk_score")), reverse=True)[:4]
    ]
    defaults = [
        "Treasury yield re-acceleration",
        "VIX / volatility spike",
        "AI capex slowdown",
        "Earnings disappointment",
        "Geopolitical escalation",
        "Oil shock",
        "Breadth deterioration",
        "Liquidity deterioration",
    ]
    combined = macro_risks + signal_risks + defaults
    for idx, risk in enumerate(combined[:limit], start=1):
        lines.append(f"{idx}. {risk}")
    return lines


def render_risk_management(data: dict) -> list[str]:
    regime = first(data.get("regime_v2") or [])
    confidence = flt(regime.get("confidence"))
    volatility = str(regime.get("volatility_state") or "")
    if confidence < 0.15 and volatility in {"stressed", "elevated"}:
        equity = "55%-75%"
        cash = "15%-25%"
    else:
        equity = "65%-85%"
        cash = "10%-20%"
    return heading("Risk Management") + [
        f"- Recommended equity exposure: **{equity}**",
        f"- Cash range: **{cash}**",
        "- Hedges: gold, quality factor exposure, optional volatility hedge, and reduced crowded AI beta.",
        "- What not to do: do not chase broad beta or parabolic momentum when confidence is low.",
    ]


def render_final_house_view(data: dict) -> list[str]:
    critical = ensure_critical_view(data)
    groups = allocation_groups(data)
    lines = heading("Final House View")
    lines.append(critical["final_house_view"])
    lines.extend(["", "### House Allocation", ""])
    for label, rows in [
        ("OVERWEIGHT", groups["overweight"]),
        ("NEUTRAL", groups["neutral"]),
        ("UNDERWEIGHT", groups["underweight"]),
    ]:
        lines.append(f"- {label}: {comma([row['sector'] for row in rows[:6]], 'None')}")
    lines.append("- 12-Month Outlook: Constructive with periodic volatility spikes.")
    return lines


def render_executive_summary(data: dict) -> list[str]:
    critical = ensure_critical_view(data)
    regime = first(data.get("regime_v2") or [])
    return heading("Executive Summary") + [
        f"- Dominant Regime: **{critical['market_view']}**",
        f"- Confidence: **{confidence_label(flt(regime.get('confidence')))}**",
        f"- Market Character: {market_character(data)}",
        f"- Portfolio Stance: **{critical['positioning_bias']}**",
    ]


def render_market_bullishness(data: dict) -> list[str]:
    mix = bullishness_mix(data)
    critical = ensure_critical_view(data)
    lines = heading("Market Bullishness / Bearishness")
    lines.extend(
        [
            f"- Bullish: `{mix['bullish']}%`",
            f"- Neutral: `{mix['neutral']}%`",
            f"- Bearish: `{mix['bearish']}%`",
            "",
            "### Bullish Factors",
            "- Stable/positive momentum where present.",
            "- Secular support in AI infrastructure, cybersecurity, healthcare, and defense.",
            "- Healthy breadth prevents declaring the bull structure broken.",
            "",
            "### Bearish Factors",
            "- Low Market Regime 2.0 confidence limits directional conviction.",
            "- Stressed volatility argues against aggressive broad beta exposure.",
            "- Opportunity scanner currently has no high-conviction single-name pass-through.",
            "",
            f"Overall View: **{critical['market_view']}**",
        ]
    )
    return lines


def render_investor_sentiment(data: dict) -> list[str]:
    regime = first(data.get("regime_v2") or [])
    critical = ensure_critical_view(data)
    btc = next((row for row in data.get("macro", []) if row.get("symbol") in {"BTC-USD", "ETH-USD"}), {})
    return heading("Investor Sentiment") + [
        f"- Institutional Sentiment: **{critical['positioning_bias']}**",
        "- Retail Sentiment: inferred as mixed/high beta; validate with crypto and speculative growth behavior.",
        f"- Options / Volatility Signal: **{regime.get('volatility_state', 'unknown')}**",
        f"- Risk Appetite Signal: **{regime.get('risk_appetite_state', 'unknown')}**",
        f"- Crypto Proxy: `{btc.get('symbol', 'n/a')}` pct_chg `{fmt(btc.get('pct_chg'))}`",
        f"- Conclusion: {critical['sentiment_view']}",
    ]


def render_buy_list(data: dict) -> list[str]:
    candidates = ["CIBR", "SMH", "SOXX", "XLV", "XAR", "GRID", "NLR", "QQQ"]
    for signal in data.get("opportunities", [])[:5]:
        ticker = signal.get("ticker")
        if ticker and ticker not in candidates:
            candidates.append(ticker)
    return heading("Tactical Buy List") + [
        f"- Accumulation candidates: **{comma(candidates)}**",
        "- Preferred entry style: buy pullbacks, avoid momentum chasing, scale in by tranche.",
        "- Add only if Market Regime 2.0 confidence and sector rotation confirm.",
    ]


def render_reduce_list(data: dict) -> list[str]:
    groups = allocation_groups(data)
    weak = [row["sector"] for row in groups["underweight"][:6]]
    return heading("Tactical Sell / Reduce List") + [
        f"- Reduce into strength: **{comma(weak, 'weak tactical sectors')}**",
        "- Avoid chasing: semis after sharp rebound, parabolic momentum names, low-quality speculative growth.",
        "- Trim crowded AI beta if volatility remains stressed and breadth fails to broaden.",
    ]


def render_appendix(data: dict) -> list[str]:
    lines = heading("Appendix / Raw Diagnostics")
    old = first(data.get("regime") or [])
    v2 = first(data.get("regime_v2") or [])
    lines.extend(
        [
            "### Old Market Regime Model",
            f"- Regime: {old.get('regime_label', 'n/a')}",
            f"- Confidence: {fmt(old.get('confidence_score'), 4)}",
            "",
            "### Market Regime 2.0 Details",
            f"- Regime: {v2.get('market_regime', 'n/a')}",
            f"- Phase: {v2.get('market_phase', 'n/a')}",
            f"- Drivers: {v2.get('drivers', 'n/a')}",
            "",
        ]
    )
    lines.extend(render_sector_ranking(data, limit=None))
    lines.extend(["", "### Watchlist Commentary", ""])
    for row in data.get("watchlist", [])[:20]:
        lines.append(f"- {row.get('ticker')}: close `{fmt(row.get('close'))}`, pct_chg `{fmt(row.get('pct_chg'))}`")
    return lines


def render_daily_house_view_report(
    data: dict,
    *,
    qwen_overlay: dict | None = None,
    include_appendix: bool = False,
) -> str:
    generated_at = data.get("generated_at") or datetime.now(timezone.utc)
    window_hours = int(data.get("window_hours") or 24)
    lines = generated_header("Institutional Market Intelligence Report", generated_at, window_hours)
    sections = [
        render_executive_summary(data),
        render_market_snapshot(data),
        render_market_bullishness(data),
        render_investor_sentiment(data),
        render_sector_ranking(data),
        render_opportunities(data),
        render_tactical_positioning(data),
        render_buy_list(data),
        render_reduce_list(data),
        render_risks(data),
        render_top_news(data, limit=10),
        render_risk_management(data),
        render_final_house_view(data),
    ]
    for section in sections:
        lines.extend(section)
        lines.append("")
    if qwen_overlay:
        lines.extend(
            heading("Qwen Analyst Overlay")
            + [
                f"- Market view: **{qwen_overlay.get('market_view', 'unclear')}**",
                f"- Positioning: **{qwen_overlay.get('positioning', 'selective_risk')}**",
                f"- Risk level: **{qwen_overlay.get('risk_level', 'moderate')}**",
                f"- Summary: {qwen_overlay.get('summary', '')}",
                f"- Top risk: {qwen_overlay.get('top_risk', '')}",
                f"- Top opportunity: {qwen_overlay.get('top_opportunity', '')}",
            ]
        )
        lines.append("")
    if include_appendix:
        lines.extend(render_appendix(data))
    return "\n".join(lines).rstrip() + "\n"


def render_market_pulse_report(data: dict, *, qwen_overlay: dict | None = None) -> str:
    generated_at = data.get("generated_at") or datetime.now(timezone.utc)
    window_hours = int(data.get("window_hours") or 3)
    critical = ensure_critical_view(data)
    regime = first(data.get("regime_v2") or [])
    lines = generated_header("3-Hour Market Pulse Report", generated_at, window_hours)
    lines.extend(
        heading("Market Pulse Summary")
        + [
            f"- Dominant regime: **{critical['market_view']}**",
            f"- Confidence: **{confidence_label(flt(regime.get('confidence')))}**",
            f"- Market character: {market_character(data)}",
            f"- Portfolio stance: **{critical['positioning_bias']}**",
        ]
    )
    if qwen_overlay:
        lines.extend(["", f"Qwen read: {qwen_overlay.get('summary', '')}"])
    lines.extend([""])
    rotation_rows = data.get("sector_rotation") or []
    rotation_leaders = (
        comma([row.get("sector_name") for row in rank_sector_views(data)[:3]])
        if rotation_rows
        else "no 3-hour sector rotation rows are available yet"
    )
    lines.extend(
        heading("What Changed in Last 3 Hours")
        + [
            f"- Top macro moves: {comma([row.get('symbol') for row in data.get('macro', [])[:5]])}",
            f"- Highest risk news signals: {comma([row.get('dimension_value') for row in sorted(data.get('news_signals', []), key=lambda row: flt(row.get('risk_score')), reverse=True)[:5]])}",
            f"- Sector rotation leaders: {rotation_leaders}",
        ]
    )
    lines.extend([""])
    lines.extend(render_top_news(data, limit=5))
    lines.extend([""])
    lines.extend(
        heading("Market Sentiment")
        + [
            f"- Risk stance: **{critical['positioning_bias']}**",
            "- Bullish factors: secular leadership and any stable-positive momentum.",
            "- Bearish factors: low confidence, stressed volatility, and empty single-name scanner.",
            f"- Conclusion: {critical['sentiment_view']}",
        ]
    )
    lines.extend([""])
    ranked = rank_sector_views(data) if rotation_rows else []
    top = ranked[:3]
    bottom = list(reversed(ranked))[0:3]
    lines.extend(
        heading("Sector Pulse")
        + [
            f"- Top sectors: {comma([row['sector'] for row in top], 'no 3-hour rotation data')}",
            f"- Bottom sectors: {comma([row['sector'] for row in bottom], 'no 3-hour rotation data')}",
            "- Rotation note: favor confirmed leaders and avoid chasing sharp rebounds.",
        ]
    )
    lines.extend([""])
    lines.extend(
        heading("Tactical Action Notes")
        + [
            "- Add on pullback: Cybersecurity, Healthcare, selective Semiconductors.",
            "- Hold: Technology and Defense unless rotation improves.",
            "- Trim into strength: weak tactical sectors and crowded speculative beta.",
            "- Avoid chasing: broad beta while confidence remains low.",
        ]
    )
    lines.extend([""])
    lines.extend(heading("Risk Watch") + render_risks(data, limit=3)[2:])
    return "\n".join(lines).rstrip() + "\n"
