"""AEGIS Factor Attribution + Model Validation for market_regime_v2 and rate_sector.

Answers three questions for both production models:

  1. **Attribution** -- which factors drive the score, by how much, and what
     changed since the previous observation.
  2. **Predictive power** -- Rank IC / hit rate / downside behaviour of every
     factor against forward 5D/20D/60D outcomes, plus one-at-a-time ablation.
  3. **Optimization** -- chronological walk-forward comparison of the production
     model against equal-weight, shrinkage and regularized challengers, where
     every challenger weight is fitted on TRAIN origins only.

Reuses the existing AEGIS backtest infrastructure (``db_builder.aegis_backtest``):
as-of-date fetchers, ``reconstruct_regime_at``, ``rate_sector``, ``spearman_rho``,
``_rank``/``_std`` and the ``FORECAST_HORIZONS`` contract. No scoring pipeline is
duplicated: regime attribution is *arithmetic on the engine's own outputs*, and
sector attribution decomposes ``rate_sector`` term-by-term (guarded by an
exact-equivalence regression test).

Production scores and scheduled reports are NOT touched. Everything here is
read-only against PostgreSQL except ``store_attribution`` (opt-in via ``--store``).

Modes:
    python -m db_builder.factor_attribution regime --start YYYY-MM-DD --end YYYY-MM-DD
    python -m db_builder.factor_attribution sector --start YYYY-MM-DD --end YYYY-MM-DD
    python -m db_builder.factor_attribution all    --start YYYY-MM-DD --end YYYY-MM-DD
    python -m db_builder.factor_attribution selftest
    python -m db_builder.factor_attribution ddl            # print the DDL, write nothing
    python -m db_builder.factor_attribution store --start ... --end ... [--dry-run]
    python -m db_builder.factor_attribution weights [--horizon N]

``store`` persists the row-level attribution dataset (per regime date; per
date+sector into the existing ``aegis_sector_backtest`` table). ``weights`` reads
that dataset back and proposes weight vectors validated walk-forward.

Every analysis run also writes a timestamped .md + .json copy into ``reports/`` (the
existing analytics folder, gitignored) unless ``--no-save`` is passed; use
``--report-dir DIR`` to redirect it.

Key statistical safeguards:
  * Point-in-time: every input row is filtered ``date <= origin`` (via the
    existing as-of fetchers); forward data is used ONLY as the outcome.
  * Overlapping horizons: 20D/60D headline metrics are computed on a
    non-overlapping origin subsample (``non_overlapping``); the all-origin value
    is reported alongside so the difference in sample size is explicit.
  * No in-sample weight fitting: challenger weights are fitted on expanding
    TRAIN blocks and scored only on later EVAL blocks.

Attribution identity (regime):
    bullish_score = 0.25*technical + 0.20*momentum + 0.20*risk_appetite
                  + 0.15*breadth + 0.10*macro + 0.10*news
    => contribution_i = w_i * score_i, and sum(contribution_i) == bullish_score.

Attribution identity (sector):
    rate_sector = 50 + MA50 term + MA200 term + ret20 term + ret60 term
                     + RSI term + MACD term, clipped to [0, 100]
    => sum(components) + clip_adjustment == rate_sector(row).
"""
from __future__ import annotations

import json
import math
import sys
from bisect import bisect_left
from datetime import date, datetime, timedelta
from typing import Any, Optional, Sequence

from db_builder.aegis_backtest import (
    FORECAST_HORIZONS,
    GICS_SECTOR_ETFS,
    SPX_TICKER,
    TRANSACTION_COST_BPS,
    _forward_return,
    _std,
    spearman_rho,
)

ATTRIBUTION_TABLE = "public.aegis_factor_attribution"

# (key, weight, label) -- mirrors build_market_regime_v2's assembly exactly.
REGIME_FACTORS: tuple[tuple[str, float, str], ...] = (
    ("technical_score", 0.25, "Technical (MA20/50/200 + RSI + MACD per key ETF)"),
    ("momentum_score", 0.20, "Momentum (0.3*5D + 0.4*20D + 0.3*60D)"),
    ("risk_appetite_score", 0.20, "Risk appetite (VIX + IWM/QQQ/SMH vs SPY + crypto + gold)"),
    ("breadth_score", 0.15, "Breadth (%>MA50, %>MA200, %>0 on 20D)"),
    ("macro_score", 0.10, "Macro (index futures + 10Y/30Y yields)"),
    ("news_score", 0.10, "News (risk-on opportunity vs risk-off risk)"),
)

# (key, max_points, label) -- mirrors rate_sector's point budget exactly.
SECTOR_COMPONENTS: tuple[tuple[str, float, str], ...] = (
    ("trend_ma50", 10.0, "Close vs MA50"),
    ("trend_ma200", 10.0, "Close vs MA200"),
    ("momentum_20d", 15.0, "20D return (scaled, capped)"),
    ("momentum_60d", 15.0, "60D return (scaled, capped)"),
    ("rsi14", 10.0, "RSI14 vs 50"),
    ("macd_hist", 10.0, "MACD histogram (scaled, capped)"),
)

# Realised regime labels that count as a risk-on outcome.
RISK_ON_LABELS = frozenset({"risk_on", "strong_risk_on"})
RISK_OFF_LABELS = frozenset({"risk_off", "strong_risk_off"})
SECTOR_RATING_BASE = 50.0
MA50_POINTS = 10.0
MA200_POINTS = 10.0
MOM20_POINTS = 15.0
MOM60_POINTS = 15.0
RSI_POINTS = 10.0
MACD_POINTS = 10.0
MOM20_SCALE = 0.10
MOM60_SCALE = 0.20
MACD_SCALE = 0.005


# --------------------------------------------------------------------------- #
#  Numeric helpers (stdlib only -- these must run without pandas/scipy)
# --------------------------------------------------------------------------- #
def _num(value: Any, default: float = 0.0) -> float:
    """Coerce to float; NaN/None/garbage -> default. Mirrors rate_sector's _f."""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return default if f != f else f  # NaN check without importing math.isnan


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _pct(xs: Sequence[float], q: float) -> float:
    """Simple linear-interpolated quantile (q in [0,1])."""
    if not xs:
        return 0.0
    ordered = sorted(xs)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def _normal_two_sided_p(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2.0))


def ic_t_stat(rank_ic: Optional[float], n: int) -> Optional[float]:
    """Approximate t-statistic for a single pooled rank IC over ``n`` observations."""
    if rank_ic is None or n is None or n < 3 or abs(rank_ic) >= 1.0:
        return None
    return _round(rank_ic * math.sqrt(n - 2) / math.sqrt(1.0 - rank_ic ** 2), 4)


def ic_p_value(rank_ic: Optional[float], n: int) -> Optional[float]:
    t = ic_t_stat(rank_ic, n)
    return _round(_normal_two_sided_p(t), 4) if t is not None else None


def _round(value: float, ndigits: int = 6) -> float:
    return round(float(value), ndigits)


# --------------------------------------------------------------------------- #
#  Regime attribution  (pure)
# --------------------------------------------------------------------------- #
def regime_factor_weights() -> dict[str, float]:
    return {key: weight for key, weight, _ in REGIME_FACTORS}


def attribute_regime(regime: dict) -> dict:
    """Exact decomposition of ``bullish_score`` into weighted factor contributions.

    Returns ``factors`` (per-factor weight/score/contribution), ``residual``
    (rounding difference between the engine's score and the reconstructed sum,
    expected < 1e-4), and ``saturated`` factor keys whose sub-score hit a 0/100
    bound (their marginal sensitivity to inputs is zero there).
    """
    score = _num(regime.get("bullish_score"), 50.0)
    factors = []
    total = 0.0
    saturated = []
    missing = []
    for key, weight, label in REGIME_FACTORS:
        raw = regime.get(key)
        if raw is None:
            missing.append(key)
        value = _num(raw, 50.0)
        contribution = weight * value
        total += contribution
        if value <= 0.0 or value >= 100.0:
            saturated.append(key)
        factors.append({
            "factor": key,
            "label": label,
            "weight": weight,
            "score": _round(value, 4),
            "contribution": _round(contribution, 4),
            "excess_vs_neutral": _round(weight * (value - 50.0), 4),
            "share_of_score": _round(contribution / score * 100, 4) if score else None,
        })
    excess = score - 50.0
    for row in factors:
        row["share_of_excess"] = (
            _round(row["excess_vs_neutral"] / excess * 100, 4)
            if abs(excess) > 1e-9 else None
        )
    ranked = sorted(factors, key=lambda r: r["excess_vs_neutral"], reverse=True)
    return {
        "model": "market_regime_v2",
        "run_time": str(regime.get("run_time", "")),
        "score": _round(score, 4),
        "regime_label": regime.get("market_regime", ""),
        "phase": regime.get("market_phase", ""),
        "confidence": _num(regime.get("confidence"), 0.0),
        "neutral_baseline": 50.0,
        "factors": factors,
        "sum_contributions": _round(total, 6),
        "residual": _round(score - total, 6),
        "top_positive": ranked[0]["factor"] if ranked else None,
        "top_negative": ranked[-1]["factor"] if ranked else None,
        "saturated_factors": saturated,
        "missing_factors": missing,
    }


def regime_delta(prev: dict, curr: dict) -> dict:
    """Factor contribution change between two observations.

    ``sum(delta_contribution)`` reconciles to ``curr_score - prev_score`` up to
    the two engine rounding residuals.
    """
    prev_map = {f["factor"]: f for f in prev.get("factors", [])}
    rows = []
    total = 0.0
    for f in curr.get("factors", []):
        key = f["factor"]
        before = prev_map.get(key)
        if before is None:
            continue
        d = f["contribution"] - before["contribution"]
        d_score = f["score"] - before["score"]
        total += d
        rows.append({
            "factor": key,
            "prev_score": before["score"],
            "curr_score": f["score"],
            "delta_score": _round(d_score, 4),
            "prev_contribution": before["contribution"],
            "curr_contribution": f["contribution"],
            "delta_contribution": _round(d, 4),
            "weight": f["weight"],
        })
    rows.sort(key=lambda r: r["delta_contribution"])
    return {
        "prev_time": prev.get("run_time", ""),
        "curr_time": curr.get("run_time", ""),
        "prev_score": prev.get("score", 50.0),
        "curr_score": curr.get("score", 50.0),
        "delta_score": _round(curr.get("score", 50.0) - prev.get("score", 50.0), 4),
        "delta_contributions": rows,
        "sum_delta_contributions": _round(total, 6),
        "largest_negative": rows[0]["factor"] if rows else None,
        "largest_positive": rows[-1]["factor"] if rows else None,
    }


def regime_weight_sensitivity(regime: dict, shock: float = 0.5) -> list[dict]:
    """Re-score with one weight shocked by +/- ``shock`` (others renormalized).

    Two effects are reported per factor:
      * ``direct``  -- d(score)/d(w_i) = score_i  (no renormalization)
      * ``renormalized`` -- realistic case where the freed weight is redistributed
        proportionally across the other five factors.
    """
    base = _num(regime.get("bullish_score"), 50.0)
    values = {key: _num(regime.get(key), 50.0) for key, _, _ in REGIME_FACTORS}
    base_weights = regime_factor_weights()
    out = []
    for key, weight, _ in REGIME_FACTORS:
        others = sum(w for k, w in base_weights.items() if k != key)
        for sign, tag in ((1.0, "up"), (-1.0, "down")):
            new_w = weight * (1.0 + sign * shock)
            new_w = _clip(new_w, 0.0, 1.0)
            freed = weight - new_w
            scale = 1.0 if abs(others) < 1e-12 else (others + freed) / others
            score = new_w * values[key] + sum(
                base_weights[k] * scale * values[k] for k in base_weights if k != key
            )
            out.append({
                "factor": key,
                "direction": tag,
                "weight_from": weight,
                "weight_to": _round(new_w, 6),
                "score_from": _round(base, 4),
                "score_to": _round(score, 4),
                "delta_score": _round(score - base, 4),
                "direct_effect": _round(values[key] * (new_w - weight), 4),
            })
    return out


def ablate_regime_score(regime: dict, factor: str, policy: str = "neutral_fill") -> dict:
    """Re-score with one factor removed.

    ``neutral_fill``   : factor set to its 50 neutral, weight retained -> the
                         score delta IS the factor's excess contribution.
    ``renormalize``    : factor dropped and its weight redistributed to the rest
                         -> "what if the model had never included this factor".
    """
    if factor not in regime_factor_weights():
        raise ValueError(f"unknown regime factor: {factor}")
    base = _num(regime.get("bullish_score"), 50.0)
    weights = regime_factor_weights()
    values = {key: _num(regime.get(key), 50.0) for key, _, _ in REGIME_FACTORS}
    if policy == "neutral_fill":
        score = base - weights[factor] * (values[factor] - 50.0)
        kept = dict(weights)
    elif policy == "renormalize":
        kept = {k: w for k, w in weights.items() if k != factor}
        scale = 1.0 / sum(kept.values())
        kept = {k: w * scale for k, w in kept.items()}
        score = sum(kept[k] * values[k] for k in kept)
    else:
        raise ValueError(f"unknown ablation policy: {policy}")
    return {
        "removed_factor": factor,
        "policy": policy,
        "original_score": _round(base, 4),
        "ablated_score": _round(score, 4),
        "delta_score": _round(score - base, 4),
        "weights_used": {k: _round(v, 6) for k, v in kept.items()},
    }


def equal_weight_regime(regime: dict) -> float:
    """Mean of the six factor scores (same 0-100 scale as bullish_score)."""
    return _round(_mean([_num(regime.get(k), 50.0) for k, _, _ in REGIME_FACTORS]), 4)


def shrinkage_regime(regime: dict, lam: float = 0.5) -> float:
    """Convex blend of the production score and the equal-weight score."""
    base = _num(regime.get("bullish_score"), 50.0)
    return _round(lam * equal_weight_regime(regime) + (1.0 - lam) * base, 4)


