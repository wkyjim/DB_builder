"""Deterministic theme strength and outperformance setup scoring."""

from __future__ import annotations

from statistics import median

from db_builder.market_strength import avg, clamp, flt, score_macd
from db_builder.rule_based_config import THEME_BASKETS, scoring_weights


def _rows_for(rows: list[dict], tickers: list[str]) -> list[dict]:
    wanted = set(tickers)
    return [row for row in rows if row.get("ticker") in wanted]


def _row(rows: list[dict], ticker: str) -> dict:
    return next((row for row in rows if row.get("ticker") == ticker), {})


def _theme_news(name: str, news_signals: list[dict]) -> tuple[float, float]:
    matches = [row for row in news_signals if name.lower() in str(row.get("dimension_value", "")).lower()]
    if not matches:
        return 50.0, 0.0
    row = matches[0]
    intensity = min(flt(row.get("article_count")) * 10, 100)
    ratio = clamp(50 + flt(row.get("opportunity_score")) * 0.25 - flt(row.get("risk_score")) * 0.20)
    return intensity, ratio


def setup_label(score: float) -> str:
    if score >= 75:
        return "Strong outperformance setup"
    if score >= 60:
        return "Positive setup"
    if score >= 45:
        return "Neutral / watchlist"
    if score >= 30:
        return "Weak setup"
    return "Underperformance risk"


def score_theme(name: str, rows: list[dict], news_signals: list[dict]) -> dict:
    basket = THEME_BASKETS[name]
    theme_rows = _rows_for(rows, basket)
    spy = _row(rows, "SPY")
    qqq = _row(rows, "QQQ")
    spy_20 = flt(spy.get("return_20d"))
    spy_60 = flt(spy.get("return_60d"))
    qqq_60 = flt(qqq.get("return_60d"))
    returns_20 = [flt(row.get("return_20d")) for row in theme_rows]
    returns_60 = [flt(row.get("return_60d")) for row in theme_rows]
    breadth_50 = avg([100.0 if flt(row.get("close")) >= flt(row.get("ma_50"), 10**9) else 0.0 for row in theme_rows])
    breadth_200 = avg([100.0 if flt(row.get("close")) >= flt(row.get("ma_200"), 10**9) else 0.0 for row in theme_rows])
    news_intensity, headline_ratio = _theme_news(name, news_signals)
    dispersion = max(returns_20) - min(returns_20) if returns_20 else 0
    components = {
        "equal_weight_return": clamp(50 + avg(returns_20, 0)),
        "relative_return": clamp(50 + (avg(returns_60, 0) - spy_60)),
        "breadth_50d": breadth_50,
        "breadth_200d": breadth_200,
        "rsi": clamp(median([flt(row.get("rsi_14"), 50) for row in theme_rows]) if theme_rows else 50),
        "macd": avg([score_macd(row) for row in theme_rows]),
        "volume": avg([clamp(flt(row.get("volume_ratio_20"), 1) * 50) for row in theme_rows]),
        "volatility_adjusted_return": avg([clamp(50 + flt(row.get("return_20d")) / max(flt(row.get("volatility_20d"), 1), 1) * 10) for row in theme_rows]),
        "news_intensity": news_intensity,
        "headline_ratio": headline_ratio,
    }
    weights = scoring_weights()["theme_strength"]
    score = round(sum(components[key] * weights[key] for key in weights), 4)
    setup_components = {
        "relative_strength_20d": clamp(50 + (avg(returns_20, 0) - spy_20)),
        "relative_strength_60d": clamp(50 + (avg(returns_60, 0) - spy_60)),
        "trend_persistence": (breadth_50 + breadth_200) / 2,
        "breadth": (breadth_50 + breadth_200) / 2,
        "volatility_adjusted_momentum": components["volatility_adjusted_return"],
        "volume_accumulation": components["volume"],
        "drawdown_recovery": clamp(50 + avg([flt(row.get("pct_chg")) for row in theme_rows], 0)),
        "news_acceleration": news_intensity,
        "downside_volatility": clamp(100 - avg([flt(row.get("volatility_20d"), 20) for row in theme_rows])),
        "relative_vs_qqq": clamp(50 + (avg(returns_60, 0) - qqq_60)),
    }
    setup_weights = scoring_weights()["outperformance_setup"]
    setup_score = round(sum(setup_components[key] * setup_weights[key] for key in setup_weights), 4)
    return {
        "theme": name,
        "basket": basket,
        "score": score,
        "setup_score": setup_score,
        "setup_label": setup_label(setup_score),
        "components": {key: round(value, 4) for key, value in components.items()},
        "setup_drivers": [key for key, value in setup_components.items() if value >= 60][:5],
        "invalidation_triggers": ["relative strength below SPY", "breadth below 45%", "news turns negative"],
        "data_caveats": [] if theme_rows else ["theme basket rows unavailable"],
        "dispersion": round(dispersion, 4),
        "price_confirmation": score >= 60,
        "news_confirmation": headline_ratio >= 60,
    }


def rank_themes(rows: list[dict], news_signals: list[dict]) -> list[dict]:
    ranked = [score_theme(name, rows, news_signals) for name in THEME_BASKETS]
    return sorted(ranked, key=lambda row: row["score"], reverse=True)
