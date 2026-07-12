"""Markdown renderer for deterministic rule-based market update reports."""

from __future__ import annotations

import json
import math
from pathlib import Path

from db_builder.contradiction_audit import audit_report_scores
from db_builder.etf_flow.report_adapter import etf_flow_report_lines
from db_builder.market_dispersion import compute_broad_market_dispersion, compute_sector_constituent_dispersion
from db_builder.market_strength import flt
from db_builder.news_scoring import score_news
from db_builder.rule_based_regime import compute_confidence, compute_regime
from db_builder.sector_strength import rank_sectors
from db_builder.sector_theme_alignment import align_sector_themes
from db_builder.theme_strength import rank_themes


def fmt(value, digits: int = 2) -> str:
    try:
        numeric = float(value)
        if math.isnan(numeric) or math.isinf(numeric):
            return "n/a"
        return str(round(numeric, digits))
    except (TypeError, ValueError):
        return "n/a" if value is None else str(value)


def fmt_money(value) -> str:
    try:
        numeric = float(value)
        if math.isnan(numeric) or math.isinf(numeric):
            return "n/a"
        sign = "-" if numeric < 0 else ""
        return f"{sign}${abs(numeric):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def table(headers: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return lines


def score_all(data: dict) -> dict:
    technicals = data.get("technicals", [])
    macro = data.get("macro", [])
    news_rows = data.get("news", [])
    news_signals = data.get("news_signals", [])
    etf_flow = data.get("etf_flow_analytics", {})
    flow_by_exposure = {
        row.get("exposure_id"): row
        for row in (etf_flow.get("exposures") or etf_flow.get("market_segments") or [])
        if row.get("exposure_id")
    }
    from db_builder.market_strength import compute_market_strength

    market_strength = compute_market_strength(technicals)
    regime = compute_regime(technicals, macro, news_rows, market_strength, etf_flow)
    confidence = compute_confidence(regime, market_strength, technicals, macro, news_rows)
    sectors = rank_sectors(technicals, news_signals, flow_by_exposure)
    themes = rank_themes(technicals, news_signals, flow_by_exposure)
    sector_theme_alignment = align_sector_themes(sectors, themes)
    news = score_news(news_rows)
    broad_dispersion = compute_broad_market_dispersion(technicals)
    sector_dispersion = compute_sector_constituent_dispersion(data.get("sp500_technicals", []))
    scores = {
        "market_strength": market_strength,
        "regime": regime,
        "confidence": confidence,
        "sectors": sectors,
        "themes": themes,
        "sector_theme_alignment": sector_theme_alignment,
        "news": news,
        "broad_dispersion": broad_dispersion,
        "sector_dispersion": sector_dispersion,
    }
    scores["audit_flags"] = audit_report_scores(scores)
    return scores


def _top_macro_rows(macro: list[dict]) -> list[list]:
    wanted = ["^GSPC", "^IXIC", "^RUT", "^VIX", "^MOVE", "^FVX", "^TNX", "^TYX", "DX-Y.NYB", "HYG", "LQD", "JNK", "RSP", "IWF", "IWD", "TLT", "IEF", "SHY", "GC=F", "SI=F", "CL=F", "HG=F"]
    rows = []
    for symbol in wanted:
        row = next((item for item in macro if item.get("symbol") == symbol), {})
        rows.append([symbol, row.get("name", "unavailable"), fmt(row.get("close")), fmt(row.get("pct_chg")), row.get("date", "n/a")])
    return rows


def _macro_row(macro: list[dict], symbol: str) -> dict:
    return next((item for item in macro if item.get("symbol") == symbol), {})


def _direction(value) -> str:
    numeric = flt(value, None)
    if numeric is None:
        return "unavailable"
    if numeric > 0.25:
        return "rising"
    if numeric < -0.25:
        return "falling"
    return "stable"


