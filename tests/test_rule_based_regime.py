from __future__ import annotations

import math

import pytest

from db_builder import rule_based_regime


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (100, "Strong Risk-On"),
        (80, "Strong Risk-On"),
        (79.9999, "Moderate Risk-On"),
        (65, "Moderate Risk-On"),
        (64.9999, "Mild Risk-On"),
        (55, "Mild Risk-On"),
        (54.9999, "Mixed / Rotation"),
        (45, "Mixed / Rotation"),
        (44.9999, "Mild Risk-Off"),
        (35, "Mild Risk-Off"),
        (34.9999, "Moderate Risk-Off"),
        (20, "Moderate Risk-Off"),
        (19.9999, "Defensive / Risk-Off"),
        (-100, "Defensive / Risk-Off"),
    ],
)
def test_regime_label_boundaries(score, expected):
    assert rule_based_regime.regime_label(score) == expected


def test_macro_by_symbol_returns_first_exact_match():
    rows = [
        {"symbol": "^VIX", "close": 14},
        {"symbol": "^VIX", "close": 30},
        {"symbol": "VIX", "close": 20},
    ]

    assert rule_based_regime.macro_by_symbol(rows, "^VIX") == rows[0]
    assert rule_based_regime.macro_by_symbol(rows, "VIX") == rows[2]
    assert rule_based_regime.macro_by_symbol(rows, "MISSING") == {}


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"close": 30, "pct_chg": 0}, 20.0),
        ({"close": 20, "pct_chg": 10}, 20.0),
        ({"close": 22, "pct_chg": 0}, 35.0),
        ({"close": 20, "pct_chg": 4}, 35.0),
        ({"close": 16, "pct_chg": 0}, 80.0),
        ({"close": 16.0001, "pct_chg": 0}, 70.0),
        ({"close": None, "pct_chg": None}, 70.0),
        ({"close": math.nan, "pct_chg": math.nan}, 70.0),
        ({"close": 15, "pct_chg": 12}, 20.0),
    ],
)
def test_volatility_score_branches_and_boundaries(row, expected):
    score, drivers = rule_based_regime.volatility_score([{"symbol": "^VIX", **row}])
    close = rule_based_regime.flt(row["close"], 20)
    pct_chg = rule_based_regime.flt(row["pct_chg"])

    assert score == expected
    assert drivers == [f"VIX close={round(close, 2)} pct_chg={round(pct_chg, 2)}"]


def test_volatility_score_is_neutral_when_vix_row_is_unavailable():
    assert rule_based_regime.volatility_score([]) == (
        50.0,
        ["VIX missing; volatility neutral"],
    )


def test_rates_score_is_neutral_without_ten_year_yield():
    assert rule_based_regime.rates_score([{"symbol": "^FVX", "close": 4.0}]) == (
        50.0,
        ["10Y Treasury missing; rates neutral"],
    )


@pytest.mark.parametrize("pct_chg", [1.5, -1.5])
def test_rates_score_change_thresholds_are_strict(pct_chg):
    score, _ = rule_based_regime.rates_score(
        [{"symbol": "US10YT=X", "close": 4.0, "pct_chg": pct_chg}]
    )

    assert score == 55.0


def test_rates_score_combines_aliases_curve_shape_and_falling_yields():
    score, drivers = rule_based_regime.rates_score(
        [
            {"symbol": "^FVX", "close": 4.5},
            {"symbol": "^TNX", "close": 4.0, "pct_chg": -1.5001},
            {"symbol": "^TYX", "close": 4.6},
        ]
    )

    assert score == 58.0  # 55 + 8 falling yields - 8 inversion + 3 term premium
    assert drivers == ["5Y=4.5", "10Y=4.0 pct_chg=-1.5", "30Y=4.6"]


