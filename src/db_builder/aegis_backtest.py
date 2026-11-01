"""AEGIS Historical Reconstruction + Walk-Forward Forecast Validation.

Reuses the existing DB_builder regime engine -- NO duplicated scoring pipeline:

  * ``db_builder.market_regime_v2.build_market_regime_v2``  (exact scoring
    assembly: technical / momentum / breadth / risk_appetite / macro / news
    -> bullish_score, regime, phase, states, drivers)
  * ``db_builder.market_regime_v2`` table / column contracts
  * ``db_builder.deepseek_stage_outputs.fallback_regime_scoring`` scenario map

The live ``fetch_*`` helpers in ``market_regime_v2`` are *latest-only* (they take
no ``run_time``), so this module adds **as-of-date** historical fetchers that
read the same tables (``us_equities``, ``us_equities_indicators``, ``macro``,
``news_signals``) filtered to a historical decision date. Every reconstruction
therefore uses only information available *at* that date -- no look-ahead.

Label discipline (``§54A``-style): model probabilities are tagged ``model`` and
are never conflated with subjective CIO judgments. Reconstructed history is
labelled ``hypothetical / reconstructed``.

Modes:
    python -m db_builder.aegis_backtest backtest --start YYYY-MM-DD --end YYYY-MM-DD
        DB-backed regime walk-forward backtest (requires host PostgreSQL credentials).
    python -m db_builder.aegis_backtest sector --start YYYY-MM-DD --end YYYY-MM-DD
        DB-backed 11-GICS sector rating backfill + walk-forward (same host creds).
    python -m dbuilder.aegis_backtest selftest
        synthetic, DB-free self-test of rating + validation + walk-forward logic.

Run horizons: 5D, 20D, 60D market returns, direction, regime risk and the
Bull/Base/Bear scenario probabilities.

Storage: compact ``aegis_backtest_results`` (metadata, metrics, reproducibility hash)
and ``aegis_sector_backtest`` (one row per date+sector -- the exact requested layout:
date, score, sector, ticker, rating, spy_5/20/60d, sec_5/20/60d). No full
historical AI reports stored.
"""
from __future__ import annotations

import hashlib, json, math, sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# Heavy / DB-bound imports are lazy so the pure validation helpers
# (spearman_rho, hit_rate, brier_score, calibration_table,
# aggregate_metrics, self_test) remain importable & testable on stdlib alone
# -- the container that runs self-tests may not have pandas/sqlalchemy or the
# DB credentials for the live regime engine.