def _cross_asset_summary(macro: list[dict]) -> list[list]:
    gspc = _macro_row(macro, "^GSPC")
    ixic = _macro_row(macro, "^IXIC")
    rut = _macro_row(macro, "^RUT")
    vix = _macro_row(macro, "^VIX")
    move = _macro_row(macro, "^MOVE")
    ten = _macro_row(macro, "^TNX")
    dxy = _macro_row(macro, "DXY") or _macro_row(macro, "DX-Y.NYB")
    gold = _macro_row(macro, "GC=F")
    silver = _macro_row(macro, "SI=F")
    copper = _macro_row(macro, "HG=F")
    oil = _macro_row(macro, "CL=F")
    credit = _macro_row(macro, "HYG")
    rows = [
        ["Equities", f"S&P 500 {_direction(gspc.get('pct_chg'))}; Nasdaq {_direction(ixic.get('pct_chg'))}; Russell 2000 {_direction(rut.get('pct_chg'))}", "Confirms risk appetite when broad indices rise together; weak small caps would narrow the signal."],
        ["Rates", f"10Y Treasury {_direction(ten.get('pct_chg'))}; MOVE {_direction(move.get('pct_chg'))}", "Rising yields can pressure duration assets; falling MOVE supports calmer bond volatility."],
        ["Dollar", f"DXY proxy {_direction(dxy.get('pct_chg'))}", "A stronger dollar can tighten financial conditions and pressure commodities/emerging-market risk."],
        ["Credit", f"HYG {_direction(credit.get('pct_chg'))}", "High-yield weakness would challenge equity risk-on confirmation."],
        ["Gold", f"Gold {_direction(gold.get('pct_chg'))}", "Gold strength can indicate defensive demand, inflation hedging, or geopolitical concern."],
        ["Silver", f"Silver {_direction(silver.get('pct_chg'))}", "Silver helps distinguish precious-metal demand from industrial/cyclical confirmation when data is available."],
        ["Copper", f"Copper {_direction(copper.get('pct_chg'))}", "Copper strength supports cyclical growth confirmation; weakness would dilute risk-on breadth."],
        ["Oil", f"WTI crude {_direction(oil.get('pct_chg'))}", "Oil spikes can be inflationary risk; falling oil can ease cost pressure but may also flag demand softness."],
        ["Volatility", f"VIX {_direction(vix.get('pct_chg'))}", "Falling VIX supports risk appetite; a volatility spike would reduce confidence in broad risk-on."],
    ]
    return rows


def _headline_importance(row: dict) -> str:
    score = flt(row.get("news_score"))
    if row.get("relevance_type") == "macro" and score >= 45:
        return "High"
    if score >= 35:
        return "Medium"
    return "Low"


def _headline_impact(row: dict) -> str:
    sentiment = row.get("sentiment_label")
    if sentiment == "positive":
        return "Positive"
    if sentiment == "negative":
        return "Negative"
    return "Mixed"


def _headline_assets(row: dict) -> str:
    assets = []
    assets.extend(str(item) for item in (row.get("affected_tickers") or [])[:4])
    assets.extend(str(item) for item in (row.get("related_tickers") or [])[:4])
    themes = [str(item) for item in (row.get("themes") or [])[:3]]
    combined = []
    for item in assets + themes:
        if item and item not in combined:
            combined.append(item)
    if combined:
        return ", ".join(combined[:5])
    return {
        "macro": "Equities, Bonds, USD, Commodities",
        "sector_theme": "Sector/theme basket",
        "single_name": "Single-name equity",
        "noisy": "Unconfirmed narrative",
    }.get(row.get("relevance_type"), "Broad market")


def _headline_implication(row: dict) -> str:
    relevance = row.get("relevance_type")
    impact = _headline_impact(row).lower()
    horizon = _display_horizon(row.get("time_horizon"))
    if relevance == "macro":
        return f"Macro-relevant {impact} signal; use as context for rates, volatility, and index confirmation over the {horizon}."
    if relevance == "sector_theme":
        return f"Theme or sector {impact} signal; requires price and volume confirmation before affecting rankings."
    if relevance == "single_name":
        return f"Single-name {impact} signal; low weight unless it maps to a broader sector or theme."
    if relevance == "noisy":
        return "Low-quality narrative; track for sentiment only and do not let it drive model scores."
    return "General market context; lower weight unless confirmed by price action."


def _display_horizon(value) -> str:
    if not value:
        return "short-term"
    return str(value).replace("_", "-").lower()


def _headline_blocks(rows: list[dict], *, limit: int = 10) -> list[str]:
    lines = []
    for idx, row in enumerate(rows[:limit], start=1):
        title = row.get("title", "Untitled")
        lines.extend(
            [
                f"**{idx}. {title}**",
                f"- Source: {row.get('source_name', 'n/a')}",
                f"- Importance / impact: {_headline_importance(row)} / {_headline_impact(row)}",
                f"- Relevance / horizon: {row.get('relevance_type', 'general')} / {_display_horizon(row.get('time_horizon'))}",
                f"- Affected assets: {_headline_assets(row)}",
                f"- Score: `{fmt(row.get('news_score'))}`",
                f"- Investment implication: {_headline_implication(row)}",
                "",
            ]
        )
    return lines