# --------------------------------------------------------------------------- #
#  Sector attribution  (pure) -- exact term-by-term decomposition of rate_sector
# --------------------------------------------------------------------------- #
def sector_rating_components(row: dict) -> dict:
    """Decompose ``rate_sector(row)`` into its additive terms.

    Mirrors ``db_builder.aegis_backtest.rate_sector`` exactly. The final score is
    clipped to [0, 100]; any clipping (and 4-dp rounding) is isolated in
    ``clip_adjustment`` so the components always sum to the returned score.
    Guarded by tests/test_factor_attribution.py::test_components_match_rate_sector.
    """
    close = _num(row.get("close"))
    ma50 = _num(row.get("ma_50"))
    ma200 = _num(row.get("ma_200"))
    rsi14 = _num(row.get("rsi_14"))
    macd_hist = _num(row.get("macd_hist"))
    ret20 = _num(row.get("return_20d"))
    ret60 = _num(row.get("return_60d"))

    trend_ma50 = MA50_POINTS if close > ma50 > 0 else -MA50_POINTS if close < ma50 > 0 else 0.0
    trend_ma200 = MA200_POINTS if close > ma200 > 0 else -MA200_POINTS if close < ma200 > 0 else 0.0
    momentum_20d = MOM20_POINTS * _clip(ret20 / MOM20_SCALE)
    momentum_60d = MOM60_POINTS * _clip(ret60 / MOM60_SCALE)
    rsi14_pts = RSI_POINTS * (rsi14 - 50.0) / 50.0
    macd_pts = MACD_POINTS * _clip(macd_hist / MACD_SCALE)

    raw = (SECTOR_RATING_BASE + trend_ma50 + trend_ma200 + momentum_20d
           + momentum_60d + rsi14_pts + macd_pts)
    final = round(_clamp(raw), 4)
    components = {
        "trend_ma50": trend_ma50,
        "trend_ma200": trend_ma200,
        "momentum_20d": _round(momentum_20d, 6),
        "momentum_60d": _round(momentum_60d, 6),
        "rsi14": _round(rsi14_pts, 6),
        "macd_hist": _round(macd_pts, 6),
    }
    return {
        "rating": final,
        "base": SECTOR_RATING_BASE,
        "components": components,
        "sum_components": _round(SECTOR_RATING_BASE + sum(components.values()), 6),
        "clip_adjustment": _round(final - raw, 6),
        "clipped": final != _round(raw, 4),
    }


def sector_rank(ratings: dict[str, float]) -> dict[str, int]:
    """1 = strongest rating. Ties share the lower (better) rank deterministically."""
    ordered = sorted(ratings.items(), key=lambda kv: (-kv[1], kv[0]))
    return {ticker: i + 1 for i, (ticker, _) in enumerate(ordered)}


def attribute_sector_row(row: dict, ratings: dict[str, float] | None = None) -> dict:
    """Attribution for one sector ETF row, plus its rank if the peer set is given."""
    comp = sector_rating_components(row)
    out = {
        "ticker": row.get("ticker", ""),
        "rating": comp["rating"],
        "components": comp["components"],
        "clip_adjustment": comp["clip_adjustment"],
        "clipped": comp["clipped"],
        "sum_components": comp["sum_components"],
    }
    if ratings is not None and row.get("ticker") in ratings:
        ranks = sector_rank(ratings)
        out["rank"] = ranks[row["ticker"]]
        out["n_peers"] = len(ratings)
    return out


def neutralized_rating(row: dict, component: str) -> float:
    """Rating with one component set to zero (its neutral contribution)."""
    comp = sector_rating_components(row)
    raw = comp["base"] + sum(
        v for k, v in comp["components"].items() if k != component
    )
    return round(_clamp(raw), 4)


def equal_weight_from_components(comp: dict) -> float:
    """50 + mean of budget-normalized component deviations (equal factor weight)."""
    budgets = {k: b for k, b, _ in SECTOR_COMPONENTS}
    zs = [comp[k] / budgets[k] for k in budgets]
    return _round(_clamp(SECTOR_RATING_BASE + 50.0 * _mean(zs)), 4)


def momentum_flip_from_components(comp: dict) -> float:
    """Mean-reversion challenger: invert both momentum terms and the RSI term."""
    raw = (SECTOR_RATING_BASE
           - comp["momentum_20d"] - comp["momentum_60d"] - comp["rsi14"]
           + comp["trend_ma50"] + comp["trend_ma200"] + comp["macd_hist"])
    return _round(_clamp(raw), 4)


def equal_weight_sector(row: dict) -> float:
    """Equal-weight rating for one row (see equal_weight_from_components)."""
    return equal_weight_from_components(sector_rating_components(row)["components"])


def momentum_meanreversion_variant(row: dict) -> float:
    """Mean-reversion rating for one row (see momentum_flip_from_components)."""
    return momentum_flip_from_components(sector_rating_components(row)["components"])


def unclipped_rating_from_components(comp: dict) -> float:
    """Rating without the [0, 100] clip -- diagnostics only.

    The production rating saturates at the bounds, so sectors that are all "very
    strong" collapse into a tie at 100 and lose their relative ordering. This
    variant keeps the raw sum so the ranking cost of clipping can be measured.
    """
    return _round(SECTOR_RATING_BASE + sum(comp.values()), 4)


def unclipped_sector(row: dict) -> float:
    return unclipped_rating_from_components(sector_rating_components(row)["components"])


# --------------------------------------------------------------------------- #
#  Validation metrics  (pure)
# --------------------------------------------------------------------------- #
def ic_stats(ics: Sequence[float]) -> dict:
    """Mean IC, dispersion, information ratio, t-stat and sign consistency."""
    vals = [v for v in ics if v is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean_ic": None, "ic_std": None, "ic_ir": None,
                "t_stat": None, "p_value": None, "pct_positive": None}
    mean = _mean(vals)
    sd = _std(vals)
    # Guard float dust: identical inputs give sd ~1e-17, which would explode the
    # t-statistic into ~1e16 and look like overwhelming significance.
    if abs(sd) < 1e-12:
        sd = 0.0
    ir = mean / sd if sd else None
    t = mean / (sd / math.sqrt(n)) if sd else None
    return {
        "n": n,
        "mean_ic": _round(mean, 6),
        "ic_std": _round(sd, 6),
        "ic_ir": _round(ir, 6) if ir is not None else None,
        "t_stat": _round(t, 4) if t is not None else None,
        "p_value": _round(_normal_two_sided_p(t), 4) if t is not None else None,
        "pct_positive": _round(sum(1 for v in vals if v > 0) / n, 4),
    }


def reliability_bins(values: Sequence[float], outcomes: Sequence[float],
                     n_bins: int = 5) -> list[dict]:
    """Equal-width binning of a 0-100 score against a binary outcome."""
    pairs = [(v, o) for v, o in zip(values, outcomes) if v is not None and o is not None]
    if not pairs:
        return []
    lo = min(v for v, _ in pairs)
    hi = max(v for v, _ in pairs)
    if hi == lo:
        mean_o = _mean([o for _, o in pairs])
        return [{"bin": 0, "low": _round(lo, 4), "high": _round(hi, 4),
                 "mean_score": _round(lo, 4), "outcome_rate": _round(mean_o, 4),
                 "count": len(pairs)}]
    width = (hi - lo) / n_bins
    bins = []
    for i in range(n_bins):
        lo_e = lo + width * i
        hi_e = lo + width * (i + 1)
        bucket = [(v, o) for v, o in pairs
                  if (lo_e <= v < hi_e) or (i == n_bins - 1 and v == hi_e)]
        if not bucket:
            continue
        bins.append({
            "bin": i,
            "low": _round(lo_e, 4),
            "high": _round(hi_e, 4),
            "mean_score": _round(_mean([v for v, _ in bucket]), 4),
            "outcome_rate": _round(_mean([o for _, o in bucket]), 4),
            "count": len(bucket),
        })
    return bins


def brier_score(values: Sequence[float], outcomes: Sequence[float],
                lo: float = 0.05, hi: float = 0.95) -> Optional[float]:
    """Brier score treating score/100 as a probability, clipped to [lo, hi]."""
    pairs = [(v, o) for v, o in zip(values, outcomes) if v is not None and o is not None]
    if not pairs:
        return None
    return _round(_mean([(_clip(v / 100.0, lo, hi) - o) ** 2 for v, o in pairs]), 6)


def downside_profile(values: Sequence[float], returns: Sequence[float],
                     weak_below: float = 40.0, strong_above: float = 60.0) -> dict:
    """Conditional forward outcomes in weak vs strong score buckets."""
    pairs = [(v, r) for v, r in zip(values, returns) if v is not None and r is not None]
    weak = [r for v, r in pairs if v < weak_below]
    strong = [r for v, r in pairs if v > strong_above]
    neg = [r for _, r in pairs if r < 0]
    return {
        "n": len(pairs),
        "n_weak": len(weak),
        "n_strong": len(strong),
        "mean_return_weak": _round(_mean(weak), 4) if weak else None,
        "mean_return_strong": _round(_mean(strong), 4) if strong else None,
        "neg_hit_rate_weak": _round(sum(1 for r in weak if r < 0) / len(weak), 4) if weak else None,
        "pos_hit_rate_strong": _round(sum(1 for r in strong if r > 0) / len(strong), 4) if strong else None,
        "up_rate_weak": _round(sum(1 for r in weak if r > 0) / len(weak), 4) if weak else None,
        # Discrimination: P(up | strong) - P(up | weak). Perfect ranking -> 1.0,
        # no information -> 0.0. Using down-hit in the weak bucket instead would
        # collapse to 0 for a perfect model (both buckets all-correct).
        "separation": (
            _round((sum(1 for r in strong if r > 0) / len(strong))
                   - (sum(1 for r in weak if r > 0) / len(weak)), 4)
            if weak and strong else None
        ),
        "downside_semideviation": _round(
            math.sqrt(_mean([(r - _mean([x for _, x in pairs])) ** 2 for r in neg])), 4
        ) if neg else None,
        "worst_return": _round(min((r for _, r in pairs), default=0.0), 4) if pairs else None,
        "p05_return": _round(_pct([r for _, r in pairs], 0.05), 4) if pairs else None,
        "mean_return_all": _round(_mean([r for _, r in pairs]), 4) if pairs else None,
    }


def sign_hit_rate(values: Sequence[float], returns: Sequence[float],
                  neutral: float = 50.0) -> Optional[float]:
    """Share of observations where the score's side of neutral matches the sign."""
    pairs = [(v, r) for v, r in zip(values, returns) if v is not None and r is not None]
    if not pairs:
        return None
    return _round(sum(1 for v, r in pairs if (v > neutral) == (r > 0)) / len(pairs), 6)


def non_overlapping(origins: Sequence[date], horizon: int) -> list[date]:
    """Greedy subsample spaced at least ``horizon`` calendar days apart.

    Forward windows of length ``horizon`` then do not share outcome bars, so the
    reported IC is not inflated by overlap. ``horizon`` is in TRADING days, so
    the calendar spacing is scaled by 7/5 and rounded up.
    """
    if not origins:
        return []
    gap = max(1, math.ceil(horizon * 7 / 5))
    ordered = sorted(origins)
    keep = [ordered[0]]
    for d in ordered[1:]:
        if (d - keep[-1]).days >= gap:
            keep.append(d)
    return keep


def purge_before(origins: Sequence[date], first_eval: Optional[date],
                 horizon: int) -> list[date]:
    """Drop train origins whose forward window reaches into the eval period.

    Without this, the last training rows share outcome days with the first eval
    rows and the "out-of-sample" comparison leaks. ``horizon`` is in trading days,
    converted to calendar days the same way ``non_overlapping`` does.
    """
    if first_eval is None:
        return list(origins)
    gap = math.ceil(horizon * 7 / 5)
    return [d for d in origins if (d + timedelta(days=gap)) <= first_eval]


def walk_forward_blocks(origins: Sequence[date], n_blocks: int = 4,
                        min_train: int = 20) -> list[dict]:
    """Chronological expanding-window blocks: train strictly precedes eval."""
    ordered = sorted(origins)
    n = len(ordered)
    if n_blocks < 2 or n < min_train + 2:
        return []
    size = n // n_blocks
    blocks = []
    for k in range(n_blocks):
        start = k * size
        end = n if k == n_blocks - 1 else (k + 1) * size
        eval_slice = ordered[start:end]
        train_slice = ordered[:start]
        if len(train_slice) < min_train or not eval_slice:
            continue
        blocks.append({
            "block": len(blocks),
            "train": train_slice,
            "eval": eval_slice,
            "train_n": len(train_slice),
            "eval_n": len(eval_slice),
        })
    return blocks


def normalize_capped(raw: dict[str, float], cap: float = 0.40,
                     floor: float = 0.05) -> dict[str, float]:
    """Normalize to sum 1 while keeping every weight inside [floor, cap].

    Clipping *before* renormalizing does not hold: a clipped vector renormalized
    afterwards can push a single weight past the cap (observed at 0.49). This does
    the two operations together, iteratively, so the returned vector is feasible.
    """
    keys = list(raw)
    if not keys:
        return {}
    # A 2-factor subset cannot satisfy a 0.40 cap (2 x 0.40 < 1), so relax the cap
    # to the smallest feasible value instead of failing: 1/n is then binding.
    if cap * len(keys) < 1.0 - 1e-12:
        cap = 1.0 / len(keys)
    if floor * len(keys) > 1.0 + 1e-12:
        raise ValueError(f"floor {floor} is infeasible for {len(keys)} weights")
    w = {k: max(floor, float(raw[k])) for k in keys}
    for _ in range(100):
        s = sum(w.values())
        if s <= 0:
            return {k: 1.0 / len(keys) for k in keys}
        w = {k: w[k] / s for k in keys}
        over = [k for k in keys if w[k] > cap + 1e-12]
        if not over:
            break
        for k in over:
            w[k] = cap
        free = [k for k in keys if k not in over]
        if not free:
            break
        remaining = 1.0 - cap * len(over)
        base = sum(w[k] for k in free)
        if base <= 0:
            for k in free:
                w[k] = remaining / len(free)
        else:
            for k in free:
                w[k] = w[k] / base * remaining
    # undo float drift on the largest weight so the vector sums to exactly 1
    top = max(w, key=lambda k: w[k])
    w[top] = 1.0 - sum(v for k, v in w.items() if k != top)
    return w


