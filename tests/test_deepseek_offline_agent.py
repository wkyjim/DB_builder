from __future__ import annotations

from db_builder import deepseek_offline_agent as agent
from tests.test_deepseek_report_validator import VALID_REPORT


class Engine:
    pass


def sample_snapshot() -> dict:
    return {
        "market_state": {"market_regime": "risk_on", "confidence": 0.3, "volatility_state": "normal", "evidence_id": "REGIME_001"},
        "approved_ticker_universe": ["SPY", "XLE", "^VIX"],
        "approved_etf_label_map": {"SPY": "S&P 500 ETF", "XLE": "Energy ETF"},
        "cross_asset": [{"symbol": "^VIX", "close": 18.92, "evidence_id": "MACRO_001"}],
        "evidence_appendix": [
            {"evidence_id": "REGIME_001", "section": "market_state", "summary": "risk_on"},
            {"evidence_id": "MACRO_001", "section": "cross_asset", "summary": "VIX close=18.92"},
        ],
    }


def test_prompt_includes_system_prompt_knowledge_snapshot_and_task(monkeypatch):
    monkeypatch.setattr(agent, "_read_knowledge_file", lambda file_name: f"{file_name} content")
    messages = agent.build_offline_prompt(sample_snapshot(), [{"file_name": "risk.md", "heading": "Risk", "content": "Risk framework"}])
    combined = "\n".join(message["content"] for message in messages)

    assert "deepseek_agent_system_prompt.md content" in combined
    assert "Risk framework" in combined
    assert "STRUCTURED SNAPSHOT" in combined
    assert "deepseek_report_generation_playbook.md content" in combined
    assert "## Tactical Sell / Reduce List" in combined
    assert "## Evidence Appendix" in combined


def test_append_evidence_appendix_if_missing():
    markdown = "# DeepSeek CIO House View\n"

    result = agent.append_evidence_appendix_if_missing(markdown, sample_snapshot())

    assert "## Evidence Appendix" in result
    assert "[REGIME_001]" in result


def test_append_evidence_appendix_keeps_existing_appendix():
    markdown = "# DeepSeek CIO House View\n\n## Evidence Appendix\n- [REGIME_001] existing"

    result = agent.append_evidence_appendix_if_missing(markdown, sample_snapshot())

    assert result == markdown


def test_dry_run_does_not_call_ollama(monkeypatch):
    called = {"value": False}

    def fake_chat(*args, **kwargs):
        called["value"] = True
        return {"message": {"content": VALID_REPORT}}

    monkeypatch.setattr(agent, "build_offline_snapshot", lambda engine, window_hours: sample_snapshot())
    monkeypatch.setattr(agent, "retrieve_knowledge", lambda report_task, snapshot: [])
    monkeypatch.setattr(agent, "_read_knowledge_file", lambda file_name: file_name)

    result = agent.generate_offline_report(Engine(), window_hours=24, call_model=False, chat_fn=fake_chat)

    assert result["status"] == "preview"
    assert called["value"] is False


def test_model_call_validates_report(monkeypatch):
    monkeypatch.setattr(agent, "build_offline_snapshot", lambda engine, window_hours: sample_snapshot())
    monkeypatch.setattr(agent, "retrieve_knowledge", lambda report_task, snapshot: [])
    monkeypatch.setattr(agent, "_read_knowledge_file", lambda file_name: file_name)

    result = agent.generate_offline_report(
        Engine(),
        window_hours=24,
        call_model=True,
        section_mode=False,
        chat_fn=lambda *args, **kwargs: {"message": {"content": VALID_REPORT}},
    )

    assert result["status"] == "valid"


def test_invalid_model_output_returns_diagnostics(monkeypatch):
    monkeypatch.setattr(agent, "build_offline_snapshot", lambda engine, window_hours: sample_snapshot())
    monkeypatch.setattr(agent, "retrieve_knowledge", lambda report_task, snapshot: [])
    monkeypatch.setattr(agent, "_read_knowledge_file", lambda file_name: file_name)

    result = agent.generate_offline_report(
        Engine(),
        window_hours=24,
        call_model=True,
        section_mode=False,
        chat_fn=lambda *args, **kwargs: {"message": {"content": "# Bad"}},
    )

    assert result["status"] == "invalid"
    assert "# DeepSeek Offline Report Diagnostics" in result["markdown"]


def test_section_mode_invalid_sections_return_fallback_report(monkeypatch):
    monkeypatch.setattr(agent, "build_offline_snapshot", lambda engine, window_hours: sample_snapshot())
    monkeypatch.setattr(agent, "_read_knowledge_file", lambda file_name: file_name)

    result = agent.generate_offline_report(
        Engine(),
        window_hours=24,
        call_model=True,
        chat_fn=lambda *args, **kwargs: {"message": {"content": "# Bad"}},
    )

    assert result["section_mode"] is True
    assert result["status"] == "invalid"
    assert result["fallback_sections"] == 12
    assert "## Validation Summary" in result["markdown"]


def test_save_offline_report_writes_valid_path(tmp_path):
    path = agent.save_offline_report(VALID_REPORT, reports_dir=tmp_path)

    assert path.name.startswith("deepseek_offline_report_")
    assert path.suffix == ".md"
    assert path.read_text(encoding="utf-8") == VALID_REPORT