def _sector_theme_alignment_lines(rows: list[dict]) -> list[str]:
    if not rows:
        return ["No sector/theme alignment rows are available."]
    return table(
        ["Sector", "Related Themes", "Sector Score", "Sector Signal", "Theme Score", "Theme Signal", "Interpretation"],
        [
            [
                row["sector"],
                row["related_themes"],
                fmt(row["sector_score"]),
                row["sector_signal"],
                fmt(row["theme_score"]),
                row["theme_signal"],
                row["interpretation"],
            ]
            for row in rows[:15]
        ],
    )


def _economic_group(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row.get("series_id", ""), []).append(row)
    for values in grouped.values():
        values.sort(key=lambda item: item.get("rn", 99))
    return grouped


def _economic_change(latest: dict, prior: dict | None) -> str:
    if not prior:
        return "n/a"
    change = flt(latest.get("value"), 0) - flt(prior.get("value"), 0)
    unit = str(latest.get("unit", "")).lower()
    if unit == "percent":
        return f"{fmt(change)} pp vs prior"
    if "index" in unit:
        return f"{fmt(change)} index pts vs prior"
    return f"{fmt(change)} {latest.get('unit', '')} vs prior".strip()


def _economic_signal(series_id: str, latest: dict, prior: dict | None) -> str:
    value = flt(latest.get("value"), None)
    change = None if prior is None else flt(latest.get("value"), 0) - flt(prior.get("value"), 0)
    name = str(latest.get("series_name", series_id)).lower()
    if value is None:
        return "No usable value."
    if series_id in {"ICSA", "CCSA"}:
        if change is None:
            return "Claims level is available; trend requires prior observation."
        if change > 0:
            return "Higher claims indicate softer labor-market conditions."
        if change < 0:
            return "Lower claims indicate firmer labor-market conditions."
        return "Claims are stable."
    if series_id == "CIVPART":
        if change is None:
            return "Participation level is available; trend requires prior observation."
        if change > 0:
            return "Higher participation expands labor supply."
        if change < 0:
            return "Lower participation constrains labor supply."
        return "Participation is stable."
    if series_id in {"UNRATE", "CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE", "PPIACO", "DFF", "SOFR", "FEDFUNDS", "BAMLH0A0HYM2", "BAMLC0A0CM", "MORTGAGE30US"} or "unemployment" in name or "inflation" in name:
        if change is None:
            return "Level is available; trend requires prior observation."
        if change > 0:
            return "Higher reading increases macro pressure."
        if change < 0:
            return "Lower reading eases macro pressure."
        return "Stable reading."
    if series_id in {"PAYEMS", "ICSA", "CCSA", "GDPC1", "INDPRO", "RSAFS", "WALCL", "M1SL", "M2SL", "HOUST", "PERMIT", "UMCSENT"} or "gdp growth" in name:
        if change is None:
            return "Level is available; trend requires prior observation."
        if change > 0:
            return "Higher reading supports activity or liquidity."
        if change < 0:
            return "Lower reading signals softer activity or liquidity."
        return "Stable reading."
    if series_id.startswith("ECB:EXR"):
        if change is None:
            return "Latest FX level available."
        if change > 0:
            return "EUR strengthened versus this currency."
        if change < 0:
            return "EUR weakened versus this currency."
        return "FX rate stable."
    return "Latest structural macro observation."


def _economic_rows(economic: list[dict], series_ids: list[str]) -> list[list]:
    grouped = _economic_group(economic)
    rows = []
    for series_id in series_ids:
        observations = grouped.get(series_id, [])
        if not observations:
            continue
        latest = observations[0]
        prior = observations[1] if len(observations) > 1 else None
        rows.append(
            [
                latest.get("series_name", series_id),
                fmt(latest.get("value")),
                latest.get("unit", ""),
                _economic_change(latest, prior),
                latest.get("date", "n/a"),
                _economic_signal(series_id, latest, prior),
            ]
        )
    return rows


