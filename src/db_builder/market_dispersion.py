"""Deterministic market and sector dispersion analytics."""

from __future__ import annotations

from statistics import pstdev

from db_builder.market_strength import flt


SECTOR_DISPERSION_ETFS = [
    "XLK",
    "XLF",
    "XLV",
    "XLE",
    "XLI",
    "XLY",
    "XLP",
    "XLU",
    "XLB",
    "XLRE",
    "XLC",
]

ETF_LABELS = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLV": "Health Care",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
    "RSP": "Equal-weight S&P 500",
    "SPY": "S&P 500",
    "IWM": "Russell 2000",
    "IWF": "Russell 1000 Growth",
    "IWD": "Russell 1000 Value",
    "QQQ": "Nasdaq 100",
}

SIZE_STYLE_PAIRS = [
    ("Equal-weight vs cap-weight", "RSP", "SPY"),
    ("Small-cap vs large-cap", "IWM", "SPY"),
    ("Growth vs value", "IWF", "IWD"),
    ("Nasdaq vs S&P 500", "QQQ", "SPY"),
]


def _row(rows: list[dict], ticker: str) -> dict:
    return next((row for row in rows if row.get("ticker") == ticker), {})


def _valid_returns(rows: list[dict], column: str) -> list[tuple[str, float]]:
    values = []
    for row in rows:
        if row.get(column) is None:
            continue
        values.append((str(row.get("ticker")), flt(row.get(column))))
    return values


def _dispersion_label(value: float) -> str:
    if value >= 20:
        return "very high"
    if value >= 12:
        return "high"
    if value >= 6:
        return "moderate"
    return "low"


def compute_broad_market_dispersion(rows: list[dict]) -> dict:
    sector_rows = [row for row in rows if row.get("ticker") in SECTOR_DISPERSION_ETFS]
    returns_20 = _valid_returns(sector_rows, "return_20d")
    returns_60 = _valid_returns(sector_rows, "return_60d")

    def stats(values: list[tuple[str, float]]) -> dict:
        if not values:
            return {
                "range": None,
                "std": None,
                "leader": "n/a",
                "laggard": "n/a",
                "label": "unavailable",
            }
        sorted_values = sorted(values, key=lambda item: item[1], reverse=True)
        numeric = [value for _, value in sorted_values]
        range_value = max(numeric) - min(numeric)
        return {
            "range": round(range_value, 4),
            "std": round(pstdev(numeric), 4) if len(numeric) > 1 else 0.0,
            "leader": sorted_values[0][0],
            "leader_label": ETF_LABELS.get(sorted_values[0][0], sorted_values[0][0]),
            "leader_return": round(sorted_values[0][1], 4),
            "laggard": sorted_values[-1][0],
            "laggard_label": ETF_LABELS.get(sorted_values[-1][0], sorted_values[-1][0]),
            "laggard_return": round(sorted_values[-1][1], 4),
            "label": _dispersion_label(range_value),
        }

    pair_rows = []
    for label, left, right in SIZE_STYLE_PAIRS:
        left_row = _row(rows, left)
        right_row = _row(rows, right)
        left_return = flt(left_row.get("return_20d"), None)
        right_return = flt(right_row.get("return_20d"), None)
        spread = None if left_return is None or right_return is None else round(left_return - right_return, 4)
        pair_rows.append(
            {
                "comparison": label,
                "left": left,
                "left_label": ETF_LABELS.get(left, left),
                "right": right,
                "right_label": ETF_LABELS.get(right, right),
                "left_return_20d": left_return,
                "right_return_20d": right_return,
                "spread_20d": spread,
                "signal": _pair_signal(label, spread),
            }
        )
    return {
        "sector_etf_count": len(returns_20),
        "sector_20d": stats(returns_20),
        "sector_60d": stats(returns_60),
        "size_style": pair_rows,
    }


def _pair_signal(label: str, spread: float | None) -> str:
    if spread is None:
        return "unavailable"
    if abs(spread) < 1:
        return "balanced"
    winner = "left" if spread > 0 else "right"
    if label == "Equal-weight vs cap-weight":
        return "broader participation" if winner == "left" else "mega-cap concentration"
    if label == "Small-cap vs large-cap":
        return "small-cap leadership" if winner == "left" else "large-cap leadership"
    if label == "Growth vs value":
        return "growth leadership" if winner == "left" else "value leadership"
    if label == "Nasdaq vs S&P 500":
        return "growth/beta leadership" if winner == "left" else "S&P 500 leadership"
    return "spread active"


def compute_sector_constituent_dispersion(rows: list[dict], *, min_count: int = 10) -> list[dict]:
    by_sector: dict[str, list[dict]] = {}
    for row in rows:
        sector = row.get("sector")
        if sector:
            by_sector.setdefault(str(sector), []).append(row)

    output = []
    for sector, sector_rows in by_sector.items():
        returns_20 = _valid_returns(sector_rows, "return_20d")
        if len(returns_20) < min_count:
            output.append(
                {
                    "sector": sector,
                    "constituent_count": len(returns_20),
                    "status": "insufficient mapped constituents",
                }
            )
            continue
        sorted_returns = sorted(returns_20, key=lambda item: item[1], reverse=True)
        numeric = [value for _, value in sorted_returns]
        above_50 = _pct_above(sector_rows, "ma_50")
        above_200 = _pct_above(sector_rows, "ma_200")
        positive_20d = sum(value > 0 for value in numeric) / len(numeric) * 100
        dispersion = max(numeric) - min(numeric)
        output.append(
            {
                "sector": sector,
                "constituent_count": len(returns_20),
                "status": "ok",
                "breadth_50d_pct": round(above_50, 2),
                "breadth_200d_pct": round(above_200, 2),
                "positive_20d_pct": round(positive_20d, 2),
                "dispersion_20d": round(dispersion, 4),
                "std_20d": round(pstdev(numeric), 4) if len(numeric) > 1 else 0.0,
                "label": _dispersion_label(dispersion),
                "leaders": [ticker for ticker, _ in sorted_returns[:3]],
                "laggards": [ticker for ticker, _ in sorted_returns[-3:]],
            }
        )
    return sorted(
        output,
        key=lambda row: (row.get("status") != "ok", -flt(row.get("dispersion_20d"))),
    )


def _pct_above(rows: list[dict], ma_column: str) -> float:
    valid = [
        row
        for row in rows
        if flt(row.get("close"), None) is not None and flt(row.get(ma_column), None) not in {None, 0}
    ]
    if not valid:
        return 0.0
    return sum(flt(row.get("close")) >= flt(row.get(ma_column)) for row in valid) / len(valid) * 100