def test_rates_score_handles_none_and_nan_optional_yields():
    score, drivers = rule_based_regime.rates_score(
        [
            {"symbol": "US5YT=X", "close": None},
            {"symbol": "US10YT=X", "close": 4.0, "pct_chg": math.nan},
            {"symbol": "US30YT=X", "close": math.nan},
        ]
    )

    assert score == 55.0
    assert drivers == ["5Y=None", "10Y=4.0 pct_chg=0.0", "30Y=None"]


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([{"symbol": "HG=F", "pct_chg": 0.5}], 50.0),
        ([{"symbol": "HG=F", "pct_chg": 0.5001}], 58.0),
        ([{"symbol": "HG=F", "pct_chg": -0.5001}], 45.0),
        ([{"symbol": "SI=F", "pct_chg": 0.5001}], 55.0),
        ([{"symbol": "SI=F", "pct_chg": -0.5001}], 47.0),
        ([{"symbol": "CL=F", "pct_chg": 0}], 54.0),
        ([{"symbol": "CL=F", "pct_chg": 2}], 54.0),
        ([{"symbol": "CL=F", "pct_chg": 4}], 50.0),
        ([{"symbol": "CL=F", "pct_chg": 4.0001}], 46.0),
        ([{"symbol": "CL=F", "pct_chg": -2}], 50.0),
        ([{"symbol": "CL=F", "pct_chg": -2.0001}], 48.0),
        ([{"symbol": "GC=F", "pct_chg": 1}], 50.0),
        ([{"symbol": "GC=F", "pct_chg": 1.0001}], 46.0),
        ([{"symbol": "HG=F", "pct_chg": math.nan}], 50.0),
    ],
)
def test_commodity_score_branches_and_boundaries(rows, expected):
    score, _ = rule_based_regime.commodity_score(rows)

    assert score == expected


def test_commodity_score_returns_neutral_with_missing_driver():
    assert rule_based_regime.commodity_score([]) == (
        50.0,
        ["commodity data missing; neutral"],
    )


@pytest.mark.parametrize(
    ("pct_chg", "expected"),
    [(1, 50.0), (11, 0.0), (-9, 100.0), (math.inf, 0.0), (-math.inf, 100.0), (math.nan, 55.0)],
)
def test_dollar_score_formula_and_caps(pct_chg, expected):
    score, _ = rule_based_regime.dollar_score(
        [{"symbol": "DX-Y.NYB", "pct_chg": pct_chg}]
    )

    assert score == expected


def test_dollar_score_is_neutral_when_both_symbols_are_missing():
    assert rule_based_regime.dollar_score([]) == (
        50.0,
        ["DXY missing; dollar neutral"],
    )


def test_compute_regime_empty_inputs_use_neutral_components_and_warnings():
    result = rule_based_regime.compute_regime([], [], [], {})

    assert result["score"] == 50.0
    assert result["label"] == "Mixed / Rotation"
    assert set(result["subscores"].values()) == {50.0}
    assert result["positive_contributors"] == []
    assert result["negative_contributors"] == []
    assert result["missing_data_warnings"] == [
        "DXY unavailable",
        "core equity technical rows unavailable",
    ]
    assert result["news"]["sentiment_counts"] == {
        "positive": 0,
        "negative": 0,
        "neutral": 1,
    }


def test_compute_regime_uses_documented_default_component_weights():
    technical_rows = [
        {
            "ticker": ticker,
            "close": 110,
            "ma_20": 100,
            "ma_50": 100,
            "ma_100": 100,
            "ma_200": 100,
            "return_5d": 0,
            "return_20d": 0,
            "return_60d": 0,
        }
        for ticker in ["SPY", "QQQ", "IWM", "SMH"]
    ]
    macro_rows = [
        {"symbol": "^VIX", "close": 15, "pct_chg": -1},
        {"symbol": "^TNX", "close": 4, "pct_chg": 0},
        {"symbol": "DXY", "pct_chg": 1},
    ]
    news_rows = [{"sentiment_score": 0.2}]

    result = rule_based_regime.compute_regime(
        technical_rows,
        macro_rows,
        news_rows,
        {"breadth": {"score": 60}},
    )

    assert result["subscores"] == {
        "equity_trend": 100.0,
        "equity_momentum": 50.0,
        "market_breadth": 60.0,
        "volatility": 80.0,
        "rates_yield_curve": 55.0,
        "credit_proxy": 50.0,
        "dollar_fx": 50.0,
        "commodity_confirmation": 50.0,
        "etf_flow": 50.0,
        "news_confirmation": 100.0,
    }
    assert result["score"] == 66.2
    assert result["label"] == "Moderate Risk-On"


