"""Tests for the factor composition / weight search (pure logic, no DB).

The important properties:
  * subset enumeration covers every composition exactly once
  * signed weights actually go negative for an anti-predictive factor, and stay capped
  * a perfectly predictive factor earns the weight and wins the search
  * the confirmation blocks are NEVER used for selection (no look-ahead)
  * the recommendation refuses to fire when the confirmation evidence is weak
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from db_builder.factor_optimization import (
    REGIME_KEYS,
    SECTOR_KEYS,
    UST_CHALLENGER_LEGS,
    evaluate_block,
    evaluate_candidate,
    factor_impact,
    recommend,
    regime_score,
    search,
    sector_ratings,
    signed_weights,
    subsets,
    ust_challenger_evaluation,
    ust_curve_signals,
)


# --------------------------------------------------------------------------- #
#  Synthetic panels
# --------------------------------------------------------------------------- #
def _regime_panel(n: int = 100, flip_at: int | None = None) -> list[dict]:
    """Weekly origins; technical/momentum predict, macro is noise.

    ``flip_at`` inverts the factor→return link from that index onward, which is
    what makes the confirmation-window test meaningful.
    """
    rows = []
    for i in range(n):
        tech = 30.0 + (i * 7) % 60
        mom = 30.0 + (i * 11) % 60
        noise = 40.0 + (i * 13) % 20
        signal = (tech - 60.0) * 0.05 + (mom - 60.0) * 0.03
        if flip_at is not None and i >= flip_at:
            signal = -signal
        factors = {
            "technical_score": tech,
            "momentum_score": mom,
            "risk_appetite_score": 50.0,
            "breadth_score": 50.0,
            "macro_score": noise,
            "news_score": 50.0,
        }
        ret = signal * 100.0
        rows.append({
            "origin": date(2025, 1, 6) + timedelta(days=7 * i),
            "score": sum(factors[k] / 6.0 for k in REGIME_KEYS),
            "label": "risk_on" if ret > 0 else "risk_off",
            "phase": "", "confidence": 0.2,
            "factors": factors,
            "fwd": {5: ret, 20: ret, 60: ret},
            "maxdd": {5: -abs(ret) / 2, 20: -abs(ret), 60: -abs(ret)},
            "realized_label": {5: "risk_on" if ret > 0 else "risk_off",
                               20: "risk_on" if ret > 0 else "risk_off",
                               60: "risk_on" if ret > 0 else "risk_off"},
        })
    return rows


def _sector_panel(n: int = 100) -> list[dict]:
    tickers = ["XLK", "XLF", "XLE", "XLV", "XLI"]
    rows = []
    for i in range(n):
        components, ratings, excess = {}, {}, {}
        for j, t in enumerate(tickers):
            mom = 3.0 + j * 2.0 + (i % 4)
            comp = {
                "trend_ma50": 10.0 if j % 2 else -10.0,
                "trend_ma200": 0.0,
                "momentum_20d": mom,
                "momentum_60d": mom / 2,
                "rsi14": 2.0,
                "macd_hist": 1.0,
            }
            components[t] = comp
            ratings[t] = 50.0 + 50.0 * (comp["momentum_20d"] / 15.0)
            excess[t] = (mom - 7.0) * 0.5
        rows.append({
            "origin": date(2025, 1, 6) + timedelta(days=7 * i),
            "regime_score": 55.0, "regime_label": "risk_on",
            "ratings": ratings, "components": components,
            "excess": {5: dict(excess), 20: dict(excess), 60: dict(excess)},
            "spy": {5: 1.0, 20: 2.0, 60: 3.0},
        })
    return rows


# --------------------------------------------------------------------------- #
#  Composition enumeration & weights
# --------------------------------------------------------------------------- #
def test_subsets_cover_every_composition_once():
    got = subsets(["a", "b", "c"], min_size=1)
    assert len(got) == 7                      # 2^3 - 1
    assert len(set(got)) == 7
    assert ("a",) in got and ("a", "b", "c") in got
    # min_size=2 drops singletons only
    assert len(subsets(["a", "b", "c"], min_size=2)) == 4


def test_subsets_regime_and_sector_sizes():
    assert len(subsets(REGIME_KEYS, min_size=2)) == 57   # 2^6 - 1 - 6
    assert len(subsets(SECTOR_KEYS, min_size=2)) == 57


def test_signed_weights_flip_anti_predictive_factor():
    w = signed_weights({"good": 0.30, "bad": -0.30, "flat": 0.0}, shrink=0.0)
    assert w["good"] > 0 > w["bad"]
    assert abs(sum(abs(v) for v in w.values()) - 1.0) < 1e-9


def test_signed_weights_respect_cap_and_shrink():
    w = signed_weights({"a": 0.9, "b": 0.0, "c": 0.0, "d": 0.0}, shrink=0.0, cap=0.5)
    assert max(abs(v) for v in w.values()) <= 0.5 + 1e-12
    shrunk = signed_weights({"a": 0.9, "b": 0.0, "c": 0.0, "d": 0.0}, shrink=0.8)
    assert abs(shrunk["a"]) < abs(w["a"])   # shrinkage pulls toward equal


def test_regime_score_and_sector_ratings_apply_weights():
    row = {"factors": {"technical_score": 80.0, "momentum_score": 40.0}}
    assert regime_score(row, {"technical_score": 0.5, "momentum_score": 0.5}) == 60.0
    assert regime_score(row, {"technical_score": 0.5, "momentum_score": -0.5}) == 20.0

    srow = {"components": {"XLK": {"momentum_20d": 15.0, "trend_ma50": 0.0,
                                   "trend_ma200": 0.0, "momentum_60d": 0.0,
                                   "rsi14": 0.0, "macd_hist": 0.0}}}
    w = {"momentum_20d": 1.0}
    assert sector_ratings(srow, w)["XLK"] == pytest.approx(100.0)
    assert sector_ratings(srow, w, clip=True)["XLK"] == 100.0
    assert sector_ratings(srow, {k: -1.0 for k in w})["XLK"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
#  Block evaluation
# --------------------------------------------------------------------------- #
def test_evaluate_block_equal_weights_on_perfect_signal():
    panel = _regime_panel(30)
    res = evaluate_block(panel, panel, ["technical_score", "momentum_score"], 20, "equal", "regime")
    assert res["ic"] is not None and res["ic"] > 0.9
    assert res["n_eval"] == 30


def test_evaluate_block_returns_none_without_rows():
    res = evaluate_block([], _regime_panel(5), ["technical_score"], 20, "equal", "regime")
    assert res["ic"] is None and res["n_eval"] == 0


# --------------------------------------------------------------------------- #
#  Search integrity
# --------------------------------------------------------------------------- #
def test_search_ranks_and_reports_baselines():
    panel = _regime_panel(120)
    s = search(panel, REGIME_KEYS, 20, "regime", n_blocks=5)
    assert "error" not in s, s
    assert s["n_candidates"] > 100
    assert s["n_confirmation_blocks"] == 2
    assert s["baselines"]["production"]["confirmation_ic"] is not None
    # top candidates are ordered by selection IC, descending
    ics = [c["selection_ic"] for c in s["top"]]
    assert ics == sorted(ics, reverse=True)
    # the predictive factors should be in the winning composition
    assert "technical_score" in s["top"][0]["factors"]


def test_search_does_not_use_confirmation_blocks_for_selection():
    """Relationships flip at 75% of the sample: selection must stay positive and
    confirmation must degrade. If confirmation had leaked into selection, the
    winner would not show that pattern."""
    n = 120
    panel = _regime_panel(n, flip_at=int(n * 0.75))
    s = search(panel, REGIME_KEYS, 20, "regime", n_blocks=5)
    best = s["top"][0]
    assert best["selection_ic"] > 0.2
    assert best["confirmation_ic"] < best["selection_ic"]
    # the flips are genuinely in the held-out part of the timeline
    assert max(best["confirmation_block_ics"]) <= best["selection_ic"] + 1e-9


def test_recommend_refuses_when_confirmation_is_weak():
    n = 120
    panel = _regime_panel(n, flip_at=int(n * 0.75))
    rec = recommend(panel, REGIME_KEYS, 20, "regime", n_blocks=5)
    assert rec["verdict"] == "do not change production"
    assert rec["gates"]["reasons"]


def test_recommend_accepts_a_persistent_signal():
    panel = _regime_panel(120)
    rec = recommend(panel, REGIME_KEYS, 20, "regime", n_blocks=5)
    assert rec["chosen"]["confirmation_ic"] is not None
    assert rec["weights_rounded"]
    # weights are rounded to 5% steps and normalized
    for v in rec["weights_rounded"].values():
        assert abs(v / 0.05 - round(v / 0.05)) < 1e-6
    assert sum(abs(v) for v in rec["weights_rounded"].values()) == pytest.approx(1.0, abs=1e-3)


def test_sector_search_finds_the_predictive_component():
    panel = _sector_panel(120)
    s = search(panel, SECTOR_KEYS, 20, "sector", n_blocks=5)
    assert "error" not in s, s
    best = s["top"][0]
    assert "momentum_20d" in best["factors"]
    assert best["selection_ic"] > 0.5


def test_factor_impact_flags_unstable_signs():
    n = 120
    panel = _regime_panel(n, flip_at=int(n * 0.5))
    imp = factor_impact(panel, REGIME_KEYS, [20], "regime")["horizons"][20]
    tech = imp["factors"]["technical_score"]
    assert tech["stable_sign"] is False
    assert tech["ic_first_half"] * tech["ic_second_half"] < 0


def test_factor_impact_incremental_ic_penalises_harmful_factor():
    panel = _regime_panel(120)
    imp = factor_impact(panel, REGIME_KEYS, [20], "regime")["horizons"][20]
    # constant factors add nothing -> incremental IC ~ 0
    assert abs(imp["factors"]["news_score"]["incremental_ic"]) < 1e-9
    # the predictive factors carry positive incremental value
    assert imp["factors"]["technical_score"]["incremental_ic"] > 0


# --------------------------------------------------------------------------- #
#  UST curve challenger (2/10/30y yields + gold)
# --------------------------------------------------------------------------- #
def _curve(n: int = 120, gold_scale: float = 1.0, with_gold: bool = True):
    """Deterministic yield/gold panel with *varying* 20-day changes."""
    dates = [date(2025, 1, 6) + timedelta(days=i) for i in range(n)]

    def yld(base):
        return {d: base + 0.01 * i + 0.05 * math.sin(i / 3.0) for i, d in enumerate(dates)}

    curve = {"y2": yld(4.0), "y10": yld(4.2), "y30": yld(4.4)}
    if with_gold:
        curve["gold"] = {d: 2400.0 + gold_scale * (0.5 * i + 3.0 * math.sin(i / 2.0))
                         for i, d in enumerate(dates)}
    return curve, dates


def test_ust_curve_signals_uses_change_not_level():
    """A constant shift in every series must leave the composite untouched."""
    curve, dates = _curve()
    shifted = {k: {d: v + 1.5 for d, v in s.items()} for k, s in curve.items()}
    base = [r["composite"] for r in ust_curve_signals(curve, dates)]
    moved = [r["composite"] for r in ust_curve_signals(shifted, dates)]
    for a, b in zip(base, moved):
        if a is None:
            assert b is None
        else:
            assert b == pytest.approx(a, abs=1e-9)


def test_ust_curve_signals_are_scale_invariant_across_legs():
    """Gold is priced in dollars and yields in percent: normalisation must make the
    composite independent of the leg's units, or gold silently dominates."""
    curve, dates = _curve()
    scaled, _ = _curve(gold_scale=10.0)
    base = [r["composite"] for r in ust_curve_signals(curve, dates)]
    wide = [r["composite"] for r in ust_curve_signals(scaled, dates)]
    pairs = [(a, b) for a, b in zip(base, wide) if a is not None]
    assert len(pairs) > 40
    for a, b in pairs:
        assert b == pytest.approx(a, abs=1e-9)


