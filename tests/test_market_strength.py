from __future__ import annotations

import math

import pytest

from db_builder.market_strength import (
    avg,
    clamp,
    compute_market_strength,
    flt,
    market_breadth,
    missing_indicator_warnings,
    row_by_ticker,
    score_above_ma,
    score_macd,
    score_return_momentum,
    score_rsi,
    score_volume,
    strength_label,
    trend_label,
)


CORE_TICKERS = ("SPY", "QQQ", "IWM", "SMH")


def technical_row(ticker: str, *, stance: str = "bullish") -> dict[str, object]:
    if stance == "bullish":
        return {
            "ticker": ticker,
            "close": 110.0,
            "ma_20": 100.0,
            "ma_50": 100.0,
            "ma_100": 100.0,
            "ma_200": 100.0,
            "return_5d": 10.0,
            "return_20d": 10.0,
            "return_60d": 10.0,
            "rsi_14": 60.0,
            "macd_hist": 1.0,
            "volume_ratio_20": 1.2,
        }
    if stance == "bearish":
        return {
            "ticker": ticker,
            "close": 90.0,
            "ma_20": 100.0,
            "ma_50": 100.0,
            "ma_100": 100.0,
            "ma_200": 100.0,
            "return_5d": -100.0,
            "return_20d": -100.0,
            "return_60d": -100.0,
            "rsi_14": 30.0,
            "macd_hist": -1.0,
            "volume_ratio_20": 1.2,
        }
    raise ValueError(f"unsupported stance: {stance}")


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        ("12.5", 0.0, 12.5),
        (0, 99.0, 0.0),
        (None, 7.0, 7.0),
        (math.nan, 7.0, 7.0),
        ("not-a-number", 7.0, 7.0),
    ],
)
def test_flt_normalizes_numeric_and_missing_values(value, default, expected):
    assert flt(value, default) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(-math.inf, 0), (-1, 0), (0, 0), (50, 50), (100, 100), (101, 100), (math.inf, 100)],
)
def test_clamp_enforces_inclusive_score_range(value, expected):
    assert clamp(value) == expected


def test_avg_ignores_none_but_preserves_zero_and_has_neutral_default():
    assert avg([None, 0, 100]) == 50
    assert avg([]) == 50
    assert avg([None], default=12) == 12


def test_row_by_ticker_is_exact_first_match_and_missing_returns_empty_mapping():
    first = {"ticker": "SPY", "close": 100}
    second = {"ticker": "SPY", "close": 200}
    rows = [first, second, {"ticker": "spy", "close": 300}]

    assert row_by_ticker(rows, "SPY") is first
    assert row_by_ticker(rows, "spy") is rows[2]
    assert row_by_ticker(rows, "QQQ") == {}


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"close": 100, "ma_20": 100, "ma_50": 99, "ma_100": 98, "ma_200": 97}, 100),
        ({"close": 90, "ma_20": 100, "ma_50": 100, "ma_100": 100, "ma_200": 100}, 0),
        ({"close": None, "ma_20": 100}, 50),
        ({"close": math.nan, "ma_20": 100}, 50),
        ({"close": 100, "ma_20": None, "ma_50": 0, "ma_100": math.nan}, 50),
    ],
)
def test_score_above_ma_boundaries_and_missing_data(row, expected):
    assert score_above_ma(row) == expected


def test_score_above_ma_renormalizes_to_available_windows():
    row = {"close": 100, "ma_20": 90, "ma_50": 110, "ma_100": None, "ma_200": None}

    assert score_above_ma(row) == pytest.approx(44.4444)


def test_return_momentum_applies_horizon_weights():
    row = {"return_5d": 4, "return_20d": 10, "return_60d": -2}

    assert score_return_momentum(row) == 54.3


def test_return_momentum_missing_values_are_neutral_contributions():
    assert score_return_momentum({}) == 50
    assert score_return_momentum({"return_5d": None, "return_20d": math.nan}) == 50


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1000, 100), (-1000, 0), (math.inf, 100), (-math.inf, 0)],
)
def test_return_momentum_is_clamped_for_extreme_values(value, expected):
    row = {"return_5d": value, "return_20d": value, "return_60d": value}
    assert score_return_momentum(row) == expected


@pytest.mark.parametrize(
    ("rsi", "expected"),
    [
        (34.999, 35),
        (35, 40),
        (49.999, 40),
        (50, 75),
        (65, 75),
        (65.001, 65),
        (75, 65),
        (75.001, 45),
    ],
)
def test_rsi_scoring_exact_zone_boundaries(rsi, expected):
    assert score_rsi({"rsi_14": rsi}) == expected