def test_compute_regime_ignores_non_core_tickers():
    neutral = rule_based_regime.compute_regime([], [], [], {})
    with_non_core = rule_based_regime.compute_regime(
        [
            {
                "ticker": "AAPL",
                "close": 1000,
                "ma_20": 1,
                "ma_50": 1,
                "ma_100": 1,
                "ma_200": 1,
                "return_5d": 1000,
                "return_20d": 1000,
                "return_60d": 1000,
            }
        ],
        [],
        [],
        {},
    )

    assert with_non_core == neutral


@pytest.mark.parametrize(
    ("flow_score", "reliability", "expected"),
    [(100, 50, 75.0), (20, 50, 35.0), (1000, 100, 100.0), (-1000, 100, 0.0)],
)
def test_compute_regime_adjusts_and_clamps_etf_flow_by_reliability(
    flow_score, reliability, expected
):
    result = rule_based_regime.compute_regime(
        [],
        [],
        [],
        {},
        {
            "flow_regime": {
                "flow_regime_score": flow_score,
                "flow_regime_confidence": reliability,
            }
        },
    )

    assert result["subscores"]["etf_flow"] == expected


def test_compute_regime_preserves_explicit_zero_etf_flow_score():
    result = rule_based_regime.compute_regime(
        [],
        [],
        [],
        {},
        {
            "flow_regime": {
                "flow_regime_score": 0,
                "score": 80,
                "flow_regime_confidence": 100,
                "confidence": 90,
            }
        },
    )

    assert result["subscores"]["etf_flow"] == 0.0


def test_compute_regime_etf_flow_none_uses_compatibility_fields():
    result = rule_based_regime.compute_regime(
        [],
        [],
        [],
        {},
        {
            "flow_regime": {
                "flow_regime_score": None,
                "score": 20,
                "flow_regime_confidence": None,
                "confidence": 50,
            }
        },
    )

    assert result["subscores"]["etf_flow"] == 35.0


def test_compute_regime_preserves_explicit_zero_etf_flow_confidence():
    result = rule_based_regime.compute_regime(
        [],
        [],
        [],
        {},
        {
            "flow_regime": {
                "flow_regime_score": 100,
                "flow_regime_confidence": 0,
                "confidence": 80,
            }
        },
    )

    assert result["subscores"]["etf_flow"] == 50.0


def test_compute_regime_uses_weights_returned_by_configuration(monkeypatch):
    monkeypatch.setattr(
        rule_based_regime,
        "scoring_weights",
        lambda: {"market_regime": {"dollar_fx": 1.0}},
    )

    result = rule_based_regime.compute_regime(
        [],
        [{"symbol": "DXY", "pct_chg": 3}],
        [],
        {},
    )

    assert result["subscores"]["dollar_fx"] == 40.0
    assert result["score"] == 40.0
    assert result["label"] == "Mild Risk-Off"


def test_compute_regime_contributors_are_ordered_filtered_and_limited(monkeypatch):
    weights = rule_based_regime.scoring_weights()["market_regime"]
    monkeypatch.setattr(
        rule_based_regime,
        "scoring_weights",
        lambda: {"market_regime": {key: 0.0 for key in weights}},
    )
    monkeypatch.setattr(rule_based_regime, "score_above_ma", lambda row: row["trend"])
    monkeypatch.setattr(rule_based_regime, "score_return_momentum", lambda row: row["momentum"])
    monkeypatch.setattr(rule_based_regime, "volatility_score", lambda rows: (90.0, ["vol"]))
    monkeypatch.setattr(rule_based_regime, "rates_score", lambda rows: (10.0, ["rates"]))
    monkeypatch.setattr(rule_based_regime, "dollar_score", lambda rows: (80.0, ["dollar"]))
    monkeypatch.setattr(rule_based_regime, "commodity_score", lambda rows: (20.0, ["commodity"]))
    monkeypatch.setattr(rule_based_regime, "_etf_flow_score", lambda flow: (70.0, ["flow"]))
    monkeypatch.setattr(rule_based_regime, "score_news", lambda rows: {"score": 30.0})

    result = rule_based_regime.compute_regime(
        [{"ticker": "SPY", "trend": 100.0, "momentum": 60.0}],
        [{"symbol": "DXY"}],
        [],
        {"breadth": {"score": 65.0}},
    )

    assert result["positive_contributors"] == [
        "equity_trend=100.0",
        "volatility=90.0",
        "dollar_fx=80.0",
        "etf_flow=70.0",
        "market_breadth=65.0",
        "equity_momentum=60.0",
    ]
    assert result["negative_contributors"] == [
        "rates_yield_curve=10.0",
        "commodity_confirmation=20.0",
        "news_confirmation=30.0",
    ]
    assert result["drivers"] == ["vol", "rates", "dollar", "commodity", "flow"]


