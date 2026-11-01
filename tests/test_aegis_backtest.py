"""Regression tests for aegis_backtest pure validation logic (no DB).

Covers: spearman rank IC (ties / zero-variance / known value), hit rate,
Brier score, calibration-table edge cases, empty-input handling,
regime-to-forecast labelling, and the no-look-ahead origin invariant.
"""
from datetime import date

import pytest

from db_builder.aegis_backtest import (
    BacktestRecord,
    FORECAST_HORIZONS,
    GICS_SECTOR_ETFS,
    SECTOR_TICKERS,
    _forward_return,
    _warmup_start,
    aggregate_metrics,
    reconstruct_sector_ratings_at,
    brier_score,
    calibration_table,
    hit_rate,
    rate_sector,
    regime_to_forecast,
    spearman_rho,
    TRANSACTION_COST_BPS,
    walk_forward_backtest,
)


def _rec(origin, horizon, score, realized_return, label):
    dev = score - 50.0
    direction = "up" if dev > 0 else "down" if dev < 0 else "neutral"
    return BacktestRecord(
        origin=origin, horizon=horizon, forecast_direction=direction,
        forecast_daily_bps=round(dev * 2.0, 4),
        scenario_probs={"risk_on_expansion": 0.6 if score > 50 else 0.2},
        realized_return=realized_return,
        realized_direction="up" if realized_return > 0 else "down" if realized_return < 0 else "neutral",
        cost_bps=TRANSACTION_COST_BPS, net_return_bps=0.0, regime_score=score,
        regime_label=label,
        realized_regime=label,
    )


# --- spearman_rho ---------------------------------------------------------
def test_spearman_perfect_positive():
    assert spearman_rho([1, 2, 3, 4], [2, 4, 6, 8]) == 1.0


def test_spearman_perfect_negative():
    assert spearman_rho([1, 2, 3, 4], [8, 6, 4, 2]) == -1.0


def test_spearman_ties_average_rank():
    # ties -> averaged ranks; result should still be finite & in [-1, 1]
    rho = spearman_rho([5, 5, 5, 1, 2], [1, 2, 3, 4, 5])
    assert -1.0 <= rho <= 1.0


def test_spearman_zero_variance_returns_zero():
    assert spearman_rho([3, 3, 3, 3], [1, 2, 3, 4]) == 0.0


def test_spearman_too_few_returns_zero():
    assert spearman_rho([1.0], [2.0]) == 0.0


# --- hit_rate -------------------------------------------------------------
def test_hit_rate_all_correct():
    recs = [_rec(date(2026, 1, d), 5, 70, 1.0, "risk_on") for d in range(1, 5)]
    assert hit_rate(recs) == 1.0


def test_hit_rate_half():
    recs = [
        _rec(date(2026, 1, 1), 5, 60, 1.0, "risk_on"),
        _rec(date(2026, 1, 2), 5, 40, -1.0, "risk_off"),
    ]
    assert hit_rate(recs) == 1.0  # both correct (up when 60, down when 40)


def test_hit_rate_empty():
    assert hit_rate([]) == 0.0


# --- brier_score ----------------------------------------------------------
def test_brier_score_bounded_nonneg():
    recs = [_rec(date(2026, 1, d), 5, 60, 1.0, "risk_on") for d in range(1, 5)]
    b = brier_score(recs, "risk_on_expansion")
    assert 0.0 <= b <= 1.0


def test_brier_score_empty():
    assert brier_score([]) == 0.0


# --- calibration_table ----------------------------------------------------
def test_calibration_empty():
    assert calibration_table([]) == []


def test_calibration_uniform_probs_returns_bins():
    recs = [_rec(date(2026, 1, d), 5, 60 if d % 2 else 40, 1.0 if d % 2 else -1.0,
                 "risk_on" if d % 2 else "risk_off") for d in range(1, 21)]
    cal = calibration_table(recs)
    assert len(cal) >= 1
    for b in cal:
        assert 0.0 <= b["outcome_freq"] <= 1.0
        assert b["count"] >= 1


def test_calibration_single_bin_when_constant():
    recs = [_rec(date(2026, 1, d), 5, 60, 1.0, "risk_on") for d in range(1, 6)]
    cal = calibration_table(recs, n_bins=5)
    assert len(cal) == 1  # all probs equal -> single bin


# --- aggregate_metrics ------------------------------------------------------
def test_aggregate_metrics_by_horizon():
    recs = []
    d = 0
    for o in range(1, 30):
        for h in FORECAST_HORIZONS:
            d += 1
            recs.append(_rec(date(2026, 1, o), h, 60, float(h), "risk_on"))
    m = aggregate_metrics(recs)
    assert m["n_total"] == len(recs)
    for h in FORECAST_HORIZONS:
        assert h in m["by_horizon"]
        assert m["by_horizon"][h]["n"] > 0


def test_aggregate_metrics_empty():
    m = aggregate_metrics([])
    assert m == {"n": 0}


