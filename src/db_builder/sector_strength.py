"""Deterministic sector strength ranking."""

from __future__ import annotations

from db_builder.market_strength import avg, clamp, flt, score_above_ma, score_macd, score_rsi, score_volume, trend_label
from db_builder.rule_based_config import SECTOR_ETF_MAP, scoring_weights


def _rows_for(rows: list[dict], tickers: list[str]) -> list[dict]:
    wanted = set(tickers)
    return [row for row in rows if row.get("ticker") in wanted]


def _row(rows: list[dict], ticker: str) -> dict:
    return next((row for row in rows if row.get("ticker") == ticker), {})


def _news_score_for(name: str, news_signals: list[dict]) -> float:
    matches = [row for row in news_signals if str(row.get("dimension_value", "")).lower() == name.lower()]
    if not matches:
        return 50.0
    row = matches[0]
    return clamp(50 + flt(row.get("opportunity_score")) * 0.25 - flt(row.get("risk_score")) * 0.20)


def score_sector(name: str, rows: list[dict], news_signals: list[dict]) -> dict:
    etfs = SECTOR_ETF_MAP[name]
    sector_rows = _rows_for(rows, etfs)
    spy = _row(rows, "SPY")
    spy_20 = flt(spy.get("return_20d"))
    spy_60 = flt(spy.get("return_60d"))
    trend = avg([score_above_ma(row) for row in sector_rows])
    momentum = avg([clamp(50 + flt(row.get("return_20d")) * 0.5 + flt(row.get("return_60d")) * 0.3) for row in sector_rows])
    relative_strength = avg([clamp(50 + (flt(row.get("return_20d")) - spy_20) * 0.6 + (flt(row.get("return_60d")) - spy_60) * 0.4) for row in sector_rows])
    breadth = avg([
        100.0 if flt(row.get("close"), None) is not None and flt(row.get("ma_50"), None) and flt(row.get("close")) >= flt(row.get("ma_50")) else 0.0
        for row in sector_rows
    ])
    vol_adj = avg([clamp(50 + flt(row.get("return_20d")) / max(flt(row.get("volatility_20d"), 1), 1) * 10) for row in sector_rows])
    components = {
        "relative_strength": relative_strength,
        "absolute_momentum": momentum,
        "trend": trend,
        "rsi": avg([score_rsi(row) for row in sector_rows]),
        "macd": avg([score_macd(row) for row in sector_rows]),
        "volume": avg([score_volume(row) for row in sector_rows]),
        "volatility_adjusted_return": vol_adj,
        "breadth": breadth,
        "news": _news_score_for(name, news_signals),
    }
    weights = scoring_weights()["sector_strength"]
    score = round(sum(components[key] * weights[key] for key in weights), 4)
    sorted_rows = sorted(sector_rows, key=lambda row: flt(row.get("return_20d")), reverse=True)
    return {
        "sector": name,
        "related_etfs": etfs,
        "score": score,
        "trend_label": trend_label(trend),
        "momentum_label": "positive" if momentum >= 55 else "neutral" if momentum >= 45 else "negative",
        "breadth_label": "broad" if breadth >= 70 else "mixed" if breadth >= 40 else "weak",
        "volatility_label": "low" if avg([flt(row.get("volatility_20d")) for row in sector_rows]) < 20 else "elevated",
        "three_month_relative_strength": round(relative_strength, 4),
        "components": {key: round(value, 4) for key, value in components.items()},
        "top_supporting_tickers": [row.get("ticker") for row in sorted_rows[:3]],
        "top_detracting_tickers": [row.get("ticker") for row in list(reversed(sorted_rows))[:3]],
    }


def rank_sectors(rows: list[dict], news_signals: list[dict]) -> list[dict]:
    ranked = [score_sector(name, rows, news_signals) for name in SECTOR_ETF_MAP]
    return sorted(ranked, key=lambda row: row["score"], reverse=True)