def test_compute_regime_is_deterministic_for_identical_inputs():
    args = (
        [{"ticker": "SPY", "close": 100, "ma_20": 90, "return_20d": 2}],
        [{"symbol": "DX-Y.NYB", "pct_chg": -2}],
        [{"sentiment_score": -0.2}],
        {"breadth": {"score": 75}},
    )

    assert rule_based_regime.compute_regime(*args) == rule_based_regime.compute_regime(*args)


def _complete_macro_rows():
    symbols = {
        "^GSPC",
        "^IXIC",
        "^RUT",
        "^VIX",
        "^SKEW",
        "^MOVE",
        "US2YT=X",
        "US5YT=X",
        "US10YT=X",
        "US30YT=X",
        "GC=F",
        "CL=F",
        "HG=F",
        "HYG",
        "LQD",
        "RSP",
        "DXY",
    }
    return [{"symbol": symbol} for symbol in sorted(symbols)]


def test_compute_confidence_full_coverage_agreement_and_news_cap():
    result = rule_based_regime.compute_confidence(
        {"subscores": {f"component_{idx}": 60 for idx in range(10)}},
        {"missing_indicators": []},
        [],
        _complete_macro_rows(),
        [{} for _ in range(25)],
    )

    assert result == {
        "score": 97.0,
        "agreement_ratio": 1.0,
        "contradiction_count": 0,
        "missing_indicators": [],
        "warning_flags": [],
    }


def test_compute_confidence_empty_inputs_and_penalty_are_floored():
    missing = [f"missing-{idx}" for idx in range(20)]

    result = rule_based_regime.compute_confidence(
        {}, {"missing_indicators": missing}, [], [], []
    )

    assert result["score"] == 0.0
    assert result["agreement_ratio"] == 0
    assert result["contradiction_count"] == 0
    assert result["missing_indicators"] == missing


def test_compute_confidence_counts_boundary_signals_and_disagreement_warning():
    result = rule_based_regime.compute_confidence(
        {
            "subscores": {
                "bull_1": 55,
                "bull_2": 100,
                "bull_3": 60,
                "bull_4": 70,
                "bear_1": 45,
                "bear_2": 0,
                "bear_3": 40,
                "bear_4": 30,
                "neutral_1": 50,
                "neutral_2": math.nan,
            },
            "missing_data_warnings": ["DXY unavailable"],
        },
        {"missing_indicators": []},
        [],
        [],
        [],
    )

    assert result["agreement_ratio"] == 0.4
    assert result["contradiction_count"] == 4
    assert result["warning_flags"] == [
        "DXY unavailable",
        "signal disagreement elevated",
    ]


def test_compute_confidence_indicator_penalty_is_capped_at_thirty_points():
    base_args = ({"subscores": {"one": 60}}, [], [], [])
    ten_missing = rule_based_regime.compute_confidence(
        base_args[0], {"missing_indicators": [str(i) for i in range(10)]}, *base_args[1:]
    )
    twenty_missing = rule_based_regime.compute_confidence(
        base_args[0], {"missing_indicators": [str(i) for i in range(20)]}, *base_args[1:]
    )

    assert ten_missing["score"] == twenty_missing["score"]


def test_compute_confidence_counts_dxy_and_its_alias_as_same_expected_signal():
    complete = _complete_macro_rows()
    canonical = rule_based_regime.compute_confidence({}, {}, [], complete, [])
    alias = rule_based_regime.compute_confidence(
        {},
        {},
        [],
        [
            {"symbol": "DX-Y.NYB"} if row["symbol"] == "DXY" else row
            for row in complete
        ],
        [],
    )
    missing = rule_based_regime.compute_confidence(
        {}, {}, [], [row for row in complete if row["symbol"] != "DXY"], []
    )

    assert canonical["score"] == alias["score"]
    assert canonical["score"] > missing["score"]