def _economic_section_lines(economic: list[dict]) -> list[str]:
    if not economic:
        return ["No economic indicator rows are available locally."]
    sections = [
        ("U.S. Labor", ["PAYEMS", "UNRATE", "ICSA", "CCSA", "CIVPART"]),
        ("U.S. Growth", ["GDPC1", "INDPRO", "RSAFS"]),
        ("U.S. Inflation", [
            "DERIVED:CPI_HEADLINE_SA:MOM", "DERIVED:CPI_HEADLINE_SA:YOY",
            "DERIVED:CPI_CORE_SA:MOM", "DERIVED:CPI_CORE_SA:YOY",
            "DERIVED:CPI_HEADLINE_NSA:YOY", "DERIVED:CPI_CORE_NSA:YOY",
            "DERIVED:PCE_HEADLINE:MOM", "DERIVED:PCE_HEADLINE:YOY",
            "DERIVED:PCE_CORE:MOM", "DERIVED:PCE_CORE:YOY",
            "DERIVED:PPI_HEADLINE:MOM", "DERIVED:PPI_HEADLINE:YOY",
            "DERIVED:PPI_CORE:MOM", "DERIVED:PPI_CORE:YOY",
            "CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE", "PPIFID", "PPIFES",
        ]),
        ("Policy, Liquidity, Credit", ["DFF", "SOFR", "FEDFUNDS", "WALCL", "M1SL", "M2SL", "BAMLH0A0HYM2", "BAMLC0A0CM"]),
        ("Housing and Sentiment", ["HOUST", "PERMIT", "MORTGAGE30US", "UMCSENT"]),
        ("Global Structural Snapshot", [
            "WB:CHN:NY.GDP.MKTP.KD.ZG", "WB:CHN:FP.CPI.TOTL.ZG", "WB:CHN:SL.UEM.TOTL.ZS",
            "WB:JPN:NY.GDP.MKTP.KD.ZG", "WB:JPN:FP.CPI.TOTL.ZG", "WB:JPN:SL.UEM.TOTL.ZS",
            "WB:DEU:NY.GDP.MKTP.KD.ZG", "WB:DEU:FP.CPI.TOTL.ZG", "WB:DEU:SL.UEM.TOTL.ZS",
            "WB:AUS:NY.GDP.MKTP.KD.ZG", "WB:AUS:FP.CPI.TOTL.ZG", "WB:AUS:SL.UEM.TOTL.ZS",
            "WB:EMU:NY.GDP.MKTP.KD.ZG", "WB:EMU:FP.CPI.TOTL.ZG", "WB:EMU:SL.UEM.TOTL.ZS",
        ]),
        ("ECB FX Snapshot", ["ECB:EXR:D.USD.EUR.SP00.A", "ECB:EXR:D.JPY.EUR.SP00.A", "ECB:EXR:D.CNY.EUR.SP00.A", "ECB:EXR:D.AUD.EUR.SP00.A"]),
    ]
    lines = []
    for title, series_ids in sections:
        rows = _economic_rows(economic, series_ids)
        if not rows:
            continue
        lines.extend(["", f"### {title}", ""])
        lines.extend(table(["Indicator", "Latest", "Unit", "Change", "Date", "Rule-Based Interpretation"], rows))
    lines.extend(["", "ABS Australia note: ABS rows are stored locally/Neon, but most series are dimension-coded; only curated series should be promoted into narrative report text."])
    return lines


def _broad_dispersion_lines(dispersion: dict) -> list[str]:
    sector_20d = dispersion.get("sector_20d", {})
    sector_60d = dispersion.get("sector_60d", {})
    lines = [
        f"- Sector ETF 20D dispersion: `{fmt(sector_20d.get('range'))}` points ({sector_20d.get('label', 'n/a')}); leader `{sector_20d.get('leader', 'n/a')}` ({sector_20d.get('leader_label', 'n/a')}) `{fmt(sector_20d.get('leader_return'))}`, laggard `{sector_20d.get('laggard', 'n/a')}` ({sector_20d.get('laggard_label', 'n/a')}) `{fmt(sector_20d.get('laggard_return'))}`",
        f"- Sector ETF 60D dispersion: `{fmt(sector_60d.get('range'))}` points ({sector_60d.get('label', 'n/a')}); leader `{sector_60d.get('leader', 'n/a')}` ({sector_60d.get('leader_label', 'n/a')}) `{fmt(sector_60d.get('leader_return'))}`, laggard `{sector_60d.get('laggard', 'n/a')}` ({sector_60d.get('laggard_label', 'n/a')}) `{fmt(sector_60d.get('laggard_return'))}`",
        "",
    ]
    lines.extend(
        table(
            ["Comparison", "Left", "Right", "20D Spread", "Signal"],
            [
                [
                    row["comparison"],
                    f"{row['left']} ({row.get('left_label', row['left'])})",
                    f"{row['right']} ({row.get('right_label', row['right'])})",
                    fmt(row.get("spread_20d")),
                    row["signal"],
                ]
                for row in dispersion.get("size_style", [])
            ],
        )
    )
    return lines


