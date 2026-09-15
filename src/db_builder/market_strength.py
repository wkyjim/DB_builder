"""Rule-based market strength scoring."""

from __future__ import annotations

import math


def flt(value, default: float = 0.0) -> float:
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def avg(values: list[float], default: float = 50.0) -> float:
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else default


def row_by_ticker(rows: list[dict], ticker: str) -> dict:
    return next((row for row in rows if row.get("ticker") == ticker), {})


def score_above_ma(row: dict) -> float:
    close = flt(row.get("close"), None)
    if close is None:
        return 50.0
    score = 0.0
    total = 0.0
    for column, weight in [("ma_20", 20), ("ma_50", 25), ("ma_100", 20), ("ma_200", 35)]:
        ma = flt(row.get(column), None)
        if ma is None or ma == 0:
            continue
        total += weight
        score += weight if close >= ma else 0
    return round((score / total) * 100, 4) if total else 50.0


def score_return_momentum(row: dict) -> float:
    weighted = flt(row.get("return_5d")) * 0.25 + flt(row.get("return_20d")) * 0.40 + flt(row.get("return_60d")) * 0.35
    return round(clamp(50 + weighted), 4)


def score_rsi(row: dict) -> float:
    rsi = flt(row.get("rsi_14"), None)
    if rsi is None or not math.isfinite(rsi):
        return 50.0
    if 50 <= rsi <= 65:
        return 75.0
    if 65 < rsi <= 75:
        return 65.0
    if rsi > 75:
        return 45.0
    if 35 <= rsi < 50:
        return 40.0
    return 35.0


def score_macd(row: dict) -> float:
    hist = flt(row.get("macd_hist"), None)
    if hist is None:
        macd = flt(row.get("macd"), None)
        signal = flt(row.get("macd_signal"), None)
        if macd is None or signal is None:
            return 50.0
        hist = macd - signal
    return 70.0 if hist > 0 else 35.0 if hist < 0 else 50.0


def score_volume(row: dict) -> float:
    ratio = flt(row.get("volume_ratio_20"), 1.0)
    ret = flt(row.get("return_5d"))
    if ratio >= 1.2 and ret > 0:
        return 70.0
    if ratio >= 1.2 and ret < 0:
        return 35.0
    return 50.0


def strength_label(score: float) -> str:
    if score >= 75:
        return "strong"
    if score >= 60:
        return "constructive"
    if score >= 45:
        return "neutral"
    return "weak"


def trend_label(score: float) -> str:
    if score >= 75:
        return "strong uptrend"
    if score >= 60:
        return "uptrend"
    if score >= 45:
        return "neutral"
    if score >= 30:
        return "downtrend"
    return "strong downtrend"


def market_breadth(rows: list[dict]) -> dict:
    valid_50 = [row for row in rows if flt(row.get("close"), None) is not None and flt(row.get("ma_50"), None)]
    valid_200 = [row for row in rows if flt(row.get("close"), None) is not None and flt(row.get("ma_200"), None)]
    above_50 = sum(flt(row.get("close")) >= flt(row.get("ma_50")) for row in valid_50) / len(valid_50) if valid_50 else None
    above_200 = sum(flt(row.get("close")) >= flt(row.get("ma_200")) for row in valid_200) / len(valid_200) if valid_200 else None
    return_20d_values = [flt(row.get("return_20d"), None) for row in rows]
    return_20d_values = [value for value in return_20d_values if value is not None and not math.isnan(value)]
    positive_20d = sum(value > 0 for value in return_20d_values) / len(return_20d_values) if return_20d_values else None
    components = [value * 100 for value in [above_50, above_200, positive_20d] if value is not None]
    score = round(avg(components), 4)
    return {
        "score": score,
        "label": "broad" if score >= 70 else "healthy" if score >= 55 else "narrow" if score >= 40 else "weak",
        "above_50d_pct": round((above_50 or 0) * 100, 2) if above_50 is not None else None,
        "above_200d_pct": round((above_200 or 0) * 100, 2) if above_200 is not None else None,
        "positive_20d_pct": round((positive_20d or 0) * 100, 2) if positive_20d is not None else None,
    }


def missing_indicator_warnings(rows: list[dict]) -> list[str]:
    warnings = []
    for ticker in ["SPY", "QQQ", "IWM", "SMH"]:
        row = row_by_ticker(rows, ticker)
        if not row:
            warnings.append(f"{ticker} market row missing")
            continue
        for column in ["ma_20", "ma_50", "ma_100", "ma_200", "rsi_14", "macd_hist", "return_20d"]:
            if row.get(column) is None:
                warnings.append(f"{ticker} {column} missing")
    return warnings


def compute_market_strength(rows: list[dict]) -> dict:
    indices = [row_by_ticker(rows, ticker) for ticker in ["SPY", "QQQ", "IWM", "SMH"]]
    indices = [row for row in indices if row]
    breadth = market_breadth(rows)
    decomposition = {
        "indices_above_moving_averages": avg([score_above_ma(row) for row in indices]),
        "sp500_trend": score_above_ma(row_by_ticker(rows, "SPY")),
        "nasdaq_trend": score_above_ma(row_by_ticker(rows, "QQQ")),
        "russell_participation": score_above_ma(row_by_ticker(rows, "IWM")),
        "returns_momentum": avg([score_return_momentum(row) for row in indices]),
        "rsi_zone": avg([score_rsi(row) for row in indices]),
        "macd_confirmation": avg([score_macd(row) for row in indices]),
        "volume_confirmation": avg([score_volume(row) for row in indices]),
        "breadth": breadth["score"],
    }
    score = round(
        decomposition["indices_above_moving_averages"] * 0.20
        + decomposition["sp500_trend"] * 0.12
        + decomposition["nasdaq_trend"] * 0.12
        + decomposition["russell_participation"] * 0.10
        + decomposition["returns_momentum"] * 0.16
        + decomposition["rsi_zone"] * 0.08
        + decomposition["macd_confirmation"] * 0.10
        + decomposition["volume_confirmation"] * 0.05
        + decomposition["breadth"] * 0.07,
        4,
    )
    return {
        "score": score,
        "label": strength_label(score),
        "decomposition": {key: round(value, 4) for key, value in decomposition.items()},
        "breadth": breadth,
        "missing_indicators": missing_indicator_warnings(indices),
    }
