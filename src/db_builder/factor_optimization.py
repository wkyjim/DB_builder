"""AEGIS factor composition + weight search for market_regime_v2 and rate_sector.

Answers: *which factors should be in the model, and at what weight*, judged on
out-of-sample evidence rather than on the sample the weights were fitted on.

Method
------
1. **Impact diagnostics** per factor: univariate rank IC, incremental IC (full
   model minus the model without that factor), and stability across sub-periods.
2. **Composition search**: every non-empty subset of the available factors is
   scored under three weighting schemes:
     * ``equal``     -- 1/n, the honest "no skill" composition baseline
     * ``ic_pos``    -- IC-proportional, non-negative, shrunk 50% toward equal,
                        hard cap/floor (``normalize_capped``)
     * ``ic_signed`` -- signed IC-proportional (a factor that anti-predicts gets a
                        negative weight instead of being floored to zero), shrunk
                        toward equal, capped in absolute value
3. **Nested validation**: candidates are ranked on all walk-forward blocks except
   the last two; the last two are held out as confirmation and never used for
   selection. Every block fits weights on purged TRAIN dates only and scores
   non-overlapping EVAL dates.
4. **Verdict**: a composition is only recommended if it beats both production and
   the equal-weight baseline on the confirmation blocks, by a margin that clears
   the same gates used for single weight changes.

Read-only. Nothing here writes to the database or changes a production score.
"""
from __future__ import annotations

import itertools
import json
import math
from datetime import date
from typing import Any, Optional, Sequence

from db_builder.aegis_backtest import spearman_rho
from db_builder.factor_attribution import (
    REGIME_FACTORS,
    SECTOR_COMPONENTS,
    SECTOR_RATING_BASE,
    _mean,
    _round,
    fit_component_weights,
    fit_ic_weights,
    load_regime_rows,
    load_sector_rows,
    non_overlapping,
    purge_before,
    regime_factor_weights,
    walk_forward_blocks,
    weight_decision,
)

REGIME_KEYS = tuple(k for k, _, _ in REGIME_FACTORS)
SECTOR_KEYS = tuple(k for k, _, _ in SECTOR_COMPONENTS)
SECTOR_BUDGETS = {k: b for k, b, _ in SECTOR_COMPONENTS}
SCHEMES = ("equal", "ic_pos", "ic_signed")
MAX_ABS_WEIGHT = 0.50


# --------------------------------------------------------------------------- #
#  Candidate construction
# --------------------------------------------------------------------------- #
def subsets(keys: Sequence[str], min_size: int = 2) -> list[tuple[str, ...]]:
    """Every subset of at least ``min_size`` factors (single-factor models excluded)."""
    out = []
    for r in range(min_size, len(keys) + 1):
        out.extend(itertools.combinations(sorted(keys), r))
    return out


def _center(ics: dict[str, float]) -> dict[str, float]:
    m = _mean(list(ics.values()))
    return {k: v - m for k, v in ics.items()}


def _l1_normalize(w: dict[str, float]) -> dict[str, float]:
    total = sum(abs(v) for v in w.values())
    if total <= 0:
        return {k: 0.0 for k in w}
    return {k: v / total for k, v in w.items()}


def signed_weights(ics: dict[str, float], shrink: float = 0.5,
                   cap: float = MAX_ABS_WEIGHT) -> dict[str, float]:
    """Signed IC-proportional weights: an anti-predictive factor gets a negative weight.

    Centering first makes the vector a long/short blend whose absolute level does
    not matter for ranking; ``shrink`` pulls it toward equal weight, and the cap
    keeps any single factor from dominating.
    """
    keys = list(ics)
    if not keys:
        return {}
    equal = 1.0 / len(keys)
    centered = _l1_normalize(_center(ics))
    w = {k: shrink * equal + (1 - shrink) * centered.get(k, 0.0) for k in keys}
    w = {k: max(-cap, min(cap, v)) for k, v in w.items()}
    total = sum(abs(v) for v in w.values())
    return {k: v / total for k, v in w.items()} if total else {k: equal for k in keys}


def _regime_ics(rows: Sequence[dict], keys: Sequence[str], horizon: int) -> dict[str, float]:
    ys = [r["fwd"][horizon] for r in rows if r.get("fwd", {}).get(horizon) is not None]
    ics = {}
    for key in keys:
        xs = [r["factors"][key] for r in rows if r.get("fwd", {}).get(horizon) is not None
              and key in r.get("factors", {})]
        ics[key] = spearman_rho(xs, ys) if len(xs) >= 5 and len(xs) == len(ys) else 0.0
    return ics


