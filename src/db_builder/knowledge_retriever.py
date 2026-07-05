"""Local markdown knowledge retrieval for the offline DeepSeek CIO agent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


KNOWLEDGE_FILENAMES = [
    "cross_asset_relationships.md",
    "institutional_output_templates.md",
    "confidence_scoring_framework.md",
    "commodity_framework.md",
    "market_state_schema.md",
    "equity_playbook.md",
    "fixed_income_playbook.md",
    "historical_regimes.md",
    "quant_factor_models.md",
    "risk_management_framework.md",
    "deepseek_agent_system_prompt.md",
    "deepseek_report_generation_playbook.md",
]

TASK_KEYWORDS = {
    "market_regime": [
        "cross_asset_relationships.md",
        "confidence_scoring_framework.md",
        "historical_regimes.md",
        "market_state_schema.md",
    ],
    "equities": [
        "equity_playbook.md",
        "quant_factor_models.md",
        "institutional_output_templates.md",
    ],
    "rates": [
        "fixed_income_playbook.md",
        "cross_asset_relationships.md",
    ],
    "commodities": [
        "commodity_framework.md",
        "cross_asset_relationships.md",
    ],
    "risk": [
        "risk_management_framework.md",
        "historical_regimes.md",
    ],
}


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    file_name: str
    heading: str
    content: str


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def candidate_roots(extra_roots: list[Path] | None = None) -> list[Path]:
    roots = [project_root() / "knowledge", Path.home() / "Downloads"]
    if extra_roots:
        roots = extra_roots + roots
    return roots


def find_knowledge_file(file_name: str, *, roots: list[Path] | None = None) -> Path | None:
    for root in roots or candidate_roots():
        path = root / file_name
        if path.exists():
            return path
    return None


def available_knowledge_files(*, roots: list[Path] | None = None) -> dict[str, Path]:
    found = {}
    for file_name in KNOWLEDGE_FILENAMES:
        path = find_knowledge_file(file_name, roots=roots)
        if path:
            found[file_name] = path
    return found


def split_markdown_chunks(file_name: str, text: str, *, max_chars: int = 1800) -> list[KnowledgeChunk]:
    parts = re.split(r"(?m)^(#{1,3}\s+.+)$", text)
    chunks: list[KnowledgeChunk] = []
    if parts and parts[0].strip():
        chunks.append(KnowledgeChunk(f"{file_name}:intro", file_name, "intro", parts[0].strip()[:max_chars]))
    for idx in range(1, len(parts), 2):
        heading = parts[idx].strip()
        content = parts[idx + 1].strip() if idx + 1 < len(parts) else ""
        if not content:
            continue
        chunks.append(
            KnowledgeChunk(
                f"{file_name}:{len(chunks) + 1}",
                file_name,
                heading.lstrip("#").strip(),
                f"{heading}\n\n{content}"[:max_chars],
            )
        )
    return chunks or [KnowledgeChunk(f"{file_name}:all", file_name, "document", text.strip()[:max_chars])]


def load_knowledge_chunks(*, roots: list[Path] | None = None) -> list[KnowledgeChunk]:
    chunks: list[KnowledgeChunk] = []
    for file_name, path in available_knowledge_files(roots=roots).items():
        chunks.extend(split_markdown_chunks(file_name, path.read_text(encoding="utf-8", errors="ignore")))
    return chunks


def _snapshot_terms(snapshot: dict) -> set[str]:
    terms: set[str] = set()
    payload = str(snapshot).lower()
    for word in [
        "market",
        "regime",
        "risk",
        "equity",
        "rates",
        "commodities",
        "oil",
        "sector",
        "technology",
        "semiconductor",
        "volatility",
        "confidence",
        "portfolio",
        "cross-asset",
        "crypto",
    ]:
        if word in payload:
            terms.add(word)
    return terms


def relevant_file_names(report_task: str, snapshot: dict) -> set[str]:
    text = f"{report_task} {' '.join(_snapshot_terms(snapshot))}".lower()
    selected = {"deepseek_agent_system_prompt.md", "deepseek_report_generation_playbook.md"}
    for key, file_names in TASK_KEYWORDS.items():
        if key in text or any(token in text for token in key.split("_")):
            selected.update(file_names)
    if "commodity" in text or "oil" in text or "gold" in text:
        selected.update(TASK_KEYWORDS["commodities"])
    if "rate" in text or "yield" in text or "bond" in text:
        selected.update(TASK_KEYWORDS["rates"])
    if "sector" in text or "equity" in text or "semiconductor" in text:
        selected.update(TASK_KEYWORDS["equities"])
    if "risk" in text or "volatility" in text:
        selected.update(TASK_KEYWORDS["risk"])
    if "regime" in text or "confidence" in text:
        selected.update(TASK_KEYWORDS["market_regime"])
    return selected


def score_chunk(chunk: KnowledgeChunk, terms: set[str], selected_files: set[str]) -> int:
    score = 5 if chunk.file_name in selected_files else 0
    haystack = f"{chunk.file_name} {chunk.heading} {chunk.content}".lower()
    score += sum(1 for term in terms if term in haystack)
    return score


def retrieve_knowledge(
    report_task: str,
    snapshot: dict,
    *,
    roots: list[Path] | None = None,
    max_chunks: int = 8,
) -> list[dict]:
    chunks = load_knowledge_chunks(roots=roots)
    terms = _snapshot_terms(snapshot) | set(report_task.lower().split())
    selected_files = relevant_file_names(report_task, snapshot)
    ranked = sorted(
        chunks,
        key=lambda chunk: (score_chunk(chunk, terms, selected_files), chunk.file_name, chunk.heading),
        reverse=True,
    )
    result = [
        {
            "chunk_id": chunk.chunk_id,
            "file_name": chunk.file_name,
            "heading": chunk.heading,
            "content": chunk.content,
        }
        for chunk in ranked
        if score_chunk(chunk, terms, selected_files) > 0
    ][:max_chunks]
    return result
