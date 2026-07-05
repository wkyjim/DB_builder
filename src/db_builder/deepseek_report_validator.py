"""Evidence-locked report validation for offline DeepSeek CIO reports."""

from __future__ import annotations

import re
from typing import Any

from db_builder.deepseek_snapshot_builder import ETF_LABELS, APPROVED_TICKERS


REQUIRED_HEADINGS = [
    "# DeepSeek CIO House View",
    "## Executive Summary",
    "## Market Bullishness / Bearishness",
    "## Investor Sentiment",
    "## Cross-Asset Confirmation",
    "## Sector Strength Ranking",
    "## Highest Conviction Opportunities",
    "## Tactical Positioning",
    "## Tactical Buy List",
    "## Tactical Sell / Reduce List",
    "## Key Risks",
    "## Risk Management",
    "## Alternative Scenario",
    "## Final House View",
    "## Evidence Appendix",
]
MAJOR_SECTIONS = [heading for heading in REQUIRED_HEADINGS if heading.startswith("## ") and heading != "## Evidence Appendix"]
EVIDENCE_TOKEN_RE = re.compile(r"\b(?:REGIME|MACRO|TECH|NEWS|SECTOR|THEME|RISK|OPPORTUNITY|CONFIDENCE)_\d{3}\b")
ANY_EVIDENCE_ID_RE = re.compile(r"\b[A-Z]+_\d{3}\b")
IGNORE_TOKENS = {
    "CIO",
    "ETF",
    "ETFs",
    "VIX",
    "RSI",
    "MA",
    "MACD",
    "USD",
    "AI",
    "GDP",
    "CPI",
    "PPI",
    "NFP",
    "FX",
    "ID",
    "ESG",
    "FOMO",
    "CAC",
    "DAX",
}


def strip_thinking_text(content: str) -> str:
    return re.sub(r"<think>.*?</think>", "", content or "", flags=re.DOTALL | re.IGNORECASE).strip()


def _walk_numbers(value: Any, values: set[str]) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _walk_numbers(item, values)
    elif isinstance(value, list):
        for item in value:
            _walk_numbers(item, values)
    elif isinstance(value, str):
        for number in re.findall(r"(?<![A-Z_])\b\d+(?:\.\d+)?\b", value):
            values.add(number)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        values.add(str(value))
        values.add(str(int(number)) if number.is_integer() else str(number))
        values.add(f"{number:.1f}")
        values.add(f"{number:.2f}")
        values.add(f"{number:.4f}")
        if abs(number) <= 1:
            values.add(f"{number * 100:.1f}")
            values.add(f"{number * 100:.2f}")
            values.add(f"{number * 100:.4f}")
        values.add(f"{number:.1f}".rstrip("0").rstrip("."))
        values.add(f"{number:.2f}".rstrip("0").rstrip("."))
        values.add(f"{number:.4f}".rstrip("0").rstrip("."))


def _walk_tickers(value: Any, tickers: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"ticker", "symbol", "dimension_value"} and isinstance(item, str):
                tickers.add(item)
            elif key in {"affected_tickers", "related_etfs"} and isinstance(item, list):
                tickers.update(str(token) for token in item if token)
            _walk_tickers(item, tickers)
    elif isinstance(value, list):
        for item in value:
            _walk_tickers(item, tickers)


def allowed_numbers(snapshot: dict) -> set[str]:
    values: set[str] = set()
    _walk_numbers(snapshot, values)
    return values


def allowed_tickers(snapshot: dict) -> set[str]:
    tickers = set(snapshot.get("approved_ticker_universe") or APPROVED_TICKERS)
    _walk_tickers(snapshot, tickers)
    tickers.update(ticker[1:] for ticker in list(tickers) if ticker.startswith("^"))
    tickers.update(ticker.split("=", 1)[0] for ticker in list(tickers) if "=" in ticker)
    return tickers


def allowed_evidence_ids(snapshot: dict) -> set[str]:
    return {str(row.get("evidence_id")) for row in snapshot.get("evidence_appendix", []) if row.get("evidence_id")}


def normalize_etf_label(label: str) -> str:
    normalized = " ".join(str(label or "").split()).strip()
    normalized = re.sub(r"^(The|and)\s+", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bSector ETF\b", "ETF", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bSemiconductors ETF\b", "Semiconductor ETF", normalized, flags=re.IGNORECASE)
    words = []
    for word in normalized.split():
        if word.upper() == "ETF":
            words.append("ETF")
        elif word.upper() == "S&P":
            words.append("S&P")
        else:
            words.append(word[:1].upper() + word[1:])
    return " ".join(words)


