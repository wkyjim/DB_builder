"""Canonical economic exposure mapping for ETF flow analytics."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ETFExposure:
    exposure_id: str
    exposure_name: str
    exposure_type: str
    benchmark_family: str | None = None
    is_primary_proxy: bool = False
    allocation_weight_cap: float = 0.50


SEGMENT_EXPOSURE_MAP: dict[str, ETFExposure] = {
    "Broad Equity": ETFExposure("US_BROAD_EQUITY", "Broad U.S. Equity", "asset_class", "US equity", True),
    "Total U.S. Equity": ETFExposure("US_BROAD_EQUITY", "Broad U.S. Equity", "asset_class", "US equity"),
    "Dow Industrials": ETFExposure("US_LARGE_CAP", "U.S. Large Cap", "market_cap", "US large cap"),
    "Large Caps": ETFExposure("US_LARGE_CAP", "U.S. Large Cap", "market_cap", "US large cap"),
    "Mid Caps": ETFExposure("US_MID_CAP", "U.S. Mid Cap", "market_cap", "US mid cap"),
    "Small Caps": ETFExposure("US_SMALL_CAP", "Small Caps", "market_cap", "US small cap", True),
    "Equal-Weight Equity": ETFExposure("US_EQUAL_WEIGHT", "Equal-Weight U.S. Equity", "style", "US equal weight", True),
    "Growth": ETFExposure("US_LARGE_GROWTH", "Large-Cap Growth", "style", "US growth", True),
    "Value": ETFExposure("US_LARGE_VALUE", "Large-Cap Value", "style", "US value", True),
    "Technology": ETFExposure("US_TECHNOLOGY", "Technology", "sector", "US sector", True),
    "Semiconductors": ETFExposure("US_SEMICONDUCTORS", "Semiconductors", "sector", "US semiconductors", True),
    "Financials": ETFExposure("US_FINANCIALS", "Financials", "sector", "US sector", True),
    "Energy": ETFExposure("US_ENERGY", "Energy", "sector", "US sector", True),
    "Healthcare": ETFExposure("US_HEALTHCARE", "Healthcare", "sector", "US sector", True),
    "Industrials": ETFExposure("US_INDUSTRIALS", "Industrials", "sector", "US sector", True),
    "Utilities": ETFExposure("US_UTILITIES", "Utilities", "sector", "US sector", True),
    "Real Estate": ETFExposure("US_REAL_ESTATE", "Real Estate", "sector", "US sector", True),
    "Consumer Discretionary": ETFExposure("US_CONSUMER_DISCRETIONARY", "Consumer Discretionary", "sector", "US sector", True),
    "Consumer Staples": ETFExposure("US_CONSUMER_STAPLES", "Consumer Staples", "sector", "US sector", True),
    "Communication Services": ETFExposure("US_COMMUNICATION_SERVICES", "Communication Services", "sector", "US sector", True),
    "Materials": ETFExposure("US_MATERIALS", "Materials", "sector", "US sector", True),
    "Cybersecurity": ETFExposure("CYBERSECURITY", "Cybersecurity", "theme", "cybersecurity", True),
    "Defense": ETFExposure("DEFENSE", "Defense", "theme", "defense", True),
    "Grid Infrastructure": ETFExposure("GRID_INFRASTRUCTURE", "Grid Infrastructure", "theme", "grid infrastructure", True),
    "Nuclear": ETFExposure("NUCLEAR", "Nuclear", "theme", "nuclear", True),
    "Gold": ETFExposure("GOLD", "Gold", "commodity", "gold", True),
    "Silver": ETFExposure("SILVER", "Silver", "commodity", "silver", True),
    "Long Duration Treasury": ETFExposure("US_TREASURY_LONG", "Long-Duration Treasuries", "duration", "US Treasury", True),
    "Intermediate Treasury": ETFExposure("US_TREASURY_INTERMEDIATE", "Intermediate Treasuries", "duration", "US Treasury", True),
    "Short Treasury": ETFExposure("US_TREASURY_SHORT", "Short Treasuries", "duration", "US Treasury", True),
    "Treasury Bills": ETFExposure("US_TREASURY_BILLS", "Treasury Bills", "duration", "US Treasury", True),
    "U.S. Treasuries": ETFExposure("US_TREASURY_INTERMEDIATE", "Intermediate Treasuries", "duration", "US Treasury"),
    "Core Bonds": ETFExposure("US_CORE_BONDS", "Core Bonds", "credit", "US aggregate bonds", True),
    "Investment Grade Credit": ETFExposure("US_INVESTMENT_GRADE", "Investment-Grade Credit", "credit", "US credit", True),
    "High Yield Credit": ETFExposure("US_HIGH_YIELD", "High-Yield Credit", "credit", "US high yield", True),
    "Developed Markets ex-US": ETFExposure("DEVELOPED_EX_US", "Developed Markets ex-U.S.", "region", "developed ex-US", True),
    "International Equity": ETFExposure("DEVELOPED_EX_US", "Developed Markets ex-U.S.", "region", "developed ex-US"),
    "Emerging Markets": ETFExposure("EMERGING_MARKETS", "Emerging Markets", "region", "emerging markets", True),
    "Global Equity": ETFExposure("GLOBAL_EQUITY", "Global Equity", "region", "global equity", True),
    "Bitcoin": ETFExposure("BITCOIN", "Bitcoin", "digital_asset", "bitcoin", True),
    "Quality Factor": ETFExposure("QUALITY_FACTOR", "Quality Factor", "factor", "quality", True),
    "Dividend Growth": ETFExposure("DIVIDEND_DEFENSIVES", "Dividend Defensives", "factor", "dividend", True),
    "Equity Factor Rotation": ETFExposure("FACTOR_ROTATION", "Factor Rotation", "factor", "factor rotation", True),
}


def exposure_for_segment(segment: str | None) -> ETFExposure:
    if not segment:
        return ETFExposure("UNCLASSIFIED", "Unclassified ETF Exposure", "unknown")
    return SEGMENT_EXPOSURE_MAP.get(
        str(segment),
        ETFExposure(str(segment).upper().replace(" ", "_").replace("/", "_").replace("-", "_"), str(segment), "theme"),
    )


def attach_exposure_columns(row: dict) -> dict:
    exposure = exposure_for_segment(row.get("primary_segment") or row.get("segment"))
    return {
        **row,
        "exposure_id": exposure.exposure_id,
        "exposure_name": exposure.exposure_name,
        "exposure_type": exposure.exposure_type,
        "benchmark_family": exposure.benchmark_family,
        "is_primary_proxy": exposure.is_primary_proxy,
        "allocation_weight_cap": exposure.allocation_weight_cap,
    }