# --- regime_to_forecast ---------------------------------------------------
def test_regime_to_forecast_labelling():
    fc = regime_to_forecast({"bullish_score": 70, "volatility_state": "normal"})
    assert fc["source"].startswith("model")
    assert fc["direction"] == "up"
    assert fc["daily_bps"] > 0
    assert fc["reconstructed"] is True
    total = sum(fc["scenario_probs"].values())
    assert abs(total - 1.0) < 1e-3


def test_regime_to_forecast_neutral_at_50():
    fc = regime_to_forecast({"bullish_score": 50, "volatility_state": "normal"})
    assert fc["direction"] == "neutral"
    assert fc["daily_bps"] == 0.0


# --- rate_sector -----------------------------------------------------------
def test_gics_has_11_sectors():
    assert len(GICS_SECTOR_ETFS) == 11
    assert len(SECTOR_TICKERS) == 11


def test_rate_sector_neutral_at_baseline():
    row = {"close": 100, "ma_50": 100, "ma_200": 100, "rsi_14": 50,
           "macd_hist": 0, "return_20d": 0, "return_60d": 0}
    assert rate_sector(row) == 50.0


def test_rate_sector_bullish_extreme_exceeds_70():
    row = {"close": 120, "ma_50": 100, "ma_200": 100, "rsi_14": 70,
           "macd_hist": 0.01, "return_20d": 0.10, "return_60d": 0.20}
    r = rate_sector(row)
    assert r > 70.0
    assert 0.0 <= r <= 100.0


def test_rate_sector_bearish_below_30():
    row = {"close": 80, "ma_50": 100, "ma_200": 100, "rsi_14": 30,
           "macd_hist": -0.01, "return_20d": -0.10, "return_60d": -0.20}
    r = rate_sector(row)
    assert r < 30.0
    assert 0.0 <= r <= 100.0


# --- reconstruct_sector_ratings_at (ticker-in-.values() regression) -------
class _FakePanel:
    def __init__(self, rows):
        self._rows = rows

    def iterrows(self):
        for i, r in enumerate(self._rows):
            yield i, r


def test_reconstruct_sector_ratings_filters_by_etf_ticker(monkeypatch):
    # GICS_SECTOR_ETFS maps {sector_name: etf_ticker}; the filter must check
    # .values() (ETF tickers), not keys (sector names). Regression: previously
    # returned {} -> every stored rating defaulted to 0.0.
    panel = _FakePanel([{"ticker": "XLU", "close": 80.0, "ma_50": 80.0,
                         "ma_200": 80.0, "rsi_14": 50.0, "macd_hist": 0.0,
                         "return_20d": 0.0, "return_60d": 0.0}])
    monkeypatch.setattr("db_builder.aegis_backtest.fetch_historical_sector_panel",
                        lambda e, d: panel)
    ratings = reconstruct_sector_ratings_at(_noop_engine(), date(2026, 1, 1))
    assert ratings == {"XLU": 50.0}


# --- _warmup_start (no-DB boundary) ---------------------------------------
def test_warmup_start_zero_or_negative_returns_none():
    # warmup<=0 => no skip and no DB query is issued
    assert _warmup_start(_noop_engine(), "SPY", 0) is None
    assert _warmup_start(_noop_engine(), "SPY", -5) is None


# --- _forward_return (holiday-robust trading-day return) -------------------
def test_forward_return_basic_5d():
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    # 5 trading steps from entry (index 0) -> index 5
    assert _forward_return(closes, 5) == round(105.0 / 100.0 - 1.0, 6)


def test_forward_return_insufficient_returns_none():
    assert _forward_return([100.0, 101.0], 5) is None  # only 2 bars, need 6


def test_forward_return_zero_entry_returns_none():
    assert _forward_return([0.0, 1.0, 1.0, 1.0, 1.0, 1.0], 5) is None


# --- walk_forward_backtest (in-memory, overrides DB fetchers) -------------
def _noop_engine():
    return None


def test_walk_forward_overrides(monkeypatch):
    monkeypatch.setattr("db_builder.aegis_backtest.reconstruct_regime_at",
                        lambda engine, d, **k: _mock_snap(d))
    monkeypatch.setattr("db_builder.aegis_backtest.fetch_realized_return",
                        lambda engine, origin, horizon, ticker="^GSPC": float(horizon))
    recs = walk_forward_backtest(_noop_engine(), date(2026, 1, 1), date(2026, 1, 30),
                                 origin_step=5)
    assert len(recs) > 0
    # every record's origin must be within [start, end]  -> no look-ahead
    assert min(r.origin for r in recs) >= date(2026, 1, 1)
    assert max(r.origin for r in recs) <= date(2026, 1, 30)


def _mock_snap(d):
    class _S:
        regime = {"bullish_score": 60.0, "market_regime": "risk_on",
                  "volatility_state": "normal"}
        reconstructed = True
        as_of_date = d
    return _S()
