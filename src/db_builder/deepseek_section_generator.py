"""Section-by-section DeepSeek CIO report generation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable

from db_builder.deepseek_report_validator import (
    allowed_evidence_ids,
    validate_section,
)
from db_builder.knowledge_retriever import retrieve_knowledge


@dataclass(frozen=True)
class ReportSection:
    heading: str
    allowed_prefixes: tuple[str, ...]
    task: str


SECTION_SPECS = [
    ReportSection(
        "## Executive Summary",
        ("REGIME", "CONFIDENCE", "SECTOR", "THEME", "RISK"),
        "Summarize dominant regime, confidence, market character, portfolio stance, and the three most important conclusions.",
    ),
    ReportSection(
        "## Market Bullishness / Bearishness",
        ("REGIME", "MACRO", "TECH", "NEWS", "RISK", "CONFIDENCE"),
        "Weigh bullish, bearish, and neutral market evidence and state the critical interpretation.",
    ),
    ReportSection(
        "## Investor Sentiment",
        ("REGIME", "MACRO", "TECH", "NEWS", "RISK"),
        "Interpret institutional, retail/speculative, volatility, and risk-appetite signals.",
    ),
    ReportSection(
        "## Cross-Asset Confirmation",
        ("MACRO", "TECH", "RISK"),
        "Assess equities, rates, FX, commodities, volatility, crypto, and whether cross-assets confirm the regime.",
    ),
    ReportSection(
        "## Sector Strength Ranking",
        ("SECTOR", "THEME", "TECH", "NEWS"),
        "Rank sectors by tactical regime, secular support, opportunity, risk, and portfolio bias.",
    ),
    ReportSection(
        "## Highest Conviction Opportunities",
        ("OPPORTUNITY", "SECTOR", "THEME", "NEWS"),
        "Group opportunities into conviction tiers and explain tactical and secular alignment.",
    ),
    ReportSection(
        "## Tactical Positioning",
        ("SECTOR", "THEME", "TECH", "RISK", "CONFIDENCE"),
        "Translate evidence into overweight, neutral, underweight, avoid, and reduce positioning.",
    ),
    ReportSection(
        "## Tactical Buy List",
        ("SECTOR", "THEME", "TECH", "OPPORTUNITY"),
        "List buy-on-pullback, hold/monitor, and avoid-chasing candidates.",
    ),
    ReportSection(
        "## Tactical Sell / Reduce List",
        ("SECTOR", "RISK", "TECH", "CONFIDENCE"),
        "List reduce-into-strength, avoid, and hedge candidates.",
    ),
    ReportSection(
        "## Key Risks",
        ("RISK", "MACRO", "NEWS", "REGIME"),
        "Identify the key market risks and portfolio impact.",
    ),
    ReportSection(
        "## Risk Management",
        ("REGIME", "CONFIDENCE", "RISK", "MACRO", "TECH"),
        "Define exposure discipline, cash buffer, hedging ideas, max aggression rule, and what not to do.",
    ),
    ReportSection(
        "## Final House View",
        ("REGIME", "CONFIDENCE", "SECTOR", "THEME", "RISK"),
        "Write one concise CIO house-view paragraph.",
    ),
]


def _shorten(text: str, *, max_chars: int = 420) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "..."


def evidence_rows_for_section(snapshot: dict, prefixes: tuple[str, ...], *, limit: int = 12) -> list[dict]:
    rows = []
    per_prefix = max(2, limit // max(1, len(prefixes)))
    for prefix in prefixes:
        prefix_rows = [
            row
            for row in snapshot.get("evidence_appendix", [])
            if str(row.get("evidence_id", "")).startswith(f"{prefix}_")
        ]
        rows.extend(prefix_rows[:per_prefix])
    return rows[:limit]


def compact_evidence_rows(rows: list[dict]) -> list[dict]:
    return [
        {
            "evidence_id": row.get("evidence_id"),
            "section": row.get("section"),
            "summary": _shorten(str(row.get("summary", ""))),
        }
        for row in rows
    ]


def section_snapshot(snapshot: dict, spec: ReportSection) -> dict:
    evidence_rows = compact_evidence_rows(evidence_rows_for_section(snapshot, spec.allowed_prefixes))
    return {
        "generated_at": snapshot.get("generated_at"),
        "window_hours": snapshot.get("window_hours"),
        "section": spec.heading,
        "allowed_evidence_ids": [row["evidence_id"] for row in evidence_rows],
        "evidence": evidence_rows,
        "approved_ticker_universe": snapshot.get("approved_ticker_universe", []),
        "approved_etf_label_map": snapshot.get("approved_etf_label_map", {}),
        "market_state": snapshot.get("market_state") if "REGIME" in spec.allowed_prefixes else None,
    }


def build_section_prompt(spec: ReportSection, snapshot: dict, knowledge_chunks: list[dict]) -> list[dict]:
    mini_snapshot = section_snapshot(snapshot, spec)
    knowledge = "\n\n".join(
        f"### {chunk['file_name']} / {chunk['heading']}\n{_shorten(chunk['content'], max_chars=900)}"
        for chunk in knowledge_chunks[:2]
    )
    allowed_ids = mini_snapshot["allowed_evidence_ids"]
    system = (
        "You are an offline CIO-level investment strategist. "
        "Use only the provided evidence. Do not browse. Do not invent facts. "
        "Analyze internally, but output only the requested markdown section."
    )
    user = (
        f"SECTION TITLE:\n{spec.heading}\n\n"
        f"SECTION TASK:\n{spec.task}\n\n"
        "RELEVANT KNOWLEDGE:\n"
        f"{knowledge}\n\n"
        "SECTION SNAPSHOT:\n"
        f"{json.dumps(mini_snapshot, ensure_ascii=True, indent=2)}\n\n"
        "RULES:\n"
        "- Write only the requested section.\n"
        f"- The first line must be exactly: {spec.heading}\n"
        f"- Allowed evidence IDs are: {', '.join(allowed_ids)}.\n"
        "- Every substantive claim must include inline evidence IDs in brackets.\n"
        "- Never create evidence IDs.\n"
        "- Do not use unsupported tickers, ETFs, or numbers.\n"
        "- If evidence is insufficient, say it is insufficient and cite the closest evidence.\n"
        "- Include: what the evidence says, what it means, why it matters, what could be wrong, and portfolio implication."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def fallback_section(spec: ReportSection, snapshot: dict, *, reason: str | None = None) -> str:
    rows = compact_evidence_rows(evidence_rows_for_section(snapshot, spec.allowed_prefixes, limit=4))
    ids = [row["evidence_id"] for row in rows]
    citation = "[" + ", ".join(ids[:2]) + "]" if ids else ""
    lines = [
        spec.heading,
        "",
        f"- Fallback used: DeepSeek section output did not pass evidence validation. {citation}".rstrip(),
    ]
    if reason:
        lines.append(f"- Validation reason: {reason}")
    if rows:
        lines.append(f"- Evidence base: {rows[0]['summary']} [{rows[0]['evidence_id']}]")
    else:
        lines.append("- Evidence base: No section-specific evidence was available.")
    lines.append("- Portfolio implication: Treat this section as deterministic fallback until a valid model section is generated.")
    return "\n".join(lines).rstrip() + "\n"


def evidence_appendix_markdown(snapshot: dict, *, limit: int = 80) -> str:
    lines = ["## Evidence Appendix", ""]
    for row in snapshot.get("evidence_appendix", [])[:limit]:
        lines.append(f"- [{row['evidence_id']}] {row['section']}: {row['summary']}")
    return "\n".join(lines).rstrip() + "\n"


def validation_summary_markdown(results: list[dict]) -> str:
    lines = ["## Validation Summary", "", "| Section | Status | Reason |", "|---|---|---|"]
    for result in results:
        status = "valid_deepseek" if result["valid"] else "fallback_used"
        reason = str(result.get("reason") or "").replace("|", "/")
        lines.append(f"| {result['heading'].replace('## ', '')} | {status} | {reason} |")
    return "\n".join(lines).rstrip() + "\n"


def assemble_section_report(sections: list[str], validation_results: list[dict], evidence_appendix: str) -> str:
    body = ["# DeepSeek Offline CIO Report", ""]
    body.extend(section.strip() for section in sections)
    body.extend(["", validation_summary_markdown(validation_results).strip(), "", evidence_appendix.strip(), ""])
    return "\n\n".join(part for part in body if part).rstrip() + "\n"


def generate_sections(
    snapshot: dict,
    *,
    call_model: bool,
    call_section_fn: Callable[[list[dict]], str],
    evidence_locked: bool = True,
    max_section_retries: int = 1,
) -> dict:
    sections = []
    validation_results = []
    all_messages = []
    knowledge_chunks = retrieve_knowledge("offline CIO section market sector risk report", snapshot)
    snapshot_ids = allowed_evidence_ids(snapshot)

    for spec in SECTION_SPECS:
        messages = build_section_prompt(spec, snapshot, knowledge_chunks)
        all_messages.append({"heading": spec.heading, "messages": messages})
        if not call_model:
            sections.append(fallback_section(spec, snapshot, reason="dry-run preview"))
            validation_results.append({"heading": spec.heading, "valid": False, "reason": "dry-run preview"})
            continue

        last_reason = ""
        section_done = False
        for _attempt in range(max(1, max_section_retries + 1)):
            try:
                raw = call_section_fn(messages)
            except Exception as exc:
                last_reason = f"model call failed: {exc}"
                break
            try:
                section = validate_section(
                    raw,
                    snapshot,
                    section_heading=spec.heading,
                    allowed_evidence_ids=set(section_snapshot(snapshot, spec)["allowed_evidence_ids"]) & snapshot_ids,
                    evidence_locked=evidence_locked,
                )
                sections.append(section)
                validation_results.append({"heading": spec.heading, "valid": True, "reason": ""})
                section_done = True
                break
            except Exception as exc:
                last_reason = str(exc)
        if not section_done:
            sections.append(fallback_section(spec, snapshot, reason=last_reason))
            validation_results.append({"heading": spec.heading, "valid": False, "reason": last_reason})

    valid_count = sum(1 for result in validation_results if result["valid"])
    fallback_count = len(validation_results) - valid_count
    return {
        "sections": sections,
        "validation_results": validation_results,
        "messages": all_messages,
        "knowledge_chunks": knowledge_chunks,
        "valid_count": valid_count,
        "fallback_count": fallback_count,
    }