def _sector_ics(rows: Sequence[dict], keys: Sequence[str], horizon: int) -> dict[str, float]:
    ics = {}
    for key in keys:
        xs, ys = [], []
        for row in rows:
            excess = row.get("excess", {}).get(horizon)
            if not excess:
                continue
            for ticker, comp in row.get("components", {}).items():
                if ticker in excess and key in comp:
                    xs.append(comp[key])
                    ys.append(excess[ticker])
        ics[key] = spearman_rho(xs, ys) if len(xs) >= 10 else 0.0
    return ics


def fit_candidate(train: Sequence[dict], keys: Sequence[str], horizon: int,
                  scheme: str, kind: str) -> dict[str, float]:
    """Weights for one (subset, scheme) fitted on TRAIN rows only."""
    if scheme == "equal":
        return {k: 1.0 / len(keys) for k in keys}
    if scheme == "ic_pos":
        if kind == "regime":
            return fit_ic_weights(train, keys, horizon)
        # component fitter returns weights over ALL components; restrict + renormalize
        full = fit_component_weights(train, horizon)
        sub = {k: full.get(k, 0.0) for k in keys}
        total = sum(sub.values())
        return {k: v / total for k, v in sub.items()} if total else {k: 1.0 / len(keys) for k in keys}
    if scheme == "ic_signed":
        ics = (_regime_ics if kind == "regime" else _sector_ics)(train, keys, horizon)
        return signed_weights({k: ics.get(k, 0.0) for k in keys})
    raise ValueError(f"unknown scheme {scheme}")


# --------------------------------------------------------------------------- #
#  Scoring a candidate
# --------------------------------------------------------------------------- #
def regime_score(row: dict, weights: dict[str, float]) -> float:
    """Composite score with arbitrary (possibly negative) weights."""
    return _round(sum(w * row["factors"].get(k, 50.0) for k, w in weights.items()), 6)


def sector_ratings(row: dict, weights: dict[str, float],
                   clip: bool = False) -> dict[str, float]:
    """Rating from component z-scores; ``clip`` reproduces production's [0,100] bound."""
    out = {}
    for ticker, comp in row.get("components", {}).items():
        z = sum(w * (comp.get(k, 0.0) / SECTOR_BUDGETS[k]) for k, w in weights.items())
        rating = SECTOR_RATING_BASE + 50.0 * z
        out[ticker] = _round(max(0.0, min(100.0, rating)) if clip else rating, 4)
    return out


def evaluate_block(rows_train: Sequence[dict], rows_eval: Sequence[dict], keys: Sequence[str],
                   horizon: int, scheme: str, kind: str,
                   clip: bool = False) -> dict:
    """Fit on train, score eval. Returns IC + sample size for one walk-forward block."""
    if not rows_train or not rows_eval:
        return {"ic": None, "n_eval": 0, "weights": None}
    weights = fit_candidate(rows_train, keys, horizon, scheme, kind)
    if kind == "regime":
        scores, rets = [], []
        for r in rows_eval:
            if r.get("fwd", {}).get(horizon) is None:
                continue
            scores.append(regime_score(r, weights))
            rets.append(r["fwd"][horizon])
        ic = spearman_rho(scores, rets) if len(scores) >= 3 else None
        return {"ic": ic, "n_eval": len(scores), "weights": weights}
    ics = []
    for r in rows_eval:
        excess = r.get("excess", {}).get(horizon)
        if not excess:
            continue
        ratings = sector_ratings(r, weights, clip=clip)
        tickers = [t for t in ratings if t in excess]
        if len(tickers) < 3:
            continue
        ics.append(spearman_rho([ratings[t] for t in tickers], [excess[t] for t in tickers]))
    return {"ic": _mean(ics) if ics else None, "n_eval": len(ics), "weights": weights}


def _rows_for(panel: Sequence[dict], horizon: int, dates: Sequence[date], kind: str) -> list[dict]:
    keep = set(dates)
    key = "fwd" if kind == "regime" else "excess"
    return [r for r in panel
            if r["origin"] in keep and r.get(key, {}).get(horizon)]


