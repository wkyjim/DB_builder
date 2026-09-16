from __future__ import annotations

import math

import pytest

from db_builder.rule_based_config import SECTOR_ETF_MAP, scoring_weights
from db_builder.sector_strength import rank_sectors, score_sector


def market_row(ticker: str, *, stance: str = "strong", **overrides) -> dict[str, object]:
    if stance == "strong":
        values = {
            "close": 110.0,
            "ma_20": 100.0,
            "ma_50": 100.0,
            "ma_100": 100.0,
            "ma_200": 100.0,
            "return_5d": 1.0,
            "return_20d": 20.0,
            "return_60d": 20.0,
            "rsi_14": 60.0,
            "macd_hist": 1.0,
            "volume_ratio_20": 1.2,
            "volatility_20d": 10.0,
        }
    elif stance == "weak":
        values = {
            "close": 90.0,
            "ma_20": 100.0,
            "ma_50": 100.0,
            "ma_100": 100.0,
            "ma_200": 100.0,
            "return_5d": -1.0,
            "return_20d": -20.0,
            "return_60d": -20.0,
            "rsi_14": 30.0,
            "macd_hist": -1.0,
            "volume_ratio_20": 1.2,
            "volatility_20d": 10.0,
        }
    elif stance == "flat":
        values = {
            "close": 100.0,
            "ma_20": 100.0,
            "ma_50": 100.0,
            "ma_100": 100.0,
            "ma_200": 100.0,
            "return_5d": 0.0,
            "return_20d": 0.0,
            "return_60d": 0.0,
            "rsi_14": None,
            "macd_hist": None,
            "volume_ratio_20": 1.0,
            "volatility_20d": 20.0,
        }
    else:
        raise ValueError(f"unsupported stance: {stance}")
    return {"ticker": ticker, **values, **overrides}


def spy_row(**overrides) -> dict[str, object]:
    return market_row("SPY", stance="flat", **overrides)


def test_score_sector_representative_strong_and_weak_inputs():
    strong = score_sector("Healthcare", [spy_row(), market_row("XLV")], [])
    weak = score_sector("Energy", [spy_row(), market_row("XLE", stance="weak")], [])

    assert strong["score"] == 73.86
    assert strong["trend_label"] == "strong uptrend"
    assert strong["momentum_label"] == "positive"
    assert strong["breadth_label"] == "broad"
    assert weak["score"] == 27.64
    assert weak["trend_label"] == "strong downtrend"
    assert weak["momentum_label"] == "negative"
    assert weak["breadth_label"] == "weak"


def test_score_sector_representative_neutral_two_etf_group():
    rows = [
        spy_row(),
        market_row("XLK", stance="flat"),
        market_row(
            "QQQ",
            stance="flat",
            close=90.0,
            ma_20=100.0,
            ma_50=100.0,
            ma_100=100.0,
            ma_200=100.0,
        ),
    ]

    result = score_sector("Technology", rows, [])

    assert result["score"] == 50.0
    assert result["components"] == {
        "relative_strength": 50.0,
        "absolute_momentum": 50.0,
        "trend": 50.0,
        "rsi": 50.0,
        "macd": 50.0,
        "volume": 50.0,
        "volatility_adjusted_return": 50.0,
        "breadth": 50.0,
        "grouped_etf_flow": 50.0,
        "news": 50.0,
    }
    assert result["trend_label"] == "neutral"
    assert result["momentum_label"] == "neutral"
    assert result["breadth_label"] == "mixed"


def test_score_sector_output_schema_and_configured_etf_contract():
    result = score_sector("Semiconductors", [spy_row(), market_row("SMH")], [])

    assert set(result) == {
        "sector",
        "related_etfs",
        "score",
        "trend_label",
        "momentum_label",
        "breadth_label",
        "volatility_label",
        "three_month_relative_strength",
        "components",
        "flow_reliability",
        "top_supporting_tickers",
        "top_detracting_tickers",
    }
    assert result["sector"] == "Semiconductors"
    assert result["related_etfs"] == SECTOR_ETF_MAP["Semiconductors"]
    assert set(result["components"]) == set(scoring_weights()["sector_strength"])


def test_sector_score_reconciles_to_configured_weights():
    result = score_sector("Healthcare", [spy_row(), market_row("XLV")], [])
    weights = scoring_weights()["sector_strength"]

    assert sum(weights.values()) == pytest.approx(1.0)
    expected = sum(result["components"][name] * weight for name, weight in weights.items())
    assert result["score"] == round(expected, 4)


