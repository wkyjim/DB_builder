"""Report adapter for ETF flow analytics output."""

from __future__ import annotations

from typing import Any
import math


def _fmt(value, digits: int = 1) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if math.isnan(numeric) or math.isinf(numeric):
        return "n/a"
    return f"{numeric:.{digits}f}"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["No rows available."]
    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
        *["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows],
    ]


CORE_TICKERS = ["IVV", "ACWI", "EFA", "IEMG", "IJH", "IWM", "LQD", "HYG", "SGOV", "SHY", "IEF", "TLT", "GLD", "IBIT"]
SECTOR_TICKERS = ["XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLK", "XLB", "XLRE", "XLU"]
SUBSECTOR_TICKERS = [
    "IAT", "ITA", "IYZ", "SOXX", "SMH", "IGV", "CIBR", "IBB", "XBI", "IHI",
    "KBE", "IAI", "KIE", "ITB", "XHB", "IYT", "IFRA", "ICLN", "NLR", "XOP",
    "OIH", "XME", "XRT", "FDN", "SKYY", "ROBT",
]


def _by_ticker(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("ticker") or "").upper(): row for row in rows}


def _flow_row(row: dict[str, Any]) -> list[str]:
    return [
        str(row.get("ticker") or "n/a"),
        str(row.get("exposure_name") or row.get("exposure_id") or "n/a"),
        _fmt(row.get("flow_zscore_20d")),
        _fmt(row.get("flow_zscore_60d")),
        _fmt(row.get("flow_persistence_20d")),
        _fmt(row.get("volume_zscore_60d")),
        str(row.get("price_flow_volume_state") or "n/a"),
    ]


def _ticker_table(rows_by_ticker: dict[str, dict[str, Any]], tickers: list[str], *, subsector: bool = False) -> list[list[str]]:
    rows = []
    for ticker in tickers:
        row = rows_by_ticker.get(ticker)
        if not row:
            continue
        if subsector:
            rows.append(
                [
                    ticker,
                    str(row.get("exposure_name") or row.get("exposure_id") or "n/a"),
                    _fmt(row.get("flow_zscore_20d")),
                    _fmt(row.get("flow_zscore_60d")),
                    _fmt(row.get("flow_persistence_20d")),
                    _fmt(row.get("volume_zscore_60d")),
                    str(row.get("price_state") or "n/a"),
                    str(row.get("flow_state") or "n/a"),
                    str(row.get("price_flow_volume_state") or "n/a"),
                    str(row.get("flow_rotation_state") or "n/a"),
                ]
            )
        else:
            rows.append(_flow_row(row))
    return rows


def etf_flow_report_lines(data: dict[str, Any]) -> list[str]:
    if not data:
        return ["## ETF Flows Analysis", "", "No ETF flow signals are available yet.", ""]
    regime = data.get("flow_regime") or {}
    market_flow = data.get("market_flow") or {}
    representative = data.get("representative_signals") or []
    contradictions = data.get("representative_divergences") or data.get("contradictions") or []
    rows_by_ticker = _by_ticker(representative)
    lines = ["## ETF Flows Analysis", ""]
    if market_flow:
        lines.extend(
            [
                f"- Market flow regime: `{market_flow.get('market_flow_regime', 'n/a')}`",
                f"- Market flow score: `{_fmt(market_flow.get('market_flow_score'))} / 100`",
                f"- Equity risk flow: `{_fmt(market_flow.get('equity_risk_flow_score'))}`",
                f"- Credit risk flow: `{_fmt(market_flow.get('credit_risk_flow_score'))}`",
                f"- Sector cyclicality flow: `{_fmt(market_flow.get('sector_cyclicality_flow_score'))}`",
                f"- Duration/liquidity flow: `{_fmt(market_flow.get('duration_liquidity_score'))}`",
                f"- Alternatives: `{market_flow.get('alternative_asset_interpretation', 'n/a')}`",
            ]
        )
    if regime:
        lines.extend(
            [
                f"- ETF flow reliability: `{_fmt(regime.get('flow_regime_confidence') or regime.get('confidence'))} / 100`",
                f"- Dominant allocation direction: `{regime.get('dominant_allocation_direction', 'n/a')}`",
                "",
            ]
        )
    lines.extend(["### Core Flow Signals", ""])
    lines.extend(
        _table(
            ["Ticker", "Exposure", "20D Flow Z", "60D Flow Z", "Persistence", "Volume Z", "Price/Flow/Volume State"],
            _ticker_table(rows_by_ticker, CORE_TICKERS),
        )
    )
    lines.append("")
    lines.extend(["### Sector Flow Signals", ""])
    lines.extend(
        _table(
            ["Ticker", "Exposure", "20D Flow Z", "60D Flow Z", "Persistence", "Volume Z", "Price/Flow/Volume State"],
            _ticker_table(rows_by_ticker, SECTOR_TICKERS),
        )
    )
    lines.append("")
    lines.extend(["### Subsector Rotation Signals", ""])
    lines.extend(
        _table(
            ["Ticker", "Exposure", "20D Flow Z", "60D Flow Z", "Persistence", "Volume Z", "Price State", "Flow State", "Price/Flow/Volume State", "Rotation State"],
            _ticker_table(rows_by_ticker, SUBSECTOR_TICKERS, subsector=True),
        )
    )
    lines.append("")
    useful = [
        row for row in contradictions
        if row.get("flag_type") not in {"flow_concentration"}
    ][:6]
    if useful:
        lines.extend(["### Material Flow Divergences", ""])
        lines.extend(
            _table(
                ["Severity", "Type", "Primary", "Comparison", "Interpretation"],
                [
                    [
                        row.get("severity", "n/a"),
                        row.get("flag_type", "n/a"),
                        row.get("primary_ticker") or row.get("segment", "n/a"),
                        row.get("comparison_ticker", "n/a"),
                        row.get("interpretation") or row.get("description", "n/a"),
                    ]
                    for row in useful
                ],
            )
        )
        lines.append("")
    return lines
