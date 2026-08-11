"""Continuous deterministic market regime scoring."""

from __future__ import annotations

from db_builder.market_strength import clamp, flt, score_above_ma, score_return_momentum
from db_builder.news_scoring import score_news
from db_builder.rule_based_config import scoring_weights


def macro_by_symbol(rows: list[dict], symbol: str) -> dict:
    return next((row for row in rows if row.get("symbol") == symbol), {})


def regime_label(score: float) -> str:
    if score >= 80:
        return "Strong Risk-On"
    if score >= 65:
        return "Moderate Risk-On"
    if score >= 55:
        return "Mild Risk-On"
    if score >= 45:
        return "Mixed / Rotation"
    if score >= 35:
        return "Mild Risk-Off"
    if score >= 20:
        return "Moderate Risk-Off"
    return "Defensive / Risk-Off"


def volatility_score(macro_rows: list[dict]) -> tuple[float, list[str]]:
    vix = macro_by_symbol(macro_rows, "^VIX")
    if not vix:
        return 50.0, ["VIX missing; volatility neutral"]
    close = flt(vix.get("close"), 20)
    pct = flt(vix.get("pct_chg"))
    score = 70.0
    if close >= 30 or pct >= 10:
        score = 20.0
    elif close >= 22 or pct >= 4:
        score = 35.0
    elif close <= 16 and pct <= 0:
        score = 80.0
    return score, [f"VIX close={round(close, 2)} pct_chg={round(pct, 2)}"]


def rates_score(macro_rows: list[dict]) -> tuple[float, list[str]]:
    five_row = macro_by_symbol(macro_rows, "US5YT=X") or macro_by_symbol(macro_rows, "^FVX")
    ten_row = macro_by_symbol(macro_rows, "US10YT=X") or macro_by_symbol(macro_rows, "^TNX")
    thirty_row = macro_by_symbol(macro_rows, "US30YT=X") or macro_by_symbol(macro_rows, "^TYX")
    five = flt(five_row.get("close"), None)
    ten = flt(ten_row.get("close"), None)
    thirty = flt(thirty_row.get("close"), None)
    ten_chg = flt(ten_row.get("pct_chg"))
    if ten is None:
        return 50.0, ["10Y Treasury missing; rates neutral"]
    score = 55.0
    if ten_chg > 1.5:
        score -= 12
    elif ten_chg < -1.5:
        score += 8
    if five is not None and ten < five:
        score -= 8
    if thirty is not None and ten is not None and thirty > ten:
        score += 3
    return clamp(score), [f"5Y={five}", f"10Y={ten} pct_chg={round(ten_chg, 2)}", f"30Y={thirty}"]


def commodity_score(macro_rows: list[dict]) -> tuple[float, list[str]]:
    copper = flt(macro_by_symbol(macro_rows, "HG=F").get("pct_chg"), None)
    silver = flt(macro_by_symbol(macro_rows, "SI=F").get("pct_chg"), None)
    oil = flt(macro_by_symbol(macro_rows, "CL=F").get("pct_chg"), None)
    gold = flt(macro_by_symbol(macro_rows, "GC=F").get("pct_chg"), None)
    score = 50.0
    drivers = []
    if copper is not None:
        score += 8 if copper > 0.5 else -5 if copper < -0.5 else 0
        drivers.append(f"copper pct_chg={round(copper, 2)}")
    if silver is not None:
        score += 5 if silver > 0.5 else -3 if silver < -0.5 else 0
        drivers.append(f"silver pct_chg={round(silver, 2)}")
    if oil is not None:
        score += 4 if 0 <= oil <= 2 else -4 if oil > 4 else -2 if oil < -2 else 0
        drivers.append(f"oil pct_chg={round(oil, 2)}")
    if gold is not None and gold > 1:
        score -= 4
        drivers.append(f"gold pct_chg={round(gold, 2)}")
    return clamp(score), drivers or ["commodity data missing; neutral"]


def dollar_score(macro_rows: list[dict]) -> tuple[float, list[str]]:
    dxy = macro_by_symbol(macro_rows, "DXY") or macro_by_symbol(macro_rows, "DX-Y.NYB")
    if not dxy:
        return 50.0, ["DXY missing; dollar neutral"]
    pct = flt(dxy.get("pct_chg"))
    score = 55 - pct * 5
    return clamp(score), [f"DXY pct_chg={round(pct, 2)}"]