def fit_ic_weights(rows: Sequence[dict], factor_keys: Sequence[str],
                   horizon: int, floor: float = 0.05, cap: float = 0.40,
                   shrink: float = 0.5) -> dict[str, float]:
    """Regularized weights from TRAIN rows only: IC-proportional, floored, capped.

    Non-negative IC only (a factor that anti-predicts on train is floored rather
    than inverted, to avoid overfitting a sign on a small sample), then shrunk
    toward equal weight by ``shrink`` and renormalized to sum to 1.
    """
    k = len(factor_keys)
    if k == 0:
        return {}
    equal = 1.0 / k
    ics = {}
    for key in factor_keys:
        xs = [r["factors"].get(key) for r in rows if r["fwd"].get(horizon) is not None]
        ys = [r["fwd"][horizon] for r in rows if r["fwd"].get(horizon) is not None]
        ics[key] = max(0.0, spearman_rho(xs, ys)) if len(xs) >= 3 else 0.0
    total = sum(ics.values())
    raw = {key: (ics[key] / total if total > 0 else equal) for key in factor_keys}
    blended = {key: shrink * equal + (1 - shrink) * raw[key] for key in factor_keys}
    return normalize_capped(blended, cap=cap, floor=floor)


def fit_component_weights(rows: Sequence[dict], horizon: int,
                          floor: float = 0.05, cap: float = 0.40,
                          shrink: float = 0.5) -> dict[str, float]:
    """Sector analogue: pooled cross-sectional IC per component on TRAIN dates."""
    keys = [k for k, _, _ in SECTOR_COMPONENTS]
    ics = {}
    for key in keys:
        xs, ys = [], []
        for row in rows:
            excess = row["excess"].get(horizon)
            if not excess:
                continue
            for ticker, comp in row["components"].items():
                if ticker in excess:
                    xs.append(comp[key])
                    ys.append(excess[ticker])
        ics[key] = max(0.0, spearman_rho(xs, ys)) if len(xs) >= 5 else 0.0
    total = sum(ics.values())
    equal = 1.0 / len(keys)
    raw = {k: (ics[k] / total if total > 0 else equal) for k in keys}
    blended = {k: shrink * equal + (1 - shrink) * raw[k] for k in keys}
    return normalize_capped(blended, cap=cap, floor=floor)


def fit_regime_overlay(rows: Sequence[dict], horizon: int,
                       beta_cap: float = 1.0) -> dict:
    """OLS of sector excess return on the regime deviation, fitted on TRAIN only.

    Returns ``beta`` such that ``score + beta*(regime_score-50)`` is the overlay,
    plus the in-train slope quality. Beta is capped to avoid a single-block fit
    dominating the ranking.
    """
    xs, ys = [], []
    for row in rows:
        excess = row["excess"].get(horizon)
        if not excess:
            continue
        xs.append(_num(row.get("regime_score"), 50.0) - 50.0)
        ys.append(_mean(list(excess.values())))
    n = len(xs)
    if n < 3:
        return {"beta": 0.0, "n_train": n, "note": "insufficient train rows"}
    mx, my = _mean(xs), _mean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 0:
        return {"beta": 0.0, "n_train": n, "note": "no regime variance in train"}
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    capped = _clip(beta, -beta_cap, beta_cap)
    return {"beta": _round(capped, 6), "beta_raw": _round(beta, 6), "n_train": n}


# --------------------------------------------------------------------------- #
#  DB panels  (lazy pandas/sqlalchemy imports; read-only)
# --------------------------------------------------------------------------- #
def _connect(engine=None):
    if engine is not None:
        return engine
    from db_builder.config import local_engine
    return local_engine()


def fetch_close_matrix(engine, tickers: Sequence[str], start: date,
                       end: date) -> dict[str, dict]:
    """One bulk read of daily closes -> {ticker: {date: close}} (ordered).

    Replaces per-origin forward-return queries: the same ``_forward_return``
    semantics are reproduced locally by positional indexing from the first bar
    on/after the origin.
    """
    import pandas as pd
    from sqlalchemy import text
    sql = text("""
        SELECT ticker, date, close
        FROM public.us_equities
        WHERE ticker = ANY(:tickers) AND date BETWEEN :start AND :end
        ORDER BY ticker, date
    """)
    df = pd.read_sql(sql, engine, params={"tickers": list(tickers), "start": start, "end": end})
    out: dict[str, dict] = {}
    for ticker, sub in df.groupby("ticker"):
        ordered = sub.sort_values("date")
        dates = [d.date() if hasattr(d, "date") else d for d in ordered["date"]]
        closes = [float(c) for c in ordered["close"]]
        out[str(ticker)] = {"dates": dates, "closes": closes}
    return out


def _forward_from_matrix(series: dict, origin: date, horizon: int,
                         kind: str = "return") -> Optional[float]:
    """Forward return (%) or max drawdown (%) over ``horizon`` trading days."""
    dates, closes = series["dates"], series["closes"]
    i = bisect_left(dates, origin)
    if i >= len(dates) or i + horizon >= len(closes):
        return None
    entry = closes[i]
    if not entry:
        return None
    if kind == "return":
        # Reuse the shared holiday-robust helper so semantics cannot drift.
        ret = _forward_return(closes[i:], horizon)
        return _round(ret * 100.0, 4) if ret is not None else None
    if kind == "maxdd":
        window = closes[i:i + horizon + 1]
        return _round(min(c / entry - 1.0 for c in window) * 100.0, 4)
    raise ValueError(f"unknown forward kind: {kind}")


def build_regime_panel(engine, start: date, end: date, step: int = 7,
                       horizons: Sequence[int] = FORECAST_HORIZONS,
                       tickers: Optional[Sequence[str]] = None) -> list[dict]:
    """One row per origin: regime outputs + forward SPY returns and drawdowns."""
    from db_builder.aegis_backtest import reconstruct_regime_at
    from db_builder.market_regime_v2 import BREADTH_ETFS, KEY_ETFS

    tickers = list(tickers or sorted(set(KEY_ETFS + BREADTH_ETFS)))
    pad = max(horizons) * 3 + 10
    matrix = fetch_close_matrix(engine, tickers, start, end + timedelta(days=pad))
    if SPX_TICKER not in matrix:
        raise ValueError(f"{SPX_TICKER} missing from us_equities for the requested window")

    rows: list[dict] = []
    cur = start
    while cur <= end:
        try:
            snap = reconstruct_regime_at(engine, cur)
        except Exception:
            cur += timedelta(days=step)
            continue
        regime = snap.regime
        fwd, dd, realized = {}, {}, {}
        for h in horizons:
            ret = _forward_from_matrix(matrix[SPX_TICKER], cur, h, "return")
            if ret is None:
                continue
            fwd[h] = ret
            dd[h] = _forward_from_matrix(matrix[SPX_TICKER], cur, h, "maxdd")
            try:
                label = reconstruct_regime_at(
                    engine, cur + timedelta(days=h)
                ).regime.get("market_regime", "")
            except Exception:
                label = ""
            realized[h] = label or None
        if fwd:
            rows.append({
                "origin": cur,
                "score": _num(regime.get("bullish_score"), 50.0),
                "label": regime.get("market_regime", ""),
                "phase": regime.get("market_phase", ""),
                "confidence": _num(regime.get("confidence"), 0.0),
                "factors": {k: _num(regime.get(k), 50.0) for k, _, _ in REGIME_FACTORS},
                "fwd": fwd,
                "maxdd": dd,
                "realized_label": realized,
            })
        cur += timedelta(days=step)
    return rows


def build_sector_panel(engine, start: date, end: date, step: int = 7,
                       horizons: Sequence[int] = FORECAST_HORIZONS) -> list[dict]:
    """One row per origin: 11 sector ratings + components + forward excess returns."""
    from db_builder.aegis_backtest import (
        fetch_historical_sector_panel,
        reconstruct_regime_at,
    )

    tickers = list(GICS_SECTOR_ETFS.values())
    pad = max(horizons) * 3 + 10
    matrix = fetch_close_matrix(engine, tickers + [SPX_TICKER], start, end + timedelta(days=pad))

    rows: list[dict] = []
    cur = start
    while cur <= end:
        try:
            panel = fetch_historical_sector_panel(engine, cur)
        except Exception:
            cur += timedelta(days=step)
            continue
        if panel is None or len(panel) == 0:
            cur += timedelta(days=step)
            continue
        components, ratings = {}, {}
        for _, row in panel.iterrows():
            ticker = row["ticker"]
            if ticker not in tickers:
                continue
            comp = sector_rating_components(row.to_dict())
            components[ticker] = comp["components"]
            ratings[ticker] = comp["rating"]
        if len(ratings) < 5:
            cur += timedelta(days=step)
            continue

        excess: dict[int, dict[str, float]] = {}
        spy: dict[int, float] = {}
        for h in horizons:
            bench = _forward_from_matrix(matrix[SPX_TICKER], cur, h, "return")
            if bench is None:
                continue
            per_sector = {}
            for ticker in ratings:
                r = _forward_from_matrix(matrix[ticker], cur, h, "return")
                if r is not None:
                    per_sector[ticker] = _round(r - bench, 4)
            if per_sector:
                excess[h] = per_sector
                spy[h] = bench
        if not excess:
            cur += timedelta(days=step)
            continue

        try:
            regime = reconstruct_regime_at(engine, cur).regime
            regime_score = _num(regime.get("bullish_score"), 50.0)
            regime_label = regime.get("market_regime", "")
        except Exception:
            regime_score, regime_label = 50.0, ""

        rows.append({
            "origin": cur,
            "regime_score": regime_score,
            "regime_label": regime_label,
            "ratings": ratings,
            "components": components,
            "excess": excess,
            "spy": spy,
        })
        cur += timedelta(days=step)
    return rows


def latest_regime_snapshot(engine, window_hours: int = 24) -> Optional[dict]:
    """Newest stored market_regime_v2 row, as a flat regime dict.

    The analysis panel stops at ``--end``, so its last origin is *not* the current
    production state. Attribution of "today" must read the live table instead.
    """
    from sqlalchemy import text
    sql = text("""
        SELECT run_time, market_regime, market_phase, confidence, bullish_score,
               technical_score, momentum_score, risk_appetite_score, breadth_score,
               macro_score, news_score
        FROM public.market_regime_v2
        WHERE window_hours = :wh
        ORDER BY run_time DESC
        LIMIT 1
    """)
    with engine.connect() as conn:
        row = conn.execute(sql, {"wh": window_hours}).fetchone()
    return dict(row._mapping) if row else None