def evaluate_candidate(panel: Sequence[dict], blocks: Sequence[dict], keys: Sequence[str],
                       horizon: int, scheme: str, kind: str,
                       clip: bool = False) -> dict:
    per_block = []
    for blk in blocks:
        train_dates = purge_before(blk["train"], blk["eval"][0] if blk["eval"] else None, horizon)
        train = _rows_for(panel, horizon, train_dates, kind)
        evals = _rows_for(panel, horizon, non_overlapping(blk["eval"], horizon), kind)
        if len(train) < 8:
            continue
        res = evaluate_block(train, evals, keys, horizon, scheme, kind, clip=clip)
        if res["ic"] is None:
            continue
        per_block.append({"block": blk["block"], "n_train": len(train),
                          "n_eval": res["n_eval"], "ic": res["ic"],
                          "weights": res["weights"]})
    ics = [b["ic"] for b in per_block]
    return {
        "per_block": per_block,
        "mean_ic": _round(_mean(ics), 6) if ics else None,
        "ic_std": _round(_std(ics), 6) if len(ics) > 1 else None,
        "n_blocks": len(per_block),
        "n_oos": sum(b["n_eval"] for b in per_block),
    }


def _std(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


# --------------------------------------------------------------------------- #
#  Factor impact diagnostics
# --------------------------------------------------------------------------- #
def factor_impact(panel: Sequence[dict], keys: Sequence[str], horizons: Sequence[int],
                  kind: str) -> dict:
    """Univariate IC, incremental IC (leave-one-out) and sub-period stability."""
    out: dict[str, Any] = {"horizons": {}}
    for h in horizons:
        all_dates = [r["origin"] for r in panel]
        rows = _rows_for(panel, h, non_overlapping(all_dates, h), kind)
        if not rows:
            continue
        full = _candidate_ic(rows, keys, h, kind)
        uni = (_regime_ics if kind == "regime" else _sector_ics)(rows, keys, h)
        # split the sample in half chronologically to expose instability
        mid = len(rows) // 2
        first = (_regime_ics if kind == "regime" else _sector_ics)(rows[:mid], keys, h)
        second = (_regime_ics if kind == "regime" else _sector_ics)(rows[mid:], keys, h)
        per_factor = {}
        for key in keys:
            without = tuple(k for k in keys if k != key)
            wo = _candidate_ic(rows, without, h, kind) if len(without) >= 2 else None
            per_factor[key] = {
                "univariate_ic": _round(uni.get(key, 0.0), 6),
                "incremental_ic": (_round(full - wo, 6)
                                   if full is not None and wo is not None else None),
                "ic_first_half": _round(first.get(key, 0.0), 6),
                "ic_second_half": _round(second.get(key, 0.0), 6),
                "stable_sign": (first.get(key, 0.0) * second.get(key, 0.0) > 0),
            }
        corr = []
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                corr.append({"a": a, "b": b, "r": _round(_factor_corr(rows, a, b, h, kind), 4)})
        corr.sort(key=lambda p: -abs(p["r"]))
        out["horizons"][h] = {
            "n_dates": len(rows),
            "equal_weight_all_ic": _round(full, 6) if full is not None else None,
            "factors": per_factor,
            "top_correlations": corr[:4],
        }
    return out


def _candidate_ic(rows: Sequence[dict], keys: Sequence[str], horizon: int, kind: str) -> Optional[float]:
    """Equal-weight blend IC (no fitting) -- a fitting-free reference point."""
    w = {k: 1.0 / len(keys) for k in keys}
    res = evaluate_block(rows, rows, keys, horizon, "equal", kind)
    return res["ic"] if res["ic"] is not None else None


def _factor_corr(rows: Sequence[dict], a: str, b: str, horizon: int, kind: str) -> float:
    if kind == "regime":
        pairs = [(r["factors"].get(a), r["factors"].get(b)) for r in rows]
    else:
        pairs = []
        for r in rows:
            for _t, comp in r.get("components", {}).items():
                if a in comp and b in comp:
                    pairs.append((comp[a], comp[b]))
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    if len(pairs) < 5:
        return 0.0
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    mx, my = _mean(xs), _mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


# --------------------------------------------------------------------------- #
#  Search
# --------------------------------------------------------------------------- #
def search(panel: Sequence[dict], keys: Sequence[str], horizon: int, kind: str,
           n_blocks: int = 5, max_candidates: int = 200) -> dict:
    """Rank every (subset, scheme) on selection blocks; confirm on held-out blocks."""
    origins = [r["origin"] for r in panel]
    blocks = walk_forward_blocks(origins, n_blocks, min_train=20)
    if len(blocks) < 3:
        return {"error": f"only {len(blocks)} walk-forward block(s); need >= 3",
                "n_blocks": len(blocks)}
    selection, confirmation = blocks[:-2], blocks[-2:]
    results = []
    for subset in subsets(keys, min_size=2):
        for scheme in SCHEMES:
            sel = evaluate_candidate(panel, selection, subset, horizon, scheme, kind)
            if sel["mean_ic"] is None:
                continue
            conf = evaluate_candidate(panel, confirmation, subset, horizon, scheme, kind)
            results.append({
                "factors": list(subset),
                "n_factors": len(subset),
                "scheme": scheme,
                "selection_ic": sel["mean_ic"],
                "selection_blocks": sel["n_blocks"],
                "confirmation_ic": conf["mean_ic"],
                "confirmation_blocks": conf["n_blocks"],
                "n_oos": conf["n_oos"],
                "weights": (conf["per_block"][-1]["weights"] if conf["per_block"]
                            else (sel["per_block"][-1]["weights"] if sel["per_block"] else None)),
                "confirmation_block_ics": [b["ic"] for b in conf["per_block"]],
            })
    results.sort(key=lambda r: -(r["selection_ic"] if r["selection_ic"] is not None else -9))
    return {
        "horizon": horizon,
        "kind": kind,
        "n_blocks": len(blocks),
        "n_selection_blocks": len(selection),
        "n_confirmation_blocks": len(confirmation),
        "n_candidates": len(results),
        "top": results[:10],
        "all": results[:max_candidates],
        "baselines": _baselines(panel, selection, confirmation, keys, horizon, kind),
    }


def _baselines(panel, selection, confirmation, keys, horizon, kind) -> dict:
    """Production-equivalent and equal-weight-all references on the same blocks."""
    out = {}
    if kind == "regime":
        prod = {k: regime_factor_weights()[k] for k in keys}
        eq = {k: 1.0 / len(keys) for k in keys}
        inverted = {k: -v for k, v in prod.items()}
        for name, w in (("production", prod), ("equal_weight_all", eq),
                        ("production_inverted", inverted)):
            sel = _eval_fixed(panel, selection, w, horizon, "regime")
            conf = _eval_fixed(panel, confirmation, w, horizon, "regime")
            out[name] = {
                "selection_ic": sel,
                "confirmation_ic": conf,
                "confirmation_block_ics": _eval_fixed_blocks(panel, confirmation, w, horizon, "regime"),
            }
    else:
        for name in ("production", "equal_weight_all", "ic_pos_all"):
            sel = evaluate_candidate(panel, selection, keys, horizon,
                                     "ic_pos" if name == "ic_pos_all" else "equal", kind,
                                     clip=True)
            conf = evaluate_candidate(panel, confirmation, keys, horizon,
                                      "ic_pos" if name == "ic_pos_all" else "equal", kind,
                                      clip=True)
            out[name] = {"selection_ic": sel["mean_ic"], "confirmation_ic": conf["mean_ic"]}
        # production budget shares: each component carries its budget/70 of the total
        prod_w = {k: SECTOR_BUDGETS[k] / sum(SECTOR_BUDGETS.values()) for k in keys}
        inverted = {k: -v for k, v in prod_w.items()}
        for name, w in (("production", prod_w), ("production_inverted", inverted)):
            out[name] = {
                "selection_ic": _eval_fixed(panel, selection, w, horizon, "sector", clip=True),
                "confirmation_ic": _eval_fixed(panel, confirmation, w, horizon, "sector", clip=True),
                "confirmation_block_ics": _eval_fixed_blocks(panel, confirmation, w, horizon,
                                                             "sector", clip=True),
            }
    return out


def _eval_fixed(panel, blocks, weights, horizon, kind, clip=False) -> Optional[float]:
    ics = []
    for blk in blocks:
        evals = _rows_for(panel, horizon, non_overlapping(blk["eval"], horizon), kind)
        if not evals:
            continue
        if kind == "regime":
            scores = [regime_score(r, weights) for r in evals if r.get("fwd", {}).get(horizon)]
            rets = [r["fwd"][horizon] for r in evals if r.get("fwd", {}).get(horizon)]
            if len(scores) >= 3:
                ics.append(spearman_rho(scores, rets))
        else:
            per_date = []
            for r in evals:
                excess = r.get("excess", {}).get(horizon)
                if not excess:
                    continue
                ratings = sector_ratings(r, weights, clip=clip)
                tickers = [t for t in ratings if t in excess]
                if len(tickers) >= 3:
                    per_date.append(spearman_rho([ratings[t] for t in tickers],
                                                 [excess[t] for t in tickers]))
            if per_date:
                ics.append(_mean(per_date))
    return _round(_mean(ics), 6) if ics else None


def _eval_fixed_blocks(panel, blocks, weights, horizon, kind, clip=False) -> list[float]:
    """Per-block ICs for a fixed weight vector (the gate needs real block counts)."""
    out = []
    for blk in blocks:
        ic = _eval_fixed(panel, [blk], weights, horizon, kind, clip=clip)
        if ic is not None:
            out.append(ic)
    return out


# --------------------------------------------------------------------------- #
#  Recommendation
# --------------------------------------------------------------------------- #
def recommend(panel: Sequence[dict], keys: Sequence[str], horizon: int, kind: str,
              n_blocks: int = 5) -> dict:
    """Best composition for one horizon, gated against production and equal weight."""
    s = search(panel, keys, horizon, kind, n_blocks=n_blocks)
    if s.get("error"):
        return s
    best = s["top"][0] if s["top"] else None
    if not best:
        return {"error": "no candidate produced an IC", **s}
    prod = s["baselines"]["production"]["confirmation_ic"]
    eq = s["baselines"]["equal_weight_all"]["confirmation_ic"]
    # Round weights to 5% steps so the recommendation is implementable.
    raw_w = best["weights"] or {}
    rounded = {k: round(v / 0.05) * 0.05 for k, v in raw_w.items()}
    total = sum(abs(v) for v in rounded.values()) or 1.0
    rounded = {k: _round(v / total, 4) for k, v in rounded.items()}
    conf_ics = best["confirmation_block_ics"]
    # Gate on the REAL confirmation blocks. Feeding a single synthetic record made
    # the "min_blocks" gate fail by construction, so no recommendation could ever
    # be issued regardless of the evidence.
    prod_blocks = (s["baselines"].get("production") or {}).get("confirmation_block_ics") or []
    records = [{"n_eval": best["n_oos"], "base": base, "prop": prop}
               for base, prop in zip(prod_blocks, conf_ics)]
    if not records:
        records = [{"n_eval": best["n_oos"],
                    "base": prod if prod is not None else 0.0,
                    "prop": best["confirmation_ic"] if best["confirmation_ic"] is not None else 0.0}]
    gate = weight_decision(records, "base", "prop", min_oos=10)
    wins_both = (best["confirmation_ic"] is not None and prod is not None and eq is not None
                 and best["confirmation_ic"] > prod and best["confirmation_ic"] > eq)
    consistent = len(conf_ics) >= 2 and all(ic > 0 for ic in conf_ics)
    verdict = ("recommend" if (wins_both and consistent and not gate["reasons"])
               else "do not change production")
    return {
        "kind": kind, "horizon": horizon,
        "n_candidates": s["n_candidates"],
        "chosen": {k: best[k] for k in ("factors", "n_factors", "scheme",
                                        "selection_ic", "confirmation_ic",
                                        "confirmation_blocks", "n_oos")},
        "weights_rounded": rounded,
        "weights_raw": raw_w,
        "confirmation_block_ics": conf_ics,
        "baselines": s["baselines"],
        "gates": gate,
        "beats_production": wins_both,
        "confirmation_consistent": consistent,
        "verdict": verdict,
        "top": s["top"][:5],
    }


def run(start: Optional[date] = None, engine=None) -> dict:
    """Full study over the stored datasets."""
    if engine is None:
        from db_builder.config import local_engine
        engine = local_engine()
    regime_panel = load_regime_rows(engine)
    sector_panel = load_sector_rows(engine)
    if start:
        regime_panel = [r for r in regime_panel if r["origin"] >= start]
        sector_panel = [r for r in sector_panel if r["origin"] >= start]
    out: dict[str, Any] = {
        "n_regime_dates": len(regime_panel),
        "n_sector_dates": len(sector_panel),
        "regime": {}, "sector": {},
    }
    for h in (5, 20, 60):
        out["regime"][h] = {
            "impact": factor_impact(regime_panel, REGIME_KEYS, [h], "regime")["horizons"].get(h),
            "recommendation": recommend(regime_panel, REGIME_KEYS, h, "regime"),
        }
        out["sector"][h] = {
            "impact": factor_impact(sector_panel, SECTOR_KEYS, [h], "sector")["horizons"].get(h),
            "recommendation": recommend(sector_panel, SECTOR_KEYS, h, "sector"),
        }
    return out


def _fmt(v, digits=3, suffix=""):
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return f"{v:.{digits}f}{suffix}"
    return str(v)


def render_markdown(result: dict) -> str:
    lines = [
        "# AEGIS Factor Composition & Weight Study",
        "",
        f"Stored dataset: **{result['n_regime_dates']}** regime dates, "
        f"**{result['n_sector_dates']}** sector dates.",
        "",
        "Selection ranks candidates on all walk-forward blocks except the last two; "
        "the last two are held out as confirmation and never used to choose.",
        "",
    ]
    for section, label in (("regime", "Regime (market_regime_v2)"),
                           ("sector", "Sector (rate_sector)")):
        lines += [f"## {label}", ""]
        for h, block in result[section].items():
            imp, rec = block.get("impact"), block.get("recommendation", {})
            lines += [f"### {h}D", ""]
            if imp:
                lines += ["| Factor | Univariate IC | Incremental IC | IC 1st half | IC 2nd half | Sign stable |",
                          "|---|---|---|---|---|---|"]
                for key, f in imp["factors"].items():
                    lines.append(f"| {key} | {_fmt(f['univariate_ic'])} | {_fmt(f['incremental_ic'])} | "
                                 f"{_fmt(f['ic_first_half'])} | {_fmt(f['ic_second_half'])} | "
                                 f"{_fmt(f['stable_sign'])} |")
                lines += ["", f"Equal-weight-all IC: **{_fmt(imp['equal_weight_all_ic'])}** "
                          f"on {imp['n_dates']} non-overlapping dates.", ""]
            if rec.get("error"):
                lines += [f"Search unavailable: {rec['error']}", ""]
                continue
            c = rec["chosen"]
            lines += [f"**Best composition**: {', '.join(c['factors'])} "
                      f"({c['scheme']}, {c['n_factors']} factors)",
                      f"- selection IC {_fmt(c['selection_ic'])} → confirmation IC "
                      f"**{_fmt(c['confirmation_ic'])}** over {c['n_oos']} OOS dates",
                      f"- production {_fmt(rec['baselines']['production']['confirmation_ic'])}, "
                      f"equal-weight-all {_fmt(rec['baselines']['equal_weight_all']['confirmation_ic'])} "
                      f"on the same confirmation blocks",
                      f"- weights (5% steps): " + ", ".join(
                          f"{k} {v:+.2f}" for k, v in rec["weights_rounded"].items()),
                      f"- **verdict: {rec['verdict']}**"]
            if rec["gates"].get("reasons"):
                lines.append(f"- gate notes: {'; '.join(rec['gates']['reasons'])}")
            lines.append("")
    lines += ["## Reading this",
              "",
              "- Univariate IC: the factor alone vs forward outcome (non-overlapping dates).",
              "- Incremental IC: equal-weight-all minus equal-weight-without-this-factor — "
              "what the factor adds to a blend that already contains the others.",
              "- A negative incremental IC means the factor is harming the blend, which is "
              "different from a factor that is merely weak on its own.",
              "- Selection IC is optimistically biased by construction (it chose the winner "
              "from many candidates). Only the confirmation column is evidence.",
              "- Weights in 5% steps are rounded for implementation; the raw vector is in the JSON."]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  UST curve challenger -- 2/10/30y yields plus gold
# --------------------------------------------------------------------------- #
UST_CURVE_SYMBOLS = {
    "y2": "US2YT=X",
    "y10": "US10YT=X",
    "y30": "US30YT=X",
    "gold": "GC=F",
}
# Gold is inverted: a falling gold price counts as risk-on. Yields are used as-is;
# in this sample a rising yield co-moved with rising equities (growth-led).
UST_CHALLENGER_LEGS = (("y2", False), ("y10", False), ("y30", False), ("gold", True))
UST_LAG = 20


def _rolling_sd(values: Sequence[Optional[float]], i: int, window: int,
                min_obs: int = 20) -> Optional[float]:
    """Standard deviation of the trailing ``window`` observations up to ``i``.

    Point-in-time only: uses nothing after index ``i``, so the normalisation cannot
    leak future information into the signal.
    """
    start = max(0, i - window + 1)
    obs = [v for v in values[start:i + 1] if v is not None]
    if len(obs) < min_obs:
        return None
    mean = sum(obs) / len(obs)
    var = sum((v - mean) ** 2 for v in obs) / (len(obs) - 1)
    sd = math.sqrt(var)
    return sd if sd > 1e-12 else None


def ust_curve_signals(curve: dict[str, dict[date, float]],
                      dates: Optional[Sequence[date]] = None,
                      lag: int = UST_LAG,
                      norm_window: int = 60) -> list[dict]:
    """Change-based, volatility-normalised UST-curve composite, one row per date.

    Uses the ``lag``-trading-day *change* in each series rather than the level: the
    level ICs are contaminated by the sample's single trend, while the change ICs
    held the same sign across both halves of the sample.

    Each leg is divided by its own trailing standard deviation before averaging.
    Without that step the dollar-scaled gold leg (moves of tens of dollars) would
    swamp the percentage-scaled yield legs (moves of tenths of a percent) and the
    "composite" would silently be gold alone.

    Missing legs are skipped rather than neutral-filled; ``composite`` is None when
    no leg has enough history to normalise.
    """
    base = dates if dates is not None else (curve.get("y2") or {})
    ordered = sorted(base)
    # pre-compute the change series so the rolling sd is well defined
    changes: dict[str, list[Optional[float]]] = {}
    for name, invert in UST_CHALLENGER_LEGS:
        series = curve.get(name) or {}
        vals: list[Optional[float]] = []
        for j in range(len(ordered)):
            if j < lag:
                vals.append(None)
                continue
            now, then = series.get(ordered[j]), series.get(ordered[j - lag])
            if now is None or then is None:
                vals.append(None)
                continue
            delta = now - then
            vals.append(-delta if invert else delta)
        changes[name] = vals
    out = []
    for j, d in enumerate(ordered):
        legs: dict[str, Optional[float]] = {}
        for name, _ in UST_CHALLENGER_LEGS:
            raw = changes[name][j]
            if raw is None:
                legs[name] = None
                continue
            sd = _rolling_sd(changes[name], j, norm_window)
            legs[name] = (raw / sd) if sd else None
        vals = [v for v in legs.values() if v is not None]
        out.append({"date": d, "legs": legs,
                    "composite": (sum(vals) / len(vals)) if vals else None})
    return out


def _ic_pairs(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 11:
        return None
    return _round(spearman_rho([p[0] for p in pairs], [p[1] for p in pairs]), 6)


def ust_challenger_evaluation(curve: dict[str, dict[date, float]],
                              forward_returns: dict[date, dict],
                              production_scores: Optional[dict[date, float]] = None,
                              lag: int = UST_LAG) -> dict:
    """IC of the curve composite vs forward returns, split by horizon and by half.

    ``forward_returns`` maps date -> {5: pct, 20: pct, 60: pct}. ``production_scores``
    is optional and only used to report how much of the signal the existing model
    already contains.
    """
    sig = {s["date"]: s["composite"] for s in ust_curve_signals(curve, sorted(curve.get("y2") or {}), lag)}
    out: dict[str, Any] = {"lag": lag, "legs": [n for n, _ in UST_CHALLENGER_LEGS], "horizons": {}}
    for h in (5, 20, 60):
        ds = [d for d in sorted(sig) if sig[d] is not None and d in forward_returns
              and forward_returns[d].get(h) is not None]
        xs = [sig[d] for d in ds]
        ys = [forward_returns[d][h] for d in ds]
        half = len(ds) // 2
        out["horizons"][h] = {
            "n": len(ds),
            "ic": _ic_pairs(xs, ys),
            "ic_first_half": _ic_pairs(xs[:half], ys[:half]),
            "ic_second_half": _ic_pairs(xs[half:], ys[half:]),
        }
    if production_scores:
        ds = [d for d in sorted(sig) if sig[d] is not None and d in production_scores]
        out["corr_with_production_score"] = _ic_pairs(
            [sig[d] for d in ds], [production_scores[d] for d in ds])
        out["n_corr"] = len(ds)
    return out


def _dated_where(start: Optional[date], end: Optional[date],
                 base: Optional[list[str]] = None) -> tuple[str, dict]:
    """Build a WHERE clause without binding NULL dates.

    Passing ``None`` straight into ``:start IS NULL OR date >= :start`` leaves the
    driver unable to infer the parameter type ("could not determine data type of
    parameter"), so optional bounds are dropped from the SQL instead of bound.
    """
    where = list(base or [])
    params: dict = {}
    if start is not None:
        where.append("date >= :start")
        params["start"] = start
    if end is not None:
        where.append("date <= :end")
        params["end"] = end
    return (" WHERE " + " AND ".join(where)) if where else "", params


def load_ust_curve(engine, start: Optional[date] = None, end: Optional[date] = None) -> dict:
    """Daily 2/10/30y UST yields and gold from ``public.macro``."""
    from sqlalchemy import text
    syms = list(UST_CURVE_SYMBOLS.values())
    clause, params = _dated_where(start, end, ["symbol = ANY(:syms)", "close IS NOT NULL"])
    params["syms"] = syms
    sql = text("SELECT symbol, date, close FROM public.macro" + clause + " ORDER BY date")
    with engine.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    reverse = {v: k for k, v in UST_CURVE_SYMBOLS.items()}
    out: dict[str, dict[date, float]] = {k: {} for k in UST_CURVE_SYMBOLS}
    for sym, d, close in rows:
        name = reverse.get(sym)
        if name:
            out[name][d] = float(close)
    return out


def ust_report(engine, start: Optional[date] = None, end: Optional[date] = None,
               lag: int = UST_LAG) -> dict:
    """Load, score and compare the UST-curve challenger against production."""
    from sqlalchemy import text
    curve = load_ust_curve(engine, start, end)
    clause, params = _dated_where(start, end, ["ticker = 'SPY'"])
    with engine.connect() as conn:
        spy = {r[0]: float(r[1]) for r in conn.execute(
            text("SELECT date, close FROM public.us_equities" + clause + " ORDER BY date"),
            params).fetchall()}
    dates = sorted(spy)
    idx = {d: i for i, d in enumerate(dates)}
    fwd = {}
    for d in dates:
        i = idx[d]
        row = {}
        for h in (5, 20, 60):
            row[h] = ((spy[dates[i + h]] / spy[d] - 1) * 100) if i + h < len(dates) else None
        fwd[d] = row
    clause, params = _dated_where(start, None, [])
    with engine.connect() as conn:
        prod = {r[0]: float(r[1]) for r in conn.execute(
            text("SELECT date, score FROM public.aegis_regime_attribution" + clause + " ORDER BY date"),
            params).fetchall()}
    return {
        "kind": "ust_curve_challenger",
        "symbols": UST_CURVE_SYMBOLS,
        "evaluation": ust_challenger_evaluation(curve, fwd, prod, lag),
    }


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv if argv is not None else []
    start = None
    out_md = out_json = None
    want_ust = False
    for i, a in enumerate(argv):
        if a == "--start" and i + 1 < len(argv):
            from datetime import datetime
            start = datetime.strptime(argv[i + 1], "%Y-%m-%d").date()
        elif a == "--out" and i + 1 < len(argv):
            out_md = argv[i + 1]
        elif a == "--json" and i + 1 < len(argv):
            out_json = argv[i + 1]
        elif a == "--ust":
            want_ust = True
    result = run(start=start)
    if want_ust:
        from db_builder.config import local_engine
        result["ust_challenger"] = ust_report(local_engine(), start=start)
    md = render_markdown(result)
    if want_ust and result.get("ust_challenger"):
        ev = result["ust_challenger"]["evaluation"]
        md += "\n\n## UST curve challenger (2/10/30y yields + gold)\n\n"
        md += "| Horizon | n | IC | 1st half | 2nd half |\n|---|---|---|---|---|\n"
        for h, blk in ev["horizons"].items():
            md += (f"| {h}D | {blk['n']} | {_fmt(blk['ic'], 4)} | "
                   f"{_fmt(blk['ic_first_half'], 4)} | {_fmt(blk['ic_second_half'], 4)} |\n")
        md += f"\nCorrelation with the production composite score: {_fmt(ev.get('corr_with_production_score'), 4)}\n"
    if out_md:
        with open(out_md, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"markdown written: {out_md}")
    else:
        print(md)
    if out_json:
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, default=str)
        print(f"json written: {out_json}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
