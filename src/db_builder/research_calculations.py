"""Deterministic research calculations and claim arithmetic.

This module is intentionally pure and is used by controlled research validation.
"""
from __future__ import annotations

import ast
import math
from typing import Mapping


def _finite(value: float, name: str = "value") -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def growth_pct(current: float, prior: float) -> float:
    current = _finite(current, "current")
    prior = _finite(prior, "prior")
    if prior == 0:
        raise ValueError("prior must be non-zero")
    return (current / prior - 1.0) * 100.0


def margin_pct(numerator: float, denominator: float) -> float:
    numerator = _finite(numerator, "numerator")
    denominator = _finite(denominator, "denominator")
    if denominator == 0:
        raise ValueError("denominator must be non-zero")
    return numerator / denominator * 100.0


def parse_fiscal_label(label: str) -> str:
    """Normalize FY2027 or Q3 FY2026; never infer a year from a bare suffix."""
    import re
    m = re.fullmatch(r"\s*(?:FY\s*(\d{4})|Q\s*([1-4])\s+FY\s*(\d{4}))\s*", label.strip(), re.I)
    if not m:
        raise ValueError(f"unsupported fiscal label: {label}")
    if m.group(1):
        return f"FY{m.group(1)}"
    return f"Q{m.group(2)} FY{m.group(3)}"

def pct_to_bps(pct: float) -> float:
    return _finite(pct) * 100.0


def usd_billions_to_millions(value: float) -> float:
    return _finite(value) * 1000.0


class FormulaError(ValueError):
    pass


_ALLOWED = {
    "growth_pct": growth_pct,
    "margin_pct": margin_pct,
    "pct_to_bps": pct_to_bps,
    "usd_billions_to_millions": usd_billions_to_millions,
    "abs": abs,
    "min": min,
    "max": max,
}


def evaluate_calculation(formula: str, inputs: Mapping[str, float]) -> float:
    """Evaluate a deliberately small arithmetic expression.

    Inputs are named scalar values referenced by the calculation record; arbitrary
    Python calls or attributes are rejected.
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"invalid formula syntax: {exc}") from exc

    def ev(node):
        if isinstance(node, ast.Expression): return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return _finite(node.value)
        if isinstance(node, ast.Name):
            if node.id not in inputs: raise FormulaError(f"unknown input: {node.id}")
            return _finite(inputs[node.id], node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = ev(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add): return left + right
            if isinstance(node.op, ast.Sub): return left - right
            if isinstance(node.op, ast.Mult): return left * right
            if right == 0: raise FormulaError("division by zero")
            return left / right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _ALLOWED:
            if node.keywords: raise FormulaError("keyword arguments are not allowed")
            return _ALLOWED[node.func.id](*[ev(a) for a in node.args])
        raise FormulaError(f"unsupported expression: {type(node).__name__}")
    result = ev(tree)
    return _finite(result, "result")
