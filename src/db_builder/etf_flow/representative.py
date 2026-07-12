"""Representative ETF flow analytics.

This module keeps the default interpretation anchored on one representative ETF
per economic exposure. Secondary ETFs are retained for divergence checks rather
than blindly merged into the primary signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from db_builder.etf_flow.config import ETFAnalyticsConfig, clamp, flow_regime_label


SUPPORTED_ISSUER_TOKENS = ("ishares", "spdr", "state street", "vaneck", "first trust", "blackrock")


@dataclass(frozen=True)
class RepresentativeETF:
    exposure_id: str
    exposure_name: str
    exposure_type: str
    primary_ticker: str
    secondary_ticker: str | None = None
    tertiary_ticker: str | None = None
    benchmark_family: str | None = None
    selection_priority: int = 100
    aggregation_allowed: bool = False
    notes: str | None = None


REPRESENTATIVE_MAP: tuple[RepresentativeETF, ...] = (
    RepresentativeETF("US_BROAD_EQUITY", "U.S. Broad Equity", "asset_class", "IVV", "SPY", "ITOT", "US broad equity", 100, True),
    RepresentativeETF("GLOBAL_EQUITY", "Global Equity", "region", "ACWI", "URTH", None, "global equity"),
    RepresentativeETF("DEVELOPED_EX_US", "Developed Markets ex-U.S.", "region", "EFA", "IDEV", None, "developed ex-US"),
    RepresentativeETF("EMERGING_MARKETS", "Emerging Markets", "region", "IEMG", "EEM", None, "emerging markets", 100, True),
    RepresentativeETF("US_LARGE_CAP", "U.S. Large Cap", "market_cap", "IVV", "SPY", None, "US large cap", 100, True),
    RepresentativeETF("US_MID_CAP", "U.S. Mid Cap", "market_cap", "IJH", "MDY", None, "US mid cap"),
    RepresentativeETF("US_SMALL_CAP", "U.S. Small Cap", "market_cap", "IWM", "IJR", None, "US small cap"),
    RepresentativeETF("US_EQUAL_WEIGHT", "Equal-Weight U.S. Equity", "style", "RSP", None, None, "US equal weight"),
    RepresentativeETF("US_LARGE_GROWTH", "Large-Cap Growth", "style", "IWF", "IVW", None, "US growth"),
    RepresentativeETF("US_LARGE_VALUE", "Large-Cap Value", "style", "IWD", "IVE", None, "US value"),
    RepresentativeETF("QUALITY_FACTOR", "Quality", "factor", "QUAL", None, None, "quality"),
    RepresentativeETF("MOMENTUM_FACTOR", "Momentum", "factor", "MTUM", None, None, "momentum"),
    RepresentativeETF("LOW_VOLATILITY", "Low Volatility", "factor", "USMV", "SPLV", None, "low volatility"),
    RepresentativeETF("DIVIDEND_DEFENSIVES", "Dividend / Defensive Equity", "factor", "DVY", "SDY", None, "dividend"),
    RepresentativeETF("FACTOR_ROTATION", "Factor Rotation", "factor", "DYNF", None, None, "factor rotation"),
    RepresentativeETF("US_COMMUNICATION_SERVICES", "Communication Services", "sector", "XLC"),
    RepresentativeETF("US_CONSUMER_DISCRETIONARY", "Consumer Discretionary", "sector", "XLY"),
    RepresentativeETF("US_CONSUMER_STAPLES", "Consumer Staples", "sector", "XLP"),
    RepresentativeETF("US_ENERGY", "Energy", "sector", "XLE"),
    RepresentativeETF("US_FINANCIALS", "Financials", "sector", "XLF"),
    RepresentativeETF("US_HEALTHCARE", "Health Care", "sector", "XLV"),
    RepresentativeETF("US_INDUSTRIALS", "Industrials", "sector", "XLI"),
    RepresentativeETF("US_TECHNOLOGY", "Information Technology", "sector", "XLK"),
    RepresentativeETF("US_MATERIALS", "Materials", "sector", "XLB"),
    RepresentativeETF("US_REAL_ESTATE", "Real Estate", "sector", "XLRE"),
    RepresentativeETF("US_UTILITIES", "Utilities", "sector", "XLU"),
    RepresentativeETF("US_REGIONAL_BANKS", "U.S. Regional Banks", "industry", "IAT"),
    RepresentativeETF("US_AEROSPACE_DEFENSE", "Aerospace and Defense", "industry", "ITA"),
    RepresentativeETF("US_TELECOM", "U.S. Telecommunications", "industry", "IYZ"),
    RepresentativeETF("US_SEMICONDUCTORS", "Semiconductors", "industry", "SOXX", "SMH", None, "semiconductors", 100, True),
    RepresentativeETF("US_SOFTWARE", "Software", "industry", "IGV"),
    RepresentativeETF("CYBERSECURITY", "Cybersecurity", "theme", "CIBR"),
    RepresentativeETF("US_BIOTECH", "Biotechnology", "industry", "IBB", "XBI"),
    RepresentativeETF("US_MEDICAL_DEVICES", "Medical Devices", "industry", "IHI"),
    RepresentativeETF("US_BANKS", "U.S. Banks", "industry", "KBE"),
    RepresentativeETF("US_CAPITAL_MARKETS", "Capital Markets / Broker-Dealers", "industry", "IAI"),
    RepresentativeETF("US_INSURANCE", "Insurance", "industry", "KIE"),
    RepresentativeETF("US_HOMEBUILDERS", "Homebuilders", "industry", "ITB", "XHB", None, "homebuilders", 100, True),
    RepresentativeETF("US_TRANSPORTATION", "Transportation", "industry", "IYT"),
    RepresentativeETF("US_INFRASTRUCTURE", "Infrastructure", "theme", "IFRA"),
    RepresentativeETF("CLEAN_ENERGY", "Clean Energy", "theme", "ICLN"),
    RepresentativeETF("NUCLEAR", "Nuclear", "theme", "NLR"),
    RepresentativeETF("NATURAL_RESOURCES", "Natural Resources", "commodity", "GNR"),
    RepresentativeETF("OIL_SERVICES", "Oil Services", "industry", "OIH"),
    RepresentativeETF("OIL_GAS_EXPLORATION", "Oil and Gas E&P", "industry", "XOP"),
    RepresentativeETF("METALS_MINING", "Metals and Mining", "industry", "XME"),
    RepresentativeETF("AGRICULTURE", "Agriculture", "industry", "MOO"),
    RepresentativeETF("RETAIL", "Retail", "industry", "XRT"),
    RepresentativeETF("MEDIA_ENTERTAINMENT", "Media and Entertainment", "industry", "PBS"),
    RepresentativeETF("INTERNET", "Internet", "industry", "FDN"),
    RepresentativeETF("CLOUD_COMPUTING", "Cloud Computing", "theme", "SKYY"),
    RepresentativeETF("ROBOTICS_AI", "Robotics and Artificial Intelligence", "theme", "ROBT"),
    RepresentativeETF("US_INVESTMENT_GRADE", "Investment-Grade Credit", "credit", "LQD", "IGIB"),
    RepresentativeETF("US_HIGH_YIELD", "High-Yield Credit", "credit", "HYG", "JNK", None, "US high yield", 100, True),
    RepresentativeETF("US_CORE_BONDS", "Core Aggregate Bonds", "fixed_income", "AGG", "IUSB"),
    RepresentativeETF("US_TREASURY_BILLS", "Treasury Bills / Cash", "duration", "SGOV", "BIL"),
    RepresentativeETF("US_TREASURY_SHORT", "Short-Duration Treasuries", "duration", "SHY"),
    RepresentativeETF("US_TREASURY_INTERMEDIATE", "Intermediate-Duration Treasuries", "duration", "IEF"),
    RepresentativeETF("US_TREASURY_LONG", "Long-Duration Treasuries", "duration", "TLT"),
    RepresentativeETF("US_TIPS", "TIPS", "duration", "TIP"),
    RepresentativeETF("GOLD", "Gold", "commodity", "GLD", "IAU", None, "gold", 100, True),
    RepresentativeETF("BITCOIN", "Bitcoin", "crypto", "IBIT", "FBTC", "ARKB"),
)


RELATED_COMPARISONS = (
    ("US_FINANCIALS", "XLF", "IAT", "financial leadership concentrated outside regional banks"),
    ("US_FINANCIALS", "XLF", "KBE", "large financials diverge from banks"),
    ("US_FINANCIALS", "XLF", "IAI", "capital-markets leadership differs from broad financials"),
    ("US_INDUSTRIALS", "XLI", "ITA", "defense-specific demand differs from broad industrials"),
    ("US_INDUSTRIALS", "XLI", "IYT", "transportation does not confirm industrial flow"),
    ("US_COMMUNICATION_SERVICES", "XLC", "IYZ", "internet/media differs from telecom"),
    ("US_TECHNOLOGY", "XLK", "SOXX", "technology differs from semiconductors"),
    ("US_TECHNOLOGY", "XLK", "IGV", "technology differs from software"),
    ("US_HEALTHCARE", "XLV", "IBB", "health care differs from large-cap biotech"),
    ("US_HEALTHCARE", "XLV", "XBI", "health care differs from speculative biotech"),
    ("US_ENERGY", "XLE", "XOP", "energy differs from exploration and production"),
    ("US_ENERGY", "XLE", "OIH", "energy differs from oil services"),
    ("US_MATERIALS", "XLB", "XME", "materials differs from metals and mining"),
)


def representative_rows() -> list[dict[str, Any]]:
    return [
        {
            "exposure_id": item.exposure_id,
            "exposure_name": item.exposure_name,
            "exposure_type": item.exposure_type,
            "primary_ticker": item.primary_ticker,
            "secondary_ticker": item.secondary_ticker,
            "tertiary_ticker": item.tertiary_ticker,
            "primary_issuer": None,
            "benchmark_family": item.benchmark_family,
            "selection_priority": item.selection_priority,
            "aggregation_allowed": item.aggregation_allowed,
            "divergence_threshold_z": 1.5,
            "minimum_history_days": 40,
            "is_active": True,
            "notes": item.notes,
        }
        for item in REPRESENTATIVE_MAP
    ]


def _rolling_z(series: pd.Series, lookback: int, minimum: int) -> pd.Series:
    mean = series.rolling(lookback, min_periods=minimum).mean()
    std = series.rolling(lookback, min_periods=minimum).std()
    return (series - mean) / std.replace(0, pd.NA)


def _rolling_percentile(series: pd.Series, lookback: int, minimum: int) -> pd.Series:
    def pct(values) -> float:
        current = values[-1]
        return float((values <= current).sum() / len(values))

    return series.rolling(lookback, min_periods=minimum).apply(pct, raw=True)


def _consecutive(values: pd.Series, positive: bool) -> pd.Series:
    result = []
    count = 0
    for value in values.fillna(0):
        if (value > 0) if positive else (value < 0):
            count += 1
        else:
            count = 0
        result.append(count)
    return pd.Series(result, index=values.index)


def _state_from_score(value: float | None, *, band: float = 0.0005) -> str:
    if value is None or pd.isna(value):
        return "flat"
    if float(value) > band:
        return "up"
    if float(value) < -band:
        return "down"
    return "flat"


def _flow_state(row: pd.Series, cfg: ETFAnalyticsConfig) -> str:
    """Current allocation-flow state using statistically meaningful flow evidence."""
    neutral_band = cfg.representative.neutral_flow_pct_aum
    z1 = row.get("flow_zscore_1d")
    z5 = row.get("flow_zscore_5d")
    z20 = row.get("flow_zscore_20d")
    z60 = row.get("flow_zscore_60d")
    pct1 = row.get("flow_pct_aum_1d")
    pct5 = row.get("flow_pct_aum_5d")

    if pd.notna(z5):
        if z5 >= 1.0:
            return "inflow"
        if z5 <= -1.0:
            return "outflow"

    if pd.notna(z20) and pd.notna(z60):
        if z20 >= 1.0 and z60 >= 1.0:
            return "inflow"
        if z20 <= -1.0 and z60 <= -1.0:
            return "outflow"

    if pd.notna(z1):
        if z1 >= 2.0:
            return "inflow"
        if z1 <= -2.0:
            return "outflow"

    # Raw percent-flow fallback is used only when z-score context is unavailable.
    if pd.isna(z5) and pd.isna(z20) and pd.isna(z60) and pd.notna(pct5):
        if pct5 > neutral_band:
            return "inflow"
        if pct5 < -neutral_band:
            return "outflow"
    if pd.isna(z1) and pd.isna(z5) and pd.isna(z20) and pd.isna(z60) and pd.notna(pct1):
        if pct1 > neutral_band:
            return "inflow"
        if pct1 < -neutral_band:
            return "outflow"
    return "neutral"


def _volume_state(row: pd.Series) -> str:
    z = row.get("volume_zscore_60d")
    ratio = row.get("volume_ratio_20d")
    if pd.notna(z):
        if z >= 2.0:
            return "very high activity"
        if z >= 1.0:
            return "high activity"
        if z <= -1.5:
            return "very low activity"
    if pd.notna(ratio):
        if ratio >= 1.5:
            return "high activity"
        if ratio <= 0.5:
            return "low activity"
    return "normal activity"


PRICE_FLOW_VOLUME_INTERPRETATIONS = {
    ("up", "inflow", "high"): ("Confirmed Accumulation", "Strong buying supports the uptrend with high participation.", "Strong Risk-On"),
    ("up", "inflow", "normal"): ("Steady Accumulation", "Buying continues to support the uptrend.", "Risk-On"),
    ("up", "inflow", "low"): ("Quiet Accumulation", "Buying supports the trend but participation is limited.", "Mild Risk-On"),
    ("up", "neutral", "high"): ("Momentum Rally", "Strong trading activity but no clear net buying or selling.", "Watch"),
    ("up", "neutral", "normal"): ("Price Leadership", "Price continues higher without clear buying support.", "Neutral Bullish"),
    ("up", "neutral", "low"): ("Fragile Rally", "Rising prices on limited participation.", "Low Confidence"),
    ("up", "outflow", "high"): ("Distribution into Strength", "Heavy selling occurs despite rising prices.", "Bearish Divergence"),
    ("up", "outflow", "normal"): ("Profit Taking", "Moderate selling during an uptrend.", "Slightly Bearish"),
    ("up", "outflow", "low"): ("Weak Distribution", "Limited selling pressure despite rising prices.", "Neutral"),
    ("flat", "inflow", "high"): ("Strong Accumulation", "Heavy buying while prices consolidate.", "Early Bullish"),
    ("flat", "inflow", "normal"): ("Quiet Accumulation", "Buying during consolidation.", "Improving"),
    ("flat", "inflow", "low"): ("Early Accumulation", "Initial buying with limited conviction.", "Watch"),
    ("flat", "neutral", "high"): ("High-Turnover Consolidation", "Heavy trading but balanced buying and selling.", "Transition"),
    ("flat", "neutral", "normal"): ("Neutral", "Balanced market conditions.", "Neutral"),
    ("flat", "neutral", "low"): ("Dormant Market", "Little activity or useful information.", "Neutral"),
    ("flat", "outflow", "high"): ("Distribution Before Breakdown", "Heavy selling while prices remain stable.", "Early Bearish"),
    ("flat", "outflow", "normal"): ("Quiet Distribution", "Steady selling during consolidation.", "Weakening"),
    ("flat", "outflow", "low"): ("Weak Distribution", "Limited selling pressure.", "Neutral"),
    ("down", "inflow", "high"): ("Aggressive Dip Buying", "Strong buying during a decline.", "Recovery Candidate"),
    ("down", "inflow", "normal"): ("Contrarian Buying", "Buying emerges despite weakness.", "Watch"),
    ("down", "inflow", "low"): ("Tentative Buying", "Limited buying interest.", "Low Confidence"),
    ("down", "neutral", "high"): ("Heavy Selling Pressure", "Heavy trading accompanies price weakness but without clear net buying or selling.", "Cautious"),
    ("down", "neutral", "normal"): ("Unconfirmed Weakness", "Prices fall without clear buying or selling imbalance.", "Neutral Bearish"),
    ("down", "neutral", "low"): ("Weak Downtrend", "Low-conviction decline.", "Neutral"),
    ("down", "outflow", "high"): ("Confirmed Distribution", "Strong selling confirms the downtrend.", "Strong Risk-Off"),
    ("down", "outflow", "normal"): ("Persistent Distribution", "Continued selling pressure.", "Risk-Off"),
    ("down", "outflow", "low"): ("Thin Distribution", "Selling persists but with limited participation.", "Mild Risk-Off"),
}


def _flow_direction(row: pd.Series, horizon: int, cfg: ETFAnalyticsConfig) -> str:
    z = row.get(f"flow_zscore_{horizon}d")
    pct = row.get(f"flow_pct_aum_{horizon}d")
    band = cfg.representative.neutral_flow_pct_aum
    if pd.notna(z):
        if z >= 1.0:
            return "positive"
        if z <= -1.0:
            return "negative"
    if pd.notna(pct):
        if pct > band:
            return "positive"
        if pct < -band:
            return "negative"
    return "neutral"


def _flow_structure(row: pd.Series, cfg: ETFAnalyticsConfig) -> tuple[str, float, str]:
    """Explain whether current flow is tactical or structural; replaces rotation state."""
    current = str(row.get("flow_state") or "neutral")
    d20 = _flow_direction(row, 20, cfg)
    d60 = _flow_direction(row, 60, cfg)
    p20 = row.get("flow_persistence_20d")
    z_values = [row.get(name) for name in ("flow_zscore_1d", "flow_zscore_5d", "flow_zscore_20d", "flow_zscore_60d")]
    z_values = [float(value) for value in z_values if pd.notna(value)]

    confidence_modifier = 0.0
    tags: list[str] = []

    if z_values and max(z_values) > 2 and current != "outflow":
        tags.append("Exceptional institutional buying")
        confidence_modifier += 8
    if z_values and min(z_values) < -2 and current != "inflow":
        tags.append("Exceptional institutional selling")
        confidence_modifier -= 8
    if pd.notna(p20) and float(p20) > 0.70:
        tags.append("Strong sponsorship")
        confidence_modifier += 8
    elif pd.notna(p20) and float(p20) < 0.30:
        tags.append("Persistent selling")
        confidence_modifier -= 8

    if current == "inflow" and d20 == "positive" and d60 == "positive":
        base = "Strong confirmation"
        confidence_modifier += 10
    elif current == "outflow" and d20 == "negative" and d60 == "negative":
        base = "Strong confirmation"
        confidence_modifier += 10
    elif current == "outflow" and d20 == "positive" and d60 == "positive":
        base = "Tactical profit-taking inside structural accumulation"
        confidence_modifier -= 2
    elif current == "inflow" and d20 == "negative" and d60 == "negative":
        base = "Tactical rebound inside structural distribution"
        confidence_modifier -= 2
    elif d20 == "positive" and d60 == "positive":
        base = "Structural accumulation"
        confidence_modifier += 6
    elif d20 == "negative" and d60 == "negative":
        base = "Structural distribution"
        confidence_modifier -= 6
    elif d20 == "positive" and d60 == "negative":
        base = "Medium-term recovery"
        confidence_modifier += 2
    elif d20 == "negative" and d60 == "positive":
        base = "Medium-term deterioration"
        confidence_modifier -= 2
    else:
        base = "Mixed or neutral flow structure"

    narrative = base if not tags else f"{base}; {', '.join(tags)}"
    return narrative, confidence_modifier, base

def _volume_bucket(volume_state: str) -> str:
    if "high" in volume_state:
        return "high"
    if "low" in volume_state:
        return "low"
    return "normal"


def _price_flow_volume(row: pd.Series) -> tuple[str, str, str, float, float]:
    state, interpretation, regime_bias = PRICE_FLOW_VOLUME_INTERPRETATIONS.get(
        (row["price_state"], row["flow_state"], _volume_bucket(row["volume_state"])),
        ("Neutral", "Balanced market.", "Neutral"),
    )
    z = row.get("flow_zscore_20d")
    volume_z = row.get("volume_zscore_60d")
    strength = clamp(50 + (0 if pd.isna(z) else abs(float(z)) * 12) + (0 if pd.isna(volume_z) else min(abs(float(volume_z)) * 4, 12)))
    confidence = clamp(float(row.get("data_quality_score") or 50) * 0.5 + (20 if pd.notna(z) else 0) + (15 if pd.notna(volume_z) else 0))
    return state, interpretation, regime_bias, strength, confidence


def build_coverage_audit(raw: pd.DataFrame) -> pd.DataFrame:
    rows = []
    tickers = {str(row.primary_ticker).upper() for row in REPRESENTATIVE_MAP}
    for item in REPRESENTATIVE_MAP:
        for ticker in [item.primary_ticker, item.secondary_ticker, item.tertiary_ticker]:
            if ticker:
                tickers.add(ticker.upper())
    for ticker in sorted(tickers):
        data = raw[raw["ticker"].astype(str).str.upper().eq(ticker)] if not raw.empty else pd.DataFrame()
        missing = []
        for field in ("shares_outstanding", "nav", "aum", "close"):
            if data.empty or field not in data or data[field].isna().all():
                missing.append(field)
        if data.empty or "volume" not in data or data["volume"].isna().all():
            missing.append("volume")
        issuer = None if data.empty else data["issuer"].dropna().astype(str).iloc[-1] if data["issuer"].notna().any() else None
        rows.append(
            {
                "ticker": ticker,
                "issuer": issuer,
                "exposure": next((item.exposure_name for item in REPRESENTATIVE_MAP if ticker in {item.primary_ticker, item.secondary_ticker, item.tertiary_ticker}), None),
                "available": not data.empty and not missing,
                "history_start": None if data.empty else data["date"].min(),
                "history_days": int(data["date"].nunique()) if not data.empty else 0,
                "flow_eligible": not data.empty and not missing,
                "missing_fields": missing,
            }
        )
    return pd.DataFrame(rows)


def build_etf_flow_signal_daily(daily: pd.DataFrame, raw: pd.DataFrame, config: ETFAnalyticsConfig | None = None) -> pd.DataFrame:
    cfg = config or ETFAnalyticsConfig()
    if daily.empty:
        return pd.DataFrame()
    rep_by_ticker: dict[str, RepresentativeETF] = {}
    for item in REPRESENTATIVE_MAP:
        for ticker in [item.primary_ticker, item.secondary_ticker, item.tertiary_ticker]:
            if ticker:
                rep_by_ticker.setdefault(ticker.upper(), item)
    df = daily.copy().sort_values(["ticker", "date"])
    df["ticker"] = df["ticker"].astype(str).str.upper()
    df = df[df["ticker"].isin(rep_by_ticker)].copy()
    if df.empty:
        return pd.DataFrame()
    raw_volume = raw[["date", "ticker", "volume"]].copy() if "volume" in raw else pd.DataFrame()
    if not raw_volume.empty:
        raw_volume["ticker"] = raw_volume["ticker"].astype(str).str.upper()
        raw_volume["date"] = pd.to_datetime(raw_volume["date"]).dt.date
        df = df.drop(columns=["volume"], errors="ignore").merge(raw_volume, on=["date", "ticker"], how="left")
    grouped = df.groupby("ticker", group_keys=False)
    df["price"] = pd.to_numeric(df["close"], errors="coerce").fillna(pd.to_numeric(df["nav"], errors="coerce"))
    df["volume"] = pd.to_numeric(df.get("volume"), errors="coerce")
    for horizon in cfg.representative.horizons:
        df[f"flow_{horizon}d"] = grouped["estimated_flow"].transform(lambda x, h=horizon: x.rolling(h, min_periods=1).sum())
        lag_aum = grouped["aum"].shift(horizon).replace(0, pd.NA)
        df[f"flow_pct_aum_{horizon}d"] = df[f"flow_{horizon}d"] / lag_aum
    df["flow_zscore_1d"] = grouped["flow_pct_aum_1d"].transform(lambda x: _rolling_z(x, cfg.representative.short_zscore_lookback, cfg.representative.minimum_observations))
    df["flow_zscore_5d"] = grouped["flow_pct_aum_5d"].transform(lambda x: _rolling_z(x, cfg.representative.short_zscore_lookback, cfg.representative.minimum_observations))
    df["flow_zscore_20d"] = grouped["flow_pct_aum_20d"].transform(lambda x: _rolling_z(x, cfg.representative.long_zscore_lookback, cfg.representative.minimum_observations))
    df["flow_zscore_60d"] = grouped["flow_pct_aum_60d"].transform(lambda x: _rolling_z(x, cfg.representative.long_zscore_lookback, cfg.representative.minimum_observations))
    df["flow_percentile_20d"] = grouped["flow_pct_aum_20d"].transform(lambda x: _rolling_percentile(x, cfg.representative.long_zscore_lookback, cfg.representative.minimum_observations))
    df["flow_percentile_60d"] = grouped["flow_pct_aum_60d"].transform(lambda x: _rolling_percentile(x, cfg.representative.long_zscore_lookback, cfg.representative.minimum_observations))
    df["positive_flow_days_20d"] = grouped["estimated_flow"].transform(lambda x: (x > 0).rolling(20, min_periods=1).sum())
    df["positive_flow_days_60d"] = grouped["estimated_flow"].transform(lambda x: (x > 0).rolling(60, min_periods=1).sum())
    df["flow_persistence_20d"] = df["positive_flow_days_20d"] / 20.0
    df["flow_persistence_60d"] = df["positive_flow_days_60d"] / 60.0
    df["consecutive_inflow_days"] = grouped["estimated_flow"].transform(lambda x: _consecutive(x, True))
    df["consecutive_outflow_days"] = grouped["estimated_flow"].transform(lambda x: _consecutive(x, False))
    df["flow_momentum"] = df["flow_pct_aum_5d"] - df["flow_pct_aum_20d"]
    df["flow_acceleration"] = grouped["flow_momentum"].transform(lambda x: x - x.shift(5))
    df["volume_ratio_20d"] = df["volume"] / grouped["volume"].transform(lambda x: x.rolling(20, min_periods=5).mean()).replace(0, pd.NA)
    df["volume_ratio_60d"] = df["volume"] / grouped["volume"].transform(lambda x: x.rolling(60, min_periods=10).mean()).replace(0, pd.NA)
    df["volume_zscore_20d"] = grouped["volume"].transform(lambda x: _rolling_z(x, 20, 10))
    df["volume_zscore_60d"] = grouped["volume"].transform(lambda x: _rolling_z(x, 60, 20))
    df["dollar_volume"] = df["price"] * df["volume"]
    df["dollar_volume_ratio_20d"] = df["dollar_volume"] / grouped["dollar_volume"].transform(lambda x: x.rolling(20, min_periods=5).mean()).replace(0, pd.NA)
    df["dollar_volume_zscore_60d"] = grouped["dollar_volume"].transform(lambda x: _rolling_z(x, 60, 20))
    df["return_5d"] = grouped["price"].transform(lambda x: x.pct_change(5))
    df["return_20d"] = grouped["price"].transform(lambda x: x.pct_change(20))
    df["ma_20"] = grouped["price"].transform(lambda x: x.rolling(20, min_periods=5).mean())
    df["price_state"] = [
        "up" if pd.notna(row.return_20d) and row.return_20d > 0.01 and row.price > row.ma_20
        else "down" if pd.notna(row.return_20d) and row.return_20d < -0.01 and row.price < row.ma_20
        else "flat"
        for row in df.itertuples()
    ]
    df["flow_state"] = [_flow_state(row, cfg) for _, row in df.iterrows()]
    df["volume_state"] = [_volume_state(row) for _, row in df.iterrows()]
    pfv = [_price_flow_volume(row) for _, row in df.iterrows()]
    df["price_flow_volume_state"] = [item[0] for item in pfv]
    df["interpretation"] = [item[1] for item in pfv]
    df["regime_bias"] = [item[2] for item in pfv]
    df["state_strength"] = [item[3] for item in pfv]
    df["state_confidence"] = [item[4] for item in pfv]
    flow_structure = [_flow_structure(row, cfg) for _, row in df.iterrows()]
    df["flow_structure"] = [item[0] for item in flow_structure]
    df["confidence_modifier"] = [item[1] for item in flow_structure]
    df["flow_structure_base"] = [item[2] for item in flow_structure]
    df["state_confidence"] = [
        clamp(conf + modifier)
        for conf, modifier in zip(df["state_confidence"], df["confidence_modifier"])
    ]
    df["exposure_id"] = df["ticker"].map(lambda ticker: rep_by_ticker[ticker].exposure_id)
    df["exposure_type"] = df["ticker"].map(lambda ticker: rep_by_ticker[ticker].exposure_type)
    return df


def _score_from_z(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return clamp(50 + float(value) * 12.5)


def _ticker_score(latest: pd.DataFrame, ticker: str) -> float | None:
    rows = latest[latest["ticker"].eq(ticker)]
    if rows.empty:
        return None
    row = rows.iloc[0]
    parts = [_score_from_z(row.get("flow_zscore_20d")), _score_from_z(row.get("flow_zscore_60d"))]
    parts = [part for part in parts if part is not None]
    if not parts:
        pct = row.get("flow_pct_aum_20d")
        return clamp(50 + (0 if pd.isna(pct) else float(pct) * 500))
    return float(sum(parts) / len(parts))


def _weighted_score(latest: pd.DataFrame, weights: dict[str, float], default: float = 50.0) -> float:
    values = []
    for ticker, weight in weights.items():
        score = _ticker_score(latest, ticker)
        if score is not None:
            values.append((score, weight))
    if not values:
        return default
    total_weight = sum(weight for _, weight in values)
    return float(sum(score * weight for score, weight in values) / total_weight)


def _market_flow_regime(score: float) -> str:
    if score >= 75:
        return "Strong Broad Risk-On"
    if score >= 65:
        return "Moderate Risk-On"
    if score >= 55:
        return "Selective Risk-On"
    if score >= 45:
        return "Mixed / Neutral"
    if score >= 35:
        return "Defensive Rotation"
    if score >= 25:
        return "Moderate Risk-Off"
    return "Strong Risk-Off"


def build_market_flow(signal_daily: pd.DataFrame) -> dict[str, Any]:
    if signal_daily.empty:
        return {}
    as_of = signal_daily["date"].max()
    latest = signal_daily[signal_daily["date"].eq(as_of)].copy()
    equity = _weighted_score(latest, {"IVV": 0.30, "ACWI": 0.10, "EFA": 0.10, "IEMG": 0.10, "IJH": 0.15, "IWM": 0.25})
    credit = _weighted_score(latest, {"HYG": 0.70, "LQD": 0.30})
    cyclical = _weighted_score(latest, {"XLY": 1, "XLE": 1, "XLF": 1, "XLI": 1, "XLK": 1, "XLB": 1, "XLC": 1, "IAT": 1, "ITA": 1, "SOXX": 1})
    defensive = _weighted_score(latest, {"XLP": 1, "XLV": 1, "XLU": 1, "XLRE": 1, "IYZ": 1})
    sector_cyclicality = clamp(50 + (cyclical - defensive) * 0.5)
    cash = _weighted_score(latest, {"SGOV": 1, "BIL": 1})
    duration = _weighted_score(latest, {"SHY": 0.2, "IEF": 0.35, "TLT": 0.45})
    duration_liquidity = clamp((cash * 0.45) + (duration * 0.55))
    gold = _ticker_score(latest, "GLD") or 50.0
    bitcoin = _ticker_score(latest, "IBIT") or 50.0
    alternative = (gold + bitcoin) / 2.0
    market_score = clamp(equity * 0.35 + credit * 0.20 + sector_cyclicality * 0.25 + duration_liquidity * 0.15 + alternative * 0.05)
    if bitcoin > 55 and gold < 45:
        alt_interpretation = "speculative risk preference"
    elif bitcoin < 45 and gold > 55:
        alt_interpretation = "defensive or uncertainty hedge"
    elif bitcoin > 55 and gold > 55:
        alt_interpretation = "barbell demand"
    elif bitcoin < 45 and gold < 45:
        alt_interpretation = "reduced alternative-asset allocation"
    else:
        alt_interpretation = "mixed alternative-asset demand"
    reliability = clamp(float(latest["state_confidence"].dropna().mean()) if latest["state_confidence"].notna().any() else 50.0)
    return {
        "date": as_of,
        "equity_risk_flow_score": equity,
        "credit_risk_flow_score": credit,
        "sector_cyclicality_flow_score": sector_cyclicality,
        "cash_preference_score": cash,
        "duration_demand_score": duration,
        "duration_liquidity_score": duration_liquidity,
        "gold_signal": gold,
        "bitcoin_signal": bitcoin,
        "alternative_asset_score": alternative,
        "alternative_asset_interpretation": alt_interpretation,
        "market_flow_score": market_score,
        "market_flow_regime": _market_flow_regime(market_score),
        "market_flow_reliability": reliability,
    }


def build_divergence_flags(signal_daily: pd.DataFrame, config: ETFAnalyticsConfig | None = None) -> pd.DataFrame:
    cfg = config or ETFAnalyticsConfig()
    if signal_daily.empty:
        return pd.DataFrame()
    as_of = signal_daily["date"].max()
    latest = signal_daily[signal_daily["date"].eq(as_of)].set_index("ticker", drop=False)
    flags = []

    def add_pair(exposure_id: str, primary: str, comparison: str, reason: str, related: bool = False) -> None:
        if primary not in latest.index or comparison not in latest.index:
            return
        a = latest.loc[primary]
        b = latest.loc[comparison]
        az = a.get("flow_zscore_20d")
        bz = b.get("flow_zscore_20d")
        if pd.isna(az) or pd.isna(bz):
            return
        opposite = (az > 0 > bz) or (az < 0 < bz)
        material = abs(float(az) - float(bz)) > cfg.representative.divergence_threshold_z
        extreme = abs(float(az)) >= cfg.representative.extreme_zscore_threshold or abs(float(bz)) >= cfg.representative.extreme_zscore_threshold
        state_mismatch = a.get("price_flow_volume_state") != b.get("price_flow_volume_state")
        if opposite or material or extreme or state_mismatch:
            flags.append(
                {
                    "date": as_of,
                    "flag_type": "related_subsector_divergence" if related else "close_substitute_divergence",
                    "severity": "medium" if related else "high",
                    "exposure_id": exposure_id,
                    "primary_ticker": primary,
                    "comparison_ticker": comparison,
                    "description": f"{primary} and {comparison} ETF flow/price states diverge.",
                    "interpretation": reason,
                }
            )

    for item in REPRESENTATIVE_MAP:
        if item.secondary_ticker:
            add_pair(item.exposure_id, item.primary_ticker, item.secondary_ticker, "primary representative differs from close substitute")
    for exposure_id, primary, comparison, reason in RELATED_COMPARISONS:
        add_pair(exposure_id, primary, comparison, reason, related=True)
    return pd.DataFrame(flags)

