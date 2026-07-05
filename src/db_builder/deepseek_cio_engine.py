"""Manual DeepSeek CIO house-view report engine."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Callable

import pandas as pd

from db_builder.critical_house_view import build_critical_house_view
from db_builder.investment_report import collect_report_data, save_report
from db_builder.market_intelligence_report import top_news_articles
from db_builder.news_classifier import _ollama_chat, ollama_deep_model, ollama_url


REQUIRED_CIO_HEADINGS = [
    "# DeepSeek CIO House View",
    "## Executive Summary",
    "## Market Bullishness / Bearishness",
    "## Sector Strength Ranking",
    "## Tactical Positioning",
    "## Risk Management",
    "## Final House View",
    "## Evidence Appendix",
]
UNSUPPORTED_EVIDENCE_PHRASES = [
    "investor survey",
    "investor surveys",
    "fund flow",
    "fund flows",
    "etf inflow",
    "etf inflows",
    "positioning data",
]

WATCHLIST_SYMBOLS = {
    "^VIX",
    "SPY",
    "QQQ",
    "IWM",
    "SMH",
    "SOXX",
    "CIBR",
    "XAR",
    "GRID",
    "NLR",
    "XLV",
    "XLF",
    "XLE",
}

ETF_LABELS = {
    "SPY": "S&P 500 ETF",
    "QQQ": "Nasdaq 100 ETF",
    "IWM": "Russell 2000 ETF",
    "SMH": "Semiconductor ETF",
    "SOXX": "Semiconductor ETF",
    "CIBR": "Cybersecurity ETF",
    "XAR": "Defense ETF",
    "GRID": "Grid Infrastructure ETF",
    "NLR": "Nuclear ETF",
    "XLV": "Healthcare ETF",
    "XLF": "Financials ETF",
    "XLE": "Energy ETF",
    "XLU": "Utilities ETF",
    "XLY": "Consumer Discretionary ETF",
    "XLP": "Consumer Staples ETF",
    "XLRE": "Real Estate ETF",
    "XLI": "Industrials ETF",
    "UTES": "Utilities ETF",
    "TAN": "Solar ETF",
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
}
APPROVED_TICKERS = set(ETF_LABELS) | {"^VIX", "XLK"}
EVIDENCE_ID_PATTERN = re.compile(
    r"\[(?:REGIME|MACRO|WATCHLIST|SECTOR_ROTATION|SECTOR_REGIME|SECULAR_THEME|NEWS|HEADLINE|RISK|OPPORTUNITY)_\d{3}\]"
)
MAJOR_SECTION_HEADINGS = [
    "## Executive Summary",
    "## Market Bullishness / Bearishness",
    "## Investor Sentiment",
    "## Sector Strength Ranking",
    "## Highest Conviction Opportunities",
    "## Tactical Positioning",
    "## Tactical Buy List",
    "## Tactical Sell / Reduce List",
    "## Key Risks",
    "## Risk Management",
    "## Final House View",
]


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def framework_path() -> Path:
    return project_root() / "knowledge" / "deepseek_cio_framework.md"


def load_cio_framework(path: Path | None = None) -> str:
    selected = path or framework_path()
    return selected.read_text(encoding="utf-8")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if hasattr(value, "tolist"):
        return value.tolist()
    return []


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _pick(row: dict, fields: list[str]) -> dict:
    return {field: _json_safe(row.get(field)) for field in fields if field in row}


def _first(rows: list[dict]) -> dict:
    return rows[0] if rows else {}


def _with_evidence(rows: list[dict], prefix: str) -> list[dict]:
    return [{**row, "evidence_id": f"{prefix}_{idx:03d}"} for idx, row in enumerate(rows, start=1)]


def _with_evidence_from(rows: list[dict], prefix: str, *, start: int) -> list[dict]:
    return [{**row, "evidence_id": f"{prefix}_{idx:03d}"} for idx, row in enumerate(rows, start=start)]


def _evidence_summary(row: dict) -> str:
    pairs = [f"{key}={value}" for key, value in row.items() if key != "evidence_id" and value not in (None, "", [])]
    return "; ".join(pairs[:12])


def compact_market_regime_v2(rows: list[dict]) -> dict:
    row = _first(rows)
    compact = _pick(
        row,
        [
            "market_regime",
            "market_phase",
            "confidence",
            "macro_score",
            "technical_score",
            "momentum_score",
            "breadth_score",
            "risk_appetite_score",
            "news_score",
            "risk_on_score",
            "risk_off_score",
            "bullish_score",
            "bearish_score",
            "market_strength",
            "trend_state",
            "momentum_state",
            "volatility_state",
            "breadth_state",
            "risk_appetite_state",
            "drivers",
        ],
    )
    drivers = compact.get("drivers")
    if isinstance(drivers, dict):
        compact["drivers"] = {key: _as_list(value)[:3] for key, value in drivers.items()}
    if compact:
        compact["evidence_id"] = "REGIME_001"
    return compact


def compact_sector_rotation(rows: list[dict]) -> dict:
    overweight = [
        row for row in rows
        if row.get("allocation_bias") in {"overweight", "modest_overweight"}
    ][:5]
    underweight = [
        row for row in rows
        if row.get("allocation_bias") in {"underweight", "avoid"}
    ][:5]
    fields = [
        "sector_name",
        "related_etfs",
        "rotation_rank",
        "rotation_score",
        "relative_strength_rank",
        "momentum_rank",
        "news_rank",
        "risk_rank",
        "allocation_bias",
        "recommended_action",
    ]
    return {
        "top_overweight": _with_evidence([_pick(row, fields) for row in overweight], "SECTOR_ROTATION"),
        "bottom_underweight_or_avoid": _with_evidence_from(
            [_pick(row, fields) for row in underweight],
            "SECTOR_ROTATION",
            start=len(overweight) + 1,
        ),
    }


def compact_sector_regimes(rows: list[dict]) -> list[dict]:
    fields = [
        "sector_name",
        "related_etfs",
        "sector_regime",
        "cycle_phase",
        "confidence",
        "trend_score",
        "momentum_score",
        "relative_strength_score",
        "breadth_score",
        "news_score",
        "risk_score",
        "final_score",
        "trend_state",
        "momentum_state",
        "relative_strength_state",
        "cycle_state",
        "top_themes",
    ]
    return _with_evidence([_pick(row, fields) for row in rows], "SECTOR_REGIME")


def compact_secular_themes(rows: list[dict]) -> dict:
    fields = [
        "theme_name",
        "parent_theme",
        "secular_score",
        "tactical_score",
        "theme_phase",
        "confidence",
        "related_etfs",
        "top_subthemes",
        "article_count",
        "evidence_score",
    ]
    top = _with_evidence(
        [_pick(row, fields) for row in sorted(rows, key=lambda row: _float(row.get("secular_score")), reverse=True)[:10]],
        "SECULAR_THEME",
    )
    divergences = [
        _pick(row, fields)
        for row in rows
        if abs(_float(row.get("secular_score")) - _float(row.get("tactical_score"))) >= 12
    ][:8]
    return {"top_themes": top, "tactical_vs_secular_divergences": _with_evidence(divergences, "SECULAR_THEME")}


def compact_news_signals(rows: list[dict]) -> dict:
    fields = [
        "dimension_type",
        "dimension_value",
        "article_count",
        "high_impact_count",
        "weighted_sentiment_score",
        "avg_impact_score",
        "opportunity_score",
        "risk_score",
    ]
    themes = [row for row in rows if row.get("dimension_type") == "theme"]
    return {
        "top_news_themes": _with_evidence([_pick(row, fields) for row in sorted(themes, key=lambda row: _float(row.get("opportunity_score")), reverse=True)[:8]], "NEWS"),
        "top_risk_signals": _with_evidence([_pick(row, fields) for row in sorted(rows, key=lambda row: _float(row.get("risk_score")), reverse=True)[:8]], "RISK"),
        "top_opportunity_signals": _with_evidence([_pick(row, fields) for row in sorted(rows, key=lambda row: _float(row.get("opportunity_score")), reverse=True)[:8]], "OPPORTUNITY"),
    }


def compact_top_headlines(data: dict, *, limit: int = 10) -> list[dict]:
    fields = [
        "title",
        "source_name",
        "source_category",
        "source_priority",
        "importance_score",
        "classification_priority",
        "impact_score",
        "sentiment_score",
        "themes",
        "affected_tickers",
        "published_at",
        "importance_reasons",
    ]
    return _with_evidence([_pick(row, fields) for row in top_news_articles(data, limit=limit)], "HEADLINE")


def compact_macro_watchlist(data: dict) -> dict:
    macro = [
        _pick(row, ["symbol", "name", "asset_type", "date", "close", "pct_chg"])
        for row in data.get("macro", [])
        if row.get("symbol") in WATCHLIST_SYMBOLS
    ]
    watchlist = [
        _pick(row, ["ticker", "date", "close", "pct_chg", "rsi_14", "ma_50", "ma_200", "return_20d"])
        for row in data.get("watchlist", [])
        if row.get("ticker") in WATCHLIST_SYMBOLS
    ]
    return {"macro": _with_evidence(macro, "MACRO"), "watchlist": _with_evidence(watchlist, "WATCHLIST")}


def evidence_appendix(snapshot: dict) -> list[dict]:
    entries: list[dict] = []

    regime = snapshot.get("market_regime_v2") or {}
    if regime.get("evidence_id"):
        entries.append({"evidence_id": regime["evidence_id"], "source": "market_regime_v2", "summary": _evidence_summary(regime)})

    for group_name, group in (snapshot.get("sector_rotation") or {}).items():
        for row in group:
            entries.append({"evidence_id": row["evidence_id"], "source": f"sector_rotation.{group_name}", "summary": _evidence_summary(row)})
    for row in snapshot.get("sector_regimes") or []:
        entries.append({"evidence_id": row["evidence_id"], "source": "sector_regimes", "summary": _evidence_summary(row)})
    for group_name, group in (snapshot.get("secular_themes") or {}).items():
        for row in group:
            entries.append({"evidence_id": row["evidence_id"], "source": f"secular_themes.{group_name}", "summary": _evidence_summary(row)})
    for group_name, group in (snapshot.get("news_signals") or {}).items():
        for row in group:
            entries.append({"evidence_id": row["evidence_id"], "source": f"news_signals.{group_name}", "summary": _evidence_summary(row)})
    for row in snapshot.get("opportunity_signals") or []:
        if row.get("evidence_id"):
            entries.append({"evidence_id": row["evidence_id"], "source": "opportunity_signals", "summary": _evidence_summary(row)})
    for row in snapshot.get("top_news_headlines") or []:
        entries.append({"evidence_id": row["evidence_id"], "source": "top_news_headlines", "summary": _evidence_summary(row)})
    for row in (snapshot.get("macro_watchlist") or {}).get("macro", []):
        entries.append({"evidence_id": row["evidence_id"], "source": "macro", "summary": _evidence_summary(row)})
    for row in (snapshot.get("macro_watchlist") or {}).get("watchlist", []):
        entries.append({"evidence_id": row["evidence_id"], "source": "watchlist", "summary": _evidence_summary(row)})
    return entries


def build_cio_snapshot_from_data(data: dict) -> dict:
    critical_view = data.get("critical_house_view") or build_critical_house_view(data)
    data["critical_house_view"] = critical_view
    snapshot = {
        "generated_at": _json_safe(data.get("generated_at") or datetime.now(timezone.utc)),
        "window_hours": int(data.get("window_hours") or 24),
        "critical_pm_view": {
            "market_interpretation": critical_view.get("market_interpretation"),
            "confidence_interpretation": critical_view.get("confidence_interpretation"),
            "positioning_bias": critical_view.get("positioning_bias"),
            "contradiction_flags": critical_view.get("contradiction_flags", []),
            "opportunity_view": critical_view.get("opportunity_view"),
            "final_house_view": critical_view.get("final_house_view"),
            "sector_pm_table": critical_view.get("sector_views", []),
        },
        "market_regime_v2": compact_market_regime_v2(data.get("regime_v2") or []),
        "sector_rotation": compact_sector_rotation(data.get("sector_rotation") or []),
        "sector_regimes": compact_sector_regimes(data.get("sector_regimes") or []),
        "secular_themes": compact_secular_themes(data.get("secular_themes") or []),
        "news_signals": compact_news_signals(data.get("news_signals") or []),
        "opportunity_signals": _with_evidence([_json_safe(row) for row in (data.get("opportunities") or [])[:10]], "OPPORTUNITY"),
        "top_news_headlines": compact_top_headlines(data, limit=10),
        "macro_watchlist": compact_macro_watchlist(data),
        "ticker_reference": ETF_LABELS,
        "approved_etf_label_map": ETF_LABELS,
        "approved_ticker_universe": sorted(APPROVED_TICKERS),
        "unavailable_fields": {
            "investor_surveys": None,
            "fund_flows": None,
            "options_positioning": None,
            "single_name_opportunities": None if data.get("opportunities") else "none passed filters",
        },
    }
    snapshot["evidence_appendix"] = evidence_appendix(snapshot)
    return snapshot


def build_cio_snapshot(engine, *, window_hours: int) -> dict:
    data = collect_report_data(engine, window_hours=window_hours)
    return build_cio_snapshot_from_data(data)


def build_cio_prompt(framework: str, snapshot: dict) -> list[dict]:
    system = (
        "You are CIO / senior market strategist. Use the framework below. "
        "The deterministic snapshot is the source of truth. Be critical. "
        "Do not invent missing data. Resolve contradictions. Low confidence should reduce conviction. "
        "Analyze the data step by step internally. Do not show hidden reasoning. "
        "Output markdown only. The first line must be exactly: # DeepSeek CIO House View. "
        "Use the required headings verbatim. Keep each section concise enough to complete the full report.\n\n"
        f"FRAMEWORK:\n{framework}"
    )
    user = (
        "DATA SNAPSHOT:\n"
        f"{json.dumps(_json_safe(snapshot), ensure_ascii=True, indent=2)}\n\n"
        "APPROVED ETF LABEL MAP:\n"
        f"{json.dumps(ETF_LABELS, ensure_ascii=True, indent=2)}\n\n"
        "TASK:\n"
        "Generate the final CIO report with every required section from the framework. "
        "Interpret, challenge, and synthesize the deterministic outputs. "
        "Do not overwrite the deterministic scoring engines; use them as evidence. "
        "Every major claim must cite one or more evidence IDs inline, for example [REGIME_001] or [WATCHLIST_001]. "
        "If no evidence ID supports a claim, do not make that claim. "
        "Do not quote exact numeric values unless they appear in the snapshot. "
        "Never invent company names for ticker symbols. Use exact labels from APPROVED ETF LABEL MAP. "
        "Do not invent alternate ETF labels. "
        "If opportunity_signals is empty, say no high-conviction single-name opportunities passed filters and discuss thematic/ETF opportunities instead. "
        "Do not mention surveys, fund flows, ETF inflows, earnings reports, central-bank actions, or economic releases unless they are present in the snapshot. "
        "For investor sentiment, infer only from the provided VIX, risk_appetite_state, macro/watchlist moves, news signals, and sector rotation. "
        "Use these headings exactly and in this order: "
        "# DeepSeek CIO House View; ## Executive Summary; ## Market Bullishness / Bearishness; "
        "## Investor Sentiment; ## Sector Strength Ranking; ## Highest Conviction Opportunities; "
        "## Tactical Positioning; ## Tactical Buy List; ## Tactical Sell / Reduce List; "
        "## Key Risks; ## Risk Management; ## Final House View."
        "End with ## Evidence Appendix listing the evidence IDs used and a one-line source summary for each."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def prompt_preview(messages: list[dict], *, max_chars: int = 5000) -> str:
    combined = "\n\n".join(f"{message['role'].upper()}:\n{message['content']}" for message in messages)
    if len(combined) <= max_chars:
        return combined
    return combined[:max_chars].rstrip() + "\n\n...[prompt truncated for preview]..."


def strip_thinking_text(content: str) -> str:
    return re.sub(r"<think>.*?</think>", "", content or "", flags=re.DOTALL | re.IGNORECASE).strip()


def validate_cio_markdown(markdown: str, *, snapshot: dict | None = None, evidence_locked: bool = True) -> str:
    text = strip_thinking_text(markdown)
    missing = [heading for heading in REQUIRED_CIO_HEADINGS if heading not in text]
    if missing:
        raise ValueError(f"DeepSeek CIO report missing required headings: {missing}")
    if evidence_locked:
        validate_cio_quality(text, snapshot=snapshot)
    return text + "\n"


def _allowed_numbers(snapshot: dict | None) -> set[str]:
    values: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
            values.add(str(int(numeric)) if numeric.is_integer() else str(numeric))
            values.add(f"{numeric:.1f}".rstrip("0").rstrip("."))
            values.add(f"{numeric:.2f}".rstrip("0").rstrip("."))

    walk(snapshot or {})
    return values


def _numbers_in_judgment_line(line: str) -> bool:
    return "judgment:" in line.lower()


def _number_in_approved_label(line: str, number: str) -> bool:
    return any(number in label and label in line for label in ETF_LABELS.values())


def _section_blocks(markdown: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^## .+$", markdown, flags=re.MULTILINE))
    blocks = []
    for idx, match in enumerate(matches):
        heading = match.group(0).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
        blocks.append((heading, markdown[start:end].strip()))
    return blocks


def _snapshot_vix_close(snapshot: dict | None) -> float | None:
    if not snapshot:
        return None
    for row in (snapshot.get("macro_watchlist") or {}).get("macro", []):
        if row.get("symbol") == "^VIX":
            return _float(row.get("close"), default=None)
    return None


def normalize_etf_label(label: str) -> str:
    normalized = " ".join(str(label or "").split()).strip()
    normalized = re.sub(r"\bSector ETF\b", "ETF", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bSemiconductors ETF\b", "Semiconductor ETF", normalized, flags=re.IGNORECASE)
    words = []
    for word in normalized.split(" "):
        if word.upper() == "ETF":
            words.append("ETF")
        elif word.upper() == "S&P":
            words.append("S&P")
        else:
            words.append(word[:1].upper() + word[1:])
    return " ".join(words)


def validate_cio_quality(markdown: str, *, snapshot: dict | None = None) -> None:
    lower = markdown.lower()
    unsupported = [phrase for phrase in UNSUPPORTED_EVIDENCE_PHRASES if phrase in lower and "unavailable" not in lower]
    if unsupported:
        raise ValueError(f"DeepSeek CIO report used unsupported evidence claims: {unsupported}")

    for label, ticker in re.findall(r"([A-Za-z &/-]+ ETF)\s+\(([A-Z0-9-]+)\)", markdown):
        expected = ETF_LABELS.get(ticker)
        if expected and normalize_etf_label(label).lower() != expected.lower():
            raise ValueError(f"DeepSeek CIO report mislabeled {ticker}: '{label.strip()}' should be '{expected}'")

    vix_close = _snapshot_vix_close(snapshot)
    if vix_close is not None:
        for value in re.findall(r"\bVIX\b.{0,40}?\b(?:at|=|close|level)\s*`?([0-9]+(?:\.[0-9]+)?)", markdown, flags=re.IGNORECASE):
            if abs(float(value) - vix_close) > 0.25:
                raise ValueError(f"DeepSeek CIO report quoted VIX {value}, but snapshot VIX close is {vix_close}")

    allowed_tickers = set((snapshot or {}).get("approved_ticker_universe") or APPROVED_TICKERS)
    for ticker in re.findall(r"\b(?:[A-Z]{2,5}(?:-[A-Z]{3})?|\^[A-Z]{2,5})\b", markdown):
        if ticker in {"CIO", "ETF", "ETFs", "VIX"}:
            continue
        if ticker not in allowed_tickers:
            raise ValueError(f"DeepSeek CIO report used unsupported ticker/ETF: {ticker}")

    allowed_numbers = _allowed_numbers(snapshot)
    for line in markdown.splitlines():
        if _numbers_in_judgment_line(line):
            continue
        for number in re.findall(r"(?<![A-Z_])\b\d+(?:\.\d+)?\b(?!\])", line):
            if re.match(rf"^\s*{re.escape(number)}\.", line):
                continue
            if _number_in_approved_label(line, number):
                continue
            if number not in allowed_numbers:
                raise ValueError(f"DeepSeek CIO report used unsupported number: {number}")

    for heading, body in _section_blocks(markdown):
        if heading not in MAJOR_SECTION_HEADINGS:
            continue
        claim_lines = [
            line.strip()
            for line in body.splitlines()
            if line.strip() and not line.strip().startswith("|") and not line.strip().startswith("---")
        ]
        if claim_lines and not any(EVIDENCE_ID_PATTERN.search(line) for line in claim_lines):
            raise ValueError(f"DeepSeek CIO report section lacks evidence IDs: {heading}")


def fallback_cio_report(
    *,
    reason: str,
    snapshot: dict,
    raw_response: str | None = None,
    include_raw_response: bool = False,
) -> str:
    regime = snapshot.get("market_regime_v2", {})
    critical = snapshot.get("critical_pm_view", {})
    lines = [
        "# DeepSeek CIO House View",
        "",
        "## Executive Summary",
        "",
        "- DeepSeek output did not pass validation, so this fallback report preserves the deterministic house view.",
        f"- Validation failure: {reason}",
        f"- Deterministic market view: {critical.get('final_house_view', 'not available')}",
        "",
        "## Market Bullishness / Bearishness",
        "",
        f"- Market Regime 2.0: {regime.get('market_regime', 'n/a')} / {regime.get('market_phase', 'n/a')}.",
        f"- Confidence: {regime.get('confidence', 'n/a')}.",
        "",
        "## Investor Sentiment",
        "",
        f"- Positioning bias: {critical.get('positioning_bias', 'n/a')}.",
        "",
        "## Sector Strength Ranking",
        "",
        "- See deterministic sector_rotation and sector_regimes snapshot for ranking details.",
        "",
        "## Highest Conviction Opportunities",
        "",
        f"- {critical.get('opportunity_view', 'No opportunity view is available.')}",
        "",
        "## Tactical Positioning",
        "",
        "- Use deterministic sector rotation and Critical PM View until a valid DeepSeek report is available.",
        "",
        "## Tactical Buy List",
        "",
        "- Use deterministic daily house view buy list.",
        "",
        "## Tactical Sell / Reduce List",
        "",
        "- Use deterministic daily house view reduce list.",
        "",
        "## Key Risks",
        "",
        "- LLM validation failure is a report-quality risk, not a market signal.",
        "",
        "## Risk Management",
        "",
        "- Keep sizing tied to deterministic confidence, volatility, breadth, trend, and risk appetite.",
        "",
        "## Final House View",
        "",
        critical.get("final_house_view", "Deterministic final house view is unavailable."),
        "",
        "## Evidence Appendix",
        "",
    ]
    for entry in snapshot.get("evidence_appendix", [])[:20]:
        lines.append(f"- [{entry['evidence_id']}] {entry['source']}: {entry['summary']}")
    if include_raw_response and raw_response:
        lines.extend(["", "## Diagnostics", "", "Raw DeepSeek response follows for local diagnostics only.", "", raw_response[:12000]])
    return "\n".join(lines).rstrip() + "\n"


def call_deepseek_cio(
    messages: list[dict],
    *,
    model: str | None = None,
    timeout: int = 900,
    chat_fn: Callable[..., dict] = _ollama_chat,
) -> str:
    response = chat_fn(
        messages,
        url=ollama_url(),
        model=model or ollama_deep_model(),
        timeout=timeout,
        options={"temperature": 0.2, "num_predict": 8000},
        response_format=None,
    )
    return str(response.get("message", {}).get("content", ""))


def generate_cio_report(
    engine,
    *,
    window_hours: int,
    timeout: int = 900,
    model: str | None = None,
    call_model: bool = True,
    include_raw_response: bool = False,
    evidence_locked: bool = True,
    chat_fn: Callable[..., dict] = _ollama_chat,
) -> dict:
    snapshot = build_cio_snapshot(engine, window_hours=window_hours)
    framework = load_cio_framework()
    messages = build_cio_prompt(framework, snapshot)
    if not call_model:
        return {
            "status": "preview",
            "snapshot": snapshot,
            "messages": messages,
            "markdown": "",
            "raw_response": None,
            "reason": None,
        }

    raw_response = call_deepseek_cio(messages, model=model, timeout=timeout, chat_fn=chat_fn)
    try:
        markdown = validate_cio_markdown(raw_response, snapshot=snapshot, evidence_locked=evidence_locked)
        status = "valid"
        reason = None
    except ValueError as exc:
        reason = str(exc)
        markdown = fallback_cio_report(
            reason=reason,
            snapshot=snapshot,
            raw_response=raw_response,
            include_raw_response=include_raw_response,
        )
        status = "fallback"
    return {
        "status": status,
        "snapshot": snapshot,
        "messages": messages,
        "markdown": markdown,
        "raw_response": raw_response,
        "reason": reason,
    }


def save_deepseek_cio_report(markdown: str, *, reports_dir: Path | None = None) -> Path:
    return save_report(markdown, reports_dir=reports_dir, prefix="deepseek_house_view")
