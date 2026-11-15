"""Canonical SEC period resolution for research evidence.

A metric is research-eligible only when its fiscal label is supported by the
reported period and derivation lineage. Date arithmetic alone is not sufficient.
"""
from __future__ import annotations


def resolve_period(row: dict) -> dict:
    ptype = (row.get("period_type") or "").upper()
    formula = (row.get("derivation_method") or row.get("formula") or "").lower()
    original = (row.get("fiscal_period_original") or row.get("fiscal_period") or "").upper()
    metric_fp = (row.get("metric_fiscal_period") or "").upper()
    if ptype == "Y":
        return {"status": "verified", "label": "FY", "reason": "reported annual"}
    if ptype == "TTM":
        return {"status": "verified", "label": "TTM", "reason": "four-quarter TTM"}
    if ptype != "Q":
        return {"status": "ambiguous", "label": None, "reason": "unsupported period type"}
    if formula in {"fy_minus_9m", "ytd_9m_minus_6m", "ytd_6m_minus_3m"}:
        expected = {"fy_minus_9m": "Q4", "ytd_9m_minus_6m": "Q3",
                    "ytd_6m_minus_3m": "Q2"}[formula]
        if metric_fp == expected:
            return {"status": "verified", "label": expected, "reason": formula}
        return {"status": "ambiguous", "label": None,
                "reason": f"derivation {formula} conflicts with normalized label {metric_fp or 'missing'}"}
    if formula == "reported_quarter":
        if original in {"Q1", "Q2", "Q3", "Q4"} and (not metric_fp or metric_fp == original):
            return {"status": "verified", "label": original, "reason": "reported quarter"}
        return {"status": "ambiguous", "label": None,
                "reason": f"reported quarter has unsupported original fiscal label {original or 'missing'}"}
    return {"status": "ambiguous", "label": None,
            "reason": f"unknown derivation {formula or 'missing'}"}


def render_fact(row: dict) -> str:
    """Render a canonical fact without asking AI to reproduce it."""
    p = resolve_period(row)
    if p["status"] != "verified":
        return "UNAVAILABLE: ambiguous canonical period"
    return f"{row.get('ticker')} {row.get('metric')} {p['label']} {row.get('period')}: {row.get('value')} {row.get('unit')}"