def test_ust_curve_signals_skip_missing_legs():
    curve, dates = _curve(with_gold=False)
    rows = ust_curve_signals(curve, dates)
    usable = [r for r in rows if r["composite"] is not None]
    assert usable
    for r in usable:
        assert r["legs"]["gold"] is None
        present = [v for k, v in r["legs"].items() if v is not None]
        assert len(present) == 3
        assert r["composite"] == pytest.approx(sum(present) / 3.0)


def test_ust_curve_signals_need_enough_history_to_normalise():
    curve, dates = _curve(n=45)
    rows = ust_curve_signals(curve, dates, lag=20, norm_window=60)
    # 20 changes are needed before the trailing sd is defined
    first = next(i for i, r in enumerate(rows) if r["composite"] is not None)
    assert first == 20 + 19, first
    assert all(r["composite"] is None for r in rows[:first])


def test_ust_challenger_evaluation_reports_ics_and_corr():
    curve, dates = _curve(n=140)
    sig = {r["date"]: r["composite"] for r in ust_curve_signals(curve, dates)}
    usable = [d for d in dates if sig.get(d) is not None]
    # forward returns constructed to follow the composite
    fwd = {d: {20: sig[d] * 10.0} for d in usable}
    prod = {d: sig[d] for d in usable}
    ev = ust_challenger_evaluation(curve, fwd, prod)
    h20 = ev["horizons"][20]
    assert h20["n"] == len(usable)
    assert h20["ic"] > 0.9
    assert h20["ic_first_half"] * h20["ic_second_half"] > 0
    # production scores equal the signal -> correlation is 1
    assert ev["corr_with_production_score"] == pytest.approx(1.0, abs=1e-6)
    assert ev["n_corr"] == len(usable)


def test_ust_challenger_evaluation_handles_thin_data():
    curve, dates = _curve(n=25)
    ev = ust_challenger_evaluation(curve, {}, None)
    for h in (5, 20, 60):
        assert ev["horizons"][h]["ic"] is None
    assert "corr_with_production_score" not in ev
