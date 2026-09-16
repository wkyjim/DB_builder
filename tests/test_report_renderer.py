from __future__ import annotations

import json
import math
import sys
import types
from datetime import datetime, timezone
from importlib import import_module

import pytest


@pytest.fixture(autouse=True, scope="function")
def _mock_and_import_renderer(monkeypatch):
    """Mock heavy dependencies and import report_renderer.

    Mocks pandas-dependent modules before importing report_renderer.
    Uses monkeypatch to ensure sys.modules is restored after each test,
    preventing mock leakage to other test modules in the same run.
    """
    mocked_modules = [
        "db_builder.contradiction_audit",
        "db_builder.etf_flow",
        "db_builder.etf_flow.report_adapter",
        "db_builder.market_dispersion",
        "db_builder.market_strength",
        "db_builder.news_scoring",
        "db_builder.rule_based_regime",
        "db_builder.sector_strength",
        "db_builder.sector_theme_alignment",
        "db_builder.theme_strength",
    ]

    original_modules = {name: sys.modules.get(name) for name in mocked_modules}

    def _mock(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    contradiction_audit = _mock("db_builder.contradiction_audit")
    contradiction_audit.audit_report_scores = lambda scores: []

    etf_flow_report_adapter = _mock("db_builder.etf_flow.report_adapter")
    etf_flow_report_adapter.etf_flow_report_lines = lambda data: []

    market_dispersion = _mock("db_builder.market_dispersion")
    market_dispersion.compute_broad_market_dispersion = lambda technicals: {}
    market_dispersion.compute_sector_constituent_dispersion = lambda rows: []

    market_strength = _mock("db_builder.market_strength")
    market_strength.flt = lambda value, default=0.0: default if value is None else float(value)

    news_scoring = _mock("db_builder.news_scoring")
    news_scoring.score_news = lambda rows: {}

    rule_based_regime = _mock("db_builder.rule_based_regime")
    rule_based_regime.compute_confidence = lambda *args, **kwargs: {}
    rule_based_regime.compute_regime = lambda *args, **kwargs: {}

    sector_strength = _mock("db_builder.sector_strength")
    sector_strength.rank_sectors = lambda *args, **kwargs: []

    sector_theme_alignment = _mock("db_builder.sector_theme_alignment")
    sector_theme_alignment.align_sector_themes = lambda *args, **kwargs: []

    theme_strength = _mock("db_builder.theme_strength")
    theme_strength.rank_themes = lambda *args, **kwargs: []

    # Import report_renderer with mocks in place
    if "db_builder.report_renderer" in sys.modules:
        monkeypatch.delitem(sys.modules, "db_builder.report_renderer")
    renderer = import_module("db_builder.report_renderer")

    yield renderer

    # Cleanup: remove report_renderer from sys.modules for fresh import next test
    monkeypatch.delitem(sys.modules, "db_builder.report_renderer", raising=False)


# ---------------------------------------------------------------------------
# fmt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "digits", "expected"),
    [
        (3.14159, 2, "3.14"),
        (3.14159, 4, "3.1416"),
        (0, 2, "0.0"),
        (-12.5, 2, "-12.5"),
        (None, 2, "n/a"),
        (float("nan"), 2, "n/a"),
        (float("inf"), 2, "n/a"),
        (-float("inf"), 2, "n/a"),
        ("not-a-number", 2, "not-a-number"),
        ("text", 2, "text"),
    ],
)
def test_fmt_formats_numeric_values_and_handles_missing(_mock_and_import_renderer, value, digits, expected):
    assert _mock_and_import_renderer.fmt(value, digits) == expected


def test_fmt_uses_default_two_digits(_mock_and_import_renderer):
    assert _mock_and_import_renderer.fmt(3.14159) == "3.14"


def test_fmt_returns_string_representation_for_non_numeric_strings(_mock_and_import_renderer):
    assert _mock_and_import_renderer.fmt("hello") == "hello"


# ---------------------------------------------------------------------------
# fmt_money
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1234567, "$1,234,567"),
        (-1500, "-$1,500"),
        (0, "$0"),
        (99.9, "$100"),
        (-0.5, "-$0"),
        (None, "n/a"),
        (float("nan"), "n/a"),
        (float("inf"), "n/a"),
        ("abc", "n/a"),
    ],
)
def test_fmt_money_formats_currency_values(_mock_and_import_renderer, value, expected):
    assert _mock_and_import_renderer.fmt_money(value) == expected


# ---------------------------------------------------------------------------
# fmt_hkt_timestamp
# ---------------------------------------------------------------------------