def latest_regime_deltas(engine, window_hours: int = 24) -> Optional[dict]:
    """Attribution of the newest production row vs the previous production row."""
    from sqlalchemy import text
    sql = text("""
        SELECT run_time, market_regime, bullish_score, technical_score, momentum_score,
               risk_appetite_score, breadth_score, macro_score, news_score
        FROM public.market_regime_v2
        WHERE window_hours = :wh
        ORDER BY run_time DESC
        LIMIT 2
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"wh": window_hours}).fetchall()
    if len(rows) < 2:
        return None
    curr, prev = rows[0]._mapping, rows[1]._mapping
    return regime_delta(attribute_regime(dict(prev)), attribute_regime(dict(curr)))


# --------------------------------------------------------------------------- #
#  Evaluation -- regime
# --------------------------------------------------------------------------- #
def _rows_for(panel: Sequence[dict], horizon: int,
              sample: Optional[Sequence[date]] = None) -> list[dict]:
    """Regime-panel selector: rows with a realised forward return at ``horizon``."""
    keep = set(sample) if sample is not None else None
    return [r for r in panel
            if r.get("fwd", {}).get(horizon) is not None
            and (keep is None or r["origin"] in keep)]


def _sector_rows_for(panel: Sequence[dict], horizon: int,
                     sample: Optional[Sequence[date]] = None) -> list[dict]:
    """Sector-panel selector: rows with realised forward *excess* at ``horizon``.

    Sector rows store cross-sectional excess returns in ``excess`` (ticker -> excess)
    rather than the regime panel's scalar ``fwd``, so they need their own filter.
    """
    keep = set(sample) if sample is not None else None
    return [r for r in panel
            if r.get("excess", {}).get(horizon)
            and (keep is None or r["origin"] in keep)]


def _regime_metrics(rows: Sequence[dict], horizon: int, score_key: str = "score",
                    factor: Optional[str] = None) -> dict:
    """IC / hit / Brier / downside stats for the composite or a single factor."""
    def value(r: dict) -> Optional[float]:
        if factor is None:
            return r.get(score_key)
        return r["factors"].get(factor)

    vals = [value(r) for r in rows]
    rets = [r["fwd"][horizon] for r in rows]
    dds = [r["maxdd"].get(horizon) for r in rows]
    outcomes = [1.0 if (r["realized_label"].get(horizon) in RISK_ON_LABELS) else 0.0
                if r["realized_label"].get(horizon) else None
                for r in rows]
    dd_pairs = [(v, -d) for v, d in zip(vals, dds) if v is not None and d is not None]
    base_rate = _mean([o for o in outcomes if o is not None]) if any(
        o is not None for o in outcomes) else None
    return {
        "n": len(rows),
        "rank_ic": spearman_rho([v for v in vals if v is not None],
                                [r for v, r in zip(vals, rets) if v is not None]),
        "sign_hit_rate": sign_hit_rate(vals, rets),
        "brier_risk_on": brier_score(vals, outcomes),
        "risk_on_base_rate": _round(base_rate, 4) if base_rate is not None else None,
        "n_brier": sum(1 for o in outcomes if o is not None),
        "dd_rank_ic": spearman_rho([v for v, _ in dd_pairs], [d for _, d in dd_pairs]),
        "downside": downside_profile(vals, rets),
        "zero_variance": len({round(v, 6) for v in vals if v is not None}) <= 1,
    }


def evaluate_regime(panel: Sequence[dict], horizons: Sequence[int] = FORECAST_HORIZONS,
                    neutral: float = 50.0) -> dict:
    """Per-factor and composite predictive metrics, all-origin and non-overlapping."""
    origins = [r["origin"] for r in panel]
    out: dict[str, Any] = {"model": "market_regime_v2", "n_origins": len(panel), "horizons": {}}
    for h in horizons:
        all_rows = _rows_for(panel, h)
        no_rows = _rows_for(panel, h, non_overlapping(origins, h))
        hz = {
            "n_all": len(all_rows),
            "n_non_overlapping": len(no_rows),
            "composite_all": _regime_metrics(all_rows, h),
            "composite_non_overlapping": _regime_metrics(no_rows, h),
            "factors": {},
            "calibration": reliability_bins(
                [r["score"] for r in no_rows],
                [1.0 if r["realized_label"].get(h) in RISK_ON_LABELS else 0.0
                 for r in no_rows if r["realized_label"].get(h)],
            ) if no_rows else [],
        }
        for key, _, _ in REGIME_FACTORS:
            hz["factors"][key] = {
                "all": _regime_metrics(all_rows, h, factor=key),
                "non_overlapping": _regime_metrics(no_rows, h, factor=key),
            }
        out["horizons"][h] = hz
    out["factor_redundancy"] = factor_redundancy(panel)
    return out


def factor_redundancy(panel: Sequence[dict], threshold: float = 0.80) -> dict:
    """Pairwise Pearson correlation of factor scores across origins."""
    keys = [k for k, _, _ in REGIME_FACTORS]
    series = {k: [r["factors"].get(k) for r in panel] for k in keys}
    pairs = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            xs = [(x, y) for x, y in zip(series[a], series[b])
                  if x is not None and y is not None]
            if len(xs) < 3:
                continue
            r = _pearson([x for x, _ in xs], [y for _, y in xs])
            pairs.append({"a": a, "b": b, "r": _round(r, 4), "n": len(xs),
                          "redundant": abs(r) >= threshold})
    pairs.sort(key=lambda p: -abs(p["r"]))
    return {"pairs": pairs, "threshold": threshold,
            "redundant_pairs": [p for p in pairs if p["redundant"]]}


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = _mean(xs), _mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return 0.0
    return cov / (sx * sy)


def evaluate_regime_ablation(panel: Sequence[dict],
                             horizons: Sequence[int] = FORECAST_HORIZONS) -> dict:
    """One-at-a-time factor removal, scored on the non-overlapping sample."""
    origins = [r["origin"] for r in panel]
    out: dict[str, Any] = {}
    for policy in ("neutral_fill", "renormalize"):
        policy_out = {}
        for key, _, _ in REGIME_FACTORS:
            per_h = {}
            for h in horizons:
                rows = _rows_for(panel, h, non_overlapping(origins, h))
                if not rows:
                    continue
                base = _regime_metrics(rows, h)
                # Panel rows nest the factors and call the score "score"; the
                # ablation helper needs the flattened regime dict, otherwise it
                # reads every input as the 50 neutral default and returns a
                # constant score (zero-variance IC -> None).
                ablated = _regime_metrics(
                    [dict(r, score=ablate_regime_score(
                        _regime_dict_from_row(r), key, policy)["ablated_score"])
                     for r in rows], h)
                ic_base, ic_ablated = base["rank_ic"], ablated["rank_ic"]
                hit_base, hit_ablated = base["sign_hit_rate"], ablated["sign_hit_rate"]
                brier_base, brier_ablated = base["brier_risk_on"], ablated["brier_risk_on"]
                per_h[h] = {
                    "n": base["n"],
                    "ic_base": ic_base,
                    "ic_ablated": ic_ablated,
                    # Undefined (zero-variance) ICs stay None rather than being
                    # coerced to 0.0, which would fake a "removal improves IC"
                    # signal for every factor at once.
                    "delta_ic": (_round(ic_ablated - ic_base, 6)
                                 if ic_ablated is not None and ic_base is not None else None),
                    "hit_base": hit_base,
                    "hit_ablated": hit_ablated,
                    "delta_hit": (_round(hit_ablated - hit_base, 6)
                                  if hit_ablated is not None and hit_base is not None else None),
                    "brier_base": brier_base,
                    "brier_ablated": brier_ablated,
                    "delta_brier": (_round(brier_ablated - brier_base, 6)
                                    if brier_ablated is not None and brier_base is not None else None),
                    "zero_variance_ablated": ablated["zero_variance"],
                }
            policy_out[key] = per_h
        out[policy] = policy_out
    return out


def _regime_challengers(rows_train: Sequence[dict], rows_eval: Sequence[dict],
                        horizon: int) -> dict:
    """Score production vs equal/shrinkage/regularized challengers on EVAL rows."""
    if not rows_eval:
        return {}
    keys = [k for k, _, _ in REGIME_FACTORS]
    weights = fit_ic_weights(rows_train, keys, horizon)
    scored = []
    for r in rows_eval:
        f = {k: _num(r["factors"].get(k), 50.0) for k in keys}
        ic_score = _round(sum(weights.get(k, 0.0) * f[k] for k in keys), 4)
        eq = _round(_mean(list(f.values())), 4)
        sh = _round(0.5 * eq + 0.5 * _num(r.get("score"), 50.0), 4)
        scored.append(dict(r, score_ic=ic_score, score_eq=eq, score_sh=sh))
    return {
        "weights": weights,
        "production": _regime_metrics(scored, horizon, score_key="score"),
        "equal_weight": _regime_metrics(scored, horizon, score_key="score_eq"),
        "shrinkage_50": _regime_metrics(scored, horizon, score_key="score_sh"),
        "ic_weighted_regularized": _regime_metrics(scored, horizon, score_key="score_ic"),
        "n_eval": len(scored),
    }


def evaluate_regime_challengers(panel: Sequence[dict],
                                horizons: Sequence[int] = FORECAST_HORIZONS,
                                n_blocks: int = 4, min_train: int = 20) -> dict:
    """Chronological walk-forward comparison; weights fitted on train blocks only."""
    origins = [r["origin"] for r in panel]
    blocks = walk_forward_blocks(origins, n_blocks, min_train)
    out: dict[str, Any] = {"n_blocks": len(blocks), "horizons": {}}
    for h in horizons:
        per_block = []
        pooled = {name: [] for name in
                  ("production", "equal_weight", "shrinkage_50", "ic_weighted_regularized")}
        for blk in blocks:
            # Train on every train origin (purged of windows that reach into the
            # eval period): overlap inflates the row count but does not bias the
            # fitted weights, and we report no inference on them. Eval stays
            # non-overlapping so the compared metrics are not double-counted.
            train_dates = purge_before(blk["train"], blk["eval"][0] if blk["eval"] else None, h)
            train = _rows_for(panel, h, train_dates)
            evals = _rows_for(panel, h, non_overlapping(blk["eval"], h))
            if len(train) < min_train or not evals:
                continue
            res = _regime_challengers(train, evals, h)
            if not res:
                continue
            for name in pooled:
                pooled[name].append((res[name]["rank_ic"], res[name]["n"]))
            per_block.append({
                "block": blk["block"],
                "n_train": len(train),
                "n_eval": len(evals),
                "weights": res["weights"],
                "ic": {name: res[name]["rank_ic"] for name in pooled},
                "hit": {name: res[name]["sign_hit_rate"] for name in pooled},
            })
        summary = {}
        for name in pooled:
            ics = [ic for ic, _ in pooled[name] if ic is not None]
            summary[name] = {
                "n_blocks": len(ics),
                "mean_block_ic": _round(_mean(ics), 6) if ics else None,
                "ic_stats": ic_stats(ics),
                "n_oos": sum(n for _, n in pooled[name]),
            }
        out["horizons"][h] = {"per_block": per_block, "summary": summary}
    return out


# --------------------------------------------------------------------------- #
#  Evaluation -- sector
# --------------------------------------------------------------------------- #
def _sector_cs_ic(row: dict, horizon: int, ratings: dict[str, float]) -> Optional[float]:
    excess = row["excess"].get(horizon)
    if not excess:
        return None
    tickers = [t for t in ratings if t in excess]
    if len(tickers) < 3:
        return None
    return spearman_rho([ratings[t] for t in tickers], [excess[t] for t in tickers])


def _sector_metrics(rows: Sequence[dict], horizon: int,
                    ratings_fn=None, k: int = 3,
                    ow: float = 70.0, uw: float = 30.0,
                    cost_bps: float = TRANSACTION_COST_BPS) -> dict:
    """Cross-sectional IC, top-bottom spread, OW/UW hit rate and active alpha."""
    ics, spreads, top_alphas = [], [], []
    ow_hits = ow_n = uw_hits = uw_n = 0
    for row in rows:
        excess = row["excess"].get(horizon)
        if not excess:
            continue
        ratings = ratings_fn(row) if ratings_fn else row["ratings"]
        tickers = [t for t in ratings if t in excess]
        if len(tickers) < 3:
            continue
        ic = spearman_rho([ratings[t] for t in tickers], [excess[t] for t in tickers])
        if ic is not None:
            ics.append(ic)
        ordered = sorted(tickers, key=lambda t: (-ratings[t], t))
        top, bottom = ordered[:k], ordered[-k:]
        top_ret = _mean([excess[t] for t in top])
        bottom_ret = _mean([excess[t] for t in bottom])
        spreads.append(top_ret - bottom_ret)
        top_alphas.append(top_ret - cost_bps / 100.0)
        for t in tickers:
            if ratings[t] >= ow:
                ow_n += 1
                ow_hits += 1 if excess[t] > 0 else 0
            elif ratings[t] <= uw:
                uw_n += 1
                uw_hits += 1 if excess[t] < 0 else 0
    stats = ic_stats(ics)
    return {
        "n_dates": len(ics),
        "ic": stats,
        "top_bottom_spread_bps": _round(_mean(spreads), 4) if spreads else None,
        "spread_hit_rate": _round(sum(1 for s in spreads if s > 0) / len(spreads), 4) if spreads else None,
        "active_alpha_bps": _round(_mean(top_alphas), 4) if top_alphas else None,
        "ow_hit_rate": _round(ow_hits / ow_n, 4) if ow_n else None,
        "ow_n": ow_n,
        "uw_hit_rate": _round(uw_hits / uw_n, 4) if uw_n else None,
        "uw_n": uw_n,
    }


def _rating_fn_factory(variant: str):
    """Rating function per variant, computed from the stored components."""
    def baseline(row: dict) -> dict:
        return row["ratings"]

    def equal(row: dict) -> dict:
        return {t: equal_weight_from_components(comp)
                for t, comp in row["components"].items()}

    def momentum_flip(row: dict) -> dict:
        return {t: momentum_flip_from_components(comp)
                for t, comp in row["components"].items()}

    def unclipped(row: dict) -> dict:
        return {t: unclipped_rating_from_components(comp)
                for t, comp in row["components"].items()}

    variants = {"baseline": baseline, "equal_weight": equal,
                "momentum_flip": momentum_flip, "unclipped": unclipped}
    if variant not in variants:
        raise ValueError(f"unknown sector variant: {variant}")
    return variants[variant]


def evaluate_sector(panel: Sequence[dict], horizons: Sequence[int] = FORECAST_HORIZONS,
                    cost_bps: float = TRANSACTION_COST_BPS) -> dict:
    """Baseline sector model metrics, all-origin and non-overlapping."""
    origins = [r["origin"] for r in panel]
    out: dict[str, Any] = {"model": "rate_sector", "n_origins": len(panel), "horizons": {}}
    for h in horizons:
        all_rows = _sector_rows_for(panel, h)
        no_rows = _sector_rows_for(panel, h, non_overlapping(origins, h))
        out["horizons"][h] = {
            "n_all": len(all_rows),
            "n_non_overlapping": len(no_rows),
            "all": _sector_metrics(all_rows, h, cost_bps=cost_bps),
            "non_overlapping": _sector_metrics(no_rows, h, cost_bps=cost_bps),
        }
    return out


def evaluate_sector_components(panel: Sequence[dict],
                               horizons: Sequence[int] = FORECAST_HORIZONS) -> dict:
    """Per-component cross-sectional IC and ablation impact on IC/rank churn."""
    origins = [r["origin"] for r in panel]
    out: dict[str, Any] = {}
    for key, _, _ in SECTOR_COMPONENTS:
        per_h = {}
        for h in horizons:
            rows = _sector_rows_for(panel, h, non_overlapping(origins, h))
            if not rows:
                continue
            base_ic = _sector_metrics(rows, h)["ic"]["mean_ic"]
            # component's own cross-sectional IC
            comp_ics = []
            for row in rows:
                excess = row["excess"].get(h)
                if not excess:
                    continue
                tickers = [t for t in row["components"] if t in excess]
                if len(tickers) < 3:
                    continue
                comp_ics.append(spearman_rho(
                    [row["components"][t][key] for t in tickers],
                    [excess[t] for t in tickers]))
            # ablation: neutralize the component, then re-rank
            churn = []
            ab_ics = []
            for row in rows:
                excess = row["excess"].get(h)
                if not excess:
                    continue
                base_ratings = row["ratings"]
                abl = {}
                for t, comp in row["components"].items():
                    raw = SECTOR_RATING_BASE + sum(
                        v for k2, v in comp.items() if k2 != key)
                    abl[t] = _round(_clamp(raw), 4)
                tickers = [t for t in base_ratings if t in excess and t in abl]
                if len(tickers) < 3:
                    continue
                rb = sector_rank({t: base_ratings[t] for t in tickers})
                ra = sector_rank({t: abl[t] for t in tickers})
                churn.extend(abs(rb[t] - ra[t]) for t in tickers)
                ab_ics.append(spearman_rho([abl[t] for t in tickers],
                                           [excess[t] for t in tickers]))
            per_h[h] = {
                "n_dates": len(comp_ics),
                "component_ic": ic_stats(comp_ics),
                "ic_baseline": base_ic,
                "ic_ablated": ic_stats(ab_ics)["mean_ic"],
                "delta_ic": _round((ic_stats(ab_ics)["mean_ic"] or 0.0) - (base_ic or 0.0), 6),
                "mean_abs_rank_change": _round(_mean(churn), 4) if churn else None,
            }
        out[key] = per_h
    return out


def evaluate_sector_variants(panel: Sequence[dict],
                             horizons: Sequence[int] = FORECAST_HORIZONS,
                             cost_bps: float = TRANSACTION_COST_BPS) -> dict:
    """Momentum vs mean-reversion and equal-weight variants on the same rows."""
    origins = [r["origin"] for r in panel]
    out: dict[str, Any] = {}
    for variant in ("baseline", "equal_weight", "momentum_flip", "unclipped"):
        per_h = {}
        for h in horizons:
            rows = _sector_rows_for(panel, h, non_overlapping(origins, h))
            if not rows:
                continue
            per_h[h] = _sector_metrics(rows, h, ratings_fn=_rating_fn_factory(variant),
                                       cost_bps=cost_bps)
        out[variant] = per_h
    return out


def evaluate_sector_conditioned(panel: Sequence[dict],
                                horizons: Sequence[int] = FORECAST_HORIZONS) -> dict:
    """Baseline sector IC split by the regime label observed at the origin."""
    groups = {"risk_on": [], "neutral": [], "risk_off": []}
    for row in panel:
        label = row.get("regime_label") or ""
        if label in RISK_ON_LABELS:
            groups["risk_on"].append(row)
        elif label in RISK_OFF_LABELS:
            groups["risk_off"].append(row)
        else:
            groups["neutral"].append(row)
    out = {}
    for name, rows in groups.items():
        per_h = {}
        for h in horizons:
            sample = _sector_rows_for(rows, h, non_overlapping([r["origin"] for r in rows], h))
            per_h[h] = _sector_metrics(sample, h)["ic"] if sample else {"n": 0}
        out[name] = {"n_origins": len(rows), "horizons": per_h}
    return out


def evaluate_sector_challengers(panel: Sequence[dict],
                                horizons: Sequence[int] = FORECAST_HORIZONS,
                                n_blocks: int = 4, min_train: int = 12,
                                cost_bps: float = TRANSACTION_COST_BPS) -> dict:
    """Walk-forward sector challengers; component weights fitted on train dates only."""
    origins = [r["origin"] for r in panel]
    blocks = walk_forward_blocks(origins, n_blocks, min_train)
    budgets = {k: b for k, b, _ in SECTOR_COMPONENTS}
    out: dict[str, Any] = {"n_blocks": len(blocks), "horizons": {}}
    for h in horizons:
        per_block = []
        for blk in blocks:
            # Same convention as the regime challengers: purge the training
            # windows, train on all remaining train origins, evaluate on a
            # non-overlapping eval sample.
            train_dates = purge_before(blk["train"], blk["eval"][0] if blk["eval"] else None, h)
            train = _sector_rows_for(panel, h, train_dates)
            evals = _sector_rows_for(panel, h, non_overlapping(blk["eval"], h))
            if len(train) < min_train or not evals:
                continue
            weights = fit_component_weights(train, h)
            overlay = fit_regime_overlay(train, h)

            def wf_weighted(row: dict, _w=weights) -> dict:
                out_r = {}
                for t, comp in row["components"].items():
                    z = sum(_w.get(k, 0.0) * (comp[k] / budgets[k]) for k in budgets)
                    out_r[t] = _round(_clamp(SECTOR_RATING_BASE + 50.0 * z), 4)
                return out_r

            def wf_overlay(row: dict, _b=overlay.get("beta", 0.0)) -> dict:
                base = row["ratings"]
                dev = _num(row.get("regime_score"), 50.0) - 50.0
                return {t: _round(_clamp(v + _b * dev), 4) for t, v in base.items()}

            metrics = {
                "baseline": _sector_metrics(evals, h, cost_bps=cost_bps),
                "equal_weight": _sector_metrics(evals, h, ratings_fn=_rating_fn_factory("equal_weight"),
                                                cost_bps=cost_bps),
                "ic_weighted_regularized": _sector_metrics(evals, h, ratings_fn=wf_weighted,
                                                           cost_bps=cost_bps),
                "regime_overlay": _sector_metrics(evals, h, ratings_fn=wf_overlay,
                                                  cost_bps=cost_bps),
            }
            per_block.append({
                "block": blk["block"],
                "n_train": len(train),
                "n_eval": len(evals),
                "component_weights": weights,
                "regime_overlay_beta": overlay.get("beta"),
                "ic": {name: m["ic"]["mean_ic"] for name, m in metrics.items()},
                "spread_bps": {name: m["top_bottom_spread_bps"] for name, m in metrics.items()},
                "alpha_bps": {name: m["active_alpha_bps"] for name, m in metrics.items()},
                "ow_hit": {name: m["ow_hit_rate"] for name, m in metrics.items()},
            })
        summary = {}
        for name in ("baseline", "equal_weight", "ic_weighted_regularized", "regime_overlay"):
            ics = [b["ic"][name] for b in per_block if b["ic"].get(name) is not None]
            summary[name] = {
                "n_blocks": len(ics),
                "mean_block_ic": _round(_mean(ics), 6) if ics else None,
                "ic_stats": ic_stats(ics),
                "mean_spread_bps": _round(_mean([b["spread_bps"][name] for b in per_block
                                                 if b["spread_bps"].get(name) is not None]), 4)
                if per_block else None,
                "mean_alpha_bps": _round(_mean([b["alpha_bps"][name] for b in per_block
                                                if b["alpha_bps"].get(name) is not None]), 4)
                if per_block else None,
                "n_oos_dates": sum(b["n_eval"] for b in per_block),
            }
        out["horizons"][h] = {"per_block": per_block, "summary": summary}
    return out


# --------------------------------------------------------------------------- #
#  Orchestration
# --------------------------------------------------------------------------- #
def run_regime_analysis(engine, start: date, end: date, step: int = 7,
                        horizons: Sequence[int] = FORECAST_HORIZONS,
                        n_blocks: int = 4) -> dict:
    panel = build_regime_panel(engine, start, end, step, horizons)
    result = {
        "model": "market_regime_v2",
        "window": {"start": str(start), "end": str(end), "step_days": step},
        "n_origins": len(panel),
        "evaluation": evaluate_regime(panel, horizons),
        "ablation": evaluate_regime_ablation(panel, horizons),
        "challengers": evaluate_regime_challengers(panel, horizons, n_blocks=n_blocks),
        "latest_change": latest_regime_deltas(engine),
        "data_quality": regime_data_quality(panel, horizons),
    }
    # Prefer the newest stored production row; the panel ends at --end and is a
    # reconstruction, so it is only the fallback.
    snapshot = latest_regime_snapshot(engine)
    if snapshot:
        result["current_attribution"] = attribute_regime(snapshot)
        result["current_attribution"]["source"] = "market_regime_v2 (latest stored row)"
    elif panel:
        result["current_attribution"] = attribute_regime(_regime_dict_from_row(panel[-1]))
        result["current_attribution"]["source"] = "panel reconstruction (no stored row)"
    return result


def _regime_dict_from_row(row: dict) -> dict:
    return {
        "run_time": row["origin"].isoformat(),
        "bullish_score": row["score"],
        "market_regime": row["label"],
        "market_phase": row.get("phase", ""),
        "confidence": row.get("confidence", 0.0),
        **row["factors"],
    }


def run_sector_analysis(engine, start: date, end: date, step: int = 7,
                        horizons: Sequence[int] = FORECAST_HORIZONS,
                        n_blocks: int = 4) -> dict:
    panel = build_sector_panel(engine, start, end, step, horizons)
    result = {
        "model": "rate_sector",
        "window": {"start": str(start), "end": str(end), "step_days": step},
        "n_origins": len(panel),
        "evaluation": evaluate_sector(panel, horizons),
        "components": evaluate_sector_components(panel, horizons),
        "variants": evaluate_sector_variants(panel, horizons),
        "conditioned": evaluate_sector_conditioned(panel, horizons),
        "challengers": evaluate_sector_challengers(panel, horizons, n_blocks=n_blocks),
        "data_quality": sector_data_quality(panel, horizons),
    }
    if panel:
        last = panel[-1]
        ratings = last["ratings"]
        ranks = sector_rank(ratings)
        result["current_attribution"] = {
            "origin": last["origin"].isoformat(),
            "regime_score": last["regime_score"],
            "regime_label": last["regime_label"],
            "sectors": [
                {
                    "ticker": ticker,
                    "sector": _sector_name(ticker),
                    "rating": _round(ratings[ticker], 4),
                    "rank": ranks[ticker],
                    "components": {k: _round(v, 4) for k, v in last["components"][ticker].items()},
                    "top_positive_component": max(
                        last["components"][ticker], key=lambda k: last["components"][ticker][k]),
                    "top_negative_component": min(
                        last["components"][ticker], key=lambda k: last["components"][ticker][k]),
                }
                for ticker in sorted(ratings, key=lambda t: -ratings[t])
            ],
        }
    return result


def _sector_name(ticker: str) -> str:
    for name, tk in GICS_SECTOR_ETFS.items():
        if tk == ticker:
            return name
    return ticker


def regime_data_quality(panel: Sequence[dict],
                        horizons: Sequence[int] = FORECAST_HORIZONS) -> dict:
    """Coverage, factor variance and zero-variance flags."""
    factor_var = {}
    for key, _, _ in REGIME_FACTORS:
        vals = [r["factors"].get(key) for r in panel if r["factors"].get(key) is not None]
        distinct = len({round(v, 4) for v in vals})
        factor_var[key] = {
            "n": len(vals),
            "distinct_values": distinct,
            "min": _round(min(vals), 4) if vals else None,
            "max": _round(max(vals), 4) if vals else None,
            "mean": _round(_mean(vals), 4) if vals else None,
            "constant": distinct <= 1,
        }
    coverage = {h: sum(1 for r in panel if r["fwd"].get(h) is not None) for h in horizons}
    return {
        "n_origins": len(panel),
        "origins_with_forward_by_horizon": coverage,
        "factor_variance": factor_var,
        "constant_factors": [k for k, v in factor_var.items() if v["constant"]],
        "first_origin": str(panel[0]["origin"]) if panel else None,
        "last_origin": str(panel[-1]["origin"]) if panel else None,
    }


def sector_data_quality(panel: Sequence[dict],
                        horizons: Sequence[int] = FORECAST_HORIZONS) -> dict:
    coverage = {h: sum(1 for r in panel if r["excess"].get(h)) for h in horizons}
    ratings = [v for r in panel for v in r["ratings"].values()]
    clipped = sum(1 for r in panel for t in r["ratings"]
                  if r["ratings"][t] <= 0.0 or r["ratings"][t] >= 100.0)
    # Saturation matters for ranking: once several sectors tie at a bound, the
    # rating carries no cross-sectional information for those names. Report the
    # per-origin tie structure so a low IC can be traced to clipping rather than
    # to the factors themselves.
    distinct_per_origin, tied_origins = [], 0
    for r in panel:
        vals = list(r["ratings"].values())
        if not vals:
            continue
        distinct_per_origin.append(len(set(round(v, 4) for v in vals)))
        bound_vals = [v for v in vals if v <= 0.0 or v >= 100.0]
        if len(bound_vals) != len(set(bound_vals)):
            tied_origins += 1
    return {
        "n_origins": len(panel),
        "origins_with_excess_by_horizon": coverage,
        "n_ratings": len(ratings),
        "rating_min": _round(min(ratings), 4) if ratings else None,
        "rating_max": _round(max(ratings), 4) if ratings else None,
        "rating_mean": _round(_mean(ratings), 4) if ratings else None,
        "ratings_at_bound": clipped,
        "pct_ratings_at_bound": _round(clipped / len(ratings) * 100, 2) if ratings else None,
        "mean_distinct_ratings_per_origin": (
            _round(_mean(distinct_per_origin), 2) if distinct_per_origin else None),
        "origins_with_ties_at_bound": tied_origins,
        "first_origin": str(panel[0]["origin"]) if panel else None,
        "last_origin": str(panel[-1]["origin"]) if panel else None,
    }


# --------------------------------------------------------------------------- #
#  Reporting
# --------------------------------------------------------------------------- #
def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}{suffix}"
    return str(value)


def render_regime_markdown(result: dict) -> str:
    lines = [
        "# AEGIS Regime Factor Attribution — market_regime_v2",
        "",
        f"Window: **{result['window']['start']} → {result['window']['end']}** "
        f"(origin step {result['window']['step_days']}d, {result['n_origins']} origins)",
        "",
    ]
    cur = result.get("current_attribution")
    if cur:
        src = cur.get("source", "")
        lines += [
            "## Current factor attribution",
            "",
            f"Run time **{cur.get('run_time', '')}**" + (f" — source: {src}" if src else ""),
            "",
            f"Score **{_fmt(cur['score'])}** ({cur['regime_label']}), "
            f"confidence {_fmt(cur['confidence'] * 100, 1, '%')}",
            "",
            "| Factor | Weight | Score | Contribution | vs neutral | Share of excess |",
            "|---|---|---|---|---|---|",
        ]
        for f in cur["factors"]:
            lines.append(
                f"| {f['factor']} | {f['weight']:.2f} | {_fmt(f['score'])} | "
                f"{_fmt(f['contribution'])} | {_fmt(f['excess_vs_neutral'], 2, '')} | "
                f"{_fmt(f['share_of_excess'], 1, '%')} |")
        lines += [
            "",
            f"Sum of contributions {_fmt(cur['sum_contributions'], 4)} vs score "
            f"{_fmt(cur['score'], 4)} (rounding residual {_fmt(cur['residual'], 6)}).",
            f"Largest upward pull: **{cur['top_positive']}**; largest downward pull: "
            f"**{cur['top_negative']}** (pulls are vs the 50 neutral line, so a "
            "negative reading means every factor sits below neutral).",
        ]
        if cur["saturated_factors"]:
            lines.append(f"Saturated at a 0/100 bound: {', '.join(cur['saturated_factors'])}.")
        if cur["missing_factors"]:
            lines.append(f"Missing (defaulted to neutral 50): {', '.join(cur['missing_factors'])}.")

    chg = result.get("latest_change")
    if chg:
        lines += [
            "",
            "## Change since previous production run",
            "",
            f"{chg['prev_time']} → {chg['curr_time']}: score "
            f"{_fmt(chg['prev_score'])} → {_fmt(chg['curr_score'])} "
            f"({_fmt(chg['delta_score'], 2, ' pts')})",
            "",
            "| Factor | Δ score | Δ contribution |",
            "|---|---|---|",
        ]
        for row in reversed(chg["delta_contributions"]):
            lines.append(f"| {row['factor']} | {_fmt(row['delta_score'], 2)} | "
                         f"{_fmt(row['delta_contribution'], 3)} |")

    ev = result["evaluation"]
    lines += ["", "## Historical predictive performance (non-overlapping sample)", ""]
    for h, hz in ev["horizons"].items():
        lines += [
            f"### {h}D — n={hz['n_non_overlapping']} non-overlapping "
            f"({hz['n_all']} all origins)",
            "",
            "| Factor | Rank IC | Sign hit | IC t-stat | p | Brier | Δ DD IC |",
            "|---|---|---|---|---|---|---|",
        ]
        comp = hz["composite_non_overlapping"]
        lines.append(f"| **composite score** | {_fmt(comp['rank_ic'], 4)} | "
                     f"{_fmt(comp['sign_hit_rate'], 3)} | "
                     f"{_fmt(ic_t_stat(comp['rank_ic'], comp['n']), 2)} | "
                     f"{_fmt(ic_p_value(comp['rank_ic'], comp['n']), 3)} | "
                     f"{_fmt(comp['brier_risk_on'], 4)} | "
                     f"{_fmt(comp['dd_rank_ic'], 4)} |")
        for key, _, _ in REGIME_FACTORS:
            m = hz["factors"][key]["non_overlapping"]
            lines.append(f"| {key} | {_fmt(m['rank_ic'], 4)} | {_fmt(m['sign_hit_rate'], 3)} | "
                         f"{_fmt(ic_t_stat(m['rank_ic'], m['n']), 2)} | "
                         f"{_fmt(ic_p_value(m['rank_ic'], m['n']), 3)} | "
                         f"{_fmt(m['brier_risk_on'], 4)} | {_fmt(m['dd_rank_ic'], 4)} |")
        if comp["n"] < 10:
            lines += ["", f"⚠ n={comp['n']} non-overlapping dates at {h}D — a single "
                      "observation can move the IC by a full rank step. Directional only; "
                      "do not read significance from it."]
        lines.append("")

    abl = result["ablation"].get("neutral_fill", {})

    def _abl_cell(row: dict, h: int) -> str:
        cell = _fmt(row.get(h, {}).get("delta_ic"), 4)
        # A zero-variance ablated score makes IC undefined (reported as 0.0 by
        # spearman_rho); mark it so "0.0000" is never read as a real no-effect.
        if row.get(h, {}).get("zero_variance_ablated"):
            cell += " (flat)"
        return cell

    lines += ["## Factor ablation (neutral-fill policy, Δ vs full model)", "",
              "Positive ΔIC = the model ranks better *without* the factor.", "",
              "| Factor | 5D ΔIC | 20D ΔIC | 60D ΔIC | n (20D) |", "|---|---|---|---|---|"]
    for key, _, _ in REGIME_FACTORS:
        row = abl.get(key, {})
        lines.append(f"| {key} | {_abl_cell(row, 5)} | {_abl_cell(row, 20)} | "
                     f"{_abl_cell(row, 60)} | {row.get(20, {}).get('n', 'n/a')} |")

    ch = result["challengers"]
    lines += ["", "## Existing vs challenger models (walk-forward, train-only fitting)", ""]
    for h, hz in ch.get("horizons", {}).items():
        lines += [f"### {h}D", "", "| Model | Mean block IC | n blocks | n OOS |",
                  "|---|---|---|---|"]
        for name, s in hz["summary"].items():
            lines.append(f"| {name} | {_fmt(s['mean_block_ic'], 4)} | {s['n_blocks']} | {s['n_oos']} |")
        if not hz["per_block"]:
            lines.append("")
            lines.append(f"No purged training block is long enough at {h}D: the window "
                         "does not support an out-of-sample comparison at this horizon. "
                         "Shown as n/a rather than fitted on the evaluation sample.")
        lines.append("")

    dq = result.get("data_quality", {})
    lines += ["## Data quality and statistical confidence", "",
              f"- Origins: {dq.get('n_origins')} ({dq.get('first_origin')} → {dq.get('last_origin')})",
              "- Origins with complete forward window: "
              + ", ".join(f"{h}D {n}" for h, n in
                          (dq.get('origins_with_forward_by_horizon') or {}).items())]
    if dq.get("constant_factors"):
        lines.append(f"- Zero-variance factors in this window: {', '.join(dq['constant_factors'])}"
                     " — their IC is undefined and ablation is uninformative.")
    red = result["evaluation"].get("factor_redundancy", {})
    if red.get("redundant_pairs"):
        pairs = ", ".join(f"{p['a']}~{p['b']} r={p['r']:.2f}" for p in red["redundant_pairs"])
        lines.append(f"- Redundant factor pairs (|r| ≥ {red['threshold']}): {pairs}")
    else:
        lines.append(f"- No factor pair exceeds |r| ≥ {red.get('threshold')}.")
    lines += [
        "- 60D non-overlapping samples are necessarily small; treat 60D IC as directional only.",
        "- Rank IC is a time-series IC here (one asset, many origins), not cross-sectional.",
        "- Reconstructed history is hypothetical: inputs are as-of-date but the score was not stored live.",
        "- Brier uses the realised regime label at T+h as the outcome, clipped to [0.05, 0.95].",
    ]
    return "\n".join(lines)


def render_sector_markdown(result: dict) -> str:
    lines = [
        "# AEGIS Sector Factor Attribution — rate_sector",
        "",
        f"Window: **{result['window']['start']} → {result['window']['end']}** "
        f"(origin step {result['window']['step_days']}d, {result['n_origins']} origins)",
        "",
    ]
    cur = result.get("current_attribution")
    if cur:
        lines += [f"## Latest reconstructed ratings ({cur['origin']}, regime "
                  f"{cur['regime_label']} {_fmt(cur['regime_score'])})", "",
                  "Reconstructed from as-of inputs at the panel end — not a stored "
                  "production rating row.", "",
                  "| Rank | Ticker | Sector | Rating | MA50 | MA200 | 20D | 60D | RSI | MACD |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for s in cur["sectors"]:
            c = s["components"]
            lines.append(f"| {s['rank']} | {s['ticker']} | {s['sector']} | {_fmt(s['rating'])} | "
                         f"{_fmt(c['trend_ma50'], 1)} | {_fmt(c['trend_ma200'], 1)} | "
                         f"{_fmt(c['momentum_20d'], 1)} | {_fmt(c['momentum_60d'], 1)} | "
                         f"{_fmt(c['rsi14'], 1)} | {_fmt(c['macd_hist'], 1)} |")

    ev = result["evaluation"]
    lines += ["", "## Rank IC vs sector excess return (sec − SPY), non-overlapping", "",
              "| Horizon | Mean IC | IC IR | t | p | % positive | Top–Bottom bps | OW hit | UW hit | Alpha bps |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for h, hz in ev["horizons"].items():
        m = hz["non_overlapping"]
        ic = m["ic"]
        lines.append(f"| {h}D | {_fmt(ic.get('mean_ic'), 4)} | {_fmt(ic.get('ic_ir'), 3)} | "
                     f"{_fmt(ic.get('t_stat'), 2)} | {_fmt(ic.get('p_value'), 3)} | "
                     f"{_fmt(ic.get('pct_positive'), 2)} | {_fmt(m.get('top_bottom_spread_bps'), 2)} | "
                     f"{_fmt(m.get('ow_hit_rate'), 3)} | {_fmt(m.get('uw_hit_rate'), 3)} | "
                     f"{_fmt(m.get('active_alpha_bps'), 2)} |")

    comp = result["components"]
    lines += ["", "## Component diagnostics (non-overlapping)", "",
              "| Component | 5D IC | 20D IC | 60D IC | Δ IC on ablation (20D) | mean |Δrank| |",
              "|---|---|---|---|---|---|"]
    for key, _, _ in SECTOR_COMPONENTS:
        row = comp.get(key, {})
        lines.append(f"| {key} | {_fmt(row.get(5, {}).get('component_ic', {}).get('mean_ic'), 4)} | "
                     f"{_fmt(row.get(20, {}).get('component_ic', {}).get('mean_ic'), 4)} | "
                     f"{_fmt(row.get(60, {}).get('component_ic', {}).get('mean_ic'), 4)} | "
                     f"{_fmt(row.get(20, {}).get('delta_ic'), 4)} | "
                     f"{_fmt(row.get(20, {}).get('mean_abs_rank_change'), 2)} |")

    var = result["variants"]
    lines += ["", "## Momentum vs mean reversion vs equal weight vs unclipped", "",
              "`unclipped` keeps the raw rating sum without the [0, 100] clip, so it "
              "shows how much cross-sectional ranking the clip destroys.",
              "", "| Variant | 5D IC | 20D IC | 60D IC |", "|---|---|---|---|"]
    for name in ("baseline", "equal_weight", "momentum_flip", "unclipped"):
        row = var.get(name, {})
        lines.append(f"| {name} | {_fmt(row.get(5, {}).get('ic', {}).get('mean_ic'), 4)} | "
                     f"{_fmt(row.get(20, {}).get('ic', {}).get('mean_ic'), 4)} | "
                     f"{_fmt(row.get(60, {}).get('ic', {}).get('mean_ic'), 4)} |")

    cond = result["conditioned"]
    lines += ["", "## Regime-conditioned sector IC", "",
              "| Regime | n origins | 5D IC | 20D IC | 60D IC |", "|---|---|---|---|---|"]
    for name, block in cond.items():
        hz = block.get("horizons", {})
        lines.append(f"| {name} | {block.get('n_origins')} | "
                     f"{_fmt(hz.get(5, {}).get('mean_ic'), 4)} | "
                     f"{_fmt(hz.get(20, {}).get('mean_ic'), 4)} | "
                     f"{_fmt(hz.get(60, {}).get('mean_ic'), 4)} |")

    ch = result["challengers"]
    lines += ["", "## Existing vs challenger models (walk-forward, train-only fitting)", ""]
    for h, hz in ch.get("horizons", {}).items():
        lines += [f"### {h}D", "",
                  "| Model | Mean block IC | Mean spread bps | Mean alpha bps | n OOS dates |",
                  "|---|---|---|---|---|"]
        for name, s in hz["summary"].items():
            lines.append(f"| {name} | {_fmt(s['mean_block_ic'], 4)} | {_fmt(s['mean_spread_bps'], 2)} | "
                         f"{_fmt(s['mean_alpha_bps'], 2)} | {s['n_oos_dates']} |")
        betas = [b["regime_overlay_beta"] for b in hz["per_block"]]
        if betas:
            lines.append(f"\nRegime overlay beta per block: {', '.join(_fmt(b, 3) for b in betas)}")
        if not hz["per_block"]:
            lines.append("")
            lines.append(f"No purged training block is long enough at {h}D: the window "
                         "does not support an out-of-sample comparison at this horizon.")
        if hz["per_block"] and max(b["n_eval"] for b in hz["per_block"]) < 3:
            lines.append("")
            lines.append("Eval blocks hold fewer than 3 non-overlapping dates — "
                         "directional at best, not a basis for changing weights.")
        lines.append("")

    dq = result.get("data_quality", {})
    lines += ["## Data quality and statistical confidence", "",
              f"- Origins: {dq.get('n_origins')} ({dq.get('first_origin')} → {dq.get('last_origin')})",
              "- Dates with complete excess returns: "
              + ", ".join(f"{h}D {n}" for h, n in
                          (dq.get('origins_with_excess_by_horizon') or {}).items()),
              f"- Ratings at the 0/100 bound: {dq.get('ratings_at_bound')} of {dq.get('n_ratings')} "
              f"({_fmt(dq.get('pct_ratings_at_bound'), 1)}%) — a bound rating is insensitive to "
              "further input change and ties with other bound names.",
              f"- Mean distinct ratings per origin: {_fmt(dq.get('mean_distinct_ratings_per_origin'), 2)} "
              f"of 11 sectors; {dq.get('origins_with_ties_at_bound')} origins contain tied bound ratings. "
              "Ties are why the Top–Bottom spread and Rank IC can look weak even when the "
              "underlying factors are informative.",
              "- Cross-sectional IC uses 11 GICS ETF proxies; sector-level alpha is not security-level alpha.",
              "- 60D non-overlapping dates are few; treat 60D IC as directional only.",
              f"- Active alpha assumes {TRANSACTION_COST_BPS} bps round-trip cost per rebalance.",
              "- Reconstructed ratings are hypothetical (as-of inputs, not stored live ratings)."]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Row-level persistence -- the attribution dataset behind weight decisions
# --------------------------------------------------------------------------- #
# Two grains, matching how the models are actually scored:
#   * public.aegis_regime_attribution -- one row per origin date. Factor scores,
#     weights, contributions and the forward SPY outcome live side by side, so
#     "which parameter moved the score, and did it pay" is one SQL row.
#   * public.aegis_sector_backtest    -- the EXISTING per (date, sector) table,
#     extended with the six component contributions, the clip adjustment and the
#     rank. Same grain as before, so old rows keep working (new columns NULL).
REGIME_ATTR_TABLE = "public.aegis_regime_attribution"
SECTOR_BACKTEST_TABLE = "public.aegis_sector_backtest"

REGIME_FACTOR_COLUMNS = {
    "technical_score": "technical",
    "momentum_score": "momentum",
    "risk_appetite_score": "risk_appetite",
    "breadth_score": "breadth",
    "macro_score": "macro",
    "news_score": "news",
}

SECTOR_COMPONENT_COLUMNS = {
    "trend_ma50": "c_trend_ma50",
    "trend_ma200": "c_trend_ma200",
    "momentum_20d": "c_mom_20d",
    "momentum_60d": "c_mom_60d",
    "rsi14": "c_rsi14",
    "macd_hist": "c_macd_hist",
}


def attribution_ddl() -> dict[str, list[str]]:
    """Exact DDL for the row-level dataset. Additive; nothing is dropped.

    ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` is a no-op on a database that
    already has the columns, so this is safe to re-run.
    """
    factor_cols = []
    for key, short in REGIME_FACTOR_COLUMNS.items():
        factor_cols.append(f"    f_{short:<14} numeric")
    for key, short in REGIME_FACTOR_COLUMNS.items():
        factor_cols.append(f"    w_{short:<14} numeric")
    for key, short in REGIME_FACTOR_COLUMNS.items():
        factor_cols.append(f"    c_{short:<14} numeric")
    create_regime = f"""CREATE TABLE IF NOT EXISTS {REGIME_ATTR_TABLE} (
    date              date PRIMARY KEY,
    score             numeric,
    regime_label      text,
    phase             text,
    confidence        numeric,
{("," + chr(10)).join(factor_cols)},
    residual          numeric,
    spy_5d            numeric,
    spy_20d           numeric,
    spy_60d           numeric,
    dd_5d             numeric,
    dd_20d            numeric,
    dd_60d            numeric,
    label_5d          text,
    label_20d         text,
    label_60d         text,
    source            text,
    updated_at        timestamptz DEFAULT now()
);"""
    alter_sector = [
        f"ALTER TABLE {SECTOR_BACKTEST_TABLE} ADD COLUMN IF NOT EXISTS {col} numeric"
        for col in SECTOR_COMPONENT_COLUMNS.values()
    ] + [
        f"ALTER TABLE {SECTOR_BACKTEST_TABLE} ADD COLUMN IF NOT EXISTS clip_adj numeric",
        f"ALTER TABLE {SECTOR_BACKTEST_TABLE} ADD COLUMN IF NOT EXISTS rating_raw numeric",
        f"ALTER TABLE {SECTOR_BACKTEST_TABLE} ADD COLUMN IF NOT EXISTS rank int",
        f"ALTER TABLE {SECTOR_BACKTEST_TABLE} ADD COLUMN IF NOT EXISTS n_sectors int",
        f"ALTER TABLE {SECTOR_BACKTEST_TABLE} ADD COLUMN IF NOT EXISTS regime_score numeric",
        f"ALTER TABLE {SECTOR_BACKTEST_TABLE} ADD COLUMN IF NOT EXISTS regime_label text",
    ]
    return {"regime": [create_regime], "sector": alter_sector}


def regime_attribution_rows(panel: Sequence[dict]) -> list[dict]:
    """One storable row per regime origin: score, factors, weights, contributions, outcomes."""
    weights = regime_factor_weights()
    rows = []
    for r in panel:
        att = attribute_regime(_regime_dict_from_row(r))
        by_factor = {f["factor"]: f for f in att["factors"]}
        row: dict[str, Any] = {
            "date": r["origin"],
            "score": att["score"],
            "regime_label": att["regime_label"],
            "phase": att["phase"],
            "confidence": att["confidence"],
            "residual": att["residual"],
            "source": "reconstruction",
        }
        for key, short in REGIME_FACTOR_COLUMNS.items():
            f = by_factor.get(key, {})
            # A factor that was missing on the panel row is stored as NULL, not
            # as the 50 neutral fill, so the table never fabricates an input.
            present = key in r.get("factors", {})
            row[f"f_{short}"] = f.get("score") if present else None
            row[f"w_{short}"] = weights[key]
            row[f"c_{short}"] = f.get("contribution") if present else None
        for h in (5, 20, 60):
            row[f"spy_{h}d"] = r.get("fwd", {}).get(h)
            row[f"dd_{h}d"] = r.get("maxdd", {}).get(h)
            row[f"label_{h}d"] = r.get("realized_label", {}).get(h)
        rows.append(row)
    return rows


def sector_attribution_rows(panel: Sequence[dict]) -> list[dict]:
    """One storable row per (origin, sector), extending aegis_sector_backtest's grain."""
    rows = []
    for r in panel:
        ratings = r.get("ratings", {})
        if not ratings:
            continue
        ranks = sector_rank(ratings)
        n_sectors = len(ratings)
        for ticker in sorted(ratings):
            comp = r["components"].get(ticker, {})
            rating = ratings[ticker]
            raw = SECTOR_RATING_BASE + sum(comp.values()) if comp else None
            row: dict[str, Any] = {
                "date": r["origin"],
                "sector": _sector_name(ticker),
                "ticker": ticker,
                "score": r.get("regime_score"),
                "regime_score": r.get("regime_score"),
                "regime_label": r.get("regime_label"),
                "rating": rating,
                "rating_raw": _round(raw, 4) if raw is not None else None,
                "clip_adj": _round(rating - raw, 6) if raw is not None else None,
                "rank": ranks[ticker],
                "n_sectors": n_sectors,
            }
            for key, col in SECTOR_COMPONENT_COLUMNS.items():
                row[col] = _round(comp[key], 6) if key in comp else None
            for h in (5, 20, 60):
                per_ticker = r.get("excess", {}).get(h, {})
                bench = r.get("spy", {}).get(h)
                excess = per_ticker.get(ticker)
                row[f"spy_{h}d"] = bench
                # sec_Xd is stored as the absolute sector return so the legacy
                # columns keep their original meaning; excess = sec - spy.
                row[f"sec_{h}d"] = (excess + bench) if excess is not None and bench is not None else None
            rows.append(row)
    return rows


def load_regime_rows(engine) -> list[dict]:
    """Read the stored regime attribution set back into panel shape."""
    from sqlalchemy import text
    sql = text(f"SELECT * FROM {REGIME_ATTR_TABLE} ORDER BY date")
    with engine.connect() as conn:
        recs = [dict(r._mapping) for r in conn.execute(sql)]
    panel = []
    for rec in recs:
        factors, fwd, dd, labels = {}, {}, {}, {}
        for key, short in REGIME_FACTOR_COLUMNS.items():
            val = rec.get(f"f_{short}")
            if val is not None:
                factors[key] = float(val)
        for h in (5, 20, 60):
            if rec.get(f"spy_{h}d") is not None:
                fwd[h] = float(rec[f"spy_{h}d"])
            if rec.get(f"dd_{h}d") is not None:
                dd[h] = float(rec[f"dd_{h}d"])
            labels[h] = rec.get(f"label_{h}d")
        panel.append({
            "origin": rec["date"],
            "score": float(rec["score"]) if rec.get("score") is not None else 50.0,
            "label": rec.get("regime_label") or "",
            "phase": rec.get("phase") or "",
            "confidence": float(rec.get("confidence") or 0.0),
            "factors": factors,
            "fwd": fwd,
            "maxdd": dd,
            "realized_label": labels,
            "confidence_stored": True,
        })
    return panel


def load_sector_rows(engine) -> list[dict]:
    """Read the extended sector table back into panel shape."""
    from sqlalchemy import text
    sql = text(f"""
        SELECT date, ticker, rating, rank, regime_score, regime_label,
               spy_5d, spy_20d, spy_60d, sec_5d, sec_20d, sec_60d,
               {", ".join(SECTOR_COMPONENT_COLUMNS.values())}
        FROM {SECTOR_BACKTEST_TABLE}
        WHERE rating IS NOT NULL
        ORDER BY date, ticker
    """)
    with engine.connect() as conn:
        recs = [dict(r._mapping) for r in conn.execute(sql)]
    by_date: dict[date, dict] = {}
    for rec in recs:
        d = rec["date"]
        row = by_date.setdefault(d, {
            "origin": d, "ratings": {}, "components": {},
            "excess": {5: {}, 20: {}, 60: {}}, "spy": {},
            "regime_score": float(rec.get("regime_score") or 50.0),
            "regime_label": rec.get("regime_label") or "",
        })
        ticker = rec["ticker"]
        row["ratings"][ticker] = float(rec["rating"])
        comp = {}
        for key, col in SECTOR_COMPONENT_COLUMNS.items():
            if rec.get(col) is not None:
                comp[key] = float(rec[col])
        if comp:
            row["components"][ticker] = comp
        for h in (5, 20, 60):
            spy_v, sec_v = rec.get(f"spy_{h}d"), rec.get(f"sec_{h}d")
            if spy_v is not None:
                row["spy"][h] = float(spy_v)
            if spy_v is not None and sec_v is not None:
                row["excess"][h][ticker] = _round(float(sec_v) - float(spy_v), 4)
    return [by_date[d] for d in sorted(by_date)]


def setup_attribution_rows_schema(engine) -> None:
    """Create/patch the row-level dataset. Additive only; safe to re-run."""
    from sqlalchemy import text
    ddl = attribution_ddl()
    with engine.begin() as conn:
        for stmt in ddl["regime"]:
            conn.execute(text(stmt))
        for stmt in ddl["sector"]:
            conn.execute(text(stmt))


def store_regime_attribution_rows(engine, rows: Sequence[dict]) -> int:
    if not rows:
        return 0
    from sqlalchemy import text
    cols = list(rows[0].keys())
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "date")
    sql = text(f"""
        INSERT INTO {REGIME_ATTR_TABLE} ({", ".join(cols)})
        VALUES ({", ".join(":" + c for c in cols)})
        ON CONFLICT (date) DO UPDATE SET {updates}, updated_at = now();
    """)
    with engine.begin() as conn:
        conn.execute(sql, list(rows))
    return len(rows)


def store_sector_attribution_rows(engine, rows: Sequence[dict]) -> int:
    if not rows:
        return 0
    from sqlalchemy import text
    cols = list(rows[0].keys())
    updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in ("date", "sector"))
    sql = text(f"""
        INSERT INTO {SECTOR_BACKTEST_TABLE} ({", ".join(cols)})
        VALUES ({", ".join(":" + c for c in cols)})
        ON CONFLICT (date, sector) DO UPDATE SET {updates};
    """)
    with engine.begin() as conn:
        conn.execute(sql, list(rows))
    return len(rows)


# --------------------------------------------------------------------------- #
#  Weight proposal from the stored dataset
# --------------------------------------------------------------------------- #
WEIGHT_DECISION_GATES = {
    "min_blocks": 2,        # one block cannot show consistency
    "min_gain": 0.02,       # IC improvement must clear noise-level drift
    "min_win_share": 2 / 3,  # proposed must win in most blocks, not on average
}


def weight_decision(per_block: Sequence[dict], base_key: str, prop_key: str,
                    min_oos: int = 10,
                    gates: Optional[dict] = None) -> dict:
    """Gate a weight change on sample size, effect size and block consistency.

    A mean-IC comparison alone will happily bless a corner solution fitted on a
    handful of OOS dates. Every gate must pass before a change is proposed.
    """
    g = dict(WEIGHT_DECISION_GATES, **(gates or {}))
    blocks = [b for b in per_block
              if b.get(base_key) is not None and b.get(prop_key) is not None]
    n_oos = sum(b.get("n_eval", 0) for b in per_block)
    base = _mean([b[base_key] for b in blocks]) if blocks else None
    prop = _mean([b[prop_key] for b in blocks]) if blocks else None
    wins = sum(1 for b in blocks if b[prop_key] > b[base_key])
    win_share = wins / len(blocks) if blocks else 0.0
    reasons = []
    if len(blocks) < g["min_blocks"]:
        reasons.append(f"only {len(blocks)} evaluable block(s)")
    if n_oos < min_oos:
        reasons.append(f"only {n_oos} OOS dates")
    if base is None or prop is None:
        reasons.append("no IC available")
    else:
        if prop - base < g["min_gain"]:
            reasons.append(f"IC gain {prop - base:+.4f} < {g['min_gain']:.2f}")
    if blocks and win_share < g["min_win_share"]:
        reasons.append(f"proposed wins in only {wins}/{len(blocks)} blocks")
    return {
        "decision": ("proposed beats baseline OOS" if not reasons
                     else "inconclusive or non-improving OOS evidence — do not change weights"),
        "reasons": reasons,
        "n_blocks": len(blocks),
        "n_oos": n_oos,
        "ic_baseline": _round(base, 6) if base is not None else None,
        "ic_proposed": _round(prop, 6) if prop is not None else None,
        "ic_gain": _round(prop - base, 6) if base is not None and prop is not None else None,
        "blocks_won": wins,
        "gates": g,
    }

def suggest_regime_weights(engine, horizon: int = 20, n_blocks: int = 4,
                           min_train: int = 20) -> dict:
    """Propose regime weights from stored rows, validated walk-forward.

    Returns the proposed vector alongside the production vector and the OOS IC of
    both, so a weight change can be judged on out-of-sample evidence rather than
    on the same rows the weights were fitted on.
    """
    panel = load_regime_rows(engine)
    if not panel:
        return {"error": f"no rows in {REGIME_ATTR_TABLE}; run `store` first"}
    keys = [k for k, _, _ in REGIME_FACTORS]
    origins = [r["origin"] for r in panel]
    blocks = walk_forward_blocks(origins, n_blocks, min_train)
    per_block = []
    for blk in blocks:
        train_dates = purge_before(blk["train"], blk["eval"][0] if blk["eval"] else None, horizon)
        train = _rows_for(panel, horizon, train_dates)
        evals = _rows_for(panel, horizon, non_overlapping(blk["eval"], horizon))
        if len(train) < min_train or not evals:
            continue
        w = fit_ic_weights(train, keys, horizon)
        res = _regime_challengers(train, evals, horizon)
        per_block.append({
            "block": blk["block"], "n_train": len(train), "n_eval": len(evals),
            "proposed_weights": w,
            "ic_production": res["production"]["rank_ic"],
            "ic_proposed": res["ic_weighted_regularized"]["rank_ic"],
        })
    gate = weight_decision(per_block, "ic_production", "ic_proposed")
    return {
        "model": "market_regime_v2",
        "horizon": horizon,
        "n_rows": len(panel),
        "n_blocks": len(per_block),
        "production_weights": regime_factor_weights(),
        "proposed_weights": per_block[-1]["proposed_weights"] if per_block else None,
        "mean_ic_production": gate["ic_baseline"],
        "mean_ic_proposed": gate["ic_proposed"],
        "n_oos": gate["n_oos"],
        "per_block": per_block,
        "gate": gate,
        "decision": gate["decision"],
    }


def suggest_sector_weights(engine, horizon: int = 20, n_blocks: int = 4,
                           min_train: int = 12) -> dict:
    """Propose sector component weights from the stored rows, validated walk-forward."""
    panel = load_sector_rows(engine)
    if not panel:
        return {"error": f"no rows in {SECTOR_BACKTEST_TABLE}; run `store` first"}
    budgets = {k: b for k, b, _ in SECTOR_COMPONENTS}
    origins = [r["origin"] for r in panel]
    blocks = walk_forward_blocks(origins, n_blocks, min_train)
    per_block = []
    for blk in blocks:
        train_dates = purge_before(blk["train"], blk["eval"][0] if blk["eval"] else None, horizon)
        train = _sector_rows_for(panel, horizon, train_dates)
        evals = _sector_rows_for(panel, horizon, non_overlapping(blk["eval"], horizon))
        if len(train) < min_train or not evals:
            continue
        w = fit_component_weights(train, horizon)

        def weighted(row: dict, _w=w) -> dict:
            out = {}
            for t, comp in row["components"].items():
                z = sum(_w.get(k, 0.0) * (comp[k] / budgets[k]) for k in budgets)
                out[t] = _round(_clamp(SECTOR_RATING_BASE + 50.0 * z), 4)
            return out

        base = _sector_metrics(evals, horizon)
        prop = _sector_metrics(evals, horizon, ratings_fn=weighted)
        per_block.append({
            "block": blk["block"], "n_train": len(train), "n_eval": len(evals),
            "proposed_weights": w,
            "ic_baseline": base["ic"]["mean_ic"], "ic_proposed": prop["ic"]["mean_ic"],
            "spread_baseline": base["top_bottom_spread_bps"],
            "spread_proposed": prop["top_bottom_spread_bps"],
        })
    gate = weight_decision(per_block, "ic_baseline", "ic_proposed")
    return {
        "model": "rate_sector",
        "horizon": horizon,
        "n_rows": len(panel),
        "n_blocks": len(per_block),
        "production_budgets": budgets,
        "proposed_weights": per_block[-1]["proposed_weights"] if per_block else None,
        "mean_ic_baseline": gate["ic_baseline"],
        "mean_ic_proposed": gate["ic_proposed"],
        "n_oos": gate["n_oos"],
        "per_block": per_block,
        "gate": gate,
        "decision": gate["decision"],
    }


# --------------------------------------------------------------------------- #
#  Optional storage (opt-in -- additive table, no production table touched)
# --------------------------------------------------------------------------- #
def setup_attribution_schema(engine) -> None:
    from sqlalchemy import text
    sql = f"""
    CREATE TABLE IF NOT EXISTS {ATTRIBUTION_TABLE} (
        run_id        uuid PRIMARY KEY,
        created_at    timestamptz DEFAULT now(),
        model         text,
        window_start  date,
        window_end    date,
        n_origins     int,
        payload       jsonb
    );
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


def store_attribution(engine, result: dict) -> str:
    """Persist one analysis payload. Additive table only; never touches production."""
    import hashlib
    import uuid
    from sqlalchemy import text
    setup_attribution_schema(engine)
    window = result.get("window", {})
    key = json.dumps([result.get("model"), window, result.get("n_origins")], sort_keys=True)
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "aegis_factor_attribution:" +
                            hashlib.md5(key.encode()).hexdigest()))
    sql = text(f"""
        INSERT INTO {ATTRIBUTION_TABLE}
            (run_id, model, window_start, window_end, n_origins, payload)
        VALUES (:run_id, :model, :ws, :we, :n, CAST(:payload AS jsonb))
        ON CONFLICT (run_id) DO UPDATE SET payload = EXCLUDED.payload;
    """)
    with engine.begin() as conn:
        conn.execute(sql, {
            "run_id": run_id, "model": result.get("model"),
            "ws": window.get("start"), "we": window.get("end"),
            "n": result.get("n_origins"),
            "payload": json.dumps(result, default=str),
        })
    return run_id


# --------------------------------------------------------------------------- #
#  Self-test (no DB, no pandas) -- mirrors aegis_backtest.self_test
# --------------------------------------------------------------------------- #
def self_test() -> int:
    """Synthetic end-to-end check of attribution identities and metric plumbing."""
    # Factor scores chosen so the weighted sum is exactly 60.0:
    # 0.25*70 + 0.20*65 + 0.20*55 + 0.15*60 + 0.10*45 + 0.10*50 = 60.0
    regime = {
        "bullish_score": 60.0, "market_regime": "risk_on", "market_phase": "mid_bull",
        "confidence": 0.20, "technical_score": 70.0, "momentum_score": 65.0,
        "risk_appetite_score": 55.0, "breadth_score": 60.0, "macro_score": 45.0,
        "news_score": 50.0,
    }
    att = attribute_regime(regime)
    assert abs(att["sum_contributions"] - 60.0) < 1e-6, att["sum_contributions"]
    assert abs(att["residual"]) < 1e-6, att["residual"]
    assert att["top_positive"] == "technical_score", att["top_positive"]
    assert att["top_negative"] == "macro_score", att["top_negative"]

    # prev weighted sum: 0.25*60 + 0.20*60 + 0.20*55 + 0.15*60 + 0.10*45 + 0.10*50 = 56.5
    prev = dict(regime, bullish_score=56.5, technical_score=60.0, momentum_score=60.0)
    delta = regime_delta(attribute_regime(prev), att)
    assert abs(delta["sum_delta_contributions"] - delta["delta_score"]) < 1e-4, delta
    assert delta["largest_positive"] == "technical_score", delta["largest_positive"]

    abl = ablate_regime_score(regime, "technical_score")
    assert abs(abl["delta_score"] + 0.25 * 20.0) < 1e-6, abl

    row = {"ticker": "XLK", "close": 120.0, "ma_50": 110.0, "ma_200": 100.0,
           "rsi_14": 60.0, "macd_hist": 0.002, "return_20d": 0.05, "return_60d": 0.12}
    from db_builder.aegis_backtest import rate_sector
    comp = sector_rating_components(row)
    assert abs((comp["base"] + sum(comp["components"].values()) + comp["clip_adjustment"])
               - rate_sector(row)) < 1e-9, comp

    print("SELFTEST OK: regime sum=%.4f residual=%.6f; sector rating=%.4f clipped=%s"
          % (att["sum_contributions"], att["residual"], comp["rating"], comp["clipped"]))
    return 0


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _emit(result: dict, markdown: str, out_path: Optional[str], json_path: Optional[str]) -> None:
    if json_path:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, default=str)
        print(f"json written: {json_path}")
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        print(f"markdown written: {out_path}")
    else:
        print(markdown)


def _save_to_report_dir(markdown: str, json_payload: dict, command: str,
                        report_dir: str) -> tuple[str, str]:
    """Write a timestamped copy into the repo's existing reports/ analytics folder.

    Mirrors the ``rule_based_market_update_YYYYMMDD_HHMMSS.md`` convention already
    used by the scheduled pipeline. ``reports/`` is gitignored, so this never
    dirties the working tree.
    """
    from pathlib import Path

    base = Path(report_dir)
    if not base.is_absolute():
        base = Path(__file__).resolve().parents[2] / base
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = base / f"aegis_factor_attribution_{command}_{stamp}.md"
    json_path = base / f"aegis_factor_attribution_{command}_{stamp}.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(json_payload, indent=2, default=str), encoding="utf-8")
    return str(md_path), str(json_path)


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "selftest":
        return self_test()
    if cmd == "ddl":
        ddl = attribution_ddl()
        for group in ("regime", "sector"):
            for stmt in ddl[group]:
                print(stmt.rstrip().rstrip(";") + ";\n")
        return 0

    start = end = out = js = None
    report_dir: Optional[str] = "reports"
    step, blocks, cost_bps = 7, 4, TRANSACTION_COST_BPS
    store = False
    dry_run = False
    horizon = 20
    args = argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--start":
            start = args[i + 1]; i += 2
        elif a == "--end":
            end = args[i + 1]; i += 2
        elif a == "--step":
            step = int(args[i + 1]); i += 2
        elif a == "--blocks":
            blocks = int(args[i + 1]); i += 2
        elif a == "--cost-bps":
            cost_bps = float(args[i + 1]); i += 2
        elif a == "--out":
            out = args[i + 1]; i += 2
        elif a == "--json":
            js = args[i + 1]; i += 2
        elif a == "--report-dir":
            report_dir = args[i + 1]; i += 2
        elif a == "--horizon":
            horizon = int(args[i + 1]); i += 2
        elif a == "--dry-run":
            dry_run = True; i += 1
        elif a == "--no-save":
            report_dir = None; i += 1
        elif a == "--store":
            store = True; i += 1
        else:
            i += 1
    if cmd == "weights":
        # Reads the stored dataset only -- no --start/--end needed.
        from db_builder.config import local_engine
        engine = local_engine()
        horizons = (5, 20, 60) if horizon in (0, 20) else (horizon,)
        result = {
            "regime": [suggest_regime_weights(engine, h, n_blocks=blocks) for h in horizons],
            "sector": [suggest_sector_weights(engine, h, n_blocks=blocks) for h in horizons],
        }
        print(json.dumps(result, indent=2, default=str))
        return 0
    if cmd not in ("regime", "sector", "all", "store"):
        print(f"unknown command {cmd}; use 'regime', 'sector', 'all', 'store', 'weights', "
              "'ddl' or 'selftest'")
        return 2
    if not start or not end:
        print("requires --start YYYY-MM-DD --end YYYY-MM-DD")
        return 2

    from db_builder.config import local_engine
    engine = local_engine()
    start_d, end_d = _parse_date(start), _parse_date(end)
    sections = []
    payload: dict[str, Any] = {}

    if cmd in ("regime", "all"):
        res = run_regime_analysis(engine, start_d, end_d, step, n_blocks=blocks)
        payload["regime"] = res
        sections.append(render_regime_markdown(res))
        if store:
            payload["regime_run_id"] = store_attribution(engine, res)
    if cmd in ("sector", "all"):
        res = run_sector_analysis(engine, start_d, end_d, step, n_blocks=blocks)
        payload["sector"] = res
        sections.append(render_sector_markdown(res))
        if store:
            payload["sector_run_id"] = store_attribution(engine, res)

    if cmd == "store":
        # Row-level dataset. --dry-run validates the payload (counts + identities)
        # without writing and without issuing any DDL.
        regime_panel = build_regime_panel(engine, start_d, end_d, step)
        sector_panel = build_sector_panel(engine, start_d, end_d, step)
        reg_rows = regime_attribution_rows(regime_panel)
        sec_rows = sector_attribution_rows(sector_panel)
        report = {
            "dry_run": dry_run,
            "regime_rows": len(reg_rows),
            "sector_rows": len(sec_rows),
            "regime_dates": [str(r["date"]) for r in reg_rows[:3]],
            "sector_dates": sorted({str(r["date"]) for r in sec_rows})[:3],
        }
        # Identity check: contributions must reconcile to the stored score, and
        # components + clip must reconcile to the stored rating. A row that fails
        # is not written, because a broken identity silently poisons every later
        # weight decision.
        bad_regime = [r["date"] for r in reg_rows
                      if abs(sum(r[f"c_{s}"] or 0.0 for s in REGIME_FACTOR_COLUMNS.values())
                             - (r["score"] or 0.0)) > 1e-3]
        bad_sector = [(r["date"], r["ticker"]) for r in sec_rows
                      if r["rating_raw"] is not None
                      and abs(sum(r[c] or 0.0 for c in SECTOR_COMPONENT_COLUMNS.values())
                              + SECTOR_RATING_BASE - r["rating_raw"]) > 1e-3]
        report["identity_failures"] = {"regime": len(bad_regime), "sector": len(bad_sector)}
        if dry_run:
            print(json.dumps(report, indent=2, default=str))
            return 0 if not bad_regime and not bad_sector else 1
        setup_attribution_rows_schema(engine)
        report["regime_written"] = store_regime_attribution_rows(engine, reg_rows)
        report["sector_written"] = store_sector_attribution_rows(engine, sec_rows)
        print(json.dumps(report, indent=2, default=str))
        return 0

    markdown = "\n\n---\n\n".join(sections)
    _emit(payload, markdown, out, js)
    if report_dir:
        md_saved, json_saved = _save_to_report_dir(markdown, payload, cmd, report_dir)
        print(f"report saved: {md_saved}")
        print(f"report json:  {json_saved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
