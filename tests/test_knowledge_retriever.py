from __future__ import annotations

from db_builder.knowledge_retriever import available_knowledge_files, retrieve_knowledge


def test_knowledge_retriever_returns_relevant_files(tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "cross_asset_relationships.md").write_text("# Cross Asset\n\nRisk and regime relationships.", encoding="utf-8")
    (knowledge / "risk_management_framework.md").write_text("# Risk\n\nPortfolio risk management.", encoding="utf-8")
    (knowledge / "equity_playbook.md").write_text("# Equity\n\nSector and equity leadership.", encoding="utf-8")

    chunks = retrieve_knowledge(
        "market regime risk report",
        {"market_state": {"market_regime": "risk_on"}, "sector_data": [{"sector": "Technology"}]},
        roots=[knowledge],
        max_chunks=3,
    )

    files = {chunk["file_name"] for chunk in chunks}
    assert "cross_asset_relationships.md" in files
    assert "risk_management_framework.md" in files


def test_available_knowledge_files_prefers_local_roots(tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "equity_playbook.md").write_text("# Equity", encoding="utf-8")

    found = available_knowledge_files(roots=[knowledge])

    assert found["equity_playbook.md"] == knowledge / "equity_playbook.md"
