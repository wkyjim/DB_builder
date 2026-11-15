"""Evidence-aware narrative validation for P2O research outputs."""
from __future__ import annotations

import re


def _numbers(text: str) -> list[str]:
    return re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)?", text)


def _num(t: str) -> float:
    return float(t.replace(",", ""))


def _allowed_values(evidence: list[dict]) -> set[float]:
    out: set[float] = set()
    for e in evidence:
        for key in ("value", "low", "high"):
            v = e.get(key)
            if isinstance(v, (int, float)):
                v = abs(float(v))
                out.add(v)
                unit = str(e.get("unit", "")).lower()
                if "billions" in unit or unit in ("usd", "$"):
                    out.add(v * 1_000_000_000)
                    out.add(v / 1_000_000_000)
                if "%" in unit or "percent" in unit:
                    out.add(v / 100)
                    out.add(v * 100)
    return out


def validate_narrative(text: str, evidence: list[dict], ticker: str) -> list[str]:
    """Return flags for unsupported numbers, periods or estimates in a narrative.

    Evidence is the selected canonical evidence for this ticker. Claims matching it
    are accepted; contradictions and unsupported assertions are rejected.
    """
    flags = []
    low = text.lower()
    if re.search(r"\b(price target|target price|probability|probabilities)\b", low):
        flags.append("unsupported_target_or_probability")
    labels = set()
    for e in evidence:
        p = e.get("canonical_period") or e.get("metric_fiscal_period")
        if p and re.fullmatch(r"(Q[1-4]|FY\d{4}|FY|TTM)", str(p), re.I):
            labels.add(str(p).upper())
    for m in re.finditer(r"\b(Q[1-4]|FY\s*\d{4}|FY)\b", text, re.I):
        label = m.group(1).upper().replace(" ", "")
        if label not in labels and label not in {"FY2026", "FY2027", "FY2028", "FY2029"}:
            flags.append(f"unsupported_period:{ticker}:{label}")
    allowed = _allowed_values(evidence); allowed |= {2026, 2027, 2028, 2029}
    for tok in _numbers(text):
        v = abs(_num(tok))
        if v in allowed: continue
        if not any(abs(v - a) <= max(0.01, abs(a) * 0.01) for a in allowed):
            flags.append(f"unsupported_number:{ticker}:{tok}")
    if re.search(r"\b(consensus|guidance|management expects|company expects)\b", low):
        if not any(e.get("kind") in {"MANAGEMENT_GUIDANCE_TYPED", "VENDOR_ESTIMATE"} for e in evidence):
            flags.append(f"unsupported_forward_claim:{ticker}")
    return sorted(set(flags))