def _sector_dispersion_lines(rows: list[dict]) -> list[str]:
    if not rows:
        return ["No S&P 500 constituent dispersion rows are available."]
    ok_rows = [row for row in rows if row.get("status") == "ok"]
    insufficient = [row for row in rows if row.get("status") != "ok"]
    lines = [
        "- Breadth 50D / 200D: percentage of active S&P 500 constituents in the sector trading above the 50-day / 200-day moving average.",
        "- Positive 20D: percentage of active sector constituents with positive 20-day return.",
        "- Std 20D: cross-sectional standard deviation of constituent 20-day returns; higher means wider stock-level dispersion.",
        "",
    ]
    if ok_rows:
        lines.extend(
            table(
                [
                    "Sector",
                    "Count",
                    "Breadth 50D",
                    "Breadth 200D",
                    "Positive 20D",
                    "20D Dispersion",
                    "Std 20D",
                    "Label",
                    "Leaders",
                    "Laggards",
                ],
                [
                    [
                        row["sector"],
                        row["constituent_count"],
                        fmt(row.get("breadth_50d_pct")),
                        fmt(row.get("breadth_200d_pct")),
                        fmt(row.get("positive_20d_pct")),
                        fmt(row.get("dispersion_20d")),
                        fmt(row.get("std_20d")),
                        row.get("label", "n/a"),
                        ", ".join(row.get("leaders", [])),
                        ", ".join(row.get("laggards", [])),
                    ]
                    for row in ok_rows
                ],
            )
        )
    if insufficient:
        lines.extend(["", "Insufficient constituent coverage:"])
        lines.extend(
            f"- {row['sector']}: {row['constituent_count']} valid constituents"
            for row in insufficient
        )
    return lines