def test_rank_sectors_orders_strong_neutral_and_weak_groups():
    rows = [spy_row(), market_row("XLV"), market_row("XLE", stance="weak")]

    ranked = rank_sectors(rows, [])

    assert ranked[0]["sector"] == "Healthcare"
    assert ranked[0]["score"] == 73.86
    assert ranked[-1]["sector"] == "Energy"
    assert ranked[-1]["score"] == 27.64
    assert all(ranked[index]["score"] >= ranked[index + 1]["score"] for index in range(len(ranked) - 1))


def test_rank_sectors_empty_input_returns_all_sectors_as_stable_tie():
    ranked = rank_sectors([], [])

    assert [row["sector"] for row in ranked] == list(SECTOR_ETF_MAP)
    assert len(ranked) == len(SECTOR_ETF_MAP)
    assert {row["score"] for row in ranked} == {50.0}


def test_missing_sector_has_neutral_components_and_empty_support_lists():
    result = score_sector("Healthcare", [spy_row()], [])

    assert result["score"] == 50.0
    assert set(result["components"].values()) == {50.0}
    assert result["top_supporting_tickers"] == []
    assert result["top_detracting_tickers"] == []
    assert result["flow_reliability"] == 0.0


def test_partial_two_etf_sector_uses_only_available_member():
    result = score_sector("Semiconductors", [spy_row(), market_row("SMH")], [])

    assert result["components"]["trend"] == 100.0
    assert result["components"]["breadth"] == 100.0
    assert result["top_supporting_tickers"] == ["SMH"]
    assert result["top_detracting_tickers"] == ["SMH"]
    assert "SOXX" not in result["top_supporting_tickers"]


@pytest.mark.parametrize(
    ("return_20d", "expected_label"),
    [(10.0, "positive"), (-10.0, "neutral"), (-10.002, "negative")],
)
def test_momentum_label_exact_boundaries(return_20d, expected_label):
    sector = market_row("XLV", stance="flat", return_20d=return_20d, return_60d=0.0)

    result = score_sector("Healthcare", [spy_row(), sector], [])

    assert result["momentum_label"] == expected_label


def test_breadth_labels_cover_broad_mixed_and_weak_participation():
    broad = score_sector("Technology", [spy_row(), market_row("XLK"), market_row("QQQ")], [])
    mixed = score_sector("Technology", [spy_row(), market_row("XLK"), market_row("QQQ", stance="weak")], [])
    weak = score_sector("Technology", [spy_row(), market_row("XLK", stance="weak"), market_row("QQQ", stance="weak")], [])

    assert (broad["components"]["breadth"], broad["breadth_label"]) == (100.0, "broad")
    assert (mixed["components"]["breadth"], mixed["breadth_label"]) == (50.0, "mixed")
    assert (weak["components"]["breadth"], weak["breadth_label"]) == (0.0, "weak")


@pytest.mark.parametrize(
    ("volatility", "expected"),
    [(19.999, "low"), (20.0, "elevated"), (math.inf, "elevated")],
)
def test_volatility_label_boundary(volatility, expected):
    sector = market_row("XLV", volatility_20d=volatility)
    assert score_sector("Healthcare", [spy_row(), sector], [])["volatility_label"] == expected


def test_relative_strength_uses_spy_20d_and_60d_benchmark():
    spy = spy_row(return_20d=10.0, return_60d=5.0)
    sector = market_row("XLV", stance="flat", return_20d=20.0, return_60d=15.0)

    result = score_sector("Healthcare", [spy, sector], [])

    assert result["components"]["relative_strength"] == 60.0
    assert result["three_month_relative_strength"] == 60.0


def test_missing_spy_returns_use_zero_benchmark_fallback():
    sector = market_row("XLV", stance="flat", return_20d=10.0, return_60d=5.0)

    result = score_sector("Healthcare", [sector], [])

    assert result["components"]["relative_strength"] == 58.0


def test_numeric_none_nan_and_invalid_values_use_component_fallbacks():
    sector = market_row(
        "XLV",
        stance="flat",
        return_5d="invalid",
        return_20d=math.nan,
        return_60d=None,
        rsi_14="invalid",
        macd_hist=math.nan,
        volume_ratio_20="invalid",
        volatility_20d="invalid",
    )

    result = score_sector("Healthcare", [spy_row(), sector], [])

    assert result["components"]["absolute_momentum"] == 50.0
    assert result["components"]["relative_strength"] == 50.0
    assert result["components"]["rsi"] == 50.0
    assert result["components"]["macd"] == 50.0
    assert result["components"]["volume"] == 50.0
    assert result["components"]["volatility_adjusted_return"] == 50.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1000.0, 100.0), (-1000.0, 0.0), (math.inf, 100.0), (-math.inf, 0.0)],
)
def test_return_driven_components_are_clamped_for_extreme_values(value, expected):
    sector = market_row("XLV", return_20d=value, return_60d=value)

    result = score_sector("Healthcare", [spy_row(), sector], [])

    assert result["components"]["absolute_momentum"] == expected
    assert result["components"]["relative_strength"] == expected
    assert result["components"]["volatility_adjusted_return"] == expected
    assert math.isfinite(result["score"])
    assert 0 <= result["score"] <= 100


