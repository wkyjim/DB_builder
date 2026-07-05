"""Validation and audits for multistage DeepSeek CIO outputs."""

from __future__ import annotations

import json
import re
from typing import Any

from db_builder.deepseek_report_validator import (
    allowed_evidence_ids,
    allowed_tickers,
    strip_thinking_text,
)
from db_builder.deepseek_stage_outputs import SCENARIOS
from db_builder.deepseek_stage_outputs import confidence_percent
from db_builder.deepseek_stage_outputs import macro_dashboard_lines


FINAL_HEADINGS = [
    "# Qwen Multi-Stage CIO Report",
    "## Executive Summary",
    "## Macro Market Dashboard",
    "## Macro Regime Classification",
    "## News Analysis",
    "## Evidence Table",
    "## Cross-Asset Confirmation",
    "## Market Narrative",
    "## Scenario Analysis",
    "## Sector Analysis",
    "## Risk Register",
    "## Contradictions",
    "## Investment Recommendations",
    "## Invalidators",
    "## Confidence Assessment",
]

RECOMMENDATION_WORDS = re.compile(r"\b(overweight|underweight|buy|sell|trim|reduce|add|allocate|position)\b", re.IGNORECASE)
ALLOWED_MACRO_TOKENS = {
    "BOE",
    "BOJ",
    "CPI",
    "DXY",
    "ECB",
    "FOMC",
    "GDP",
    "NFP",
    "OPEC",
    "PMI",
    "PPI",
    "VIX",
}


def extract_json_object(text: str) -> dict:
    cleaned = strip_thinking_text(text)
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("No JSON object found.")
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(cleaned)):
        char = cleaned[idx]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(cleaned[start : idx + 1])
    raise ValueError("Unbalanced JSON object.")


def _walk_ids(value: Any) -> set[str]:
    found = set()
    if isinstance(value, dict):
        for item in value.values():
            found.update(_walk_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_walk_ids(item))
    elif isinstance(value, str):
        found.update(re.findall(r"\b[A-Z]+_\d{3}\b", value))
    return found


def _ensure_ids_exist(payload: dict, snapshot: dict) -> None:
    allowed = allowed_evidence_ids(snapshot)
    for evidence_id in _walk_ids(payload):
        if evidence_id not in allowed:
            raise ValueError(f"Unsupported evidence ID: {evidence_id}")


def _ensure_tickers_supported(payload: dict, snapshot: dict) -> None:
    approved = allowed_tickers(snapshot) | ALLOWED_MACRO_TOKENS | {"OW", "UW", "CIO", "ETF", "ETFs", "AI", "IPO", "FX", "USD", "ID"}
    text = json.dumps(payload, ensure_ascii=True)
    for token in re.findall(r"\b(?:[A-Z]{2,5}(?:-[A-Z]{3})?|\^[A-Z]{2,5})\b", text):
        if token not in approved:
            raise ValueError(f"Unsupported ticker/ETF: {token}")


def _check_probability_sum(probabilities: dict) -> None:
    total = sum(float(probabilities.get(key, 0)) for key in SCENARIOS)
    if abs(total - 100) > 0.01:
        raise ValueError(f"Scenario probabilities must sum to 100, got {total}.")


def _number_tokens(text: str) -> set[str]:
    tokens = set()
    for match in re.findall(r"(?<![A-Z_])\b\d+(?:\.\d+)?\b", text):
        tokens.add(match)
        try:
            number = float(match)
            tokens.add(str(int(number)))
            for digits in (2, 4, 6):
                tokens.add(f"{number:.{digits}f}".rstrip("0").rstrip("."))
        except ValueError:
            pass
    return tokens


def _locked_probability_table(probabilities: dict) -> str:
    bull = int(probabilities.get("risk_on_expansion") or 0)
    bear = int(probabilities.get("defensive_slowdown") or 0) + int(probabilities.get("risk_off_shock") or 0)
    base = max(0, 100 - bull - bear)
    lines = ["| Scenario | Probability | Source |", "|---|---:|---|"]
    lines.append(f"| Base Case | {base} | soft_landing + growth_plus_inflation |")
    lines.append(f"| Bull Case | {bull} | risk_on_expansion |")
    lines.append(f"| Bear Case | {bear} | defensive_slowdown + risk_off_shock |")
    return "\n".join(lines)