def _positioning_flow_lines(rows: list[dict]) -> list[str]:
    if not rows:
        return [
            "No positioning/flow signals are available yet.",
            "",
            "Phase 1 sources expected here after ingestion: CFTC COT futures positioning and FINRA daily short-sale volume.",
        ]
    by_source: dict[str, list[dict]] = {}
    for row in rows:
        by_source.setdefault(row.get("source", "Other"), []).append(row)
    lines = [
        "Positioning and flow data is used as confirmation only. FINRA short-sale volume is not short interest.",
        "",
    ]
    if by_source.get("CFTC COT"):
        lines.extend(["### Futures Positioning", ""])
        lines.extend(
            table(
                ["Date", "Asset", "Signal", "Value", "Z", "Percentile", "Interpretation"],
                [
                    [
                        row.get("signal_date", "n/a"),
                        row.get("asset_id", "n/a"),
                        row.get("signal_name", "n/a"),
                        fmt(row.get("signal_value"), 4),
                        fmt(row.get("z_score")),
                        fmt(row.get("percentile")),
                        row.get("interpretation", ""),
                    ]
                    for row in by_source["CFTC COT"][:12]
                ],
            )
        )
        lines.append("")
    etf_flow_rows = by_source.get("ETF daily data") or by_source.get("ETF flow proxy")
    if etf_flow_rows:
        lines.extend(["### ETF Fund Flows", ""])
        lines.append("Net fund flow is estimated from ETF shares outstanding changes multiplied by NAV. Flows are grouped into broad-market, fixed-income/macro, and sector/thematic ETFs.")
        lines.append("")
        for bucket_title in ["Broad Market ETF Flows", "Fixed Income / Macro ETF Flows", "Sector / Thematic ETF Flows"]:
            bucket_rows = [row for row in etf_flow_rows if row.get("flow_bucket") == bucket_title]
            if not bucket_rows:
                continue
            lines.extend([f"**{bucket_title}**", ""])
            lines.extend(
                table(
                    ["Date", "ETF / Segment", "1D Net Flow", "5D Net Flow", "Rule-Based Comment"],
                    [
                        [
                            row.get("signal_date", "n/a"),
                            row.get("display_name") or f"{row.get('asset_id', 'n/a')} - {row.get('asset_group', 'ETF')}",
                            fmt_money(row.get("signal_value")),
                            fmt_money(row.get("z_score")),
                            row.get("flow_comment", ""),
                        ]
                        for row in bucket_rows[:12]
                    ],
                )
            )
            lines.append("")
    if by_source.get("FINRA short-sale volume"):
        lines.extend(["### Short-Sale Pressure", ""])
        lines.append("Curated to broad index ETFs, sector/theme ETFs, Mag 7, and high-beta chip names. FINRA short-sale volume is not short interest.")
        lines.append("")
        lines.extend(
            table(
                ["Date", "Ticker", "Group", "Ratio", "Z", "Price Move", "Market Implication"],
                [
                    [
                        row.get("signal_date", "n/a"),
                        row.get("asset_id", "n/a"),
                        row.get("asset_group", "n/a"),
                        fmt(row.get("signal_value"), 4),
                        fmt(row.get("z_score")),
                        fmt(row.get("pct_chg")),
                        row.get("market_implication") or row.get("interpretation", ""),
                    ]
                    for row in by_source["FINRA short-sale volume"][:15]
                ],
            )
        )
        lines.append("")
    missing_sections = [
        "Official ETF / fund flows: current report uses shares-outstanding-derived net fund flow estimates from free ETF metadata.",
        "Institutional Ownership: not available until SEC 13F ingestion is implemented.",
        "Crowding / Squeeze Risks: initial coverage uses CFTC crowded positioning and FINRA elevated short-sale volume only.",
        "Grouped exposure flow reliability: use issuer coverage and availability status before treating ETF flow as confirmation.",
    ]
    lines.extend(["### Deferred Flow Sections", ""])
    lines.extend(f"- {item}" for item in missing_sections)
    return lines


def _sector_name_aliases(name: str) -> list[str]:
    aliases = {
        "Technology": ["Information Technology"],
        "Healthcare": ["Health Care"],
    }
    return [name, *aliases.get(name, [])]


def _constituent_leaders_for_sector(sector_name: str, sector_dispersion: list[dict], field: str) -> list[str]:
    for candidate in _sector_name_aliases(sector_name):
        row = next((item for item in sector_dispersion if item.get("sector") == candidate and item.get("status") == "ok"), None)
        if row:
            return row.get(field, [])
    return []


