"""Configuration and shared constants for deterministic market update reports."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCORING_CONFIG_PATH = PROJECT_ROOT / "config" / "scoring_weights.yaml"

DEFAULT_WEIGHTS = {
    "market_regime": {
        "equity_trend": 0.18,
        "equity_momentum": 0.14,
        "market_breadth": 0.15,
        "volatility": 0.12,
        "rates_yield_curve": 0.10,
        "credit_proxy": 0.05,
        "dollar_fx": 0.05,
        "commodity_confirmation": 0.06,
        "news_confirmation": 0.05,
        "market_strength": 0.10,
    },
    "sector_strength": {
        "relative_strength": 0.20,
        "absolute_momentum": 0.18,
        "trend": 0.20,
        "rsi": 0.08,
        "macd": 0.08,
        "volume": 0.06,
        "volatility_adjusted_return": 0.08,
        "breadth": 0.08,
        "news": 0.04,
    },
    "theme_strength": {
        "equal_weight_return": 0.16,
        "relative_return": 0.18,
        "breadth_50d": 0.12,
        "breadth_200d": 0.12,
        "rsi": 0.08,
        "macd": 0.08,
        "volume": 0.06,
        "volatility_adjusted_return": 0.10,
        "news_intensity": 0.05,
        "headline_ratio": 0.05,
    },
    "outperformance_setup": {
        "relative_strength_20d": 0.18,
        "relative_strength_60d": 0.18,
        "trend_persistence": 0.14,
        "breadth": 0.12,
        "volatility_adjusted_momentum": 0.12,
        "volume_accumulation": 0.08,
        "drawdown_recovery": 0.06,
        "news_acceleration": 0.05,
        "downside_volatility": 0.04,
        "relative_vs_qqq": 0.03,
    },
}

SECTOR_ETF_MAP = {
    "Technology": ["XLK", "QQQ"],
    "Semiconductors": ["SMH", "SOXX"],
    "Cybersecurity": ["CIBR"],
    "Defense": ["XAR"],
    "Healthcare": ["XLV"],
    "Financials": ["XLF"],
    "Energy": ["XLE"],
    "Utilities": ["XLU", "UTES"],
    "Real Estate": ["XLRE"],
    "Consumer Discretionary": ["XLY"],
    "Consumer Staples": ["XLP"],
    "Industrials": ["XLI"],
    "Grid Infrastructure": ["GRID"],
    "Nuclear": ["NLR"],
    "Crypto": ["BTC-USD", "ETH-USD"],
}

THEME_BASKETS = {
    "AI Infrastructure": ["QQQ", "XLK", "SMH", "SOXX", "NVDA", "MSFT", "GOOGL", "AMZN"],
    "Semiconductors": ["SMH", "SOXX", "NVDA", "AMD", "AVGO", "TSM", "ASML"],
    "Cybersecurity": ["CIBR", "PANW", "CRWD", "ZS", "FTNT"],
    "Defense": ["XAR", "LMT", "RTX", "NOC", "AVAV"],
    "Healthcare Innovation": ["XLV", "IBB", "UNH", "LLY", "MRK"],
    "Grid Infrastructure": ["GRID", "UTES", "XLU", "ETN", "PWR"],
    "Nuclear": ["NLR", "CCJ", "UEC", "LEU"],
    "Energy": ["XLE", "CL=F", "BZ=F", "XOM", "CVX"],
    "Financials": ["XLF", "JPM", "BAC", "GS"],
    "Crypto Infrastructure": ["BTC-USD", "ETH-USD", "COIN", "MSTR"],
    "Small Caps": ["IWM", "RTY=F"],
    "Dividend Defensives": ["XLP", "XLU", "XLV"],
    "Quality Growth": ["QQQ", "XLK", "MSFT", "AAPL", "GOOGL"],
}

SECTOR_THEME_MAP = {
    "Technology": ["AI Infrastructure", "Semiconductors", "Quality Growth"],
    "Semiconductors": ["AI Infrastructure", "Semiconductors"],
    "Cybersecurity": ["Cybersecurity"],
    "Defense": ["Defense"],
    "Healthcare": ["Healthcare Innovation"],
    "Financials": ["Financials"],
    "Energy": ["Energy"],
    "Utilities": ["Grid Infrastructure", "Nuclear", "Dividend Defensives"],
    "Real Estate": ["Dividend Defensives"],
    "Consumer Discretionary": ["Small Caps"],
    "Consumer Staples": ["Dividend Defensives"],
    "Industrials": ["Defense", "Grid Infrastructure"],
    "Grid Infrastructure": ["Grid Infrastructure"],
    "Nuclear": ["Nuclear"],
    "Crypto": ["Crypto Infrastructure"],
}

CORE_MARKET_TICKERS = sorted(
    {
        "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP",
        "XLU", "XLRE", "XLB", "XLC", "SMH", "SOXX", "CIBR", "XAR", "NLR", "GRID", "UTES", "TAN",
        "RSP", "IWF", "IWD", "MDY",
        "BTC-USD", "ETH-USD",
        *[ticker for tickers in SECTOR_ETF_MAP.values() for ticker in tickers],
        *[ticker for tickers in THEME_BASKETS.values() for ticker in tickers],
    }
)

MACRO_SYMBOLS = [
    "^GSPC", "^IXIC", "^DJI", "^RUT", "^VIX", "^MOVE", "^FVX", "^TNX", "^TYX",
    "DXY", "DX-Y.NYB", "GC=F", "SI=F", "CL=F", "BZ=F", "HG=F", "BTC-USD", "ETH-USD",
    "ES=F", "NQ=F", "RTY=F",
    "HYG", "LQD", "JNK", "RSP", "IWF", "IWD", "TLT", "IEF", "SHY",
]


def scoring_weights() -> dict:
    """Return deterministic scoring weights.

    The YAML file is an operator-facing config artifact. The code uses built-in
    defaults to avoid adding a PyYAML dependency to the project.
    """
    return DEFAULT_WEIGHTS