def _section_blocks(markdown: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^## .+$", markdown, flags=re.MULTILINE))
    blocks = []
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
        blocks.append((match.group(0).strip(), markdown[match.end():end].strip()))
    return blocks


def _number_in_approved_label(line: str, number: str) -> bool:
    return any(number in label and label in line for label in ETF_LABELS.values())


def _is_list_marker(line: str, number: str) -> bool:
    return bool(re.match(rf"^\s*{re.escape(number)}\.", line))


def _validate_evidence_ids(text: str, evidence_ids: set[str]) -> None:
    for evidence_id in ANY_EVIDENCE_ID_RE.findall(text):
        if evidence_id not in evidence_ids:
            raise ValueError(f"Unsupported evidence ID: {evidence_id}")


def _validate_tickers(text: str, snapshot: dict) -> None:
    approved = allowed_tickers(snapshot)
    for label, ticker in re.findall(r"([A-Za-z &/-]+ ETF)\s+\(([A-Z0-9-]+)\)", text):
        if ticker not in approved:
            raise ValueError(f"Unsupported ticker/ETF: {ticker}")
        expected = ETF_LABELS.get(ticker)
        if expected and normalize_etf_label(label).lower() != expected.lower():
            raise ValueError(f"Mislabeled {ticker}: '{label.strip()}' should be '{expected}'")

    for ticker in re.findall(r"\b(?:[A-Z]{2,5}(?:-[A-Z]{3})?|\^[A-Z]{2,5})\b", text):
        if ticker in IGNORE_TOKENS:
            continue
        if "-" in ticker:
            left, right = ticker.split("-", 1)
            if left in approved and right in approved:
                continue
        if ticker not in approved:
            raise ValueError(f"Unsupported ticker/ETF: {ticker}")


def _validate_numbers(text: str, snapshot: dict) -> None:
    numbers = allowed_numbers(snapshot)
    for line in text.splitlines():
        if "judgment:" in line.lower():
            continue
        for number in re.findall(r"(?<![A-Z_])\b\d+(?:\.\d+)?\b(?!\])", line):
            if _is_list_marker(line, number) or _number_in_approved_label(line, number):
                continue
            if number not in numbers:
                raise ValueError(f"Unsupported number: {number}")


def _validate_low_confidence_language(text: str, snapshot: dict) -> None:
    market = snapshot.get("market_state") or {}
    confidence = market.get("confidence")
    volatility = str(market.get("volatility_state") or "").lower()
    if "clean risk-on" in text.lower() and (confidence is not None and float(confidence) < 0.2 or volatility in {"elevated", "stressed"}):
        raise ValueError("Report says clean risk-on despite low confidence or contradictory volatility.")


def validate_report(markdown: str, snapshot: dict, *, evidence_locked: bool = True) -> str:
    text = strip_thinking_text(markdown)
    missing = [heading for heading in REQUIRED_HEADINGS if heading not in text]
    if missing:
        raise ValueError(f"Missing required headings: {missing}")
    if not evidence_locked:
        return text + "\n"

    _validate_evidence_ids(text, allowed_evidence_ids(snapshot))
    _validate_tickers(text, snapshot)
    _validate_numbers(text, snapshot)

    for heading, body in _section_blocks(text):
        if heading not in MAJOR_SECTIONS:
            continue
        lines = [line.strip() for line in body.splitlines() if line.strip() and not line.strip().startswith("|")]
        if lines and not any(EVIDENCE_TOKEN_RE.search(line) for line in lines):
            raise ValueError(f"Section lacks evidence IDs: {heading}")

    _validate_low_confidence_language(text, snapshot)
    return text + "\n"


def validate_section(
    markdown: str,
    snapshot: dict,
    *,
    section_heading: str,
    allowed_evidence_ids: set[str],
    evidence_locked: bool = True,
) -> str:
    text = strip_thinking_text(markdown)
    if section_heading not in text:
        raise ValueError(f"Missing section heading: {section_heading}")
    start = text.find(section_heading)
    text = text[start:].strip()
    next_heading = re.search(r"\n## ", text[len(section_heading):])
    if next_heading:
        text = text[: len(section_heading) + next_heading.start()].rstrip()
    if not evidence_locked:
        return text + "\n"

    if not allowed_evidence_ids:
        raise ValueError("Section has no allowed evidence IDs.")
    _validate_evidence_ids(text, allowed_evidence_ids)
    _validate_tickers(text, snapshot)
    _validate_numbers(text, snapshot)
    lines = [line.strip() for line in text.splitlines()[1:] if line.strip() and not line.strip().startswith("|")]
    if lines and not any(EVIDENCE_TOKEN_RE.search(line) for line in lines):
        raise ValueError(f"Section lacks evidence IDs: {section_heading}")
    _validate_low_confidence_language(text, snapshot)
    return text + "\n"