def _etf_flow_score(etf_flow: dict | None) -> tuple[float, list[str]]:
    if not etf_flow:
        return 50.0, ["grouped ETF flow unavailable; neutral"]
    regime = etf_flow.get("flow_regime") or {}
    score = flt(regime.get("flow_regime_score") or regime.get("score"), 50.0)
    reliability = flt(regime.get("flow_regime_confidence") or regime.get("confidence"), 0.0)
    adjusted = clamp(50.0 + (score - 50.0) * reliability / 100.0)
    return adjusted, [f"grouped ETF flow score={round(score, 2)} reliability={round(reliability, 2)}"]


def compute_regime(technical_rows: list[dict], macro_rows: list[dict], news_rows: list[dict], market_strength: dict, etf_flow: dict | None = None) -> dict:
    weights = scoring_weights()["market_regime"]
    equity_rows = [row for row in technical_rows if row.get("ticker") in {"SPY", "QQQ", "IWM", "SMH"}]
    equity_trend = sum(score_above_ma(row) for row in equity_rows) / len(equity_rows) if equity_rows else 50.0
    equity_momentum = sum(score_return_momentum(row) for row in equity_rows) / len(equity_rows) if equity_rows else 50.0
    breadth = flt((market_strength.get("breadth") or {}).get("score"), 50)
    vol, vol_drivers = volatility_score(macro_rows)
    rates, rates_drivers = rates_score(macro_rows)
    dollar, dollar_drivers = dollar_score(macro_rows)
    commodities, commodity_drivers = commodity_score(macro_rows)
    etf_flow_subscore, etf_flow_drivers = _etf_flow_score(etf_flow)
    news = score_news(news_rows)
    credit_proxy = 50.0
    subscores = {
        "equity_trend": equity_trend,
        "equity_momentum": equity_momentum,
        "market_breadth": breadth,
        "volatility": vol,
        "rates_yield_curve": rates,
        "credit_proxy": credit_proxy,
        "dollar_fx": dollar,
        "commodity_confirmation": commodities,
        "etf_flow": etf_flow_subscore,
        "news_confirmation": news["score"],
    }
    score = round(sum(subscores[key] * weights[key] for key in weights), 4)
    contributors = sorted(subscores.items(), key=lambda item: item[1], reverse=True)
    missing = []
    if not (macro_by_symbol(macro_rows, "DXY") or macro_by_symbol(macro_rows, "DX-Y.NYB")):
        missing.append("DXY unavailable")
    if not equity_rows:
        missing.append("core equity technical rows unavailable")
    return {
        "score": score,
        "label": regime_label(score),
        "subscores": {key: round(value, 4) for key, value in subscores.items()},
        "positive_contributors": [f"{key}={round(value, 2)}" for key, value in contributors if value >= 60][:6],
        "negative_contributors": [f"{key}={round(value, 2)}" for key, value in reversed(contributors) if value <= 45][:6],
        "drivers": vol_drivers + rates_drivers + dollar_drivers + commodity_drivers + etf_flow_drivers,
        "missing_data_warnings": missing,
        "news": news,
    }


def compute_confidence(regime: dict, market_strength: dict, technical_rows: list[dict], macro_rows: list[dict], news_rows: list[dict]) -> dict:
    expected_macro = {
        "^GSPC", "^IXIC", "^RUT", "^VIX", "^SKEW", "^MOVE",
        "US2YT=X", "US5YT=X", "US10YT=X", "US30YT=X",
        "GC=F", "CL=F", "HG=F", "HYG", "LQD", "RSP",
    }
    present_macro = {row.get("symbol") for row in macro_rows}
    if "DX-Y.NYB" in present_macro:
        present_macro.add("DXY")
    completeness = 100 * (len(expected_macro & present_macro) / len(expected_macro))
    missing_indicators = market_strength.get("missing_indicators", [])
    indicator_penalty = min(len(missing_indicators) * 3, 30)
    subscore_values = list((regime.get("subscores") or {}).values())
    bullish = sum(value >= 55 for value in subscore_values)
    bearish = sum(value <= 45 for value in subscore_values)
    agreement_ratio = max(bullish, bearish) / len(subscore_values) if subscore_values else 0
    contradiction_count = min(bullish, bearish)
    score = clamp((completeness * 0.35) + (agreement_ratio * 100 * 0.35) + (min(len(news_rows), 20) / 20 * 100 * 0.15) + 80 * 0.15 - indicator_penalty)
    return {
        "score": round(score, 4),
        "agreement_ratio": round(agreement_ratio, 4),
        "contradiction_count": contradiction_count,
        "missing_indicators": missing_indicators,
        "warning_flags": regime.get("missing_data_warnings", []) + (["signal disagreement elevated"] if contradiction_count >= 4 else []),
    }