def _replace_scenario_probability_table(text: str, probabilities: dict) -> str:
    if "## Scenario Analysis" not in text or not probabilities:
        return text
    locked = "## Scenario Analysis\n" + _locked_probability_table(probabilities)
    pattern = r"## Scenario Analysis\s*\n.*?(?=\n## |\Z)"
    return re.sub(pattern, locked, text, count=1, flags=re.DOTALL)


def _replace_macro_dashboard(text: str, snapshot: dict) -> str:
    if "## Macro Market Dashboard" not in text:
        return text
    dashboard = "## Macro Market Dashboard\n" + "\n".join(macro_dashboard_lines(snapshot))
    pattern = r"## Macro Market Dashboard\s*\n.*?(?=\n## |\Z)"
    return re.sub(pattern, dashboard, text, count=1, flags=re.DOTALL)


def _uppercase_tokens(text: str) -> set[str]:
    return set(re.findall(r"\b(?:[A-Z]{2,5}(?:-[A-Z]{3})?|\^[A-Z]{2,5})\b", text))


def _evidence_id_set(items: Any) -> set[str]:
    ids = set()
    if isinstance(items, dict):
        items = [items]
    for item in items or []:
        if isinstance(item, str):
            ids.add(item)
        elif isinstance(item, dict):
            evidence_id = item.get("evidence_id")
            if evidence_id:
                ids.add(str(evidence_id))
            ids.update(_walk_ids(item))
    return ids


def _risk_evidence_ids(risk: dict) -> set[str]:
    ids = _evidence_id_set(risk.get("evidence_ids"))
    ids.update(_evidence_id_set(risk.get("evidence_id")))
    return ids


def validate_stage_output(stage_name: str, payload: dict, snapshot: dict) -> dict:
    if payload.get("stage") != stage_name:
        raise ValueError(f"Wrong stage: {payload.get('stage')} expected {stage_name}")
    _ensure_ids_exist(payload, snapshot)
    _ensure_tickers_supported(payload, snapshot)

    if stage_name == "evidence_extraction":
        if not isinstance(payload.get("facts"), list):
            raise ValueError("facts must be a list.")
        for fact in payload.get("facts", []):
            if RECOMMENDATION_WORDS.search(str(fact.get("signal", ""))):
                raise ValueError("Evidence extraction cannot include recommendations.")
    elif stage_name == "regime_scoring":
        _check_probability_sum(payload.get("scenario_probabilities") or {})
        confidence = float(payload.get("confidence") or 0)
        if confidence < 40 and "clean risk-on" in str(payload.get("regime_summary", "")).lower():
            raise ValueError("Low confidence regime summary cannot say clean risk-on.")
    elif stage_name == "cross_asset_validation":
        tests = payload.get("asset_class_tests") or []
        covered = {str(test.get("asset_class")) for test in tests}
        if "equities" not in covered or "rates" not in covered:
            raise ValueError("Cross-asset validation must include equities and rates when available.")
        if payload.get("key_contradictions") is None:
            raise ValueError("Contradictions must be explicit.")
    elif stage_name == "news_weighting":
        noise = _evidence_id_set(payload.get("narrative_noise"))
        for risk in payload.get("top_systemic_risks") or []:
            ids = _risk_evidence_ids(risk)
            if not ids:
                raise ValueError("Every top systemic risk needs evidence IDs.")
            if ids & noise:
                raise ValueError("Narrative noise cannot be a top systemic risk.")
    elif stage_name == "portfolio_construction":
        confidence_constraints = " ".join(str(item) for item in (payload.get("confidence_constraints") or [])).lower()
        low_conf = (
            confidence_percent(snapshot) < 40
            or "below 40" in confidence_constraints
            or "low confidence" in confidence_constraints
        )
        if low_conf and payload.get("overall_stance") == "aggressive_risk_on":
            raise ValueError("Low confidence cannot support aggressive_risk_on.")
        buy_assets = {row.get("asset") for row in payload.get("tactical_buy_list") or []}
        reduce_assets = {row.get("asset") for row in payload.get("reduce_list") or []}
        overlap = buy_assets & reduce_assets
        if overlap:
            raise ValueError(f"Same asset in buy and reduce lists: {sorted(overlap)}")
        if low_conf:
            for row in payload.get("sector_recommendations") or []:
                if row.get("bias") == "OW" or row.get("sizing") == "core_overweight":
                    raise ValueError("Low confidence cannot support aggressive overweight.")
    return payload


