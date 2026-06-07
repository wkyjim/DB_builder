from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from db_builder.news_classifier import (
    ClassificationError,
    classify_with_ollama,
    parse_model_response,
    persist_classification,
    strip_deepseek_thinking,
    validate_classification,
)


VALID_JSON = """
{
  "sentiment_score": 0.25,
  "impact_score": 55,
  "confidence_score": 0.8,
  "themes": ["AI", "earnings"],
  "event_type": "earnings",
  "time_horizon": "short",
  "affected_tickers": ["AAPL"],
  "reasoning": "Article discusses earnings expectations."
}
"""


def load_news_classify_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "news_classify.py"
    scripts_dir = str(path.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("news_classify_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_valid_json_parse():
    result = parse_model_response(VALID_JSON)

    assert result["sentiment_score"] == 0.25
    assert result["themes"] == ["AI", "earnings"]


def test_invalid_json_rejected():
    with pytest.raises(ClassificationError):
        parse_model_response("not json")


def test_json_surrounded_by_prose_still_parsed():
    result = parse_model_response(f"Here is JSON:\n{VALID_JSON}\nThanks")

    assert result["impact_score"] == 55.0


def test_deepseek_think_stripping():
    content = "<think>I am thinking</think>" + VALID_JSON

    assert strip_deepseek_thinking(content).startswith("{")
    assert parse_model_response(content)["event_type"] == "earnings"


def test_score_range_validation():
    payload = parse_model_response(VALID_JSON)
    payload["impact_score"] = 101

    with pytest.raises(ClassificationError):
        validate_classification(payload)


def test_missing_reasoning_defaults_to_empty_string():
    payload = parse_model_response("""
    {
      "sentiment_score": 0,
      "impact_score": 10,
      "confidence_score": 0.5,
      "themes": [],
      "event_type": "unknown",
      "time_horizon": "unknown",
      "affected_tickers": []
    }
    """)

    assert payload["reasoning"] == ""


def test_malformed_json_repaired(monkeypatch):
    responses = [
        {"message": {"content": "{bad json"}},
        {"message": {"content": VALID_JSON}},
    ]

    def fake_chat(messages, *, url, model, timeout):
        return responses.pop(0)

    monkeypatch.setattr("db_builder.news_classifier._ollama_chat", fake_chat)

    result = classify_with_ollama({"title": "x"}, url="u", model="m")

    assert result["event_type"] == "earnings"
    assert "repair_response" in result["raw_response"]


def test_repair_failure_reports_error(monkeypatch):
    def fake_chat(messages, *, url, model, timeout):
        return {"message": {"content": "still not json"}}

    monkeypatch.setattr("db_builder.news_classifier._ollama_chat", fake_chat)

    with pytest.raises(ClassificationError, match="Repair failed"):
        classify_with_ollama({"title": "x"}, url="u", model="m")


def test_dry_run_no_writes():
    class ExplodingEngine:
        def begin(self):  # pragma: no cover - should not be called
            raise AssertionError("dry-run attempted a database write")

    persist_classification(
        ExplodingEngine(),
        {"article_id": "00000000-0000-0000-0000-000000000000"},
        parse_model_response(VALID_JSON),
        dry_run=True,
    )


def test_time_budget_reached_stops_before_classification():
    script = load_news_classify_script()
    calls = []

    def fake_classify(*args, **kwargs):  # pragma: no cover - should not be called
        calls.append("classify")
        return parse_model_response(VALID_JSON)

    result = script.classify_articles(
        object(),
        [{"article_id": "00000000-0000-0000-0000-000000000001", "title": "A"}],
        dry_run=True,
        model="m",
        timeout=1,
        repair=True,
        max_seconds=0,
        start_time=10,
        now_fn=lambda: 10,
        classify_fn=fake_classify,
    )

    assert result == {"completed": 0, "failed": 0, "time_budget_reached": True}
    assert calls == []


def test_time_budget_commits_completed_then_stops_cleanly():
    script = load_news_classify_script()
    persisted = []
    now_values = iter([0, 10])

    def fake_classify(article, **kwargs):
        return parse_model_response(VALID_JSON)

    def fake_persist(engine, article, result, *, dry_run, model):
        persisted.append(str(article["article_id"]))

    result = script.classify_articles(
        object(),
        [
            {"article_id": "00000000-0000-0000-0000-000000000001", "title": "A"},
            {"article_id": "00000000-0000-0000-0000-000000000002", "title": "B"},
        ],
        dry_run=False,
        model="m",
        timeout=1,
        repair=True,
        max_seconds=5,
        start_time=0,
        now_fn=lambda: next(now_values),
        classify_fn=fake_classify,
        persist_fn=fake_persist,
    )

    assert result == {"completed": 1, "failed": 0, "time_budget_reached": True}
    assert persisted == ["00000000-0000-0000-0000-000000000001"]