def test_fmt_hkt_timestamp_converts_utc_to_hong_kong_time(_mock_and_import_renderer):
    value = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    result = _mock_and_import_renderer.fmt_hkt_timestamp(value)

    assert "30 June 2026" in result
    assert "HKT" in result
    assert "20:00:00" in result  # HKT is UTC+8


def test_fmt_hkt_timestamp_handles_iso_string_with_z(_mock_and_import_renderer):
    result = _mock_and_import_renderer.fmt_hkt_timestamp("2026-06-30T12:00:00Z")

    assert "30 June 2026" in result
    assert "HKT" in result


def test_fmt_hkt_timestamp_handles_iso_string_with_offset(_mock_and_import_renderer):
    result = _mock_and_import_renderer.fmt_hkt_timestamp("2026-06-30T12:00:00+00:00")

    assert "30 June 2026" in result
    assert "HKT" in result


def test_fmt_hkt_timestamp_handles_none(_mock_and_import_renderer):
    assert _mock_and_import_renderer.fmt_hkt_timestamp(None) == "n/a"


def test_fmt_hkt_timestamp_handles_invalid_string(_mock_and_import_renderer):
    result = _mock_and_import_renderer.fmt_hkt_timestamp("not-a-date")
    assert result == "not-a-date"


def test_fmt_hkt_timestamp_handles_non_datetime_types(_mock_and_import_renderer):
    result = _mock_and_import_renderer.fmt_hkt_timestamp(12345)
    assert result == "12345"


def test_fmt_hkt_timestamp_assumes_utc_for_naive_datetimes(_mock_and_import_renderer):
    value = datetime(2026, 6, 30, 12, 0, 0)
    result = _mock_and_import_renderer.fmt_hkt_timestamp(value)

    assert "30 June 2026" in result
    assert "20:00:00" in result
    assert "HKT" in result


# ---------------------------------------------------------------------------
# table
# ---------------------------------------------------------------------------


def test_table_produces_well_formed_markdown_table(_mock_and_import_renderer):
    headers = ["A", "B", "C"]
    rows = [["1", "2", "3"], ["4", "5", "6"]]
    lines = _mock_and_import_renderer.table(headers, rows)

    assert lines[0] == "| A | B | C |"
    assert lines[1] == "| --- | --- | --- |"
    assert lines[2] == "| 1 | 2 | 3 |"
    assert lines[3] == "| 4 | 5 | 6 |"
    assert len(lines) == 4


def test_table_handles_single_row(_mock_and_import_renderer):
    lines = _mock_and_import_renderer.table(["X"], [["value"]])

    assert lines[0] == "| X |"
    assert lines[1] == "| --- |"
    assert lines[2] == "| value |"


def test_table_handles_empty_rows(_mock_and_import_renderer):
    lines = _mock_and_import_renderer.table(["A", "B"], [])

    assert len(lines) == 2
    assert lines[0] == "| A | B |"
    assert lines[1] == "| --- | --- |"


def test_table_handles_numeric_values(_mock_and_import_renderer):
    lines = _mock_and_import_renderer.table(["Price"], [[99.5], [100.25]])

    assert lines[2] == "| 99.5 |"
    assert lines[3] == "| 100.25 |"


# ---------------------------------------------------------------------------
# scores_to_json
# ---------------------------------------------------------------------------


def test_scores_to_json_serializes_dict_to_json_string(_mock_and_import_renderer):
    scores = {"score": 75.5, "label": "strong"}
    result = json.loads(_mock_and_import_renderer.scores_to_json(scores))

    assert result == scores


def test_scores_to_json_handles_nested_structures(_mock_and_import_renderer):
    scores = {
        "market_strength": {"score": 80, "label": "strong"},
        "breadth": {"above_50d_pct": 75.0},
    }
    result = json.loads(_mock_and_import_renderer.scores_to_json(scores))

    assert result["market_strength"]["score"] == 80
    assert result["breadth"]["above_50d_pct"] == 75.0


def test_scores_to_json_handles_default_fallback(_mock_and_import_renderer):
    data = {"value": float("nan")}
    result = json.loads(_mock_and_import_renderer.scores_to_json(data))

    # NaN may be serialized as JSON literal NaN and parsed back as float NaN,
    # or serialized as string "NaN" via default=str — either is acceptable
    assert isinstance(result["value"], (str, float))


def test_scores_to_json_handles_empty_dict(_mock_and_import_renderer):
    result = json.loads(_mock_and_import_renderer.scores_to_json({}))

    assert result == {}