def validate_final_report(markdown: str, stage_outputs: dict, snapshot: dict) -> str:
    text = strip_thinking_text(markdown)
    probs = stage_outputs.get("regime_scoring", {}).get("scenario_probabilities", {})
    text = _replace_macro_dashboard(text, snapshot)
    text = _replace_scenario_probability_table(text, probs)
    missing = [heading for heading in FINAL_HEADINGS if heading not in text]
    if missing:
        raise ValueError(f"Missing final report headings: {missing}")
    snapshot_text = json.dumps(snapshot, ensure_ascii=True, default=str)
    tickers = allowed_tickers(snapshot) | _uppercase_tokens(snapshot_text) | ALLOWED_MACRO_TOKENS | {"CIO", "ETF", "ETFs", "AI", "IPO", "FX", "USD", "OW", "UW", "ID"}
    for token in re.findall(r"\b(?:[A-Z]{2,5}(?:-[A-Z]{3})?|\^[A-Z]{2,5})\b", text):
        if token not in tickers:
            raise ValueError(f"Unsupported ticker/ETF: {token}")
    for scenario, probability in probs.items():
        if scenario not in text:
            raise ValueError("Final report scenario probabilities do not match Stage 2.")
    bull = int(probs.get("risk_on_expansion") or 0)
    bear = int(probs.get("defensive_slowdown") or 0) + int(probs.get("risk_off_shock") or 0)
    base = max(0, 100 - bull - bear)
    for label, probability in {"Base Case": base, "Bull Case": bull, "Bear Case": bear}.items():
        if label not in text or str(probability) not in text:
            raise ValueError("Final report scenario probabilities do not match Stage 2.")
    allowed_numbers = _number_tokens(json.dumps(stage_outputs, ensure_ascii=True, default=str))
    allowed_numbers.update(_number_tokens(snapshot_text))
    allowed_numbers.update(_number_tokens(_locked_probability_table(probs)))
    allowed_numbers.update(_number_tokens("\n".join(macro_dashboard_lines(snapshot))))
    unsupported_numbers = sorted(_number_tokens(text) - allowed_numbers)
    if unsupported_numbers:
        raise ValueError(f"Unsupported numeric value in final report: {unsupported_numbers[0]}")
    return text.rstrip() + "\n"


def consistency_audit(stage_outputs: dict, final_markdown: str) -> list[dict]:
    warnings = []
    regime = stage_outputs.get("regime_scoring", {})
    portfolio = stage_outputs.get("portfolio_construction", {})
    news = stage_outputs.get("news_weighting", {})
    confidence = float(regime.get("confidence") or 0)
    buy_assets = {row.get("asset") for row in portfolio.get("tactical_buy_list") or []}
    reduce_assets = {row.get("asset") for row in portfolio.get("reduce_list") or []}
    overlap = sorted(asset for asset in buy_assets & reduce_assets if asset)
    if overlap:
        warnings.append({"severity": "serious", "issue": f"Same asset in buy and reduce: {overlap}"})
    if confidence < 40:
        if portfolio.get("overall_stance") == "aggressive_risk_on":
            warnings.append({"severity": "serious", "issue": "Low confidence with aggressive allocation."})
        if "clean risk-on" in final_markdown.lower():
            warnings.append({"severity": "serious", "issue": "Report says clean risk-on while confidence is below 40."})
    noise = _evidence_id_set(news.get("narrative_noise"))
    for risk in news.get("top_systemic_risks") or []:
        if noise & _risk_evidence_ids(risk):
            warnings.append({"severity": "serious", "issue": "Narrative-noise item ranked as top systemic risk."})
    for row in portfolio.get("sector_recommendations") or []:
        if confidence < 40 and row.get("bias") == "OW":
            warnings.append({"severity": "serious", "issue": f"Low confidence but {row.get('sector')} is OW."})
        if row.get("sector") == "Consumer Staples" and row.get("bias") in {"OW", "Modest OW"} and "defensive" not in str(row.get("reason", "")).lower():
            warnings.append({"severity": "minor", "issue": "Staples overweight lacks defensive rationale."})
    return warnings