def render_rule_based_market_update(data: dict, scores: dict | None = None) -> str:
    scores = scores or score_all(data)
    generated_at = data.get("generated_at")
    window_hours = data.get("window_hours", 24)
    regime = scores["regime"]
    strength = scores["market_strength"]
    confidence = scores["confidence"]
    sectors = scores["sectors"]
    themes = scores["themes"]
    sector_theme_alignment = scores.get("sector_theme_alignment", [])
    news = scores["news"]
    flags = scores["audit_flags"]
    broad_dispersion = scores.get("broad_dispersion", {})
    sector_dispersion = scores.get("sector_dispersion", [])
    etf_regime = (data.get("etf_flow_analytics") or {}).get("flow_regime") or {}

    lines = [
        "# Rule-Based Institutional Market Update",
        "",
        f"Generated at: {generated_at.isoformat() if generated_at else 'n/a'}",
        f"Window: {window_hours}h",
        "",
        "## Executive Dashboard",
        "",
        f"- Regime score: **{fmt(regime['score'])} / 100** ({regime['label']})",
        f"- Market strength: **{fmt(strength['score'])} / 100** ({strength['label']})",
        f"- Evidence quality: **{fmt(confidence['score'])} / 100**",
        f"- ETF flow contribution: **{fmt(etf_regime.get('flow_regime_score') or etf_regime.get('score'))} / 100**, reliability **{fmt(etf_regime.get('flow_regime_confidence') or etf_regime.get('confidence'))} / 100**",
        f"- Breadth: **{strength['breadth']['label']}**; above 50DMA `{fmt(strength['breadth']['above_50d_pct'])}%`, above 200DMA `{fmt(strength['breadth']['above_200d_pct'])}%`",
        f"- Top sector score: **{sectors[0]['sector']}** `{fmt(sectors[0]['score'])}`" if sectors else "- Top sector score: unavailable",
        f"- Top theme score: **{themes[0]['theme']}** `{fmt(themes[0]['score'])}`" if themes else "- Top theme score: unavailable",
        "",
        "## Market Regime Score",
        "",
    ]
    lines.extend(table(["Sub-score", "Value"], [[key, fmt(value)] for key, value in regime["subscores"].items()]))
    lines.extend(["", "Positive contributors: " + ", ".join(regime["positive_contributors"] or ["none"])])
    lines.extend(["Negative contributors: " + ", ".join(regime["negative_contributors"] or ["none"])])
    if regime["missing_data_warnings"]:
        lines.append("Missing-data warnings: " + ", ".join(regime["missing_data_warnings"]))

    lines.extend(["", "## Market Strength Score", ""])
    lines.extend(table(["Component", "Score"], [[key, fmt(value)] for key, value in strength["decomposition"].items()]))

    lines.extend(["", "## Evidence Quality / Confidence", ""])
    lines.extend(
        [
            f"- Confidence score: `{fmt(confidence['score'])}`",
            f"- Agreement ratio: `{fmt(confidence['agreement_ratio'], 4)}`",
            f"- Contradiction count: `{confidence['contradiction_count']}`",
            f"- Missing indicators: {', '.join(confidence['missing_indicators'][:10]) if confidence['missing_indicators'] else 'none'}",
            f"- Warning flags: {', '.join(confidence['warning_flags']) if confidence['warning_flags'] else 'none'}",
        ]
    )

    lines.extend(["", "## Cross-Asset Confirmation", ""])
    lines.extend(table(["Area", "Signal", "Interpretation"], _cross_asset_summary(data.get("macro", []))))
    lines.extend(["", "### Macro Snapshot", ""])
    lines.extend(table(["Symbol", "Name", "Close", "Pct Chg", "Date"], _top_macro_rows(data.get("macro", []))))

    lines.extend(["", "## Market Dispersion Analysis", ""])
    lines.extend(_broad_dispersion_lines(broad_dispersion))

    lines.extend(["", "## Sector Constituent Dispersion", ""])
    lines.extend(_sector_dispersion_lines(sector_dispersion))

    lines.extend(["", "## Economic Data Snapshot"])
    lines.extend(_economic_section_lines(data.get("economic", [])))

    lines.extend(["", "## Sector and Theme Leadership", ""])
    lines.extend(["### Official Sector Strength", ""])
    lines.append("Supporting and detracting names are the top/bottom S&P 500 constituents by 20D return when constituent coverage is available; otherwise the report falls back to related ETFs.")
    lines.append("")
    lines.extend(
        table(
            ["Rank", "Sector", "Score", "Trend", "Momentum", "Stock Breadth", "ETF Flow", "Flow Reliability", "3M RS", "Supporting / Leaders", "Detracting / Laggards"],
            [
                [
                    idx,
                    row["sector"],
                    fmt(row["score"]),
                    row["trend_label"],
                    row["momentum_label"],
                    row["breadth_label"],
                    fmt(row["components"].get("grouped_etf_flow")),
                    fmt(row.get("flow_reliability")),
                    fmt(row["three_month_relative_strength"]),
                    ", ".join(_constituent_leaders_for_sector(row["sector"], sector_dispersion, "leaders") or row["top_supporting_tickers"]),
                    ", ".join(_constituent_leaders_for_sector(row["sector"], sector_dispersion, "laggards") or row["top_detracting_tickers"]),
                ]
                for idx, row in enumerate(sectors[:15], start=1)
            ],
        )
    )

    lines.extend(["", "### Thematic Strength", ""])
    lines.extend(table(["Rank", "Theme", "Score", "Setup", "ETF Flow", "Flow Reliability", "Dispersion", "Price", "News"], [[idx, row["theme"], fmt(row["score"]), row["setup_label"], fmt(row["components"].get("grouped_etf_flow")), fmt(row.get("flow_reliability")), fmt(row["dispersion"]), row["price_confirmation"], row["news_confirmation"]] for idx, row in enumerate(themes[:15], start=1)]))
    improving = sorted(themes, key=lambda row: flt(row["components"].get("relative_return")), reverse=True)[:5]
    deteriorating = sorted(themes, key=lambda row: flt(row["components"].get("relative_return")))[:5]
    lines.extend(["", f"- Top 5 improving themes: {', '.join(row['theme'] for row in improving)}"])
    lines.append(f"- Top 5 deteriorating themes: {', '.join(row['theme'] for row in deteriorating)}")
    strong_news_weak_price = ", ".join(row["theme"] for row in themes if row["news_confirmation"] and not row["price_confirmation"]) or "none"
    strong_price_weak_news = ", ".join(row["theme"] for row in themes if row["price_confirmation"] and not row["news_confirmation"]) or "none"
    lines.append(f"- Strong news but weak price confirmation: {strong_news_weak_price}")
    lines.append(f"- Strong price but weak news confirmation: {strong_price_weak_news}")

    lines.extend(["", "### Sector / Theme Alignment", ""])
    lines.extend(_sector_theme_alignment_lines(sector_theme_alignment))

    lines.extend(["", "## Three-Month Outperformance Setup", ""])
    lines.extend(table(["Rank", "Theme", "Score", "Classification", "Drivers", "Invalidation Triggers"], [[idx, row["theme"], fmt(row["setup_score"]), row["setup_label"], ", ".join(row["setup_drivers"]), ", ".join(row["invalidation_triggers"])] for idx, row in enumerate(sorted(themes, key=lambda row: row["setup_score"], reverse=True)[:12], start=1)]))

    lines.extend(["", "## Breadth and Participation", ""])
    breadth = strength["breadth"]
    lines.extend([f"- Above 50DMA: `{fmt(breadth['above_50d_pct'])}%`", f"- Above 200DMA: `{fmt(breadth['above_200d_pct'])}%`", f"- Positive 20D return: `{fmt(breadth['positive_20d_pct'])}%`"])

    lines.extend(["", "## Volatility and Risk Signals", ""])
    lines.extend([f"- {driver}" for driver in regime["drivers"] if "VIX" in driver] or ["- VIX unavailable from macro snapshot."])

    lines.extend(["", "## News Analytics", ""])
    lines.extend([f"- Sentiment counts: {news['sentiment_counts']}", f"- News confirmation score: `{fmt(news['score'])}`"])
    lines.extend(["", "### Top Market-Moving Headlines", ""])
    lines.extend(_headline_blocks(news["top_headlines"], limit=10))
    lines.extend(["", "### Headline Quality Checks", ""])
    lines.append("Noisy headline list: " + ", ".join(row.get("title", "Untitled")[:50] for row in news["noisy_headlines"]) if news["noisy_headlines"] else "No noisy headlines detected by current rules.")

    lines.extend(["", "## Positioning & Flow Dashboard", ""])
    lines.extend(_positioning_flow_lines(data.get("positioning_flow", [])))

    lines.extend([""])
    lines.extend(etf_flow_report_lines(data.get("etf_flow_analytics", {})))

    lines.extend(["", "## Contradiction / Audit Flags", ""])
    if flags:
        lines.extend(table(["Severity", "Section", "Issue", "Deterministic Fix"], [[row["severity"], row["section"], row["issue"], row["deterministic_fix"]] for row in flags]))
    else:
        lines.append("No contradiction flags were triggered by current deterministic rules.")

    lines.extend(["", "## Data Quality Notes", ""])
    lines.extend([f"- Technical rows loaded: `{len(data.get('technicals', []))}`", f"- S&P 500 constituent technical rows loaded: `{len(data.get('sp500_technicals', []))}`", f"- Macro rows loaded: `{len(data.get('macro', []))}`", f"- Economic rows loaded: `{len(data.get('economic', []))}`", f"- News rows loaded: `{len(data.get('news', []))}`", f"- Positioning/flow rows loaded: `{len(data.get('positioning_flow', []))}`"])
    for warning in confidence["warning_flags"][:8]:
        lines.append(f"- {warning}")
    return "\n".join(lines).rstrip() + "\n"


def save_rule_based_report(markdown: str, *, reports_dir: Path | None = None, generated_at=None) -> Path:
    from db_builder.investment_report import _project_root
    from datetime import datetime

    output_dir = reports_dir or (_project_root() / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = (generated_at or datetime.now()).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"rule_based_market_update_{timestamp}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def scores_to_json(scores: dict) -> str:
    return json.dumps(scores, indent=2, default=str)
