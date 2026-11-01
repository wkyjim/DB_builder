"""Forward-signal calibration for the production regime score.

The production regime score (``rule_based_regime.compute_regime``) is a *state*
descriptor: it says how risk-on the market looks right now. It is NOT a forward
return forecast, and the OOS evidence says its LEVEL is contrarian at longer
horizons:

    2025-10-21 .. 2026-08-03, 116 daily origins (AEGIS attribution table)
    60D forward SPY return by score bucket:
        <40   +9.78%   (n=9)
        40-45  +7.80%  (n=11)
        45-50  +4.94%  (n=9)
        50-55  +4.70%  (n=13)
        55-60  +5.07%  (n=8)
        60-65  +1.92%  (n=17)
        65-70  +1.47%  (n=24)
        70+    +2.39%  (n=25)
    long-short spread (score<45 minus score>=65) = +6.75pp, t=5.62,
    block-bootstrap 95% CI [+1.92, +10.76], P(spread<=0)=0.000.

Same monotone-declining shape appears in the production engine's own weekly
sample (58 origins) and in both halves of the sample separately.

This module does NOT change the score. It only maps score -> expected forward
return with the horizon and method stated explicitly, so the report can stop
presenting "high score" as "expect up" at 20D/60D.

Design rules:
  * Pure functions only in ``expected_forward`` — no DB, no I/O, testable.
  * ``build_calibration`` reads the stored attribution table; ``load_calibration``
    reads a cached JSON snapshot. Callers decide which to use.
  * Every output carries ``method`` and ``n`` so the report can label it
    "Analyst inference/estimate (historical bucket calibration)".
  * Default ON in the report since the 116-date bootstrap validation passed;
    ``AEGIS_REPORT_FORWARD_SIGNAL=0`` is the explicit kill switch.

Known limits (do not hide):
  * Sample is a bull-market window; no bear-market stress test is possible with
    the current database (SPY rows start 2025-01-02, macro 2024-10-21).
  * 60D windows overlap heavily; the bootstrap CI accounts for block
    autocorrelation but cannot manufacture independent observations.
  * Low-score buckets hold few observations (n=8 daily, n=3 weekly).
"""
from __future__ import annotations

import json
import os
from typing import Any, Iterable

SCHEMA = "aegis-forward-signal-v1"
HORIZONS = (5, 20, 60)

# Bucket edges used by the calibration. Kept in one place so the report and the
# tests cannot drift apart.
BUCKETS: tuple[tuple[float, float], ...] = (
    (0, 40), (40, 45), (45, 50), (50, 55), (55, 60), (60, 65), (65, 70), (70, 101),
)

METHOD_LABEL = "Analyst inference/estimate (historical bucket calibration)"

LIMITATIONS = [
    "sample covers a bull-market window (2025-10-21..2026-08-03); no bear-market stress test is possible with current data",
    "60D forward windows overlap heavily; bootstrap CI is block-adjusted but effective sample size is small",
    "low-score buckets hold few observations (n=9 daily in the <40 bucket) so the contrarian edge is directionally clear but imprecise",
]


def forward_signal_enabled() -> bool:
    """Whether the forward-signal section renders in the report.

    Default ON since the 116-date bootstrap validation passed (95% CI
    [+1.92, +10.76], P(spread<=0)=0.0000). Set ``AEGIS_REPORT_FORWARD_SIGNAL=0``
    (or false/no/off) to suppress the section without a code change.
    """
    return str(os.environ.get("AEGIS_REPORT_FORWARD_SIGNAL", "1")).strip().lower() not in {
        "0", "false", "no", "off",
    }


def bucket_for(score: float) -> tuple[float, float]:
    """Return the calibration bucket edges that contain ``score``."""
    s = float(score)
    for lo, hi in BUCKETS:
        if lo <= s < hi:
            return lo, hi
    return BUCKETS[-1]


def expected_forward(score: float, horizon: int, calibration: dict | None) -> dict[str, Any]:
    """Map a regime score to an expected forward return for one horizon.

    Returns a dict with ``expected_pct`` (None when unavailable), ``direction``,
    ``n``, ``bucket``, ``method`` and ``contrarian`` (True when the calibration
    implies higher scores lead to lower forward returns at this horizon).
    """
    if horizon not in HORIZONS:
        raise ValueError("unsupported horizon %r; expected one of %r" % (horizon, HORIZONS))
    lo, hi = bucket_for(score)
    label = "%d-%d" % (lo, hi) if hi != 101 else "70+"
    base = {
        "score": round(float(score), 4),
        "horizon": horizon,
        "bucket": label,
        "method": METHOD_LABEL,
        "expected_pct": None,
        "direction": "none",
        "n": 0,
        "contrarian": None,
    }
    buckets = (calibration or {}).get("buckets") or []
    key = "mean_%dd" % horizon
    for row in buckets:
        row_lo, row_hi = row.get("lo"), row.get("hi")
        if row_lo is None or row_hi is None:
            continue
        in_bucket = (row_lo <= float(score) < row_hi)
        if in_bucket or (row_hi >= 101 and float(score) >= row_lo):
            value = row.get(key)
            base["n"] = int(row.get("n") or 0)
            if value is not None:
                base["expected_pct"] = round(float(value), 4)
                base["direction"] = "up" if float(value) > 0 else "down" if float(value) < 0 else "flat"
            break

    # Contrarian flag describes the SHAPE of the curve at this horizon, not the
    # individual bucket: when the highest bucket's mean sits below the lowest
    # bucket's mean, score level is inversely related to forward return.
    if len(buckets) >= 2:
        bottom_mean = buckets[0].get(key)
        top_mean = buckets[-1].get(key)
        if bottom_mean is not None and top_mean is not None:
            base["contrarian"] = bool(float(top_mean) < float(bottom_mean))
    return base


def build_calibration(rows: Iterable[dict], *, sample_meta: dict | None = None) -> dict[str, Any]:
    """Build a calibration snapshot from attribution rows.

    Each row needs ``score`` plus ``spy_5d``/``spy_20d``/``spy_60d`` (percent).
    Rows missing a horizon are skipped for that horizon only.
    """
    materialised = []
    for r in rows:
        score = r.get("score")
        if score is None:
            continue
        materialised.append({
            "score": float(score),
            "mean_5d": _f(r.get("spy_5d")),
            "mean_20d": _f(r.get("spy_20d")),
            "mean_60d": _f(r.get("spy_60d")),
        })

    buckets_out = []
    for lo, hi in BUCKETS:
        members = [m for m in materialised if lo <= m["score"] < hi]
        if not members:
            continue
        entry: dict[str, Any] = {"lo": lo, "hi": hi, "n": len(members)}
        for h in HORIZONS:
            vals = [m["mean_%dd" % h] for m in members if m["mean_%dd" % h] is not None]
            entry["mean_%dd" % h] = round(sum(vals) / len(vals), 4) if vals else None
        buckets_out.append(entry)

    return {
        "schema": SCHEMA,
        "sample": sample_meta or {},
        "buckets": buckets_out,
        "method": METHOD_LABEL,
        "limitations": list(LIMITATIONS),
    }


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def load_calibration(path: str | None = None) -> dict | None:
    """Load a cached calibration JSON. Returns None when absent/unreadable."""
    target = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "forward_signal_calibration.json")
    try:
        with open(target, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
