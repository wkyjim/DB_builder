"""Multistage DeepSeek CIO report pipeline."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import time
from typing import Callable

from db_builder.deepseek_snapshot_builder import build_offline_snapshot
from db_builder.deepseek_stage_outputs import (
    fallback_cross_asset_validation,
    fallback_evidence_extraction,
    fallback_final_report,
    fallback_news_weighting,
    fallback_portfolio_construction,
    fallback_regime_scoring,
)
from db_builder.deepseek_stage_prompts import FINAL_REPORT_HEADINGS, STAGE_ORDER, build_stage_prompt
from db_builder.deepseek_stage_validator import (
    consistency_audit,
    extract_json_object,
    validate_final_report,
    validate_stage_output,
)
from db_builder.investment_report import save_report
from db_builder.news_classifier import _ollama_chat, ollama_deep_model, ollama_url


DEFAULT_MULTISTAGE_MODEL = "qwen2.5:14b"
PLACEHOLDER_HEADLINE_VALUES = {
    "",
    "headline title from snapshot",
    "source name from snapshot",
    "source unavailable",
    "headline title unavailable",
}


@dataclass(frozen=True)
class OllamaSettings:
    model: str | None = DEFAULT_MULTISTAGE_MODEL
    timeout: int = 1200
    stage_timeout: int = 240
    temperature: float = 0.2
    top_p: float = 0.85
    repeat_penalty: float = 1.1
    num_ctx: int | None = None


def fallback_for_stage(stage_name: str, snapshot: dict, stage_outputs: dict) -> dict | str:
    if stage_name == "evidence_extraction":
        return fallback_evidence_extraction(snapshot)
    if stage_name == "regime_scoring":
        return fallback_regime_scoring(snapshot, stage_outputs.get("evidence_extraction"))
    if stage_name == "cross_asset_validation":
        return fallback_cross_asset_validation(snapshot, stage_outputs.get("regime_scoring"))
    if stage_name == "news_weighting":
        return fallback_news_weighting(snapshot, stage_outputs.get("cross_asset_validation"))
    if stage_name == "portfolio_construction":
        return fallback_portfolio_construction(
            snapshot,
            stage_outputs.get("regime_scoring"),
            stage_outputs.get("cross_asset_validation"),
            stage_outputs.get("news_weighting"),
        )
    if stage_name == "final_cio_report":
        return fallback_final_report(stage_outputs, snapshot)
    raise ValueError(f"Unknown stage: {stage_name}")


def enrich_news_weighting(payload: dict, snapshot: dict) -> dict:
    by_id = {row.get("evidence_id"): row for row in snapshot.get("news", []) if row.get("evidence_id")}
    for row in payload.get("headline_classifications") or []:
        evidence = by_id.get(row.get("evidence_id"))
        if not evidence:
            continue
        title = str(row.get("title") or "").strip()
        source = str(row.get("source") or "").strip()
        if title.lower() in PLACEHOLDER_HEADLINE_VALUES:
            row["title"] = evidence.get("title")
        if source.lower() in PLACEHOLDER_HEADLINE_VALUES:
            row["source"] = evidence.get("source")
        row.setdefault("source_priority", evidence.get("source_priority"))
        row.setdefault("published_at", evidence.get("published_at"))
    return payload


def _call_model(
    messages: list[dict],
    *,
    settings: OllamaSettings,
    chat_fn: Callable[..., dict] | None = None,
) -> str:
    if chat_fn is not None:
        body = chat_fn(messages=messages, settings=settings)
        if isinstance(body, str):
            return body
        return str(body.get("message", {}).get("content", ""))
    options = {
        "temperature": settings.temperature,
        "top_p": settings.top_p,
        "repeat_penalty": settings.repeat_penalty,
        "num_predict": 6000,
    }
    if settings.num_ctx:
        options["num_ctx"] = settings.num_ctx
    body = _ollama_chat(
        messages,
        url=ollama_url(),
        model=settings.model or DEFAULT_MULTISTAGE_MODEL or ollama_deep_model(),
        timeout=None if settings.stage_timeout <= 0 else settings.stage_timeout,
        options=options,
        response_format=None,
    )
    return str(body.get("message", {}).get("content", ""))


def _final_report_repair_messages(messages: list[dict], raw: str, reason: str) -> list[dict]:
    headings = "\n".join(FINAL_REPORT_HEADINGS)
    repair_instruction = (
        "Your previous final CIO report failed validation.\n\n"
        f"VALIDATION ERROR:\n{reason}\n\n"
        "Rewrite the complete final report from scratch.\n"
        "Use every required heading below exactly once, as its own line, in this exact order.\n"
        "If any heading is missing, renamed, combined, or skipped, validation fails.\n\n"
        "REQUIRED HEADINGS:\n"
        f"{headings}\n\n"
        "In ## News Analysis, include actual headline titles and sources from the supplied TOP NEWS HEADLINES block.\n"
        "Do not introduce any new facts, tickers, ETFs, evidence IDs, dates, prices, yields, scores, or numeric values.\n"
        "Use qualitative wording instead of exposure ranges or invented thresholds.\n"
        "The only numbers allowed are the locked Stage 2 scenario probabilities already provided in the prompt.\n"
        "Copy the locked Base/Bull/Bear scenario table exactly in ## Scenario Analysis."
    )
    return messages + [
        {"role": "assistant", "content": str(raw or "")[:6000]},
        {"role": "user", "content": repair_instruction},
    ]


def run_stage(
    stage_name: str,
    snapshot: dict,
    stage_outputs: dict,
    *,
    call_model: bool,
    settings: OllamaSettings,
    retry: bool = True,
    chat_fn: Callable[..., dict] | None = None,
) -> dict:
    messages = build_stage_prompt(stage_name, snapshot, stage_outputs)
    if not call_model:
        output = fallback_for_stage(stage_name, snapshot, stage_outputs)
        return {"stage": stage_name, "status": "preview", "output": output, "messages": messages, "reason": "dry-run preview"}

    attempts = 2 if retry else 1
    last_reason = ""
    for attempt in range(attempts):
        raw = ""
        try:
            raw = _call_model(messages, settings=settings, chat_fn=chat_fn)
            if stage_name == "final_cio_report":
                markdown = validate_final_report(raw, stage_outputs, snapshot)
                return {"stage": stage_name, "status": "valid_qwen", "output": markdown, "messages": messages, "raw": raw, "reason": ""}
            payload = extract_json_object(raw)
            payload = validate_stage_output(stage_name, payload, snapshot)
            if stage_name == "news_weighting":
                payload = enrich_news_weighting(payload, snapshot)
            return {"stage": stage_name, "status": "valid_qwen", "output": payload, "messages": messages, "raw": raw, "reason": ""}
        except Exception as exc:
            last_reason = str(exc)
            if stage_name == "final_cio_report" and attempt + 1 < attempts:
                messages = _final_report_repair_messages(messages, raw, last_reason)

    fallback = fallback_for_stage(stage_name, snapshot, stage_outputs)
    if stage_name == "final_cio_report":
        try:
            fallback = validate_final_report(fallback, stage_outputs, snapshot)
        except Exception:
            pass
    else:
        fallback = validate_stage_output(stage_name, fallback, snapshot)
        if stage_name == "news_weighting":
            fallback = enrich_news_weighting(fallback, snapshot)
    return {"stage": stage_name, "status": "fallback_used", "output": fallback, "messages": messages, "reason": last_reason}


def validation_summary(stage_results: list[dict], warnings: list[dict]) -> str:
    lines = ["## Validation Summary", "", "| Stage | Status | Reason |", "|---|---|---|"]
    for result in stage_results:
        reason = str(result.get("reason") or "").replace("|", "/")
        lines.append(f"| {result['stage']} | {result['status']} | {reason} |")
    if warnings:
        lines.extend(["", "## Consistency Warnings", ""])
        for warning in warnings:
            lines.append(f"- {warning['severity']}: {warning['issue']}")
    return "\n".join(lines).rstrip() + "\n"


def append_summary(markdown: str, stage_results: list[dict], warnings: list[dict]) -> str:
    return markdown.rstrip() + "\n\n" + validation_summary(stage_results, warnings)


def run_multistage_report(
    engine,
    *,
    window_hours: int = 24,
    call_model: bool = False,
    settings: OllamaSettings | None = None,
    include_stage_json: bool = False,
    allow_warnings: bool = False,
    chat_fn: Callable[..., dict] | None = None,
) -> dict:
    settings = settings or OllamaSettings()
    snapshot = build_offline_snapshot(engine, window_hours=window_hours)
    stage_outputs: dict = {}
    stage_results = []
    prompts = {}
    deadline = None if settings.timeout <= 0 else time.monotonic() + max(1, settings.timeout)

    for stage_name in STAGE_ORDER:
        stage_call_model = call_model
        stage_settings = settings
        if call_model and deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stage_call_model = False
            else:
                stage_settings = replace(settings, stage_timeout=max(1, min(settings.stage_timeout, int(remaining))))
        result = run_stage(
            stage_name,
            snapshot,
            stage_outputs,
            call_model=stage_call_model,
            settings=stage_settings,
            retry=True,
            chat_fn=chat_fn,
        )
        if call_model and not stage_call_model:
            result["status"] = "fallback_used"
            result["reason"] = "Global time budget reached before stage."
        stage_results.append(result)
        prompts[stage_name] = result["messages"]
        stage_outputs[stage_name] = result["output"]

    final_markdown = str(stage_outputs["final_cio_report"])
    warnings = consistency_audit(stage_outputs, final_markdown)
    serious = [warning for warning in warnings if warning["severity"] == "serious"]
    status = "preview"
    if call_model:
        status = "valid" if not serious and (allow_warnings or not warnings) else "invalid"
    markdown = append_summary(final_markdown, stage_results, warnings)
    if include_stage_json:
        markdown += "\n\n## Stage JSON\n\n```json\n" + json.dumps(stage_outputs, indent=2, ensure_ascii=True, default=str) + "\n```\n"

    return {
        "status": status,
        "snapshot": snapshot,
        "stage_outputs": stage_outputs,
        "stage_results": stage_results,
        "prompts": prompts,
        "warnings": warnings,
        "markdown": markdown if call_model else "",
        "reason": "; ".join(warning["issue"] for warning in serious) if serious else None,
    }


def save_multistage_report(markdown: str, *, reports_dir: Path | None = None, diagnostics: bool = False) -> Path:
    prefix = "qwen_multistage_report_diagnostics" if diagnostics else "qwen_multistage_report"
    return save_report(markdown, reports_dir=reports_dir, prefix=prefix)
