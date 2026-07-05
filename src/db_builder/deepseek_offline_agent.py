"""Offline Ollama DeepSeek CIO agent orchestration."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Callable

from db_builder.deepseek_report_validator import validate_report
from db_builder.deepseek_section_generator import (
    assemble_section_report,
    evidence_appendix_markdown,
    generate_sections,
)
from db_builder.deepseek_snapshot_builder import build_offline_snapshot
from db_builder.investment_report import save_report
from db_builder.knowledge_retriever import find_knowledge_file, retrieve_knowledge
from db_builder.news_classifier import _ollama_chat, ollama_deep_model, ollama_url


REQUIRED_REPORT_SKELETON = """# DeepSeek CIO House View

## Executive Summary

## Market Bullishness / Bearishness

## Investor Sentiment

## Cross-Asset Confirmation

## Sector Strength Ranking

## Highest Conviction Opportunities

## Tactical Positioning

## Tactical Buy List

## Tactical Sell / Reduce List

## Key Risks

## Risk Management

## Alternative Scenario

## Final House View

## Evidence Appendix
"""


def _read_knowledge_file(file_name: str) -> str:
    path = find_knowledge_file(file_name)
    if not path:
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def build_offline_prompt(snapshot: dict, knowledge_chunks: list[dict]) -> list[dict]:
    system_prompt = _read_knowledge_file("deepseek_agent_system_prompt.md")
    playbook = _read_knowledge_file("deepseek_report_generation_playbook.md")
    knowledge = "\n\n".join(
        f"### {chunk['file_name']} / {chunk['heading']}\n{chunk['content']}"
        for chunk in knowledge_chunks
    )
    evidence_ids = [
        str(row.get("evidence_id"))
        for row in snapshot.get("evidence_appendix", [])
        if row.get("evidence_id")
    ]
    system = (
        f"{system_prompt}\n\n"
        "Analyze internally step by step. Do not show chain of thought. "
        "Output only the final report. Use evidence IDs for every major claim. "
        "Copy the required markdown headings exactly and do not omit sections."
    )
    user = (
        "RELEVANT KNOWLEDGE CHUNKS:\n"
        f"{knowledge}\n\n"
        "STRUCTURED SNAPSHOT:\n"
        f"{json.dumps(snapshot, ensure_ascii=True, indent=2)}\n\n"
        "REPORT GENERATION PLAYBOOK:\n"
        f"{playbook}\n\n"
        "EVIDENCE RULES:\n"
        "- Do not invent facts.\n"
        "- Use evidence IDs inline.\n"
        f"- Allowed evidence IDs are: {', '.join(evidence_ids)}.\n"
        "- Never create new evidence IDs.\n"
        "- Every non-empty major section must contain bracketed evidence IDs like [REGIME_001].\n"
        "- Do not write a claim unless it has at least one bracketed evidence ID.\n"
        "- Correct citation style: Semiconductors remain supported by trend evidence [TECH_004, SECTOR_002].\n"
        "- Incorrect citation style: Evidence ID: TECH_004.\n"
        "- If evidence is mixed, say mixed.\n"
        "- If data is missing, say missing.\n"
        "- Challenge deterministic outputs where evidence disagrees.\n"
        "- Output the required markdown report only.\n\n"
        "REQUIRED MARKDOWN SKELETON - COPY THESE HEADINGS EXACTLY:\n"
        f"{REQUIRED_REPORT_SKELETON}\n"
        "If a section has limited evidence, write one concise evidence-backed sentence under it."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def prompt_preview(messages: list[dict], *, max_chars: int = 6000) -> str:
    combined = "\n\n".join(f"{message['role'].upper()}:\n{message['content']}" for message in messages)
    if len(combined) <= max_chars:
        return combined
    return combined[:max_chars].rstrip() + "\n\n...[prompt truncated for preview]..."


def call_deepseek(
    messages: list[dict],
    *,
    model: str | None = None,
    timeout: int = 900,
    chat_fn: Callable[..., dict] = _ollama_chat,
) -> str:
    body = chat_fn(
        messages,
        url=ollama_url(),
        model=model or ollama_deep_model(),
        timeout=timeout,
        options={"temperature": 0.2, "num_predict": 8000},
        response_format=None,
    )
    return str(body.get("message", {}).get("content", ""))


def diagnostics_markdown(*, reason: str, raw_response: str | None, snapshot: dict, include_raw_response: bool = False) -> str:
    lines = [
        "# DeepSeek Offline Report Diagnostics",
        "",
        f"Generated at: {datetime.now().isoformat()}",
        f"Validation failure: {reason}",
        "",
        "## Evidence Appendix",
        "",
    ]
    for row in snapshot.get("evidence_appendix", [])[:40]:
        lines.append(f"- [{row['evidence_id']}] {row['section']}: {row['summary']}")
    if include_raw_response and raw_response:
        lines.extend(["", "## Raw Response", "", raw_response[:20000]])
    return "\n".join(lines).rstrip() + "\n"


def append_evidence_appendix_if_missing(markdown: str, snapshot: dict) -> str:
    if "## Evidence Appendix" in (markdown or ""):
        return markdown
    return (markdown or "").rstrip() + "\n\n" + evidence_appendix_markdown(snapshot)


def generate_offline_report(
    engine,
    *,
    window_hours: int,
    call_model: bool = False,
    timeout: int = 900,
    model: str | None = None,
    evidence_locked: bool = True,
    include_raw_response: bool = False,
    section_mode: bool = True,
    section_timeout: int = 180,
    max_section_retries: int = 1,
    chat_fn: Callable[..., dict] = _ollama_chat,
) -> dict:
    snapshot = build_offline_snapshot(engine, window_hours=window_hours)
    if section_mode:
        def call_section(messages: list[dict]) -> str:
            return call_deepseek(messages, model=model, timeout=section_timeout, chat_fn=chat_fn)

        section_result = generate_sections(
            snapshot,
            call_model=call_model,
            call_section_fn=call_section,
            evidence_locked=evidence_locked,
            max_section_retries=max_section_retries,
        )
        markdown = assemble_section_report(
            section_result["sections"],
            section_result["validation_results"],
            evidence_appendix_markdown(snapshot),
        )
        valid_count = section_result["valid_count"]
        fallback_count = section_result["fallback_count"]
        status = "valid" if call_model and valid_count >= 8 else "preview" if not call_model else "invalid"
        reason = None if status == "valid" else f"{valid_count} valid sections, {fallback_count} fallback sections"
        return {
            "status": status,
            "snapshot": snapshot,
            "knowledge_chunks": section_result["knowledge_chunks"],
            "messages": section_result["messages"],
            "markdown": markdown if call_model else "",
            "raw_response": None,
            "reason": reason,
            "section_mode": True,
            "valid_sections": valid_count,
            "fallback_sections": fallback_count,
            "validation_results": section_result["validation_results"],
        }

    knowledge_chunks = retrieve_knowledge("offline CIO market regime sector risk cross-asset report", snapshot)
    messages = build_offline_prompt(snapshot, knowledge_chunks)
    if not call_model:
        return {
            "status": "preview",
            "snapshot": snapshot,
            "knowledge_chunks": knowledge_chunks,
            "messages": messages,
            "markdown": "",
            "raw_response": None,
            "reason": None,
            "section_mode": False,
        }

    raw_response = call_deepseek(messages, model=model, timeout=timeout, chat_fn=chat_fn)
    candidate_markdown = append_evidence_appendix_if_missing(raw_response, snapshot)
    try:
        markdown = validate_report(candidate_markdown, snapshot, evidence_locked=evidence_locked)
        return {
            "status": "valid",
            "snapshot": snapshot,
            "knowledge_chunks": knowledge_chunks,
            "messages": messages,
            "markdown": markdown,
            "raw_response": raw_response,
            "reason": None,
            "section_mode": False,
        }
    except Exception as exc:
        reason = str(exc)
        return {
            "status": "invalid",
            "snapshot": snapshot,
            "knowledge_chunks": knowledge_chunks,
            "messages": messages,
            "markdown": diagnostics_markdown(
                reason=reason,
                raw_response=raw_response,
                snapshot=snapshot,
                include_raw_response=include_raw_response,
            ),
            "raw_response": raw_response,
            "reason": reason,
            "section_mode": False,
        }


def save_offline_report(markdown: str, *, reports_dir: Path | None = None, diagnostics: bool = False) -> Path:
    prefix = "deepseek_offline_report_diagnostics" if diagnostics else "deepseek_offline_report"
    return save_report(markdown, reports_dir=reports_dir, prefix=prefix)