@pytest.mark.parametrize(
    ("opportunity", "risk", "expected"),
    [(1000, 0, 100.0), (0, 1000, 0.0), (None, None, 50.0), (math.nan, math.nan, 50.0)],
)
def test_news_component_is_case_insensitive_clamped_and_missing_safe(opportunity, risk, expected):
    signals = [
        {
            "dimension_value": "hEaLtHcArE",
            "opportunity_score": opportunity,
            "risk_score": risk,
        }
    ]

    result = score_sector("Healthcare", [spy_row(), market_row("XLV", stance="flat")], signals)

    assert result["components"]["news"] == expected


def test_news_component_uses_first_matching_sector_signal():
    signals = [
        {"dimension_value": "Healthcare", "opportunity_score": 40, "risk_score": 0},
        {"dimension_value": "Healthcare", "opportunity_score": 100, "risk_score": 0},
    ]

    result = score_sector("Healthcare", [spy_row(), market_row("XLV", stance="flat")], signals)

    assert result["components"]["news"] == 60.0


def test_flow_component_uses_sector_exposure_and_reports_reliability():
    flows = {
        "US_HEALTHCARE": {
            "adjusted_flow_score": 80,
            "signal_reliability": 65,
        }
    }

    result = score_sector("Healthcare", [spy_row(), market_row("XLV", stance="flat")], [], flows)

    assert result["components"]["grouped_etf_flow"] == 80.0
    assert result["flow_reliability"] == 65.0


@pytest.mark.parametrize("flows", [None, {}, {"US_HEALTHCARE": {}}])
def test_flow_component_missing_data_falls_back_to_neutral(flows):
    result = score_sector("Healthcare", [spy_row(), market_row("XLV", stance="flat")], [], flows)

    assert result["components"]["grouped_etf_flow"] == 50.0
    assert result["flow_reliability"] == 0.0


def test_supporting_and_detracting_tickers_are_sorted_by_20d_return():
    rows = [
        spy_row(),
        market_row("SMH", return_20d=5.0),
        market_row("SOXX", return_20d=10.0),
    ]

    result = score_sector("Semiconductors", rows, [])

    assert result["top_supporting_tickers"] == ["SOXX", "SMH"]
    assert result["top_detracting_tickers"] == ["SMH", "SOXX"]


def test_duplicate_ticker_rows_are_independent_equal_weight_observations():
    rows = [
        spy_row(),
        market_row("XLV", stance="flat", return_20d=10.0),
        market_row("XLV", stance="flat", return_20d=-10.0),
    ]

    result = score_sector("Healthcare", rows, [])

    assert result["components"]["absolute_momentum"] == 50.0
    assert result["top_supporting_tickers"] == ["XLV", "XLV"]
    assert result["top_detracting_tickers"] == ["XLV", "XLV"]


def test_score_and_ranking_are_deterministic_for_repeated_inputs():
    rows = [spy_row(), market_row("SMH"), market_row("SOXX", return_20d=10.0)]
    signals = [{"dimension_value": "Semiconductors", "opportunity_score": 60, "risk_score": 10}]

    assert score_sector("Semiconductors", rows, signals) == score_sector("Semiconductors", rows, signals)
    assert rank_sectors(rows, signals) == rank_sectors(rows, signals)


def test_unknown_sector_name_is_rejected():
    with pytest.raises(KeyError):
        score_sector("Not Configured", [], [])


def test_missing_price_inputs_use_neutral_breadth_fallback():
    result = score_sector("Healthcare", [{"ticker": "XLV"}], [])

    assert result["components"]["breadth"] == 50.0
    assert result["score"] == 50.0


@pytest.mark.parametrize(
    ("close", "ma_50"),
    [
        (None, 100),
        (100, None),
        (math.nan, 100),
        (100, math.nan),
        ("invalid", 100),
        (100, "invalid"),
        (math.inf, 100),
        (100, math.inf),
        (-math.inf, 100),
        (100, -math.inf),
    ],
)
def test_invalid_or_non_finite_price_inputs_are_excluded_from_breadth(close, ma_50):
    sector = market_row("XLV", stance="flat", close=close, ma_50=ma_50)

    result = score_sector("Healthcare", [sector], [])

    assert result["components"]["breadth"] == 50.0


def test_close_equal_to_ma_50_remains_above_ma_for_breadth():
    sector = market_row("XLV", stance="flat", close=100.0, ma_50=100.0)

    result = score_sector("Healthcare", [sector], [])

    assert result["components"]["breadth"] == 100.0