@pytest.mark.parametrize("missing_rsi", [None, math.nan, math.inf, -math.inf, "not-a-number"])
def test_missing_rsi_is_neutral_instead_of_receiving_a_bullish_zone_score(missing_rsi):
    assert score_rsi({"rsi_14": missing_rsi}) == 50


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"macd_hist": 0.01}, 70),
        ({"macd_hist": 0}, 50),
        ({"macd_hist": -0.01}, 35),
        ({"macd_hist": None, "macd": 2, "macd_signal": 1}, 70),
        ({"macd_hist": math.nan, "macd": 1, "macd_signal": 2}, 35),
        ({"macd_hist": None, "macd": None, "macd_signal": 2}, 50),
        ({}, 50),
    ],
)
def test_macd_scoring_and_histogram_fallback(row, expected):
    assert score_macd(row) == expected


def test_macd_histogram_takes_precedence_over_line_fallback():
    assert score_macd({"macd_hist": -1, "macd": 2, "macd_signal": 1}) == 35


@pytest.mark.parametrize(
    ("ratio", "return_5d", "expected"),
    [
        (1.1999, 10, 50),
        (1.2, 0.001, 70),
        (1.2, 0, 50),
        (1.2, -0.001, 35),
        (None, 10, 50),
        (math.nan, -10, 50),
    ],
)
def test_volume_scoring_threshold_and_direction(ratio, return_5d, expected):
    assert score_volume({"volume_ratio_20": ratio, "return_5d": return_5d}) == expected


@pytest.mark.parametrize(
    ("score", "expected"),
    [(44.999, "weak"), (45, "neutral"), (59.999, "neutral"), (60, "constructive"), (74.999, "constructive"), (75, "strong")],
)
def test_strength_label_boundaries(score, expected):
    assert strength_label(score) == expected


@pytest.mark.parametrize(
    ("score", "expected"),
    [(29.999, "strong downtrend"), (30, "downtrend"), (45, "neutral"), (60, "uptrend"), (75, "strong uptrend")],
)
def test_trend_label_boundaries(score, expected):
    assert trend_label(score) == expected


def test_market_breadth_reports_bullish_bearish_and_mixed_universes():
    bullish = market_breadth([technical_row("AAPL"), technical_row("MSFT")])
    bearish = market_breadth([technical_row("AAPL", stance="bearish")])
    mixed = market_breadth([technical_row("AAPL"), technical_row("MSFT", stance="bearish")])

    assert bullish == {
        "score": 100.0,
        "label": "broad",
        "above_50d_pct": 100.0,
        "above_200d_pct": 100.0,
        "positive_20d_pct": 100.0,
    }
    assert bearish == {
        "score": 0.0,
        "label": "weak",
        "above_50d_pct": 0.0,
        "above_200d_pct": 0.0,
        "positive_20d_pct": 0.0,
    }
    assert mixed["score"] == 50.0
    assert mixed["label"] == "narrow"


def test_market_breadth_uses_each_available_sub_universe_independently():
    rows = [
        {"ticker": "AAPL", "close": 110, "ma_50": 100},
        {"ticker": "MSFT", "close": 90, "ma_200": 100, "return_20d": 1},
    ]

    result = market_breadth(rows)

    assert result["above_50d_pct"] == 100.0
    assert result["above_200d_pct"] == 0.0
    assert result["positive_20d_pct"] == 100.0
    assert result["score"] == pytest.approx(66.6667)
    assert result["label"] == "healthy"


def test_market_breadth_empty_input_has_neutral_score_and_unavailable_percentages():
    assert market_breadth([]) == {
        "score": 50,
        "label": "narrow",
        "above_50d_pct": None,
        "above_200d_pct": None,
        "positive_20d_pct": None,
    }


def test_market_breadth_excludes_rows_with_missing_or_nan_price_inputs():
    rows = [
        {"ticker": "AAPL", "close": None, "ma_50": 100, "ma_200": 100, "return_20d": None},
        {"ticker": "MSFT", "close": math.nan, "ma_50": 100, "ma_200": 100},
    ]

    result = market_breadth(rows)

    assert result["above_50d_pct"] is None
    assert result["above_200d_pct"] is None
    assert result["positive_20d_pct"] is None
    assert result["score"] == 50


def test_market_breadth_excludes_nan_return_from_available_universe():
    rows = [
        {"ticker": "AAPL", "return_20d": math.nan},
        {"ticker": "MSFT", "return_20d": 1.0},
    ]

    result = market_breadth(rows)

    assert result["positive_20d_pct"] == 100.0
    assert result["score"] == 100.0


