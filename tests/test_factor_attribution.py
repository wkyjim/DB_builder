"""Regression tests for factor_attribution (pure logic -- no DB, no pandas required).

Covers the attribution identities that the report depends on:
  * regime contributions sum exactly to bullish_score, and deltas reconcile
  * sector components sum exactly to rate_sector() including the clip adjustment
  * ablation policies are arithmetic-consistent and bounded
  * walk-forward blocks never let training overlap evaluation
  * non-overlapping sampling really removes overlapping forward windows
  * metric helpers (IC stats, Brier, reliability bins, downside profile) behave
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from db_builder.aegis_backtest import rate_sector
from db_builder.factor_attribution import (
    REGIME_FACTORS,
    REGIME_FACTOR_COLUMNS,
    SECTOR_COMPONENTS,
    SECTOR_COMPONENT_COLUMNS,
    attribution_ddl,
    regime_attribution_rows,
    sector_attribution_rows,
    ablate_regime_score,
    attribute_regime,
    attribute_sector_row,
    brier_score,
    downside_profile,
    equal_weight_from_components,
    equal_weight_regime,
    fit_component_weights,
    fit_ic_weights,
    fit_regime_overlay,
    normalize_capped,
    weight_decision,
    evaluate_regime_ablation,
    evaluate_regime_challengers,
    purge_before,
    ic_p_value,
    ic_stats,
    ic_t_stat,
    momentum_flip_from_components,
    neutralized_rating,
    non_overlapping,
    regime_delta,
    regime_factor_weights,
    regime_weight_sensitivity,
    reliability_bins,
    sector_rank,
    sector_rating_components,
    self_test,
    _rating_fn_factory,
    unclipped_rating_from_components,
    _clamp,
    shrinkage_regime,
    sign_hit_rate,
    walk_forward_blocks,
)

BASE_REGIME = {
    "run_time": "2026-10-31T21:07:33+08:00",
    "market_regime": "risk_off",
    "market_phase": "late_bull",
    "confidence": 0.2594,
    "technical_score": 44.5,
    "momentum_score": 45.5814,
    "risk_appetite_score": 16.0,
    "breadth_score": 45.7895,
    "macro_score": 23.4648,
    "news_score": 43.7529,
    "bullish_score": 37.0315,
}

# Panel-shaped row (the shape build_regime_panel produces) with news_score absent.
# Score = 0.25*44.5 + 0.20*45.5814 + 0.20*16.0 + 0.15*45.7895 + 0.10*23.4648 + 0.10*50
BASE_REGIME_PANEL_ROW_MISSING_NEWS = {
    "origin": date(2026, 1, 1),
    "score": 37.6562,
    "label": "risk_off",
    "phase": "late_bull",
    "confidence": 0.25,
    "factors": {
        "technical_score": 44.5,
        "momentum_score": 45.5814,
        "risk_appetite_score": 16.0,
        "breadth_score": 45.7895,
        "macro_score": 23.4648,
    },
    "fwd": {5: 1.0, 20: -2.0, 60: 4.0},
    "maxdd": {5: -0.5, 20: -3.0, 60: -5.0},
    "realized_label": {5: "risk_on", 20: "risk_off", 60: "risk_on"},
}


# --------------------------------------------------------------------------- #
#  Regime attribution
# --------------------------------------------------------------------------- #
def test_weights_sum_to_one():
    assert abs(sum(regime_factor_weights().values()) - 1.0) < 1e-12


def test_attribution_sums_to_score():
    att = attribute_regime(BASE_REGIME)
    assert abs(att["sum_contributions"] - BASE_REGIME["bullish_score"]) < 1e-4
    assert abs(att["residual"]) < 1e-4
    assert len(att["factors"]) == len(REGIME_FACTORS)


def test_attribution_matches_manual_weighting():
    att = attribute_regime(BASE_REGIME)
    by_name = {f["factor"]: f for f in att["factors"]}
    # contributions are reported to 4 dp, so compare with a matching tolerance
    assert by_name["technical_score"]["contribution"] == pytest.approx(0.25 * 44.5, abs=1e-4)
    assert by_name["risk_appetite_score"]["contribution"] == pytest.approx(0.20 * 16.0, abs=1e-4)
    assert by_name["news_score"]["contribution"] == pytest.approx(0.10 * 43.7529, abs=1e-4)


def test_top_contributors_are_the_extremes():
    att = attribute_regime(BASE_REGIME)
    # Every factor sits below the 50 neutral line here, so the "upward pull" is the
    # least-negative one (news 43.75) and the worst is risk_appetite (16.0 at w=0.20).
    assert att["top_positive"] == "news_score"
    assert att["top_negative"] == "risk_appetite_score"
    assert all(f["excess_vs_neutral"] < 0 for f in att["factors"])


def test_share_of_excess_is_signed_and_sums_to_100():
    att = attribute_regime(BASE_REGIME)
    shares = [f["share_of_excess"] for f in att["factors"]]
    assert all(s is not None for s in shares)
    assert sum(shares) == pytest.approx(100.0, abs=1e-3)


def test_share_of_excess_undefined_at_neutral():
    neutral = dict(BASE_REGIME, bullish_score=50.0)
    att = attribute_regime(neutral)
    # all six factors at exactly 50 -> excess is zero
    for key in regime_factor_weights():
        neutral[key] = 50.0
    att = attribute_regime(neutral)
    assert all(f["share_of_excess"] is None for f in att["factors"])


def test_missing_factor_defaults_to_neutral_and_is_flagged():
    partial = {k: v for k, v in BASE_REGIME.items() if k != "news_score"}
    att = attribute_regime(partial)
    assert "news_score" in att["missing_factors"]
    news = next(f for f in att["factors"] if f["factor"] == "news_score")
    assert news["score"] == 50.0


def test_saturated_factors_flagged_at_bounds():
    regime = dict(BASE_REGIME, risk_appetite_score=0.0, technical_score=100.0)
    att = attribute_regime(regime)
    assert "risk_appetite_score" in att["saturated_factors"]
    assert "technical_score" in att["saturated_factors"]


def _consistent_regime(**overrides) -> dict:
    """Regime dict whose bullish_score is derived from its own factor scores."""
    base = {
        "market_regime": "risk_off", "market_phase": "late_bull", "confidence": 0.1,
        "technical_score": 44.5, "momentum_score": 45.5814, "risk_appetite_score": 16.0,
        "breadth_score": 45.7895, "macro_score": 23.4648, "news_score": 43.7529,
    }
    base.update(overrides)
    base["bullish_score"] = round(sum(
        regime_factor_weights()[k] * base[k] for k, _, _ in REGIME_FACTORS), 4)
    return base


def test_delta_reconciles_to_score_change():
    prev = _consistent_regime(technical_score=60.0, momentum_score=60.0)
    curr = _consistent_regime(technical_score=70.0, breadth_score=55.0)
    delta = regime_delta(attribute_regime(prev), attribute_regime(curr))
    assert delta["delta_score"] == pytest.approx(curr["bullish_score"] - prev["bullish_score"])
    assert abs(delta["sum_delta_contributions"] - delta["delta_score"]) < 1e-4


def test_delta_orders_by_contribution_change():
    prev = attribute_regime(dict(BASE_REGIME, bullish_score=50.0, technical_score=40.0))
    curr = attribute_regime(dict(BASE_REGIME, bullish_score=60.0, technical_score=80.0))
    delta = regime_delta(prev, curr)
    assert delta["largest_positive"] == "technical_score"
    assert delta["delta_contributions"][-1]["factor"] == "technical_score"


def test_weight_sensitivity_renormalizes_to_one():
    base = regime_factor_weights()
    for row in regime_weight_sensitivity(BASE_REGIME, shock=0.5):
        key = row["factor"]
        assert row["weight_from"] == pytest.approx(base[key])
        assert 0.0 <= row["weight_to"] <= 1.0
        # Shifting weight onto a factor moves the score by Delta*(score_i - avg_others):
        # the reference is the weighted average of the OTHER factors, not the total.
        others = {k: w for k, w in base.items() if k != key}
        avg_others = sum(w * BASE_REGIME[k] for k, w in others.items()) / sum(others.values())
        delta = row["delta_score"]
        gap = BASE_REGIME[key] - avg_others
        if row["direction"] == "up" and abs(gap) > 1e-9:
            assert (delta > 0) == (gap > 0), row


def test_ablation_neutral_fill_equals_marginal_contribution():
    abl = ablate_regime_score(BASE_REGIME, "risk_appetite_score")
    # 0.20 * (16.0 - 50) = -6.8 -> removing it raises the score by 6.8
    assert abl["delta_score"] == pytest.approx(6.8, abs=1e-6)
    assert abl["ablated_score"] == pytest.approx(BASE_REGIME["bullish_score"] + 6.8, abs=1e-6)


def test_ablation_renormalize_is_bounded():
    for key in regime_factor_weights():
        abl = ablate_regime_score(BASE_REGIME, key, policy="renormalize")
        assert 0.0 <= abl["ablated_score"] <= 100.0
        assert abs(sum(abl["weights_used"].values()) - 1.0) < 1e-9


def test_ablation_rejects_unknown_factor_and_policy():
    with pytest.raises(ValueError):
        ablate_regime_score(BASE_REGIME, "not_a_factor")
    with pytest.raises(ValueError):
        ablate_regime_score(BASE_REGIME, "news_score", policy="bogus")


def test_equal_and_shrinkage_scores():
    att = attribute_regime(BASE_REGIME)
    eq = equal_weight_regime(BASE_REGIME)
    expected = sum(f["score"] for f in att["factors"]) / len(REGIME_FACTORS)
    assert eq == pytest.approx(expected, abs=1e-4)
    sh = shrinkage_regime(BASE_REGIME, lam=0.5)
    assert sh == pytest.approx(0.5 * eq + 0.5 * BASE_REGIME["bullish_score"], abs=1e-4)


# --------------------------------------------------------------------------- #
#  Sector attribution
# --------------------------------------------------------------------------- #
ROW_GRID = [
    {"close": 120.0, "ma_50": 110.0, "ma_200": 100.0, "rsi_14": 60.0,
     "macd_hist": 0.002, "return_20d": 0.05, "return_60d": 0.12},
    {"close": 80.0, "ma_50": 90.0, "ma_200": 95.0, "rsi_14": 28.0,
     "macd_hist": -0.004, "return_20d": -0.09, "return_60d": -0.18},
    # deliberately saturating both momentum terms in both directions
    {"close": 200.0, "ma_50": 100.0, "ma_200": 100.0, "rsi_14": 95.0,
     "macd_hist": 0.5, "return_20d": 5.0, "return_60d": 9.0},
    {"close": 1.0, "ma_50": 100.0, "ma_200": 100.0, "rsi_14": 2.0,
     "macd_hist": -0.5, "return_20d": -5.0, "return_60d": -9.0},
    # neutral exactly
    {"close": 100.0, "ma_50": 100.0, "ma_200": 100.0, "rsi_14": 50.0,
     "macd_hist": 0.0, "return_20d": 0.0, "return_60d": 0.0},
    # zero / missing MA values (guarded comparison branches)
    {"close": 50.0, "ma_50": 0.0, "ma_200": 0.0, "rsi_14": 50.0,
     "macd_hist": 0.0, "return_20d": 0.0, "return_60d": 0.0},
    # NaN inputs -> coerced to 0.0 like rate_sector's _f
    {"close": float("nan"), "ma_50": float("nan"), "ma_200": float("nan"),
     "rsi_14": float("nan"), "macd_hist": float("nan"),
     "return_20d": float("nan"), "return_60d": float("nan")},
]


@pytest.mark.parametrize("row", ROW_GRID)
def test_components_match_rate_sector(row):
    comp = sector_rating_components(row)
    reconstructed = comp["base"] + sum(comp["components"].values()) + comp["clip_adjustment"]
    assert reconstructed == pytest.approx(rate_sector(row), abs=1e-9)
    assert 0.0 <= comp["rating"] <= 100.0


def test_components_neutral_row_is_all_zero():
    comp = sector_rating_components(ROW_GRID[4])
    assert comp["rating"] == 50.0
    assert all(v == 0.0 for v in comp["components"].values())
    assert comp["clip_adjustment"] == 0.0
    assert comp["clipped"] is False


def test_clipping_is_isolated_in_adjustment():
    comp = sector_rating_components(ROW_GRID[2])  # saturated upward
    assert comp["clipped"] is True
    assert comp["clip_adjustment"] < 0  # clipped down to 100
    assert comp["rating"] == 100.0


def test_component_budgets_match_declared_maxima():
    comp = sector_rating_components(ROW_GRID[2])
    budgets = {k: b for k, b, _ in SECTOR_COMPONENTS}
    for key, budget in budgets.items():
        assert abs(comp["components"][key]) <= budget + 1e-9


def test_sector_rank_is_deterministic_and_tie_broken_by_ticker():
    ranks = sector_rank({"XLK": 80.0, "XLF": 80.0, "XLE": 40.0})
    assert ranks == {"XLF": 1, "XLK": 2, "XLE": 3}


def test_attribute_sector_row_includes_rank():
    ratings = {"XLK": 80.0, "XLF": 60.0, "XLE": 40.0}
    row = dict(ROW_GRID[0], ticker="XLK")
    att = attribute_sector_row(row, ratings)
    assert att["ticker"] == "XLK"
    assert att["rank"] == 1
    assert att["n_peers"] == 3


def test_neutralized_rating_equals_removing_component():
    row = ROW_GRID[0]
    comp = sector_rating_components(row)
    for key in comp["components"]:
        neutral = neutralized_rating(row, key)
        expected = comp["base"] + sum(v for k, v in comp["components"].items() if k != key)
        assert neutral == pytest.approx(max(0.0, min(100.0, expected)), abs=1e-4)


def test_equal_weight_and_meanreversion_variants_are_bounded():
    for row in ROW_GRID:
        comp = sector_rating_components(row)["components"]
        for fn in (equal_weight_from_components, momentum_flip_from_components):
            assert 0.0 <= fn(comp) <= 100.0


# --------------------------------------------------------------------------- #
#  Metric helpers
# --------------------------------------------------------------------------- #
def test_ic_stats_known_values():
    stats = ic_stats([0.2, 0.4, 0.1, 0.3])
    assert stats["n"] == 4
    assert stats["mean_ic"] == pytest.approx(0.25)
    assert stats["pct_positive"] == 1.0
    assert stats["ic_ir"] > 0


def test_ic_stats_handles_empty_and_zero_variance():
    empty = ic_stats([])
    assert empty["n"] == 0 and empty["mean_ic"] is None
    flat = ic_stats([0.1, 0.1, 0.1])
    assert flat["ic_std"] == 0.0
    assert flat["t_stat"] is None


def test_ic_t_stat_and_p_value_scale_with_n():
    small = ic_t_stat(0.2, 10)
    large = ic_t_stat(0.2, 100)
    assert small is not None and large is not None and large > small
    assert 0.0 <= ic_p_value(0.2, 100) < 0.05
    assert ic_t_stat(0.2, 2) is None


def test_brier_score_perfect_and_worst():
    assert brier_score([100.0], [1.0], lo=0.0, hi=1.0) == 0.0
    assert brier_score([0.0], [1.0], lo=0.0, hi=1.0) == 1.0
    assert brier_score([], []) is None


def test_reliability_bins_cover_all_observations():
    scores = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    outcomes = [0, 0, 0, 0, 1, 1, 1, 1, 1, 1]
    bins = reliability_bins(scores, outcomes, n_bins=5)
    assert sum(b["count"] for b in bins) == len(scores)
    assert bins[-1]["outcome_rate"] > bins[0]["outcome_rate"]


def test_reliability_bins_single_value():
    bins = reliability_bins([50.0], [1.0], n_bins=5)
    assert len(bins) == 1 and bins[0]["count"] == 1


def test_downside_profile_buckets():
    values = [20.0, 30.0, 70.0, 80.0]
    returns = [-5.0, -2.0, 3.0, 6.0]
    prof = downside_profile(values, returns, weak_below=40.0, strong_above=60.0)
    assert prof["n_weak"] == 2 and prof["n_strong"] == 2
    assert prof["neg_hit_rate_weak"] == 1.0
    assert prof["pos_hit_rate_strong"] == 1.0
    # separation = P(up|strong) - P(up|weak) = 1.0 - 0.0
    assert prof["separation"] == 1.0
    assert prof["up_rate_weak"] == 0.0
    assert prof["mean_return_weak"] == pytest.approx(-3.5)


def test_sign_hit_rate_counts_side_of_neutral():
    assert sign_hit_rate([60.0, 40.0, 60.0, 40.0], [2.0, -1.0, -3.0, 4.0]) == 0.5
    assert sign_hit_rate([], []) is None


# --------------------------------------------------------------------------- #
#  Sampling safeguards
# --------------------------------------------------------------------------- #
def test_non_overlapping_spacing_respects_horizon():
    origins = [date(2026, 1, 1) + timedelta(days=7 * i) for i in range(20)]
    for h in (5, 20, 60):
        kept = non_overlapping(origins, h)
        gap = max(1, math.ceil(h * 7 / 5))
        assert all((b - a).days >= gap for a, b in zip(kept, kept[1:]))
        assert kept[0] == origins[0]
        assert len(kept) >= 1


def test_non_overlapping_empty():
    assert non_overlapping([], 20) == []


def test_walk_forward_blocks_train_precedes_eval():
    origins = [date(2026, 1, 1) + timedelta(days=7 * i) for i in range(40)]
    blocks = walk_forward_blocks(origins, n_blocks=4, min_train=10)
    assert blocks
    for blk in blocks:
        assert max(blk["train"]) < min(blk["eval"])
        assert not set(blk["train"]) & set(blk["eval"])
        assert blk["train_n"] >= 10


def test_walk_forward_blocks_insufficient_history():
    origins = [date(2026, 1, 1) + timedelta(days=7 * i) for i in range(5)]
    assert walk_forward_blocks(origins, n_blocks=4, min_train=20) == []


def test_purge_before_drops_training_windows_reaching_into_eval():
    origins = [date(2026, 1, 1) + timedelta(days=7 * i) for i in range(20)]
    first_eval = date(2026, 3, 1)
    kept = purge_before(origins, first_eval, horizon=20)  # 20 trading days = 28 calendar
    assert kept
    assert all((d + timedelta(days=28)) <= first_eval for d in kept)
    assert all(d not in kept for d in origins if (d + timedelta(days=28)) > first_eval)
    assert purge_before(origins, None, 20) == origins


def test_regime_challengers_produce_blocks_at_long_horizons():
    """Non-overlapping EVAL sampling must not starve the 20D walk-forward of blocks."""
    panel = _synthetic_regime_rows(41)
    ch = evaluate_regime_challengers(panel, [20], n_blocks=4, min_train=20)
    assert ch["n_blocks"] >= 2
    summary = ch["horizons"][20]["summary"]["production"]
    assert summary["n_blocks"] >= 1
    assert summary["n_oos"] >= 1
    # training rows are the full (purged) train block, not the non-overlapping subset
    blk = ch["horizons"][20]["per_block"][0]
    assert blk["n_train"] >= 20
    assert blk["n_eval"] < blk["n_train"]


# --------------------------------------------------------------------------- #
#  Weight fitting (train-only, regularized)
# --------------------------------------------------------------------------- #
def _synthetic_regime_rows(n: int, seed: int = 0) -> list[dict]:
    rows = []
    keys = [k for k, _, _ in REGIME_FACTORS]
    for i in range(n):
        # technical/momentum genuinely predict; news is noise
        tech = 40 + (i * 7 + seed) % 40
        mom = 45 + (i * 3) % 20
        news = 50 + ((i * 13) % 9) - 4
        ret = (tech - 50) * 0.08 + ((i * 5) % 7 - 3)
        factors = {k: 50.0 for k in keys}
        factors["technical_score"] = float(tech)
        factors["momentum_score"] = float(mom)
        factors["news_score"] = float(news)
        rows.append({
            "origin": date(2026, 1, 1) + timedelta(days=7 * i),
            "score": sum(regime_factor_weights()[k] * factors[k] for k in keys),
            "factors": factors,
            "fwd": {5: ret, 20: ret * 2, 60: ret * 3},
            "maxdd": {5: -abs(ret) / 2, 20: -abs(ret), 60: -abs(ret) * 1.5},
            "realized_label": {5: "risk_on" if ret > 0 else "risk_off",
                               20: "risk_on" if ret > 0 else "risk_off",
                               60: "risk_on" if ret > 0 else "risk_off"},
            "label": "risk_on" if ret > 0 else "risk_off",
        })
    return rows


def test_fit_ic_weights_are_normalized_and_capped():
    rows = _synthetic_regime_rows(40)
    w = fit_ic_weights(rows, [k for k, _, _ in REGIME_FACTORS], 20)
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert all(0.0 <= v <= 0.40 + 1e-9 for v in w.values())


def test_fit_ic_weights_favour_predictive_factor():
    rows = _synthetic_regime_rows(40)
    w = fit_ic_weights(rows, [k for k, _, _ in REGIME_FACTORS], 20)
    assert w["technical_score"] > w["news_score"]


def test_fit_ic_weights_without_history_falls_back_to_equal():
    w = fit_ic_weights([], [k for k, _, _ in REGIME_FACTORS], 20)
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_normalize_capped_respects_cap_after_renormalizing():
    # one dominant input must still land inside the cap, not above it
    w = normalize_capped({"a": 50.0, "b": 1.0, "c": 1.0, "d": 1.0, "e": 1.0, "f": 1.0}, cap=0.40)
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-12)
    assert max(w.values()) <= 0.40 + 1e-12
    assert w["a"] == pytest.approx(0.40)
    assert all(v >= 0.05 - 1e-12 for v in w.values())


def test_normalize_capped_equal_inputs_stay_equal():
    w = normalize_capped({k: 1.0 for k in "abcdef"}, cap=0.40)
    assert all(v == pytest.approx(1 / 6, abs=1e-12) for v in w.values())


def test_normalize_capped_relaxes_infeasible_cap_to_equal_weight():
    # two weights cannot both sit under 0.40 and sum to 1 -> cap relaxes to 0.5
    w = normalize_capped({"a": 1.0, "b": 1.0}, cap=0.40)
    assert w == pytest.approx({"a": 0.5, "b": 0.5})


def test_normalize_capped_rejects_infeasible_floor():
    with pytest.raises(ValueError):
        normalize_capped({"a": 1.0, "b": 1.0}, cap=0.6, floor=0.8)


def test_fit_ic_weights_never_exceed_cap():
    rows = _synthetic_regime_rows(40)
    w = fit_ic_weights(rows, [k for k, _, _ in REGIME_FACTORS], 20)
    assert max(w.values()) <= 0.40 + 1e-12
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-12)


def test_weight_decision_requires_sample_and_consistency():
    thin = [{"n_eval": 3, "base": -0.2, "prop": 0.0}]
    d = weight_decision(thin, "base", "prop")
    assert "do not change weights" in d["decision"]
    assert any("OOS dates" in r for r in d["reasons"])

    one_block = [{"n_eval": 20, "base": -0.1, "prop": 0.3}]
    assert "do not change weights" in weight_decision(one_block, "base", "prop")["decision"]

    tiny_gain = [{"n_eval": 20, "base": 0.100, "prop": 0.105},
                 {"n_eval": 20, "base": 0.100, "prop": 0.110}]
    assert "do not change weights" in weight_decision(tiny_gain, "base", "prop")["decision"]

    inconsistent = [{"n_eval": 20, "base": 0.0, "prop": 0.5},
                    {"n_eval": 20, "base": 0.0, "prop": -0.4},
                    {"n_eval": 20, "base": 0.0, "prop": -0.4}]
    assert "do not change weights" in weight_decision(inconsistent, "base", "prop")["decision"]

    clear = [{"n_eval": 20, "base": 0.0, "prop": 0.3},
             {"n_eval": 20, "base": 0.05, "prop": 0.25},
             {"n_eval": 12, "base": 0.0, "prop": 0.20}]
    d = weight_decision(clear, "base", "prop")
    assert d["decision"] == "proposed beats baseline OOS", d
    assert d["blocks_won"] == 3


def test_fit_component_weights_normalized():
    panel = []
    for i in range(12):
        comps = {}
        excess = {}
        for j, (name, _tk) in enumerate([("XLK", "XLK"), ("XLF", "XLF"), ("XLE", "XLE"),
                                         ("XLV", "XLV"), ("XLI", "XLI")]):
            mom = 5.0 + j * 2 + i % 3
            comps[name] = {"trend_ma50": 10.0 if j % 2 else -10.0,
                           "trend_ma200": 0.0,
                           "momentum_20d": mom, "momentum_60d": mom / 2,
                           "rsi14": 2.0, "macd_hist": 1.0}
            excess[name] = mom * 0.4 - 1.0
        panel.append({"origin": date(2026, 1, 1) + timedelta(days=7 * i),
                      "components": comps, "excess": {20: excess},
                      "ratings": {t: 50.0 + c["momentum_20d"] for t, c in comps.items()},
                      "regime_score": 55.0, "regime_label": "risk_on"})
    w = fit_component_weights(panel, 20)
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert all(0.0 <= v <= 0.40 + 1e-9 for v in w.values())


def test_fit_regime_overlay_insufficient_rows():
    assert fit_regime_overlay([], 20)["beta"] == 0.0


def test_fit_regime_overlay_beta_is_capped():
    rows = []
    for i in range(10):
        excess = {t: 50.0 for t in ("XLK", "XLF", "XLE")}
        rows.append({"origin": date(2026, 1, 1) + timedelta(days=7 * i),
                     "components": {}, "ratings": {}, "excess": {20: excess},
                     "regime_score": 50.0 + i, "regime_label": "risk_on"})
    fit = fit_regime_overlay(rows, 20, beta_cap=0.5)
    assert abs(fit["beta"]) <= 0.5 + 1e-9


# --------------------------------------------------------------------------- #
#  Ablation on panel-shaped rows
# --------------------------------------------------------------------------- #
def test_ablation_uses_panel_row_shape():
    """Ablation must read panel rows (nested ``factors``, ``score``), not a flat dict.

    Regression: feeding raw panel rows to ``ablate_regime_score`` made every input
    fall back to the 50 neutral default, so all six ablations returned a constant
    score -- ``ic_ablated`` was None for every factor and the coerced delta looked
    like "removing any factor fixes the model".
    """
    panel = _synthetic_regime_rows(40)
    abl = evaluate_regime_ablation(panel, [20])["neutral_fill"]
    tech = abl["technical_score"][20]
    flat = abl["risk_appetite_score"][20]  # constant 50 in this synthetic panel

    # the composite IC is defined on the base score
    assert tech["ic_base"] is not None and tech["ic_base"] > 0.3
    # ablating a factor that actually varies must move the IC (not stay at 50)
    assert tech["ic_ablated"] is not None
    assert tech["zero_variance_ablated"] is False
    assert tech["delta_ic"] != 0.0
    # a factor pinned at the neutral 50 is a genuine no-op -> exactly zero delta
    assert flat["delta_ic"] == 0.0
    assert flat["brier_ablated"] == flat["brier_base"]
    # deltas are factor-specific rather than one shared constant
    deltas = {k: v[20]["delta_ic"] for k, v in abl.items()}
    assert len(set(deltas.values())) > 1, deltas


def test_ablation_flags_zero_variance_constant_score():
    """A constant ablated score yields IC 0.0 *and* the zero-variance flag."""
    panel = _synthetic_regime_rows(40)
    for row in panel:  # flat score -> ablation cannot change anything
        row["factors"] = {k: 50.0 for k, _, _ in REGIME_FACTORS}
        row["score"] = 50.0
    abl = evaluate_regime_ablation(panel, [20])["neutral_fill"]
    for key, per_h in abl.items():
        assert per_h[20]["ic_ablated"] == 0.0, key
        assert per_h[20]["delta_ic"] == 0.0, key
        assert per_h[20]["zero_variance_ablated"] is True, key


# --------------------------------------------------------------------------- #
#  Row-level persistence payloads (no DB)
# --------------------------------------------------------------------------- #
def test_regime_attribution_rows_reconcile_to_score():
    panel = _synthetic_regime_rows(12)
    rows = regime_attribution_rows(panel)
    assert len(rows) == 12
    for row in rows:
        total = sum(row[f"c_{s}"] for s in REGIME_FACTOR_COLUMNS.values())
        assert total == pytest.approx(row["score"], abs=1e-3)
        assert sum(row[f"w_{s}"] for s in REGIME_FACTOR_COLUMNS.values()) == pytest.approx(1.0)
        assert row["spy_5d"] is not None and row["dd_5d"] is not None
        assert row["label_5d"] in ("risk_on", "risk_off")


def test_regime_attribution_rows_keep_missing_factor_null():
    rows = regime_attribution_rows([BASE_REGIME_PANEL_ROW_MISSING_NEWS])
    row = rows[0]
    assert row["f_news"] is None
    assert row["c_news"] is None
    # the other factors still reconcile (missing factor contributes nothing)
    total = sum(row[f"c_{s}"] or 0.0 for s in REGIME_FACTOR_COLUMNS.values())
    assert total == pytest.approx(row["score"] - 0.10 * 50.0, abs=1e-3)


def test_sector_attribution_rows_decompose_rating():
    panel = [{
        "origin": date(2026, 1, 1),
        "regime_score": 55.0, "regime_label": "risk_on",
        "ratings": {"XLK": 92.5, "XLE": 100.0},
        "components": {
            "XLK": {"trend_ma50": 10.0, "trend_ma200": 10.0, "momentum_20d": 6.5,
                    "momentum_60d": 6.0, "rsi14": 5.0, "macd_hist": 5.0},
            "XLE": {"trend_ma50": 10.0, "trend_ma200": 10.0, "momentum_20d": 15.0,
                    "momentum_60d": 15.0, "rsi14": 8.0, "macd_hist": 10.0},
        },
        "excess": {5: {"XLK": 1.0, "XLE": -0.5}},
        "spy": {5: 2.0},
    }]
    rows = sector_attribution_rows(panel)
    assert len(rows) == 2
    for row in rows:
        comp_sum = sum(row[c] for c in SECTOR_COMPONENT_COLUMNS.values())
        assert 50.0 + comp_sum == pytest.approx(row["rating_raw"], abs=1e-6)
        assert row["rating"] + (-row["clip_adj"]) == pytest.approx(row["rating_raw"], abs=1e-6)
        assert row["sec_5d"] - row["spy_5d"] == pytest.approx(
            panel[0]["excess"][5][row["ticker"]], abs=1e-6)
    xlk = next(r for r in rows if r["ticker"] == "XLK")
    xle = next(r for r in rows if r["ticker"] == "XLE")
    assert xlk["rank"] == 2 and xle["rank"] == 1     # 100 beats 92.5
    # XLE's raw sum is 118, so the clip pulls it down to the 100 bound: adj = -18
    assert xle["rating_raw"] == pytest.approx(118.0)
    assert xle["clip_adj"] == pytest.approx(-18.0)
    assert xlk["clip_adj"] == pytest.approx(0.0)


def test_attribution_ddl_is_additive_only():
    ddl = attribution_ddl()
    flat = " ".join(ddl["regime"] + ddl["sector"]).upper()
    for forbidden in ("DROP ", "TRUNCATE", "DELETE ", "ALTER COLUMN", "RENAME"):
        assert forbidden not in flat
    assert "CREATE TABLE IF NOT EXISTS" in flat
    assert all("ADD COLUMN IF NOT EXISTS" in s for s in ddl["sector"])
    # every regime factor needs a score, weight and contribution column
    for short in REGIME_FACTOR_COLUMNS.values():
        for prefix in ("f_", "w_", "c_"):
            assert f"{prefix}{short}" in flat.lower()
    # and the CREATE body must be valid: every column line but the last ends with a comma
    body = ddl["regime"][0].split("(", 1)[1].rsplit(")", 1)[0].splitlines()
    body = [line.strip() for line in body if line.strip()]
    for line in body[:-1]:
        assert line.endswith(","), f"missing comma: {line}"


# --------------------------------------------------------------------------- #
#  Self-test entry point
# --------------------------------------------------------------------------- #
def test_self_test_exits_zero():
    assert self_test() == 0


# --------------------------------------------------------------------------- #
#  Clipping diagnostics
# --------------------------------------------------------------------------- #
def test_unclipped_rating_keeps_raw_sum():
    comp = sector_rating_components({
        "ticker": "XLK", "close": 120.0, "ma_50": 110.0, "ma_200": 100.0,
        "rsi_14": 90.0, "macd_hist": 0.004, "return_20d": 0.20, "return_60d": 0.40,
    })["components"]
    raw = unclipped_rating_from_components(comp)
    assert abs(raw - (50.0 + sum(comp.values()))) < 1e-6
    # this input set saturates the production clip, so the unclipped value exceeds 100
    assert raw > 100.0


def test_unclipped_variant_preserves_ordering_when_clipped():
    """Ratings tied at 100 in production keep distinct ranks without the final clip.

    Note the two-layer loss: the final [0, 100] clip is what this variant removes.
    Each component's own budget cap (e.g. momentum_20d at 15 pts) still binds, so a
    row with saturated components stays tied even unclipped -- which is why the
    diagnostic must be read per row, not as a universal fix.
    """
    base = {"trend_ma50": 10.0, "trend_ma200": 10.0,
            "momentum_20d": 15.0, "momentum_60d": 15.0}
    comps = {
        "XLK": dict(base, rsi14=2.0, macd_hist=1.0),   # raw 103 -> clipped to 100
        "XLF": dict(base, rsi14=4.0, macd_hist=3.0),   # raw 107 -> clipped to 100
        "XLE": dict(base, rsi14=6.0, macd_hist=4.0),   # raw 110 -> clipped to 100
    }
    panel = [{"origin": date(2026, 1, 1), "components": comps,
              "ratings": {t: _clamp(50.0 + sum(c.values())) for t, c in comps.items()},
              "excess": {20: {t: 0.0 for t in comps}},
              "regime_score": 55.0, "regime_label": "risk_on"}]
    # production rating saturates -> all three tie at 100
    assert set(panel[0]["ratings"].values()) == {100.0}
    unclipped = _rating_fn_factory("unclipped")(panel[0])
    assert sorted(unclipped.values()) == [103.0, 107.0, 110.0]
    assert sorted(unclipped, key=lambda t: -unclipped[t]) == ["XLE", "XLF", "XLK"]
