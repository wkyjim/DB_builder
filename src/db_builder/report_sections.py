"""Shared markdown helpers for market intelligence reports."""

from __future__ import annotations

from datetime import datetime


def as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if hasattr(value, "tolist"):
        return value.tolist()
    return []


def first(rows: list[dict]) -> dict:
    return rows[0] if rows else {}


def flt(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt(value, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    try:
        return str(round(float(value), digits))
    except (TypeError, ValueError):
        return str(value)


def heading(title: str) -> list[str]:
    return [f"## {title}", ""]


def bullet(label: str, value) -> str:
    return f"- {label}: **{value}**"


def comma(items: list, fallback: str = "n/a") -> str:
    cleaned = [str(item) for item in items if item]
    return ", ".join(cleaned) if cleaned else fallback


def generated_header(title: str, generated_at: datetime, window_hours: int) -> list[str]:
    return [
        f"# {title}",
        "",
        f"Generated at: {generated_at.isoformat()}",
        f"Window: {window_hours}h",
        "",
    ]


def table(headers: list[str], rows: list[list]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return lines