def test_market_breadth_keeps_zero_return_in_available_universe():
    rows = [
        {"ticker": "AAPL", "return_20d": 0.0},
        {"ticker": "MSFT", "return_20d": 1.0},
    ]

    result = market_breadth(rows)

    assert result["positive_20d_pct"] == 50.0
    assert result["score"] == 50.0


def test_missing_indicator_warnings_cover_absent_symbols_and_fields():
    rows = [{"ticker": "SPY", "ma_20": 100, "ma_50": 100, "ma_100": 100, "ma_200": 100}]

    warnings = missing_indicator_warnings(rows)

    assert "SPY rsi_14 missing" in warnings
    assert "SPY macd_hist missing" in warnings
    assert "SPY return_20d missing" in warnings
    assert "QQQ market row missing" in warnings
    assert "IWM market row missing" in warnings
    assert "SMH market row missing" in warnings
    assert len(warnings) == 6


def test_empty_input_returns_neutral_complete_schema_and_missing_core_warnings():
    result = compute_market_strength([])

    assert result == {
        "score": 50.0,
        "label": "neutral",
        "decomposition": {
            "indices_above_moving_averages": 50,
            "sp500_trend": 50,
            "nasdaq_trend": 50,
            "russell_participation": 50,
            "returns_momentum": 50,
            "rsi_zone": 50,
            "macd_confirmation": 50,
            "volume_confirmation": 50,
            "breadth": 50,
        },
        "breadth": {
            "score": 50,
            "label": "narrow",
            "above_50d_pct": None,
            "above_200d_pct": None,
            "positive_20d_pct": None,
        },
        "missing_indicators": [
            "SPY market row missing",
            "QQQ market row missing",
            "IWM market row missing",
            "SMH market row missing",
        ],
    }


def test_representative_bullish_and_bearish_inputs_have_expected_regimes_and_bounds():
    bullish = compute_market_strength([technical_row(ticker) for ticker in CORE_TICKERS])
    bearish = compute_market_strength([technical_row(ticker, stance="bearish") for ticker in CORE_TICKERS])

    assert bullish["score"] == 87.1
    assert bullish["label"] == "strong"
    assert bullish["missing_indicators"] == []
    assert bearish["score"] == 8.05
    assert bearish["label"] == "weak"
    assert 0 <= bearish["score"] < bullish["score"] <= 100


def test_partial_core_universe_uses_fallbacks_and_reports_missing_symbols():
    result = compute_market_strength([technical_row("SPY")])

    assert result["decomposition"]["sp500_trend"] == 100
    assert result["decomposition"]["nasdaq_trend"] == 50
    assert result["decomposition"]["russell_participation"] == 50
    assert result["missing_indicators"] == [
        "QQQ market row missing",
        "IWM market row missing",
        "SMH market row missing",
    ]


def test_non_core_rows_affect_breadth_but_not_core_index_components():
    core_rows = [technical_row(ticker) for ticker in CORE_TICKERS]
    baseline = compute_market_strength(core_rows)
    expanded = compute_market_strength(core_rows + [technical_row("AAPL", stance="bearish")])

    core_component_names = set(baseline["decomposition"]) - {"breadth"}
    for component in core_component_names:
        assert expanded["decomposition"][component] == baseline["decomposition"][component]
    assert expanded["breadth"]["score"] < baseline["breadth"]["score"]
    assert expanded["score"] < baseline["score"]


def test_overall_score_reconciles_to_documented_component_weights():
    result = compute_market_strength([technical_row(ticker) for ticker in CORE_TICKERS])
    weights = {
        "indices_above_moving_averages": 0.20,
        "sp500_trend": 0.12,
        "nasdaq_trend": 0.12,
        "russell_participation": 0.10,
        "returns_momentum": 0.16,
        "rsi_zone": 0.08,
        "macd_confirmation": 0.10,
        "volume_confirmation": 0.05,
        "breadth": 0.07,
    }

    assert sum(weights.values()) == pytest.approx(1.0)
    expected = sum(result["decomposition"][name] * weight for name, weight in weights.items())
    assert result["score"] == round(expected, 4)


def test_compute_market_strength_is_deterministic_for_repeated_input():
    rows = [technical_row(ticker) for ticker in CORE_TICKERS]
    rows.append(technical_row("AAPL", stance="bearish"))

    assert compute_market_strength(rows) == compute_market_strength(rows)
