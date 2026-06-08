from __future__ import annotations

from db_builder.llm_analyst_overlay import (
    fallback_cio_commentary,
    generate_deepseek_cio_commentary,
    json_like_to_cio_markdown,
    render_qwen_overlay_markdown,
    validate_cio_commentary,
    validate_qwen_overlay,
)
from db_builder.news_classifier import extract_json_object


VALID_QWEN = {
    "market_view": "neutral",
    "positioning": "selective_risk",
    "risk_level": "moderate",
    "confidence": 0.6,
    "summary": "Market structure is mixed with selective opportunities.",
    "top_risk": "Volatility remains elevated.",
    "top_opportunity": "Cybersecurity leadership is improving.",
    "preferred_sectors": ["Cybersecurity", "Healthcare"],
    "avoid_sectors": ["Energy", "Utilities"],
}


def test_qwen_small_json_parses():
    overlay = validate_qwen_overlay(VALID_QWEN.copy())

    assert overlay["market_view"] == "neutral"
    assert "## LLM Analyst Overlay" in render_qwen_overlay_markdown(overlay)
    assert "Cybersecurity, Healthcare" in render_qwen_overlay_markdown(overlay)


def test_qwen_missing_preferred_sectors_defaults_safely():
    overlay = validate_qwen_overlay(
        {
            "market_view": "neutral",
            "positioning": "selective_risk",
            "risk_level": "moderate",
            "confidence": 0.4,
            "summary": "Mixed.",
            "top_risk": "Breadth is weak.",
            "top_opportunity": "Healthcare is stable.",
        }
    )

    assert overlay["preferred_sectors"] == []
    assert overlay["avoid_sectors"] == []


def test_qwen_prose_wrapped_json_is_extracted():
    payload = extract_json_object(f"Here is the object:\n{__import__('json').dumps(VALID_QWEN)}\nDone.")

    assert validate_qwen_overlay(payload)["confidence"] == 0.6


def test_malformed_qwen_json_fallback():
    from db_builder.llm_analyst_overlay import fallback_qwen_overlay

    overlay = fallback_qwen_overlay("bad json")

    assert overlay["confidence"] == 0.0
    assert "LLM overlay unavailable" in overlay["top_risk"]


def test_deepseek_timeout_fallback():
    markdown = fallback_cio_commentary("timed out")

    assert "## CIO Commentary" in markdown
    assert "timed out" in markdown


def test_deepseek_empty_response_fallback(monkeypatch):
    class Engine:
        pass

    monkeypatch.setattr("db_builder.llm_analyst_overlay.collect_overlay_snapshot", lambda engine, window_hours: {})

    def fake_chat(messages, *, url, model, timeout, options=None, response_format="json"):
        return {"message": {"content": ""}}

    markdown = generate_deepseek_cio_commentary(
        "base report",
        engine=Engine(),
        window_hours=24,
        chat_fn=fake_chat,
    )

    assert "empty model response" in markdown


def test_deepseek_json_response_is_wrapped(monkeypatch):
    monkeypatch.setattr("db_builder.llm_analyst_overlay.collect_overlay_snapshot", lambda engine, window_hours: {})

    markdown = generate_deepseek_cio_commentary(
        "base report",
        engine=object(),
        window_hours=24,
        chat_fn=lambda messages, *, url, model, timeout, options=None, response_format="json": {
            "message": {"content": '{"summary": [{"type": "Risk", "description": "Volatility elevated"}]}'}
        },
    )

    assert "## CIO Commentary" in markdown
    assert "Volatility elevated" in markdown


def test_json_like_response_with_useful_text_is_wrapped():
    markdown = json_like_to_cio_markdown('{"summary": [{"type": "Opportunity", "description": "Cybersecurity leadership"}]}')

    assert markdown is not None
    assert "Cybersecurity leadership" in markdown


def test_deepseek_markdown_with_heading_and_three_bullets_passes():
    markdown = validate_cio_commentary(
        """
        ## CIO Commentary

        - Market structure is mixed.
        - Sector rotation favors defensive leadership.
        - Positioning should remain selective.
        """
    )

    assert markdown.startswith("## CIO Commentary")


def test_deepseek_fallback_if_missing_heading(monkeypatch):
    class Engine:
        pass

    monkeypatch.setattr("db_builder.llm_analyst_overlay.collect_overlay_snapshot", lambda engine, window_hours: {})

    def fake_chat(messages, *, url, model, timeout, options=None, response_format="json"):
        return {"message": {"content": "- Market mixed.\n- Risk elevated.\n- Stay selective."}}

    markdown = generate_deepseek_cio_commentary(
        "base report",
        engine=Engine(),
        window_hours=24,
        chat_fn=fake_chat,
    )

    assert "missing ## CIO Commentary heading" in markdown