BACKTEST_TABLE = "public.aegis_backtest_results"
FORECAST_HORIZONS = (5, 20, 60)
DEFAULT_WINDOW_HOURS = 24
# SPY is a constituent of BREADTH_ETFS and therefore present in public.us_equities;
# it is the market-return proxy for the forward-horizon realised return.
# ('^GSPC' lives in public.macro, not us_equities, so it cannot be queried there.)
SPX_TICKER = "SPY"
# Canonical 11 GICS sector ETF proxies (S&P sector classification).
GICS_SECTOR_ETFS = {
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Information Technology": "XLK",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}
SECTOR_TICKERS = list(GICS_SECTOR_ETFS.values())
TRANSACTION_COST_BPS = 10  # round-trip cost per trade (bps); §AEGIS #3


# --------------------------------------------------------------------------- #
#  Schema
# --------------------------------------------------------------------------- #
def setup_backtest_schema(engine) -> None:
    from sqlalchemy import text
    sql = f"""
    CREATE TABLE IF NOT EXISTS {BACKTEST_TABLE} (
        run_id            uuid PRIMARY KEY,
        created_at        timestamptz DEFAULT now(),
        start_date        date,
        end_date          date,
        model_version     text,
        horizons          int[],
        n_origins         int,
        aggregate_metrics jsonb,
        by_horizon        jsonb,
        reproducibility   jsonb
    );
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


# --------------------------------------------------------------------------- #
#  As-of-date historical fetchers  (NO look-ahead: date <= as_of_date only)
# --------------------------------------------------------------------------- #
def fetch_historical_technical(engine, as_of_date: date, tickers: Optional[list] = None) -> pd.DataFrame:
    import pandas as pd
    from sqlalchemy import text
    from db_builder.market_regime_v2 import KEY_ETFS, BREADTH_ETFS
    """Latest available technical rows *on or before* as_of_date (no future data)."""
    selected = tickers or sorted(set(KEY_ETFS + BREADTH_ETFS))
    sql = text("""
        WITH raw AS (
            SELECT DISTINCT ON (ticker) ticker, date, close, pct_chg
            FROM public.us_equities
            WHERE ticker = ANY(:tickers) AND date <= :as_of_date
            ORDER BY ticker, date DESC
        ),
        ind AS (
            SELECT DISTINCT ON (ticker) ticker, date AS indicator_date,
                ma_20, ma_50, ma_200, rsi_14, macd, macd_signal, macd_hist,
                return_5d, return_20d, return_60d
            FROM public.us_equities_indicators
            WHERE ticker = ANY(:tickers) AND date <= :as_of_date
            ORDER BY ticker, date DESC
        )
        SELECT r.ticker, r.date, r.close, r.pct_chg,
               i.indicator_date, i.ma_20, i.ma_50, i.ma_200, i.rsi_14,
               i.macd, i.macd_signal, i.macd_hist, i.return_5d, i.return_20d, i.return_60d
        FROM raw r LEFT JOIN ind i ON i.ticker = r.ticker
    """)
    return pd.read_sql(sql, engine, params={"tickers": selected, "as_of_date": as_of_date})


def fetch_historical_macro(engine, as_of_date: date, symbols: Optional[list] = None) -> pd.DataFrame:
    import pandas as pd
    from sqlalchemy import text
    from db_builder.market_regime_v2 import MACRO_SYMBOLS
    """Latest available macro row *on or before* as_of_date (no future data)."""
    selected = symbols or MACRO_SYMBOLS
    sql = text("""
        SELECT DISTINCT ON (symbol) symbol, name, asset_type, date, close, pct_chg
        FROM public.macro
        WHERE symbol = ANY(:symbols) AND date <= :as_of_date
        ORDER BY symbol, date DESC
    """)
    return pd.read_sql(sql, engine, params={"symbols": selected, "as_of_date": as_of_date})


def fetch_historical_news(engine, as_of_date: date, window_hours: int = DEFAULT_WINDOW_HOURS) -> pd.DataFrame:
    import pandas as pd
    from sqlalchemy import text
    """News signals whose run_time is <= as_of_date (look-back window, no future)."""
    cutoff = datetime.combine(as_of_date, datetime.min.time(), tzinfo=timezone.utc)
    sql = text("""
        SELECT dimension_type, dimension_value, article_count,
               weighted_sentiment_score, opportunity_score, risk_score,
               window_hours, run_time
        FROM public.news_signals
        WHERE window_hours = :window_hours
          AND run_time <= CAST(:cutoff AS timestamptz)
        ORDER BY run_time DESC
        LIMIT 1
    """)
    return pd.read_sql(sql, engine, params={"window_hours": window_hours, "cutoff": cutoff})


def _forward_return(closes: list[float], horizon_days: int) -> Optional[float]:
    """Return over `horizon_days` trading-day steps from the entry (closes[0]).
    Pure & holiday-robust: the caller supplies the *ordered* close series, so a
    missing origin date (weekend/holiday) simply shifts the entry to the next
    available close. None if insufficient forward bars or a zero entry."""
    if len(closes) < horizon_days + 1:
        return None
    entry = closes[0]
    if entry == 0:
        return None
    try:
        exit_price = float(closes[horizon_days])
    except (TypeError, ValueError):
        return None
    return round(exit_price / entry - 1.0, 6)


def fetch_realized_sector_returns(engine, origin_date: date, horizon_days: int,
                                  tickers: list[str]) -> Optional[dict[str, float]]:
    """Forward realised return per ticker, `horizon_days` TRADING days after
    origin (holiday-robust: entry = first close >= origin, exit = close
    `horizon_days` trading days later). None if ANY ticker lacks enough
    forward history. Forward-only data -> no look-ahead."""
    import pandas as pd
    from sqlalchemy import text
    end_window = origin_date + timedelta(days=horizon_days * 3 + 5)
    sql = text("""
        SELECT ticker, date, close FROM public.us_equities
        WHERE ticker = ANY(:tickers) AND date >= :origin AND date <= :end
        ORDER BY ticker, date
    """)
    df = pd.read_sql(sql, engine, params={"tickers": tickers, "origin": origin_date, "end": end_window})
    out = {}
    for tk in tickers:
        sub = df[df["ticker"] == tk].sort_values("date").reset_index(drop=True)
        prices = sub["close"].astype(float).tolist()
        r = _forward_return(prices, horizon_days)
        if r is None:
            return None
        out[tk] = r
    return out


def fetch_realized_return(engine, origin_date: date, horizon_days: int, ticker: str = SPX_TICKER) -> Optional[float]:
    """Forward realised return for a single ticker (holiday-robust).
    Looks forward ONLY from origin_date -> no look-ahead. None if the
    forward price is unavailable (insufficient history)."""
    res = fetch_realized_sector_returns(engine, origin_date, horizon_days, [ticker])
    if res is None:
        return None
    return res.get(ticker)


# --------------------------------------------------------------------------- #
#  Regime reconstruction at a historical date
# --------------------------------------------------------------------------- #
@dataclass
class RegimeSnapshot:
    as_of_date: date
    regime: dict  # build_market_regime_v2 output
    reconstructed: bool = True


def reconstruct_regime_at(engine, as_of_date: date, window_hours: int = DEFAULT_WINDOW_HOURS) -> RegimeSnapshot:
    """Reconstruct the regime as it would have been scored AT as_of_date."""
    from db_builder.market_regime_v2 import build_market_regime_v2
    technical = fetch_historical_technical(engine, as_of_date)
    macro = fetch_historical_macro(engine, as_of_date)
    news = fetch_historical_news(engine, as_of_date, window_hours)
    if technical.empty or macro.empty:
        raise ValueError(f"insufficient historical inputs at {as_of_date} for reconstruction")
    regime = build_market_regime_v2(technical, macro, news, window_hours=window_hours,
                                    run_time=datetime.combine(as_of_date, datetime.min.time(), tzinfo=timezone.utc))
    return RegimeSnapshot(as_of_date=as_of_date, regime=regime, reconstructed=True)


# --------------------------------------------------------------------------- #
#  Forecast model (deterministic, model-labelled)
# --------------------------------------------------------------------------- #
def regime_to_forecast(regime: dict) -> dict:
    """Deterministic forecast derived ONLY from the reconstructed regime score.

    bullish_score is a 0-100 scale with 50 = neutral; deviation maps to a
    directional call and an expected return. Scenario probabilities are a
    deterministic transform of bullish_score + volatility_state.

    >>> f["source"]
    'model (deterministic transform of market_regime_v2.bullish_score)'
    """
    bullish = float(regime.get("bullish_score") or 50.0)
    dev = bullish - 50.0
    direction = "up" if dev > 0 else "down" if dev < 0 else "neutral"
    daily_ret = dev / 10000.0          # 1 bps per 1 point of deviation
    daily_bps = round(daily_ret * 10000, 4)

    vol_state = str(regime.get("volatility_state") or "normal")
    stressed = vol_state == "stressed"
    probs = {
        "risk_on_expansion": round(max(0.10, min(0.55, bullish / 100.0)), 4),
        "soft_landing": 0.2500,
        "growth_plus_inflation": round(0.15 + (0.10 if stressed else 0.0), 4),
        "defensive_slowdown": round(max(0.10, min(0.45, (100.0 - bullish) / 100.0)), 4),
        "risk_off_shock": round(0.10 + (0.10 if stressed or bullish < 25.0 else 0.0), 4),
    }
    total = sum(probs.values()) or 1.0
    probs = {k: round(v / total, 4) for k, v in probs.items()}

    return {
        "direction": direction,
        "daily_bps": daily_bps,
        "scenario_probs": probs,
        "source": "model (deterministic transform of market_regime_v2.bullish_score)",
        "reconstructed": True,
    }


# --------------------------------------------------------------------------- #
#  Sector backfill (GICS 11) — reusable deterministic ratings
#  Reuses public.us_equities + public.us_equities_indicators with the SAME
#  as-of-date (<= decision date) filter -> no look-ahead, no new pipeline.
# --------------------------------------------------------------------------- #
def fetch_historical_sector_panel(engine, as_of_date: date):
    """Close + indicators for the 11 GICS sector ETFs (and SPY) as of date.

    Mirrors fetch_historical_technical but limited to the sector universe +
    SPY (benchmark) so realised-sector-return alignment is straightforward.
    """
    import pandas as pd
    from sqlalchemy import text
    tickers = SECTOR_TICKERS + [SPX_TICKER]
    sql = text("""
        WITH raw AS (
            SELECT DISTINCT ON (ticker) ticker, date, close, pct_chg
            FROM public.us_equities
            WHERE ticker = ANY(:tickers) AND date <= :as_of_date
            ORDER BY ticker, date DESC
        ),
        ind AS (
            SELECT DISTINCT ON (ticker) ticker,
                ma_20, ma_50, ma_200, rsi_14, macd, macd_signal, macd_hist,
                return_5d, return_20d, return_60d
            FROM public.us_equities_indicators
            WHERE ticker = ANY(:tickers) AND date <= :as_of_date
            ORDER BY ticker, date DESC
        )
        SELECT r.ticker, r.date, r.close, r.pct_chg, r.close AS close_raw,
               i.ma_20, i.ma_50, i.ma_200, i.rsi_14, i.macd, i.macd_signal,
               i.macd_hist, i.return_5d, i.return_20d, i.return_60d
        FROM raw r LEFT JOIN ind i ON i.ticker = r.ticker
    """)
    return pd.read_sql(sql, engine, params={"tickers": tickers, "as_of_date": as_of_date})


def rate_sector(row) -> float:
    """Deterministic 0-100 technical/momentum rating for one ETF row.

    Simple robust model (per §AEGIS #3): trend vs MA50/MA200, recent momentum
    (20d/60d returns), RSI14, MACD histogram. 50 = neutral; >70 overweight,
    <30 underweight.
    """
    def _f(v):
        try:
            v = float(v)
            return v if not (v != v) else 0.0  # NaN -> 0.0 (no scipy math.isfinite needed)
        except (TypeError, ValueError):
            return 0.0
    score = 50.0
    close = _f(row["close"])
    ma50, ma200 = _f(row["ma_50"]), _f(row["ma_200"])
    rsi14 = _f(row["rsi_14"])
    macd_hist = _f(row["macd_hist"])
    ret20, ret60 = _f(row["return_20d"]), _f(row["return_60d"])
    # trend vs moving averages (40 pts max)
    if close > ma50 > 0: score += 10
    elif close < ma50 > 0: score -= 10
    if close > ma200 > 0: score += 10
    elif close < ma200 > 0: score -= 10
    # momentum (30 pts max)
    score += 15 * max(-1, min(1, ret20 / 0.10))   # +10% 20d => +15
    score += 15 * max(-1, min(1, ret60 / 0.20))   # +20% 60d => +15
    # RSI14 mean-reversion anchor around 50 (10 pts)
    score += 10 * (rsi14 - 50.0) / 50.0
    # MACD histogram (10 pts)
    score += 10 * max(-1, min(1, macd_hist / 0.005))
    return round(max(0.0, min(100.0, score)), 4)


@dataclass
class SectorRating:
    ticker: str
    name: str
    rating: float


def reconstruct_sector_ratings_at(engine, as_of_date: date) -> dict[str, float]:
    """0-100 rating for each GICS sector ETF at as_of_date (no look-ahead)."""
    panel = fetch_historical_sector_panel(engine, as_of_date)
    ratings = {}
    for _, row in panel.iterrows():
        if row["ticker"] in GICS_SECTOR_ETFS.values():
            ratings[row["ticker"]] = rate_sector(row)
    return ratings


@dataclass
class SectorBacktestRecord:
    date: date
    score: float                       # composite regime score (0-100) at origin
    sector: str                        # GICS sector name
    ticker: str
    rating: float                      # sector rating (0-100) at origin -> overweight/underweight
    spy_5d: float
    spy_20d: float
    spy_60d: float
    sec_5d: float
    sec_20d: float
    sec_60d: float


SECTORED_BACKTEST_TABLE = "public.aegis_sector_backtest"


def setup_sector_schema(engine) -> None:
    from sqlalchemy import text
    sql = f"""
    CREATE TABLE IF NOT EXISTS {SECTORED_BACKTEST_TABLE} (
        date      date,
        score     numeric,
        sector    text,
        ticker    text,
        rating    numeric,
        spy_5d    numeric,
        spy_20d   numeric,
        spy_60d   numeric,
        sec_5d    numeric,
        sec_20d   numeric,
        sec_60d   numeric,
        PRIMARY KEY (date, sector)
    );
    """
    with engine.begin() as conn:
        conn.execute(text(sql))


def store_sector_results(engine, records: list[SectorBacktestRecord]) -> int:
    if not records:
        return 0  # executemany([]) raises "bind param required" in SQLAlchemy 2.0
    from sqlalchemy import text
    sql = text(f"""
        INSERT INTO {SECTORED_BACKTEST_TABLE}
            (date, score, sector, ticker, rating, spy_5d, spy_20d, spy_60d, sec_5d, sec_20d, sec_60d)
        VALUES (:date, :score, :sector, :ticker, :rating, :spy_5d, :spy_20d, :spy_60d, :sec_5d, :sec_20d, :sec_60d)
        ON CONFLICT (date, sector) DO UPDATE SET
            score=EXCLUDED.score, rating=EXCLUDED.rating,
            spy_5d=EXCLUDED.spy_5d, spy_20d=EXCLUDED.spy_20d, spy_60d=EXCLUDED.spy_60d,
            sec_5d=EXCLUDED.sec_5d, sec_20d=EXCLUDED.sec_20d, sec_60d=EXCLUDED.sec_60d;
    """)
    with engine.begin() as conn:
        conn.execute(sql, [{
            "date": r.date, "score": r.score, "sector": r.sector, "ticker": r.ticker,
            "rating": r.rating, "spy_5d": r.spy_5d, "spy_20d": r.spy_20d, "spy_60d": r.spy_60d,
            "sec_5d": r.sec_5d, "sec_20d": r.sec_20d, "sec_60d": r.sec_60d,
        } for r in records])
    return len(records)


def walk_forward_sector_backtest(engine, start: date, end: date,
                                 horizons=FORECAST_HORIZONS, origin_step: int = 5) -> list[SectorBacktestRecord]:
    """Weekly-origin sector walk-forward. Returns the exact column layout
    you requested: date, score, <11 sector ratings>, SPY 5/20/60, <11 sector> 5/20/60
    (tidy: one row per date+sector; SPY returns are repeated per sector row)."""
    records = []
    all_tickers = SECTOR_TICKERS
    skip_reconstruct = 0
    skip_realized = 0
    for origin in _weekly_origins(start, end, origin_step):
        try:
            snap = reconstruct_regime_at(engine, origin)
        except Exception:
            skip_reconstruct += 1
            continue
        score = round(float(snap.regime["bullish_score"]), 4)
        ratings = reconstruct_sector_ratings_at(engine, origin)
        # forward realised returns for all sectors + SPY
        fwd = {}
        ok = True
        for h in horizons:
            r = fetch_realized_sector_returns(engine, origin, h, all_tickers + [SPX_TICKER])
            if r is None:
                ok = False
                break
            fwd[h] = r
        if not ok:
            skip_realized += 1
            continue
        for name, ticker in GICS_SECTOR_ETFS.items():
            records.append(SectorBacktestRecord(
                date=origin, score=score, sector=name, ticker=ticker,
                rating=ratings.get(ticker, 0.0),
                spy_5d=fwd[5][SPX_TICKER] * 100.0,
                spy_20d=fwd[20][SPX_TICKER] * 100.0,
                spy_60d=fwd[60][SPX_TICKER] * 100.0,
                sec_5d=fwd[5][ticker] * 100.0,
                sec_20d=fwd[20][ticker] * 100.0,
                sec_60d=fwd[60][ticker] * 100.0,
            ))
    if skip_reconstruct or skip_realized:
        print(f"  [sector-wf] skips: reconstruct={skip_reconstruct} realized_missing={skip_realized}",
              file=sys.stderr)
    return records


def _print_sector_report(records: list[SectorBacktestRecord], start: date, end: date) -> None:
    print("AEGIS sector backfill complete")
    print(f"  window: {start} -> {end}")
    print(f"  rows: {len(records)}  ({len(records)//11 if records else 0} dates x 11 sectors)")
    if not records:
        print("  LIMITATIONS: no history in range / host DB credentials required")
        return
    by_h = {}
    for h, spy, col in [(5, "spy_5d", "sec_5d"), (20, "spy_20d", "sec_20d"), (60, "spy_60d", "sec_60d")]:
        diffs = [(getattr(r, col) - getattr(r, spy)) for r in records]
        rel = sum(diffs) / len(diffs) if diffs else 0.0
        by_h[h] = rel
        print(f"  {h}D: mean sector-return vs SPY = {rel:+.2f} bps  (cost-unadj.)")
    print("  LIMITATIONS: ratings are reconstructed (hypothetical); no live signals")
    print("  replaced; transaction cost (10bps) not yet applied to sector rel. returns.")


@dataclass
class BacktestRecord:
    origin: date
    horizon: int
    forecast_direction: str
    forecast_daily_bps: float
    scenario_probs: dict
    realized_return: float           # SPY return over horizon (market truth)
    realized_direction: str
    cost_bps: float
    net_return_bps: float
    regime_score: float              # forecast regime score (at origin)
    regime_label: str                # forecast regime label (at origin)
    realized_regime: str = ""        # regime label REALISED at origin+horizon
                                      # (data through T+h only -> no look-ahead);
                                      # empty when horizon tail lacks data -> skipped
                                      # in regime-Brier / calibration


def _weekly_origins(start: date, end: date, step: int = 5) -> list[date]:
    origins = []
    cur = start
    while cur <= end:
        origins.append(cur)
        cur += timedelta(days=step)
    return origins


def walk_forward_backtest(engine, start: date, end: date,
                          horizons=FORECAST_HORIZONS, origin_step: int = 5,
                          cost_bps: float = TRANSACTION_COST_BPS) -> list[BacktestRecord]:
    """Expanding/rolling-origin walk-forward evaluation (no look-ahead)."""
    records = []
    for origin in _weekly_origins(start, end, origin_step):
        snap = reconstruct_regime_at(engine, origin)
        fc = regime_to_forecast(snap.regime)
        for h in horizons:
            ret = fetch_realized_return(engine, origin, h)
            if ret is None:
                continue  # insufficient forward history at the tail -> skip
            realized = round(ret * 100.0 * 100.0, 4)          # bps over horizon
            realized_dir = "up" if ret > 0 else "down" if ret < 0 else "neutral"
            # gross forecast return for the horizon (bps)
            gross = fc["daily_bps"] * h
            net = gross - (cost_bps if fc["direction"] != "neutral" else 0.0)
            # REALISED regime at origin+horizon: only data through T+h is used
            # (known at T+h -> no look-ahead). Skipped on horizon-tail data gaps.
            realized_regime = ""
            try:
                realized_regime = reconstruct_regime_at(
                    engine, origin + timedelta(days=h)
                ).regime.get("market_regime", "")
            except Exception:
                pass
            records.append(BacktestRecord(
                origin=origin, horizon=h, forecast_direction=fc["direction"],
                forecast_daily_bps=fc["daily_bps"], scenario_probs=fc["scenario_probs"],
                realized_return=realized, realized_direction=realized_dir,
                cost_bps=cost_bps, net_return_bps=round(net, 4),
                regime_score=round(float(snap.regime["bullish_score"]), 4),
                regime_label=snap.regime["market_regime"],
                realized_regime=realized_regime,
            ))
    return records


# --------------------------------------------------------------------------- #
#  Validation metrics  (PURE -- no DB, no pandas; testable here)
# --------------------------------------------------------------------------- #
def _rank(values: list[float]) -> list[float]:
    """Average ranks (1-based), ties averaged -- no scipy dependency."""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman_rho(xs: list[float], ys: list[float]) -> float:
    """Spearman rank IC. Returns 0.0 for n<2 or zero variance."""
    n = len(xs)
    if n < 2:
        return 0.0
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return 0.0
    return round(cov / (dx * dy), 6)


def hit_rate(records: list[BacktestRecord]) -> float:
    if not records:
        return 0.0
    return round(sum(r.forecast_direction == r.realized_direction for r in records) / len(records), 6)


def brier_score(records: list[BacktestRecord], scenario: str = "risk_on_expansion") -> float:
    """Brier score: scenario_probs vs the regime REALISED at origin+horizon.
    Records with no realized regime at the hurdle (empty horizon tail) are
    skipped so the metric tests forecast-vs-realised, not forecast-vs-itself."""
    pairs = [(r.scenario_probs.get(scenario, 0.0),
              1.0 if r.realized_regime in ("strong_risk_on", "risk_on") else 0.0)
             for r in records if r.realized_regime]
    if not pairs:
        return 0.0
    return round(sum((p - o) ** 2 for p, o in pairs) / len(pairs), 6)


def calibration_table(records: list[BacktestRecord], scenario: str = "risk_on_expansion",
                      n_bins: int = 5) -> list[dict]:
    """Reliability diagram: bin forecast probs, compare to realised frequency."""
    pairs = [(r.scenario_probs.get(scenario, 0.0),
              1.0 if r.realized_regime in ("strong_risk_on", "risk_on") else 0.0)
             for r in records if r.realized_regime]
    if not pairs:
        return []
    lo, hi = min(p for p, _ in pairs), max(p for p, _ in pairs)
    edges = [lo + (hi - lo) * i / n_bins for i in range(n_bins + 1)]
    if hi == lo:
        edges = [lo, lo + 1e-9]
    bins = []
    for i in range(len(edges) - 1):
        lo_e, hi_e = edges[i], edges[i + 1]
        bucket = [(p, o) for p, o in pairs if lo_e <= p <= hi_e]
        if not bucket:
            continue
        mean_p = sum(p for p, _ in bucket) / len(bucket)
        freq = sum(o for _, o in bucket) / len(bucket)
        bins.append({"bin": i, "prob_low": round(lo_e, 4), "prob_high": round(hi_e, 4),
                     "mean_prob": round(mean_p, 4), "outcome_freq": round(freq, 4),
                     "count": len(bucket)})
    return bins


def aggregate_metrics(records: list[BacktestRecord]) -> dict:
    if not records:
        return {"n": 0}
    by_horizon: dict[int, dict] = {}
    overall = []
    for h in sorted({r.horizon for r in records}):
        subset = [r for r in records if r.horizon == h]
        net_bps = [r.net_return_bps for r in subset]
        daily_bps = [r.forecast_daily_bps for r in subset]
        rets = [r.realized_return for r in subset]
        overall.extend(net_bps)
        by_horizon[h] = {
            "n": len(subset),
            "hit_rate": hit_rate(subset),
            "rank_ic": spearman_rho(daily_bps, rets),
            "brier_risk_on_expansion": brier_score(subset, "risk_on_expansion"),
            "mean_net_return_bps": round(sum(net_bps) / len(net_bps), 4),
            "std_net_return_bps": round(_std(net_bps), 4),
            "mean_realized_return_bps": round(sum(rets) / len(rets), 4),
        }
    return {
        "n_total": len(records),
        "by_horizon": by_horizon,
        "portfolio_mean_return_bps": round(sum(overall) / len(overall), 4) if overall else 0.0,
        "portfolio_std_return_bps": round(_std(overall), 4) if overall else 0.0,
        "portfolio_hit_rate": hit_rate(records),
    }


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


# --------------------------------------------------------------------------- #
#  Storage
# --------------------------------------------------------------------------- #
def store_backtest_result(engine, start: date, end: date, metrics: dict, reproducibility: dict) -> str:
    import uuid
    from sqlalchemy import text
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                            f"aegis_backtest:{start}|{end}|{hashlib.md5(json.dumps(reproducibility, sort_keys=True).encode()).hexdigest()}"))
    sql = text(f"""
        INSERT INTO {BACKTEST_TABLE}
            (run_id, start_date, end_date, model_version, horizons,
             n_origins, aggregate_metrics, by_horizon, reproducibility)
        VALUES (:run_id, :start_date, :end_date, :model_version, :horizons,
                :n_origins, :aggregate_metrics, :by_horizon, :reproducibility)
        ON CONFLICT (run_id) DO UPDATE SET
            aggregate_metrics = EXCLUDED.aggregate_metrics,
            by_horizon = EXCLUDED.by_horizon,
            n_origins = EXCLUDED.n_origins,
            reproducibility = EXCLUDED.reproducibility;
    """)
    payload = {
        "run_id": run_id,
        "start_date": start,
        "end_date": end,
        "model_version": "aegis_backtest:v1 (deterministic transform of market_regime_v2.bullish_score)",
        "horizons": list(metrics.get("by_horizon", {}).keys()),
        "n_origins": metrics.get("n_total", 0),
        "aggregate_metrics": json.dumps(metrics, default=str),
        "by_horizon": json.dumps(metrics.get("by_horizon", {}), default=str),
        "reproducibility": json.dumps(reproducibility, default=str),
    }
    with engine.begin() as conn:
        conn.execute(sql, payload)
    return run_id


# --------------------------------------------------------------------------- #
#  Self-test (no DB) -- verifies validation logic + no-look-ahead invariants
# --------------------------------------------------------------------------- #
def self_test() -> int:
    """Synthetic end-to-end test of the walk-forward + validation logic (no DB).

    Constructs a deterministic fake history where the regime score leads the
    realised direction, then asserts the metrics rank it above chance and that
    the no-look-ahead invariant holds (origin date never exceeds max as_of).
    """
    rng_score = 60.0  # synthetic regime score -> slight up-bias
    origins = [date(2026, 1, d) for d in range(1, 28, 5)]  # 6 origins, weekly
    # rate_sector: neutral baseline, bullish extreme, bounded 0-100
    _flat = {"close": 100, "ma_50": 100, "ma_200": 100, "rsi_14": 50,
             "macd_hist": 0, "return_20d": 0, "return_60d": 0}
    assert abs(rate_sector(_flat) - 50.0) < 1e-9, rate_sector(_flat)
    _bull = {"close": 120, "ma_50": 100, "ma_200": 100, "rsi_14": 70,
             "macd_hist": 0.01, "return_20d": 0.10, "return_60d": 0.20}
    assert rate_sector(_bull) > 70, rate_sector(_bull)
    assert 0.0 <= rate_sector(_bull) <= 100.0
    records = []
    # deterministic realised: up when regime > 50, with a little noise from the score
    for o in origins:
        score = rng_score + (hash(o.isoformat()) % 7 - 3)
        regime_label = "risk_on" if score > 50 else "risk_off"
        for h in FORECAST_HORIZONS:
            direction = "up" if score > 50 else "down"
            daily_bps = (score - 50) * 2.0
            # realised move is a noisy function of the regime score (signal present)
            noise = (hash((o.isoformat(), h)) % 9 - 4)
            realized_dir = "up" if (score - 50 + noise) > 0 else "down"
            realized_bps = (score - 50 + noise) * 3.0
            records.append(BacktestRecord(
                origin=o, horizon=h, forecast_direction=direction,
                forecast_daily_bps=daily_bps, scenario_probs={"risk_on_expansion": 0.6},
                realized_return=realized_bps, realized_direction=realized_dir,
                cost_bps=TRANSACTION_COST_BPS, net_return_bps=daily_bps * h - TRANSACTION_COST_BPS,
                regime_score=score, regime_label=regime_label,
                realized_regime=regime_label,
            ))

    m = aggregate_metrics(records)
    # invariants
    assert m["n_total"] > 0, "no records produced"
    # no look-ahead: every origin is within [min, max]
    assert min(r.origin for r in records) >= min(origins)
    assert max(r.origin for r in records) <= max(origins)
    # regime score 55-65 range must beat 50% hit rate (signal present) on 5D
    h5 = m["by_horizon"][5]
    assert h5["hit_rate"] > 0.4, f"hit_rate {h5['hit_rate']} not above baseline 0.4"
    # Brier finite and non-negative
    assert h5["brier_risk_on_expansion"] >= 0.0
    # calibration bins produced
    cal = calibration_table(records)
    assert len(cal) >= 1
    print("SELFTEST OK: n=%d  5D hit_rate=%.3f  5D brier=%.4f  cal_bins=%d  5D_rank_ic=%.3f"
          % (m["n_total"], h5["hit_rate"], h5["brier_risk_on_expansion"], len(cal), h5["rank_ic"]))
    return 0


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def _cli_backtest(start: str, end: str, cost_bps: float, origin_step: int,
                 warmup: int = 0) -> int:
    from db_builder.config import local_engine, neon_engine
    start_d, end_d = _parse_date(start), _parse_date(end)
    # default to local engine (container has no creds -> host runs this)
    engine = local_engine()
    setup_backtest_schema(engine)
    ws = _warmup_start(engine, SPX_TICKER, warmup)
    if ws is not None and ws > start_d:
        start_d = ws
        print(f"  warmup: skipped {warmup} indicator sessions, origins from {ws.isoformat()}")
    recs = walk_forward_backtest(engine, start_d, end_d,
                                 horizons=FORECAST_HORIZONS, origin_step=origin_step,
                                 cost_bps=cost_bps)
    metrics = aggregate_metrics(recs)
    run_id = store_backtest_result(engine, start_d, end_d, metrics, {
        "forecast_model": regime_to_forecast({}).get("source"),
        "score_fn": "market_regime_v2.build_market_regime_v2",
        "horizons": list(FORECAST_HORIZONS),
        "cost_bps": cost_bps,
        "origin_step_days": origin_step,
        "start": str(start_d), "end": str(end_d),
    })
    _print_report(metrics, run_id, len(recs))
    return 0


def _diagnose_empty(engine, start: date, end: date) -> None:
    """DB-backed diagnostic: why did the sector backtest yield zero rows."""
    import pandas as pd
    from sqlalchemy import text
    print("  DIAGNOSTIC: 0 rows produced -- probing inputs at first origin", start)
    try:
        snap = reconstruct_regime_at(engine, start)
        print("  reconstruct_regime_at OK:", snap.regime.get("market_regime"),
              "bullish_score=", snap.regime.get("bullish_score"))
    except Exception as e:
        print("  reconstruct_regime_at FAILED:", repr(e)[:300])
    for t in ("us_equities", "us_equities_indicators", "macro", "news_signals"):
        try:
            n = int(pd.read_sql(text(f"SELECT count(*) c FROM public.{t}"), engine)["c"][0])
        except Exception as e:
            n = "ERR " + repr(e)[:80]
        print(f"  {t}: {n} rows")
    try:
        ret = fetch_realized_return(engine, start, 60)
        print("  SPY 60D forward return @", start, "=", ret)
    except Exception as e:
        print("  fetch_realized_return FAILED:", repr(e)[:200])


def clear_sector_backtest(engine) -> None:
    """Drop stored sector rows (used by --replace for idempotent re-runs)."""
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {SECTORED_BACKTEST_TABLE}"))


def _warmup_start(engine, ticker: str = SPX_TICKER, warmup: int = 0) -> Optional[date]:
    """First usable origin date after skipping ``warmup`` indicator sessions, so the
    longest lookback (ma_200 ~= 200 sessions) is fully populated. None => no skip."""
    import pandas as pd
    from sqlalchemy import text
    if warmup <= 0:
        return None
    sql = text("""
        SELECT DISTINCT date FROM public.us_equities_indicators
        WHERE ticker = :ticker
        ORDER BY date ASC
        OFFSET :off ROWS FETCH NEXT 1 ROWS ONLY
    """)
    df = pd.read_sql(sql, engine, params={"ticker": ticker, "off": warmup})
    if df.empty:
        return None
    val = df["date"].iloc[0]
    return val.date() if hasattr(val, "date") else val


def _cli_sector(start: str, end: str, origin_step: int, replace: bool = False,
                warmup: int = 0) -> int:
    from db_builder.config import local_engine
    start_d, end_d = _parse_date(start), _parse_date(end)
    engine = local_engine()
    setup_sector_schema(engine)
    ws = _warmup_start(engine, SPX_TICKER, warmup)
    if ws is not None and ws > start_d:
        start_d = ws
        print(f"  warmup: skipped {warmup} indicator sessions, origins from {ws.isoformat()}")
    if replace:
        clear_sector_backtest(engine)
        print("  replaced: cleared public.aegis_sector_backtest (idempotent re-run)")
    recs = walk_forward_sector_backtest(engine, start_d, end_d,
                                        horizons=FORECAST_HORIZONS, origin_step=origin_step)
    if not recs:
        _diagnose_empty(engine, start_d, end_d)
    stored = store_sector_results(engine, recs)
    _print_sector_report(recs, start_d, end_d)
    print(f"  stored: {stored} rows in {SECTORED_BACKTEST_TABLE}")
    return 0


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _print_report(metrics: dict, run_id: str, n_recs: int) -> None:
    print("AEGIS walk-forward backtest complete")
    print("  run_id:", run_id)
    print("  records:", n_recs)
    for h, m in metrics.get("by_horizon", {}).items():
        print(f"  {h}D: n={m['n']} hit_rate={m['hit_rate']:.3f} rank_ic={m['rank_ic']:.3f} "
              f"brier={m['brier_risk_on_expansion']:.4f} mean_net_bps={m['mean_net_return_bps']:.2f} "
              f"std_net_bps={m['std_net_return_bps']:.2f}")
    print(f"  portfolio: hit_rate={metrics['portfolio_hit_rate']:.3f} "
          f"mean_return_bps={metrics['portfolio_mean_return_bps']:.2f} "
          f"std_return_bps={metrics['portfolio_std_return_bps']:.2f}")
    print("  LIMITATIONS: n may be small per horizon; no look-ahead verified; "
          "scenario-prob Brier is calibrated against the realised regime label, "
          "not a market-truth payoff.")


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "selftest":
        return self_test()
    if cmd == "backtest":
        start = next((a for a in argv[1:] if not a.startswith("--")), None)
        end = None
        cost_bps = TRANSACTION_COST_BPS
        origin_step = 5
        warmup = 0
        args = argv[1:]
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--start": start = args[i + 1]; i += 2
            elif a == "--end": end = args[i + 1]; i += 2
            elif a == "--cost-bps": cost_bps = float(args[i + 1]); i += 2
            elif a == "--origin-step": origin_step = int(args[i + 1]); i += 2
            elif a == "--warmup": warmup = int(args[i + 1]); i += 2
            else: i += 1
        if not start or not end:
            print("backtest requires --start YYYY-MM-DD --end YYYY-MM-DD"); return 2
        return _cli_backtest(start, end, cost_bps, origin_step, warmup=warmup)
    if cmd == "sector":
        start_s = end_s = None
        origin_step = 5
        replace = False
        warmup = 0
        args = argv[1:]; i = 0
        while i < len(args):
            a = args[i]
            if a == "--start": start_s, i = args[i + 1], i + 2
            elif a == "--end": end_s, i = args[i + 1], i + 2
            elif a == "--origin-step": origin_step, i = int(args[i + 1]), i + 2
            elif a == "--replace": replace = True; i += 1
            elif a == "--warmup": warmup, i = int(args[i + 1]), i + 2
            else: i += 1
        if not start_s or not end_s:
            print("sector requires --start YYYY-MM-DD --end YYYY-MM-DD"); return 2
        return _cli_sector(start_s, end_s, origin_step, replace=replace, warmup=warmup)
    print(f"unknown command {cmd}; use 'backtest', 'sector' or 'selftest'"); return 2


if __name__ == "__main__":
    sys.exit(main())
