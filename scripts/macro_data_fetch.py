import os
import json
import time
import random
import math
import re
import urllib.request
import urllib.error
import pandas as pd
import yfinance as yf
import argparse
import sys

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import text

import _bootstrap  # noqa: F401

# Investing.com market data is HTML-only. The Investing.com JSON API
# (api.investing.com, used by the `investiny` package) is blocked for this
# project: it returns HTTP 403 and is explicitly out of scope. All
# Investing.com series are therefore fetched by scraping the public
# historical-data HTML pages with a browser TLS fingerprint.
import investing_html_scraper as _investing_html

from db_builder.config import MACRO_TABLE, local_engine as make_local_engine
from db_builder.config import neon_engine as make_neon_engine
from db_builder.trading_calendar import latest_completed_nyse_session_date
from db_builder.session_calendar import (
    CMT_SYMBOLS,
    MARKET_REFERENCE_SYMBOLS,
    ROUTE_DEFERRED,
    ROUTE_HISTORICAL,
    ROUTE_REJECT,
    SessionStatus,
    TENOR_BY_CMT_SYMBOL,
    TREASURY_PUBLICATION_SYMBOLS,
    TREASURY_SYMBOLS,
    historical_eligibility,
    adapter_for,
    previous_valid_session,
    previous_valid_observation_date,
    SOURCE_DEFINITION_BY_SYMBOL,
    SOURCE_DEF_INVESTING_CONTINUOUS_FUTURES_DAILY,
    treasury_publication_confirmed,
)
from db_builder.deferred_history import (
    MAX_ATTEMPTS,
    RECOVERY_BATCH,
    RECOVERY_DAYS,
    SqlDeferredStore,
    ProviderBackoffStore,
)
from db_builder import retry_policy as _retry_policy
from db_builder.retry_policy import (
    PROVIDER_INVESTING,
    PROVIDER_YFINANCE,
    PROVIDER_TREASURY,
    PROVIDER_UNKNOWN,
    RETRY_ELIGIBLE,
    BACKOFF_ACTIVE,
    MANUAL_REVIEW_REQUIRED,
    PROVENANCE_UNVERIFIED,
)
from db_builder.reconciliation import (
    AUTO_CORRECT_SYMBOLS,
    DETECT_ONLY_SYMBOLS,
    EXCLUDED_REPORT_ONLY_SYMBOLS,
    LEGACY_TREASURY_SYMBOLS,
    Reason,
    Row,
    Verdict,
    build_correction_plan,
    check_prior_dependency,
    decide,
    is_auto_correctable,
    is_detect_only,
    is_excluded_report_only,
    is_legacy_treasury,
)
from db_builder.reconciliation_store import (
    AuditEventType,
    ReconciliationStore,
    execute_correction_plan,
    record_detection_event,
)

# ============================================================
# CONFIG
# ============================================================

TABLE_NAME = MACRO_TABLE
LIVE_TABLE_NAME = "public.macro_live"

BATCH_SIZE = 50
GLOBAL_BUFFER_MINUTES = 30

# Phase A: official Treasury.gov session dates actually observed in this run.
# Populated by the Treasury fetch path and consumed by the EOD gate so that
# USnYT=X requires the *actual* publication for the specific session rather
# than a time-based assumption.
_TREASURY_PUBLISHED_DATES: set = set()
# Phase A: official (symbol, session_date) -> official par-yield value actually
# observed in the Treasury.gov response. A date-only check must never authorise
# an unofficial value, so the gate verifies the row's own close against this.
_TREASURY_PUBLISHED_VALUES: dict = {}
# Absolute tolerance when matching a stored close to the official par yield.
# Par yields are published to 2-4 dp; anything coarser than this is a different
# source, not rounding.
TREASURY_VALUE_TOLERANCE = 5e-5

# Phase C.1: immutable trust-start mapping for HIGH-confidence provenance anchors.
# Keyed by (symbol, source_definition). Only entries present here are authoritative.
# Sorted deterministically by symbol. No family wildcard entries.
SOURCE_DEFINITION_TRUST_START: dict = {
    # Cash indices — 100% exact full-range
    ("^GSPC", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-03",
    ("^DJI", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-03",
    ("^SKEW", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-02",
    ("^TNX", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-03",
    ("^FVX", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-03",
    ("^TYX", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-03",
    ("^HSI", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-03",
    ("^N225", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-07",
    ("^FCHI", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-03",
    # Crypto
    ("ETH-USD", "CRYPTO_24_7_DAILY"): "2025-01-02",
    ("BTC-USD", "CRYPTO_24_7_DAILY"): "2026-07-09",  # outcome B
    # Cash indices — outcome B
    ("^VIX", "YFINANCE_CASH_EQUITY_DAILY"): "2026-06-03",
    ("^KS11", "YFINANCE_CASH_EQUITY_DAILY"): "2026-08-14",
    # CMT — Treasury.gov 100% exact
    ("US2YT_CMT", "TREASURY_GOV_PAR_YIELD_DAILY"): "2026-09-25",
    ("US3YT_CMT", "TREASURY_GOV_PAR_YIELD_DAILY"): "2026-09-25",
    ("US5YT_CMT", "TREASURY_GOV_PAR_YIELD_DAILY"): "2026-09-25",
    ("US7YT_CMT", "TREASURY_GOV_PAR_YIELD_DAILY"): "2026-09-25",
    ("US10YT_CMT", "TREASURY_GOV_PAR_YIELD_DAILY"): "2026-09-25",
    ("US20YT_CMT", "TREASURY_GOV_PAR_YIELD_DAILY"): "2026-09-25",
    ("US30YT_CMT", "TREASURY_GOV_PAR_YIELD_DAILY"): "2026-09-25",
}


def _resolve_trust_start(symbol: str, source_definition: str):
    """Return the trust-start date for (symbol, source_definition) or None.

    Resolution semantics (C1-first fallback):
    1. If exact key exists in SOURCE_DEFINITION_TRUST_START (C1 authoritative):
       use the C1 trust_start.
    2. Else if exact key exists in C3A_APPROVED_ANCHORS:
       use the C3A trust_start.
    3. Else: unresolved (None).

    This prevents C3A anchors from retroactively broadening existing C1 trust
    boundaries. C1 anchors always take precedence.
    """
    from datetime import date as _date
    key = (symbol, source_definition)
    
    # C1 authoritative map takes precedence
    val = SOURCE_DEFINITION_TRUST_START.get(key)
    if val is not None:
        if isinstance(val, _date):
            return val
        return _date.fromisoformat(val)
    
    # C3A approved anchors as fallback
    c3a_val = C3A_APPROVED_ANCHORS.get(key)
    if c3a_val is not None:
        if isinstance(c3a_val, _date):
            return c3a_val
        return _date.fromisoformat(c3a_val)
    
    return None


def _stored_trusted_provenance_dates(
    engine,
    symbol: str,
    source_definition: str,
    min_date,
    max_date,
) -> set:
    """ANCHOR_DERIVED_PROVENANCE: dates with verified same-definition provenance.

    Backed by SOURCE_DEFINITION_TRUST_START. Returns stored dates that satisfy
    the anchor boundary. Only explicit (symbol, source_definition) entries
    qualify. No family inheritance. No DB source-definition column assumed.

    If max_date is None, the query returns all stored dates >= effective_min.
    """
    from datetime import date as _date

    trust_start = _resolve_trust_start(symbol, source_definition)
    if trust_start is None:
        return set()
    effective_min = max(trust_start, min_date) if min_date is not None else trust_start
    if max_date is not None:
        if effective_min > max_date:
            return set()
        sql = text(
            "SELECT date FROM public.macro "
            "WHERE symbol = :symbol AND date >= :effective_min AND date <= :max_date"
        )
        params = {
            "symbol": symbol,
            "effective_min": effective_min,
            "max_date": max_date,
        }
    else:
        sql = text(
            "SELECT date FROM public.macro "
            "WHERE symbol = :symbol AND date >= :effective_min"
        )
        params = {"symbol": symbol, "effective_min": effective_min}
    try:
        with engine.begin() as conn:
            df = pd.read_sql(sql, conn, params=params)
    except Exception:
        return set()
    return {pd.to_datetime(v).date() for v in df["date"]}


def _build_anchor_provenance_lookup(engine):
    """Return the canonical C.2 anchor-derived provenance resolver.

    This is the SINGLE shared trust resolver consumed by deferred recovery.
    It satisfies the ``route_historical_rows`` callback contract exactly::

        provenance_lookup(engine, symbol, min_date, max_date) -> set[date]

    Semantics are ANCHOR_DERIVED_PROVENANCE: trust comes from the immutable
    C.1 ``(symbol, source_definition)`` anchor mapping, NOT from a DB-verified
    source-definition column (``public.macro`` has none). The ``engine``
    argument supplied by the gate is ignored in favour of the captured engine
    so the resolver stays bound to the recovery engine it was built for.

    Symbols without an explicit C.1 anchor resolve to the empty set, so the
    provenance gate keeps failing closed for them (no family inheritance).
    """

    def _resolver(_engine, symbol, min_date, max_date):
        source_definition = SOURCE_DEFINITION_BY_SYMBOL.get(symbol)
        if source_definition is None:
            return set()
        return _stored_trusted_provenance_dates(
            engine, symbol, source_definition, min_date, max_date)

    return _resolver


# Phase A safety telemetry: populated by the EOD gate and consumed by the
# exit-code/alert path so a calendar failure raises an explicit alert instead
# of exiting 0 (coverage measures fetch success, not gate eligibility).
_PHASE_A_UNAVAILABLE_SYMBOLS: list = []
_PHASE_A_DEFERRED_COUNT: int = 0
# D1: deferrals are classified by session/window CONTEXT, not by reason string.
# A boundary deferral is benign (the expected prior session is provably outside
# the current fetch/lookback candidate window); everything else is actionable
# and remains an alert/failure candidate.
_PHASE_A_BOUNDARY_DEFERRED_COUNT: int = 0
_PHASE_A_ACTIONABLE_DEFERRALS: list = []
# Phase A.1: explicit pipeline-fatal conditions (positive list).
# Historical rows safely withheld because confirmation is unavailable are NOT
# automatically pipeline-fatal. Only conditions that make the run's integrity
# unknowable or that affect required current/live data are fatal.
_PIPELINE_FATAL_CONDITIONS: list = []

# D1: the provider fetch window starts at ``today - (LOOKBACK + CALC_BUFFER)``
# days (see ``target_start`` in the fetch paths). A prior session strictly
# before that start was never requested, so it is a benign boundary deferral.
# A prior session at or after the start is inside the requested range: if the
# provider did not supply it, that is an actionable data gap.
_BOUNDARY_WINDOW_TOLERANCE_DAYS = 0

# Target window to insert/update
LOOKBACK_DAYS = 5

# Extra days to fetch before the target window so the first target day
# can calculate prev_close/change/pct_chg/amplitude correctly.
CALC_BUFFER_DAYS = 5

# ============================================================
# C.3A: Rolling Finalization & Gap Backfill
# ============================================================

# Recent refresh: always re-fetch the last N valid sessions for revision detection.
RECENT_REFRESH_SESSIONS = 5

# Gap scan: every run independently scans this many calendar days for missing
# expected sessions.  Independent of RECENT_REFRESH_SESSIONS.
BACKFILL_SCAN_DAYS = 30

# C.3A approved new anchors.  Only these symbols may receive new C.3 trust.
# Keyed by (symbol, source_definition) -> trust_start ISO date.
# Evidence: 100% EXACT_AFTER_STORAGE_ROUNDING over tested sample.
C3A_APPROVED_ANCHORS: dict = {
    ("^GDAXI", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-03",
    ("TLT", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-02",
    ("HYG", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-02",
    ("JNK", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-02",
    ("LQD", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-02",
    ("IWD", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-02",
    ("IWF", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-02",
    ("RSP", "YFINANCE_CASH_EQUITY_DAILY"): "2025-01-02",
}

# A refresh is operationally acceptable only when every core market series is
# available and at least 90% of the requested registry returns a usable daily
# observation. All other configured symbols are supplementary: isolated
# failures may be accepted, but are reported as PARTIAL FAILURE rather than
# SUCCESS. Provider fallback is intentionally outside this policy.
MIN_COVERAGE_PCT = 90.0
APPROVED_MACRO_SYMBOLS = frozenset(
    {
        "^GSPC", "^IXIC", "^NDX", "^DJI", "^RUT", "^VIX", "^SKEW", "^HSI",
        "^N225", "^KS11", "^GDAXI", "^FCHI", "^FTSE", "^MOVE", "000001.SS",
        "TLT", "HYG", "JNK", "LQD", "IWD", "IWF", "RSP", "IEF", "SHY",
        "US2YT=X", "US3YT=X", "US5YT=X", "US7YT=X", "US10YT=X", "US20YT=X",
        "US30YT=X", "US2YT_CMT", "US3YT_CMT", "US5YT_CMT", "US7YT_CMT",
        "US10YT_CMT", "US20YT_CMT", "US30YT_CMT", "HK50", "KOR200c1",
        "CIHc1", "ES=F", "NQ=F", "YM=F", "RTY=F", "NIY=F", "GC=F", "SI=F",
        "BRENT_OILPRICE", "WTI_OILPRICE",
        "HG=F", "NG=F", "BZ=F", "CL=F", "BTC-USD", "ETH-USD", "AUDUSD=X",
        "CHFUSD=X", "CNY=X", "EURUSD=X", "GBPUSD=X", "HKD=X", "JPY=X",
        "NZDUSD=X", "SGD=X", "^FVX", "^TNX", "^TYX", "DX-Y.NYB",
    }
)
REQUIRED_SYMBOLS = APPROVED_MACRO_SYMBOLS

# Asia futures fallback: Investing.com is the primary source for HK50
# (Hang Seng futures), KOR200c1 (KOSPI 200 futures) and CIHc1 (SSE 50
# futures). When the provider is unavailable, yfinance cash-index
# proxies keep the series alive with an explicit source annotation.
# These are CASH INDEX proxies, not futures — spreads/basis vs the
# futures contract are not comparable, but level/direction/trend are.
ASIA_INDEX_YFINANCE_FALLBACK_MAP = {
    "HK50": {"symbol": "^HSI", "name": "Hang Seng Index (cash proxy)"},
    "KOR200c1": {"symbol": "^KS11", "name": "KOSPI Composite Index (cash proxy)"},
    "CIHc1": {"symbol": "000001.SS", "name": "SSE Composite Index (cash proxy)"},
}
ASIA_INDEX_YFINANCE_FALLBACK_SYMBOLS = frozenset(ASIA_INDEX_YFINANCE_FALLBACK_MAP)

# OilPriceAPI spot/indicative series. Deliberately a SEPARATE provider phase:
# these are spot indicative USD/barrel quotes, NOT futures settlement prices, so
# they must never be mixed with BZ=F / CL=F (yfinance futures).
OILPRICEAPI_MAPPING = {
    "BRENT_OILPRICE": "BRENT_CRUDE_USD",
    "WTI_OILPRICE": "WTI_USD",
}
OILPRICEAPI_ASSETS = frozenset(OILPRICEAPI_MAPPING)
OILPRICEAPI_BASE_URL = "https://api.oilpriceapi.com/v1"
OILPRICEAPI_RATE_LIMIT_SECONDS = 2.0

# P1B: U.S. Treasury official par-yield fallback.
# Canonical DB symbols map to Treasury XML BC_*YEAR elements. The Treasury
# par-yield curve is built from the most recently auctioned 2Y/3Y/5Y/7Y/10Y/
# 20Y/30Y securities, so every tenor below has a direct on-the-run input.
UST_TREASURY_FALLBACK_SYMBOLS = frozenset(
    {
        # USnYT=X series are owned ONLY by Investing.com market yield.
        # Treasury.gov fallback is exclusively for CMT symbols.
    }
) | CMT_SYMBOLS
# Treasury XML element map: ONLY CMT symbols.
# USnYT=X series are owned by Investing.com and must never use Treasury.gov.
TREASURY_XML_ELEMENT_MAP = {
    "US2YT_CMT": "BC_2YEAR",
    "US3YT_CMT": "BC_3YEAR",
    "US5YT_CMT": "BC_5YEAR",
    "US7YT_CMT": "BC_7YEAR",
    "US10YT_CMT": "BC_10YEAR",
    "US20YT_CMT": "BC_20YEAR",
    "US30YT_CMT": "BC_30YEAR",
}
TREASURY_XML_URL_TEMPLATE = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/pages/xml?data=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}"
)
TREASURY_YIELD_MIN = 0.10
TREASURY_YIELD_MAX = 25.0

# Per-run cache: the year-scoped Treasury XML document is fetched ONCE and
# shared across all seven UST symbols in the same run.
_treasury_xml_cache: dict[int, "pd.DataFrame"] = {}

local_engine = make_local_engine()
neon_engine = make_neon_engine()

# D2: the deferred-history queue is persistent in production. Deferrals must
# not exist only in memory: every historical deferral is recorded in
# public.macro_deferred_history so bounded recovery can retry it later.
# Lazily constructed so a dry run / test import never touches the DB.
_PROD_DEFERRED_STORE = None


def production_deferred_store():
    """Return the shared persistent deferred-history store (lazy)."""
    global _PROD_DEFERRED_STORE
    if _PROD_DEFERRED_STORE is None:
        _PROD_DEFERRED_STORE = SqlDeferredStore(local_engine)
    return _PROD_DEFERRED_STORE


# ============================================================
# ASSETS
# ============================================================

ASSETS = [
    ("^GSPC", "S&P 500", "stock_index"),
    ("^NDX", "NASDAQ 100", "stock_index"),
    ("^IXIC", "NASDAQ Composite", "stock_index"),
    ("^DJI", "Dow Jones Industrial Average", "stock_index"),
    ("^RUT", "Russell 2000 Index", "stock_index"),
    ("^VIX", "CBOE Volatility Index", "stock_index"),
    ("^SKEW", "CBOE SKEW Index", "volatility"),
    ("^MOVE", "ICE BofA MOVE Index", "volatility"),
    ("^HSI", "HANG SENG INDEX", "stock_index"),
    ("^N225", "Nikkei 225", "stock_index"),
    ("^KS11", "KOSPI Composite Index", "stock_index"),
    ("000001.SS", "SSE Composite Index", "stock_index"),
    ("^FTSE", "FTSE 100", "stock_index"),
    ("^GDAXI", "DAX", "stock_index"),
    ("^FCHI", "CAC 40", "stock_index"),

    ("NQ=F", "Nasdaq 100 Future", "futures"),
    ("ES=F", "E-mini S&P 500 Future", "futures"),
    ("YM=F", "Mini Dow Future", "futures"),
    ("RTY=F", "E-mini Russell 2000 Future", "futures"),
    ("NIY=F", "Nikkei 225 Future", "futures"),
    ("KOR200c1", "KOSPI 200 Futures", "futures"),
    ("HK50", "Hang Seng Futures", "futures"),
    ("CIHc1", "SSE 50 Futures", "futures"),
    ("GC=F", "Gold Future", "futures"),
    ("CL=F", "WTI Crude Oil Future", "futures"),
    ("BZ=F", "Brent Crude Oil Future", "futures"),
    ("HG=F", "Copper Future", "futures"),
    ("SI=F", "Silver Future", "futures"),
    ("NG=F", "Natural Gas Future", "futures"),

    # OilPriceAPI spot/indicative series (distinct from the futures symbols above).
    ("BRENT_OILPRICE", "Brent Crude Oil Spot (OilPriceAPI)", "oil_price"),
    ("WTI_OILPRICE", "WTI Crude Oil Spot (OilPriceAPI)", "oil_price"),

    ("^FVX", "Treasury Yield 5 Years", "ust_yield"),
    ("^TNX", "Treasury Yield 10 Years", "ust_yield"),
    ("^TYX", "Treasury Yield 30 Years", "ust_yield"),
    ("US2YT=X", "United States 2-Year Treasury Yield", "ust_yield"),
    ("US3YT=X", "United States 3-Year Treasury Yield", "ust_yield"),
    ("US5YT=X", "United States 5-Year Treasury Yield", "ust_yield"),
    ("US7YT=X", "United States 7-Year Treasury Yield", "ust_yield"),
    ("US10YT=X", "United States 10-Year Treasury Yield", "ust_yield"),
    ("US20YT=X", "United States 20-Year Treasury Yield", "ust_yield"),
    ("US30YT=X", "United States 30-Year Treasury Yield", "ust_yield"),

    # Official Treasury.gov constant-maturity par-yield series (additive).
    # Distinct symbols so legacy USnYT=X rows/metadata are never touched.
    ("US2YT_CMT", "US Treasury 2-Year CMT Par Yield", "ust_yield_cmt"),
    ("US3YT_CMT", "US Treasury 3-Year CMT Par Yield", "ust_yield_cmt"),
    ("US5YT_CMT", "US Treasury 5-Year CMT Par Yield", "ust_yield_cmt"),
    ("US7YT_CMT", "US Treasury 7-Year CMT Par Yield", "ust_yield_cmt"),
    ("US10YT_CMT", "US Treasury 10-Year CMT Par Yield", "ust_yield_cmt"),
    ("US20YT_CMT", "US Treasury 20-Year CMT Par Yield", "ust_yield_cmt"),
    ("US30YT_CMT", "US Treasury 30-Year CMT Par Yield", "ust_yield_cmt"),

    ("EURUSD=X", "EUR/USD", "fx"),
    ("DX-Y.NYB", "US Dollar Index", "fx"),
    ("JPY=X", "USD/JPY", "fx"),
    ("GBPUSD=X", "GBP/USD", "fx"),
    ("AUDUSD=X", "AUD/USD", "fx"),
    ("NZDUSD=X", "NZD/USD", "fx"),
    ("CNY=X", "USD/CNY", "fx"),
    ("HKD=X", "USD/HKD", "fx"),
    ("SGD=X", "USD/SGD", "fx"),
    ("CHFUSD=X", "CHF/USD", "fx"),

    ("BTC-USD", "Bitcoin USD", "crypto"),
    ("ETH-USD", "Ethereum USD", "crypto"),

    ("HYG", "iShares iBoxx High Yield Corporate Bond ETF", "credit_proxy"),
    ("LQD", "iShares iBoxx Investment Grade Corporate Bond ETF", "credit_proxy"),
    ("JNK", "SPDR Bloomberg High Yield Bond ETF", "credit_proxy"),
    ("RSP", "Invesco S&P 500 Equal Weight ETF", "equity_style"),
    ("IWF", "iShares Russell 1000 Growth ETF", "equity_style"),
    ("IWD", "iShares Russell 1000 Value ETF", "equity_style"),
    ("TLT", "iShares 20+ Year Treasury Bond ETF", "duration_rates"),
    ("IEF", "iShares 7-10 Year Treasury Bond ETF", "duration_rates"),
    ("SHY", "iShares 1-3 Year Treasury Bond ETF", "duration_rates"),
]


@dataclass(frozen=True)
class SymbolFetchOutcome:
    symbol: str
    name: str
    provider: str
    required: bool
    success: bool
    rows_fetched: int
    latest_observation: date | None
    failure_reason: str | None = None
    primary_provider: str | None = None
    primary_failure_reason: str | None = None
    fallback_provider: str | None = None
    fallback_failure_reason: str | None = None
    # Data freshness state (independent of provider coverage)
    # Valid values: CURRENT, CURRENT_FOR_PROVIDER, CURRENT_BUT_PROVIDER_UNAVAILABLE,
    #               STALE, UNCONFIRMED, BLOCKED, WRONG_SOURCE, PROVIDER_DATA_INSUFFICIENT
    data_freshness_state: str | None = None
    # Whether Local data is acceptably current even if provider fetch failed
    local_is_current: bool = False


@dataclass(frozen=True)
class MacroCoverageSummary:
    outcomes: tuple[SymbolFetchOutcome, ...]
    configured_count: int
    successful_count: int
    failed_count: int
    coverage_pct: float
    required_failed_symbols: tuple[str, ...]
    provider_failure_counts: tuple[tuple[str, int], ...]
    coverage_satisfied: bool
    required_historical_deferred_symbols: tuple[str, ...] = ()
    supplementary_historical_deferred_symbols: tuple[str, ...] = ()
    expected_frozen_symbols: tuple[str, ...] = ()

    @property
    def exit_code(self):
        return 0 if self.coverage_satisfied else 1


class SymbolProcessingError(RuntimeError):
    def __init__(self, message, *, rows_fetched=0, latest_observation=None):
        super().__init__(message)
        self.rows_fetched = rows_fetched
        self.latest_observation = latest_observation

INVESTINY_ASSETS = {
    "US2YT=X": 23701,
    "US3YT=X": 23702,
    "US5YT=X": 23703,
    "US7YT=X": 23704,
    "US10YT=X": 23705,
    "US20YT=X": 1161827,
    "US30YT=X": 23706,
    "KOR200c1": 8987,
    "HK50": 8984,
    "CIHc1": 1115062,
    # Investing.com continuous front-contract instruments. The public source
    # labels currently display as LCOV6 (Brent) and OIL (WTI), while the
    # canonical database symbols remain BZ=F and CL=F for API compatibility.
}

INVESTING_SOURCE_SYMBOLS = {
    # BZ=F / CL=F are yfinance-owned (yfinance serves both with full
    # history), so they no longer map to an Investing.com source symbol.
}


# ============================================================
# CLOSE TIME MAP
# ============================================================

SYMBOL_CLOSE_MAP = {
    "^DJI": {"tz": "America/New_York", "close_time": "16:00"},
    "^FCHI": {"tz": "Europe/Paris", "close_time": "17:30"},
    "^FTSE": {"tz": "Europe/London", "close_time": "16:30"},
    "^GDAXI": {"tz": "Europe/Berlin", "close_time": "17:30"},
    "^GSPC": {"tz": "America/New_York", "close_time": "16:00"},
    "^NDX": {"tz": "America/New_York", "close_time": "16:00"},
    "^HSI": {"tz": "Asia/Hong_Kong", "close_time": "16:10"},
    "^IXIC": {"tz": "America/New_York", "close_time": "16:00"},
    "^KS11": {"tz": "Asia/Seoul", "close_time": "15:30"},
    "^N225": {"tz": "Asia/Tokyo", "close_time": "15:30"},
    "^RUT": {"tz": "America/New_York", "close_time": "16:00"},
    "^VIX": {"tz": "America/Chicago", "close_time": "15:15"},
    "^SKEW": {"tz": "America/Chicago", "close_time": "15:15"},
    "^MOVE": {"tz": "America/New_York", "close_time": "16:00"},
    "000001.SS": {"tz": "Asia/Shanghai", "close_time": "15:00"},

    "NQ=F": {"tz": "America/New_York", "close_time": "17:00"},
    "ES=F": {"tz": "America/New_York", "close_time": "17:00"},
    "YM=F": {"tz": "America/New_York", "close_time": "17:00"},
    "RTY=F": {"tz": "America/New_York", "close_time": "17:00"},
    "NIY=F": {"tz": "America/Chicago", "close_time": "16:00"},
    "KOR200c1": {"tz": "Asia/Seoul", "close_time": "06:00"},
    "HK50": {"tz": "Asia/Hong_Kong", "close_time": "03:00"},
    "CIHc1": {"tz": "Asia/Shanghai", "close_time": "15:00"},
    "GC=F": {"tz": "America/New_York", "close_time": "17:00"},
    "CL=F": {"tz": "America/New_York", "close_time": "17:00"},
    "BZ=F": {"tz": "America/New_York", "close_time": "17:00"},
    "HG=F": {"tz": "America/New_York", "close_time": "17:00"},
    "SI=F": {"tz": "America/New_York", "close_time": "17:00"},
    "NG=F": {"tz": "America/New_York", "close_time": "17:00"},

    # OilPriceAPI is an indicative spot quote, not an exchange-traded session;
    # US/Eastern is used only as the reference for "is the quote current".
    "BRENT_OILPRICE": {"tz": "America/New_York", "close_time": "17:00"},
    "WTI_OILPRICE": {"tz": "America/New_York", "close_time": "17:00"},

    "^FVX": {"tz": "America/New_York", "close_time": "16:00"},
    "^TNX": {"tz": "America/New_York", "close_time": "16:00"},
    "^TYX": {"tz": "America/New_York", "close_time": "16:00"},
    # US Treasury yields (Investing.com): daily market close 17:00 ET.
    "US2YT=X": {"tz": "America/New_York", "close_time": "17:00"},
    "US3YT=X": {"tz": "America/New_York", "close_time": "17:00"},
    "US5YT=X": {"tz": "America/New_York", "close_time": "17:00"},
    "US7YT=X": {"tz": "America/New_York", "close_time": "17:00"},
    "US10YT=X": {"tz": "America/New_York", "close_time": "17:00"},
    "US20YT=X": {"tz": "America/New_York", "close_time": "17:00"},
    "US30YT=X": {"tz": "America/New_York", "close_time": "17:00"},

    # Official CMT series: same US business-day/16:00 ET reference as the
    # legacy symbols. Session confirmation still comes from the actual
    # Treasury.gov publication for the specific session.
    "US2YT_CMT": {"tz": "America/New_York", "close_time": "16:00"},
    "US3YT_CMT": {"tz": "America/New_York", "close_time": "16:00"},
    "US5YT_CMT": {"tz": "America/New_York", "close_time": "16:00"},
    "US7YT_CMT": {"tz": "America/New_York", "close_time": "16:00"},
    "US10YT_CMT": {"tz": "America/New_York", "close_time": "16:00"},
    "US20YT_CMT": {"tz": "America/New_York", "close_time": "16:00"},
    "US30YT_CMT": {"tz": "America/New_York", "close_time": "16:00"},

    "EURUSD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "DX-Y.NYB": {"tz": "America/New_York", "close_time": "17:00"},
    "JPY=X": {"tz": "America/New_York", "close_time": "17:00"},
    "GBPUSD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "AUDUSD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "NZDUSD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "CNY=X": {"tz": "America/New_York", "close_time": "17:00"},
    "HKD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "SGD=X": {"tz": "America/New_York", "close_time": "17:00"},
    "CHFUSD=X": {"tz": "America/New_York", "close_time": "17:00"},

    "BTC-USD": {"tz": "UTC", "close_time": "00:00"},
    "ETH-USD": {"tz": "UTC", "close_time": "00:00"},

    "HYG": {"tz": "America/New_York", "close_time": "16:00"},
    "LQD": {"tz": "America/New_York", "close_time": "16:00"},
    "JNK": {"tz": "America/New_York", "close_time": "16:00"},
    "RSP": {"tz": "America/New_York", "close_time": "16:00"},
    "IWF": {"tz": "America/New_York", "close_time": "16:00"},
    "IWD": {"tz": "America/New_York", "close_time": "16:00"},
    "TLT": {"tz": "America/New_York", "close_time": "16:00"},
    "IEF": {"tz": "America/New_York", "close_time": "16:00"},
    "SHY": {"tz": "America/New_York", "close_time": "16:00"},
}

# ============================================================
# DATABASE
# ============================================================

def create_macro_table(engine):
    sql = text("""
        CREATE TABLE IF NOT EXISTS public.macro (
            date DATE NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            asset_type TEXT,

            open NUMERIC(20,4),
            high NUMERIC(20,4),
            low NUMERIC(20,4),
            close NUMERIC(20,4),
            adj_close NUMERIC(20,4),
            volume NUMERIC(20,4),

            prev_close NUMERIC(20,4),
            change NUMERIC(20,4),
            pct_chg NUMERIC(20,4),
            amplitude NUMERIC(20,4),

            PRIMARY KEY (date, symbol)
        );
    """)

    with engine.begin() as conn:
        conn.execute(sql)


def create_macro_live_table(engine):
    sql = text("""
        CREATE TABLE IF NOT EXISTS public.macro_live (
            symbol TEXT PRIMARY KEY,
            market_date DATE NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            name TEXT,
            asset_type TEXT,

            open NUMERIC(20,4),
            high NUMERIC(20,4),
            low NUMERIC(20,4),
            close NUMERIC(20,4),
            adj_close NUMERIC(20,4),
            volume NUMERIC(20,4),

            prev_close NUMERIC(20,4),
            change NUMERIC(20,4),
            pct_chg NUMERIC(20,4),
            amplitude NUMERIC(20,4),
            is_market_closed BOOLEAN NOT NULL DEFAULT FALSE,
            source TEXT NOT NULL DEFAULT 'yfinance'
        );
    """)

    with engine.begin() as conn:
        conn.execute(sql)


def _prepare_macro_live_rows(rows):
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.where(pd.notna(df), None)
        duplicate_count = int(df.duplicated(subset=["symbol"], keep="last").sum())
        if duplicate_count:
            duplicate_symbols = sorted(
                df.loc[df.duplicated(subset=["symbol"], keep=False), "symbol"]
                .astype(str)
                .unique()
                .tolist()
            )
            print(
                "[LIVE SNAPSHOT DEDUPLICATED] "
                f"rows={duplicate_count:,} symbols={duplicate_symbols}"
            )
            df = df.drop_duplicates(subset=["symbol"], keep="last")
    return df


def _macro_live_upsert_sql():
    return text("""
        INSERT INTO public.macro_live (
            symbol, market_date, observed_at, name, asset_type,
            open, high, low, close, adj_close, volume,
            prev_close, change, pct_chg, amplitude,
            is_market_closed, source
        )
        VALUES (
            :symbol, :market_date, :observed_at, :name, :asset_type,
            :open, :high, :low, :close, :adj_close, :volume,
            :prev_close, :change, :pct_chg, :amplitude,
            :is_market_closed, :source
        )
        ON CONFLICT (symbol) DO UPDATE SET
            market_date = EXCLUDED.market_date,
            observed_at = EXCLUDED.observed_at,
            name = EXCLUDED.name,
            asset_type = EXCLUDED.asset_type,
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            adj_close = EXCLUDED.adj_close,
            volume = EXCLUDED.volume,
            prev_close = EXCLUDED.prev_close,
            change = EXCLUDED.change,
            pct_chg = EXCLUDED.pct_chg,
            amplitude = EXCLUDED.amplitude,
            is_market_closed = EXCLUDED.is_market_closed,
            source = EXCLUDED.source;
    """)


def _validate_macro_live_source(rows):
    """Reject wrong-source live rows for USnYT=X symbols.

    USnYT=X series are owned ONLY by Investing.com market yield.
    Treasury.gov, CMT, ^TNX, ^TYX, or any other non-Investing source
    must be rejected at the write boundary.
    """
    usnyt_pattern = re.compile(r'^US(?:[2357]|10|20|30)YT=X$')
    rejected = []
    for row in rows:
        symbol = row.get('symbol', '')
        source = row.get('source', '')
        if usnyt_pattern.match(symbol):
            if source not in ('investing.com', 'INVESTING', 'investing'):
                rejected.append((symbol, source))
                print(f"[WRONG_SOURCE_LIVE_REJECTED] {symbol} source={source}")
    return rejected


def replace_macro_live(engine, rows):
    """Atomically replace a complete transient intraday snapshot."""
    # Hard write-time guard: reject wrong-source USnYT=X live rows
    rejected = _validate_macro_live_source(rows)
    if rejected:
        raise ValueError(
            f"Wrong-source live rows rejected for USnYT=X: {rejected}"
        )

    df = _prepare_macro_live_rows(rows)

    with engine.begin() as conn:
        # Serialize overlapping hourly/manual refreshes within each database.
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('macro_live_replace'))"))
        # This table intentionally contains only the latest fetch snapshot.
        conn.execute(text("DELETE FROM public.macro_live"))
        if not df.empty:
            conn.execute(_macro_live_upsert_sql(), df.to_dict(orient="records"))


def merge_macro_live(engine, rows, successful_symbols):
    """Refresh successful symbols while retaining failed symbols unchanged."""
    symbols = sorted(set(successful_symbols))
    if not symbols:
        return

    # Hard write-time guard: reject wrong-source USnYT=X live rows
    rejected = _validate_macro_live_source(rows)
    if rejected:
        raise ValueError(
            f"Wrong-source live rows rejected for USnYT=X: {rejected}"
        )

    df = _prepare_macro_live_rows(rows)
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('macro_live_replace'))"))
        # Only successful symbols are cleared. Failed symbols keep their prior
        # observed_at, making retained stale observations distinguishable.
        conn.execute(
            text("DELETE FROM public.macro_live WHERE symbol = ANY(:symbols)"),
            {"symbols": symbols},
        )
        if not df.empty:
            conn.execute(_macro_live_upsert_sql(), df.to_dict(orient="records"))


def fetch_existing_symbol_dates(engine, symbols, min_date, max_date):
    sql = text("""
        SELECT symbol, date
        FROM public.macro
        WHERE symbol = ANY(:symbols)
          AND date BETWEEN :min_date AND :max_date
    """)

    with engine.begin() as conn:
        df = pd.read_sql(
            sql,
            conn,
            params={
                "symbols": symbols,
                "min_date": min_date,
                "max_date": max_date,
            },
        )

    if df.empty:
        return pd.DataFrame(columns=["symbol", "date"])

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["symbol"] = df["symbol"].astype(str)

    return df


def _existing_historical_dates(engine, symbol, min_date, max_date):
    """Session dates already confirmed in historical storage for ``symbol``."""
    sql = text("""
        SELECT date
        FROM public.macro
        WHERE symbol = :symbol
          AND date BETWEEN :min_date AND :max_date
    """)
    try:
        with engine.begin() as conn:
            df = pd.read_sql(
                sql,
                conn,
                params={
                    "symbol": symbol,
                    "min_date": min_date,
                    "max_date": max_date,
                },
            )
    except Exception as exc:
        print(f"[GATE WARN] prior-session lookup failed for {symbol}: {exc}")
        return None

    if df.empty:
        return set()

    return {pd.to_datetime(v).date() for v in df["date"]}


def _existing_historical_values(engine, symbol, min_date, max_date):
    """Stored closes for confirmed historical sessions of ``symbol``.

    Returns ``{date: close}``, or ``None`` on failure (fail closed).

    Same-definition identity: ``public.macro`` is keyed by ``(symbol, date)``
    and carries no per-row source-definition column. Identity is established
    safely by construction instead:

      * ``SOURCE_DEFINITION_BY_SYMBOL`` is single-valued per symbol, so a
        symbol has exactly one *approved historical* source definition;
      * the historical gate only admits a row when that approved definition
        resolves to an established observation convention. A symbol whose
        approved definition is unestablished (e.g. BZ=F/CL=F under
        INVESTING_CONTINUOUS_FUTURES_DAILY) can therefore never extend its
        stored series, so no cross-definition prior can be created.

    Consequently every stored row for a symbol was admitted under that
    symbol's approved definition, and its stored close is a valid
    same-definition prior value. If a class ever gains multiple approved
    definitions, this must be revisited and fail closed.
    """
    sql = text("""
        SELECT date, close
        FROM public.macro
        WHERE symbol = :symbol
          AND date BETWEEN :min_date AND :max_date
    """)
    try:
        with engine.begin() as conn:
            df = pd.read_sql(
                sql,
                conn,
                params={
                    "symbol": symbol,
                    "min_date": min_date,
                    "max_date": max_date,
                },
            )
    except Exception as exc:
        print(f"[GATE WARN] prior-value lookup failed for {symbol}: {exc}")
        return None

    if df.empty:
        return {}

    return {
        pd.to_datetime(row["date"]).date(): row["close"]
        for _, row in df.iterrows()
    }


def _apply_confirmed_prior(rec, confirmed_prior_close):
    """Rewrite ``rec``'s prior-dependent fields from the stored prior close.

    The provider frame's own previous value is never authoritative once the
    gate depends on a stored-confirmed prior: ``prev_close`` and the derived
    ``change`` / ``pct_chg`` / ``amplitude`` are recomputed deterministically
    from the trusted stored ``P.close``, using production rounding semantics.

    Current-observation fields (open/high/low/close/adj_close/volume) are left
    to the provider candidate.
    """
    close = rec.get("close")
    high = rec.get("high")
    low = rec.get("low")

    change = None
    pct_chg = None
    amplitude = None
    if close is not None and confirmed_prior_close not in (None, 0):
        change = round_4(close - confirmed_prior_close)
        pct_chg = round_4(
            (close - confirmed_prior_close) / confirmed_prior_close * 100)
        if high is not None and low is not None:
            amplitude = round_4(
                (high - low) / confirmed_prior_close * 100)

    rec["prev_close"] = round_4(confirmed_prior_close)
    rec["change"] = change
    rec["pct_chg"] = pct_chg
    rec["amplitude"] = amplitude
    return rec


def official_value_matches(symbol, session_date, value, *, published_values=None):
    """Phase A: the row's own close must BE the official observed value.

    A publication *date* alone must never authorise a historical write: an
    Investing.com/Yahoo-derived value for the same symbol/date/tenor must be
    rejected. When no official value has been recorded the answer is False
    (fail closed).
    """
    if value is None or session_date is None:
        return False
    table = _TREASURY_PUBLISHED_VALUES if published_values is None else published_values
    official = table.get((symbol, session_date))
    if official is None:
        return False
    try:
        return abs(float(value) - float(official)) <= TREASURY_VALUE_TOLERANCE
    except (TypeError, ValueError):
        return False


def official_value_recorded(symbol, session_date, *, published_values=None):
    """Phase A: is the official observation for this session recorded?"""
    if session_date is None:
        return False
    table = _TREASURY_PUBLISHED_VALUES if published_values is None else published_values
    return (symbol, session_date) in table


def official_prior_confirmed(symbol, expected_prior, *, treasury_published_dates=None):
    """Phase A: official-EOD source consistency for ``prev_close``.

    ``prev_close`` on an official historical row must resolve from the previous
    *valid* session AND that prior value must be present in the same official
    source series. Otherwise the official series is incomplete for this symbol
    and the dependent fields would silently mix sources (e.g. an official close
    against an Investing.com/Yahoo-derived prior close).

    Symbols without a publication-confirmed source always return True.
    """
    if expected_prior is None:
        return False
    if symbol not in TREASURY_SYMBOLS:
        return True
    return treasury_publication_confirmed(
        expected_prior, treasury_published_dates)


def _eligible_for_historical(row, *, known_prior_sessions=None,
                             treasury_published_dates=None, now_utc=None,
                             treasury_published_values=None,
                             source_definition=None):
    """Phase A EOD write gate.

    Returns an ``Eligibility`` decision. Fail-closed: any error yields a
    non-eligible decision, so an exception can never produce a historical write.

    For publication-confirmed symbols the row's own ``close`` must match the
    official observed value: a publication date alone never authorises a
    historical write, so an Investing.com/Yahoo-derived value for the same
    symbol/date is deferred instead of being stored as official EOD.

    ``source_definition`` is the approved historical source definition the
    candidate row belongs to. The previous *observation date* is resolved from
    it; an unknown or unestablished definition fails closed.
    """
    try:
        session_date = pd.to_datetime(row["date"]).date()
        decision = historical_eligibility(
            row["symbol"],
            session_date,
            known_prior_sessions=known_prior_sessions,
            now_utc=now_utc,
            treasury_published_dates=treasury_published_dates,
            source_definition=source_definition
            if source_definition is not None
            else row.get("source_definition"),
        )
        if decision.routing != ROUTE_HISTORICAL:
            return decision
        # Phase A (additive): the legacy ``USnYT=X`` series is frozen for
        # historical writes until its own EOD definition is resolved. Its live
        # macro_live reporting is unaffected. Treasury.gov CMT values are never
        # routed into the legacy symbols.
        if row["symbol"] in TREASURY_SYMBOLS:
            from db_builder.session_calendar import Eligibility
            return Eligibility(
                symbol=row["symbol"],
                session_date=session_date,
                eligible=False,
                routing=ROUTE_DEFERRED,
                reason="LEGACY_EOD_DEFINITION_UNRESOLVED",
                session_status=decision.session_status,
                calendar=decision.calendar,
                expected_prior=decision.expected_prior,
            )
        if row["symbol"] in CMT_SYMBOLS:
            if not official_value_matches(
                    row["symbol"], session_date, row.get("close"),
                    published_values=treasury_published_values):
                from db_builder.session_calendar import Eligibility
                return Eligibility(
                    symbol=row["symbol"],
                    session_date=session_date,
                    eligible=False,
                    routing=ROUTE_DEFERRED,
                    reason="CMT_VALUE_NOT_OFFICIAL",
                    session_status=decision.session_status,
                    calendar=decision.calendar,
                    expected_prior=decision.expected_prior,
                )
            # The previous close must come from the previous *official* CMT
            # observation of the same tenor, not merely a prior session date.
            if not official_value_recorded(
                    row["symbol"], decision.expected_prior,
                    published_values=treasury_published_values):
                from db_builder.session_calendar import Eligibility
                return Eligibility(
                    symbol=row["symbol"],
                    session_date=session_date,
                    eligible=False,
                    routing=ROUTE_DEFERRED,
                    reason="CMT_PREVIOUS_VALUE_NOT_OFFICIAL",
                    session_status=decision.session_status,
                    calendar=decision.calendar,
                    expected_prior=decision.expected_prior,
                )
        return decision
    except Exception as exc:                       # fail closed
        from db_builder.session_calendar import Eligibility
        return Eligibility(
            symbol=str(row.get("symbol")),
            session_date=pd.to_datetime(row.get("date")).date()
            if row.get("date") is not None else None,
            eligible=False,
            routing=ROUTE_DEFERRED,
            reason=f"GATE_ERROR:{type(exc).__name__}",
            session_status=SessionStatus.CLOSED_UNCONFIRMED,
            calendar="error",
        )


def route_historical_rows(engine, rows, *, mode="INGEST",
                          treasury_published_dates=None, now_utc=None,
                          deferred_store=None, prior_lookup=None,
                          prior_value_lookup=None,
                          provenance_lookup=None, trust_start=None,
                          treasury_published_values=None,
                          batch_start=None, count_telemetry=True):
    """Split candidate rows into HISTORICAL / DEFERRED / REJECTED.

    A completed historical bar whose prior session is missing is DEFERRED: it is
    neither written to ``public.macro`` nor to ``public.macro_live``.

    ``prior_lookup(engine, symbol, min_date, max_date)`` may be injected; it must
    return the set of confirmed prior session dates, or None on failure
    (fail-closed). Defaults to the historical-table lookup.

    ``prior_value_lookup(engine, symbol, min_date, max_date)`` returns the
    stored closes ``{date: close}`` for those confirmed sessions, or None on
    failure. When supplied, an admitted row's ``prev_close`` and derived fields
    are recomputed from the *stored* prior close -- the provider frame's own
    previous value is never authoritative.

    ``provenance_lookup(engine, symbol, min_date, max_date)`` returns the set of
    dates with verified same-definition provenance. When None, provenance is
    unknown and all rows fail closed.

    ``trust_start`` is the earliest trusted date for the source definition.
    P must be >= trust_start to be eligible.
    """
    if not rows:
        return [], [], []

    if mode != "INGEST":
        return list(rows), [], []

    lookup = prior_lookup or _existing_historical_dates
    value_lookup = prior_value_lookup or _existing_historical_values

    if treasury_published_dates is None:
        treasury_published_dates = _TREASURY_PUBLISHED_DATES or None
    if treasury_published_values is None:
        treasury_published_values = _TREASURY_PUBLISHED_VALUES or None

    df = pd.DataFrame(rows)
    df["_d"] = pd.to_datetime(df["date"]).dt.date
    min_date = df["_d"].min() - timedelta(days=10)
    max_date = df["_d"].max()
    # D1: bound the fetch window from the cycle reference time. The fetch paths
    # request ``today - (LOOKBACK_DAYS + CALC_BUFFER_DAYS)`` onwards, so a prior
    # session strictly before that start was never requested.
    if batch_start is None:
        _ref = now_utc or datetime.now(ZoneInfo("UTC"))
        batch_start = _ref.date() - timedelta(
            days=LOOKBACK_DAYS + CALC_BUFFER_DAYS)

    accepted, deferred, rejected = [], [], []
    deferral_kinds = {}
    by_symbol = {}
    for rec in rows:
        by_symbol.setdefault(rec["symbol"], []).append(rec)

    for symbol, symbol_rows in by_symbol.items():
        # Phase C.1: resolve trust_start from the immutable mapping if not
        # explicitly provided. Only (symbol, source_definition) entries in
        # SOURCE_DEFINITION_TRUST_START are authoritative.
        _sd = symbol_rows[0].get("source_definition") if symbol_rows else None
        _ts = trust_start
        if _ts is None and _sd is not None:
            _ts = _resolve_trust_start(symbol, _sd)
        known = lookup(engine, symbol, min_date, max_date)
        if known is None:
            # Lookup failed -> fail closed, defer the whole symbol.
            for rec in symbol_rows:
                deferred.append((rec, "PRIOR_SESSION_LOOKUP_FAILED"))
                continue
            continue
        # Phase A.2A: only a prior session already confirmed in historical
        # storage may satisfy D's prior dependency. A date that merely appears
        # as a *candidate* in this batch is NOT confirmed: it may itself be
        # deferred or rejected, so admitting it would let D be written with a
        # prev_close anchored to a row that was never persisted.
        #
        # Invariant: a historical row D may only be written when its previous
        # valid session P is itself confirmed under the same approved source
        # definition and is already stored. If P is missing or deferred, D
        # must also defer; the next cycle reconsiders D once P is stored.
        # Positional D-2 fallback remains prohibited.
        known_all = set(known)
        stored_closes = value_lookup(engine, symbol, min_date, max_date)
        if stored_closes is None:
            # Value lookup failed -> fail closed, defer the whole symbol:
            # without the stored prior value the value dependency cannot be
            # proven, and the provider frame is not authoritative for it.
            for rec in symbol_rows:
                deferred.append((rec, "PRIOR_VALUE_LOOKUP_FAILED"))
                continue
            continue

        # Provenance: stored P must be verified as same-definition.
        # If provenance_lookup is None, provenance is unknown -> fail closed.
        if provenance_lookup is not None:
            provenance_dates = provenance_lookup(engine, symbol, min_date, max_date)
        else:
            provenance_dates = set()  # unknown -> fail closed

        for rec in symbol_rows:
            decision = _eligible_for_historical(
                rec,
                known_prior_sessions=known_all,
                treasury_published_dates=treasury_published_dates,
                now_utc=now_utc,
                treasury_published_values=treasury_published_values,
            )
            if decision.routing == ROUTE_HISTORICAL:
                # Provenance check: P must be verified same-definition
                if decision.expected_prior not in provenance_dates:
                    deferred.append((rec, "PRIOR_PROVENANCE_UNVERIFIED"))
                    continue
                # Trust-start boundary: P must be >= trust_start
                if _ts is not None and decision.expected_prior < _ts:
                    deferred.append((rec, "PRIOR_BEFORE_TRUST_START"))
                    continue
                # Value dependency: D.prev_close must equal the *stored* P
                # close under the same approved source definition. The
                # provider frame's own previous value is not authoritative
                # once eligibility rests on a stored-confirmed prior, so the
                # prior-dependent fields are recomputed from stored P.
                prior_close = stored_closes.get(decision.expected_prior)
                if prior_close is None:
                    deferred.append((rec, "PREVIOUS_VALUE_NOT_CONFIRMED"))
                    continue
                _apply_confirmed_prior(rec, prior_close)
                accepted.append(rec)
            elif decision.routing == ROUTE_REJECT:
                rejected.append((rec, decision.reason))
            else:
                deferred.append((rec, decision.reason))
                deferral_kinds[(rec.get("symbol"), rec.get("date"))] = (
                    _classify_deferral(
                        decision.reason,
                        expected_prior=decision.expected_prior,
                        batch_start=batch_start,
                    )
                )

    if deferred and deferred_store is not None:
        for rec, reason in deferred:
            try:
                deferred_store.record_deferral(
                    rec["symbol"], pd.to_datetime(rec["date"]).date(), reason,
                    now=now_utc, attempted=False)
            except Exception as exc:
                print(f"[DEFERRED WARN] could not persist deferral "
                      f"{rec['symbol']} {rec['date']}: {exc}")

    global _PHASE_A_DEFERRED_COUNT
    global _PHASE_A_BOUNDARY_DEFERRED_COUNT
    if count_telemetry:
        _PHASE_A_DEFERRED_COUNT += len(deferred)
    for rec, reason in deferred:
        if not count_telemetry:
            break
        kind = deferral_kinds.get((rec.get("symbol"), rec.get("date")),
                                  "actionable")
        if kind == "boundary":
            _PHASE_A_BOUNDARY_DEFERRED_COUNT += 1
        else:
            _PHASE_A_ACTIONABLE_DEFERRALS.append(
                {"symbol": rec.get("symbol"), "reason": reason})
        if reason in ("CLOSED_UNCONFIRMED", "SOURCE_DEFINITION_UNRESOLVED",
                      "PRIOR_VALUE_LOOKUP_FAILED",
                      "PREVIOUS_VALUE_NOT_CONFIRMED") \
                or reason.startswith("CALENDAR"):
            _PHASE_A_UNAVAILABLE_SYMBOLS.append(
                {"symbol": rec.get("symbol"), "reason": reason})

    for rec, reason in deferred:
        print(f"[DEFERRED_HISTORY] {rec['symbol']} {rec['date']} "
              f"-> {reason} (not written to macro or macro_live)")
    for rec, reason in rejected:
        print(f"[REJECT_NON_SESSION] {rec['symbol']} {rec['date']} -> {reason}")

    return accepted, deferred, rejected


def run_deferred_recovery(engine, store, *, fetch_symbol_rows, now_utc=None,
                          limit=RECOVERY_BATCH, recovery_days=RECOVERY_DAYS,
                          max_attempts=MAX_ATTEMPTS, prior_lookup=None,
                          prior_value_lookup=None, provenance_lookup=None,
                          trust_start=None, provider_backoff=None):
    """Bounded retry of deferred historical sessions (Phase A.2B gated).

    ``fetch_symbol_rows(symbol, dates)`` must return historical row dicts for the
    requested dates (same shape as ``calculate_rows_for_symbol`` output).
    Rows that are still missing their prior session stay DEFERRED; rows that can
    now be built are written through the normal gate and marked RESOLVED.

    Phase A.2B adds a retry-state layer that is orthogonal to writability:

      * provider availability  -- decides whether a fetch may be attempted;
      * deferred retry state   -- decides whether THIS item is retry-eligible;
      * historical gate        -- independently decides whether data may write.

    ``provenance_lookup`` is an OPTIONAL injection point used by tests. When it
    is None (the production calling convention) the recovery path supplies the
    shared C.1 anchor-derived resolver internally, so provenance is never
    silently unknown. Precedence is explicit: an injected resolver wins;
    otherwise the anchor-derived resolver is used.
    """
    now = now_utc or datetime.now(ZoneInfo("UTC"))

    # Provider availability (top layer), keyed by (provider, data_class). A
    # broken endpoint family suppresses redundant requests across every symbol
    # that shares it -- but never changes any historical source definition.
    unavailable_keys = set()
    if provider_backoff is not None:
        try:
            unavailable_keys = provider_backoff.unavailable_keys(now=now)
        except Exception as exc:
            print(f"[DEFERRED BACKOFF] provider state read failed: "
                  f"{type(exc).__name__}: {exc}")

    # Phase C.2: the canonical anchor-derived provenance resolver. An explicit
    # ``provenance_lookup`` (test injection) takes precedence; otherwise the
    # recovery path uses the shared C.1 anchor-derived resolver. There is only
    # ever ONE provenance source, shared by the selector overlay and the write
    # gate, so the selector can never release a row the gate then refuses.
    _effective_provenance_lookup = (
        provenance_lookup
        if provenance_lookup is not None
        else _build_anchor_provenance_lookup(engine))

    # Phase C.2: Build anchor override closure for anchor-covered PU rows.
    # Pre-compute provenance evidence to avoid N+1 SQL in selector callbacks.
    _pu_rows = [r for r in store.all_rows()
                if r.retry_class == PROVENANCE_UNVERIFIED
                and r.status != "RESOLVED"]
    _anchor_eligibility = {}
    for _r in _pu_rows:
        _sym = _r.symbol
        _sd = SOURCE_DEFINITION_BY_SYMBOL.get(_sym)
        _key = (_sym, _r.session_date)
        if _sd is None:
            _anchor_eligibility[_key] = False
            continue
        _ts = _resolve_trust_start(_sym, _sd)
        if _ts is None:
            _anchor_eligibility[_key] = False
            continue
        if _r.session_date < _ts:
            _anchor_eligibility[_key] = False
            continue
        # Resolve expected P using canonical session logic
        _expected_p = previous_valid_observation_date(_sym, _r.session_date, _sd)
        if _expected_p is None:
            _anchor_eligibility[_key] = False
            continue
        if _expected_p < _ts:
            _anchor_eligibility[_key] = False
            continue
        # Check stored trusted P through the SAME resolver the write gate uses.
        _min_d = _r.session_date - timedelta(days=10)
        _trusted_dates = _effective_provenance_lookup(
            engine, _sym, _min_d, _r.session_date)
        if _expected_p not in _trusted_dates:
            _anchor_eligibility[_key] = False
            continue
        _anchor_eligibility[_key] = True

    def _anchor_override(row):
        """Return True only for exact C.1 anchor-covered PU rows with trusted P."""
        if getattr(row, "retry_class", None) != PROVENANCE_UNVERIFIED:
            return False
        return _anchor_eligibility.get((row.symbol, row.session_date), False)

    candidates = store.due_candidates(now=now, limit=limit,
                                      recovery_days=recovery_days,
                                      provider_available=(
                                          None if not unavailable_keys
                                          else not unavailable_keys),
                                      unavailable_keys=unavailable_keys,
                                      anchor_override=_anchor_override)
    if not candidates:
        # Still run escalation: rows past the horizon / attempt limit must be
        # flagged even when nothing is retried this cycle.
        escalated = store.escalate_stale(now=now, recovery_days=recovery_days,
                                         max_attempts=max_attempts)
        _report_deferred_states(store, now=now)
        for row in escalated:
            print(f"[DEFERRED_HISTORY_UNRESOLVED] {row.symbol} "
                  f"{row.session_date} attempts={row.attempt_count} "
                  f"(retained in tracking)")
        return {"retried": 0, "resolved": 0, "still_deferred": 0,
                "escalated": len(escalated)}

    print(f"[DEFERRED RECOVERY] candidates={len(candidates)} "
          f"(bounded to {limit} per run)")

    resolved = 0
    still = 0
    for cand in candidates:
        try:
            rows = fetch_symbol_rows(cand.symbol, [cand.session_date])
        except Exception as exc:
            exc_name = type(exc).__name__
            reason = f"RECOVERY_FETCH_ERROR:{exc_name}"
            print(f"[DEFERRED RECOVERY] fetch failed {cand.symbol} "
                  f"{cand.session_date}: {exc}")
            # A provider-wide fault is recorded at the provider layer so the
            # remaining symbols sharing that endpoint family are suppressed.
            if provider_backoff is not None \
                    and _retry_policy.is_provider_error(str(exc)):
                try:
                    prov, dclass = _retry_policy.provider_key(cand.symbol)
                    provider_backoff.record_failure(prov, dclass, str(exc),
                                                    now=now)
                except Exception as bexc:
                    print(f"[DEFERRED BACKOFF] could not record provider "
                          f"outage: {type(bexc).__name__}: {bexc}")
            # Classify by the actual blocker. A deterministic fetch error in
            # the recovery path is our own routing defect (fixable), NOT a
            # manual decision; a transient exception is ordinary backoff.
            # Precedence is applied inside the store, so an incidental error
            # never overwrites a stronger structural class already on the row.
            if exc_name in _retry_policy.TRANSIENT_EXCEPTION_TYPES:
                cls = BACKOFF_ACTIVE
            else:
                cls = _retry_policy.RETRY_PATH_DEFECT
            store.record_deferral(
                cand.symbol, cand.session_date, reason, now=now, attempted=True,
                retry_class=cls,
                last_error_class=_retry_policy.defect_identifier(str(exc)))
            still += 1
            continue

        match = [r for r in (rows or [])
                 if pd.to_datetime(r["date"]).date() == cand.session_date]
        if not match:
            store.record_deferral(cand.symbol, cand.session_date,
                                  "RECOVERY_DATA_STILL_MISSING", now=now,
                                  attempted=True, retry_class=BACKOFF_ACTIVE)
            still += 1
            continue

        written = upsert_macro(engine, match, mode="INGEST",
                               prior_lookup=prior_lookup,
                               prior_value_lookup=prior_value_lookup,
                               provenance_lookup=_effective_provenance_lookup,
                               trust_start=trust_start,
                               deferred_store=store)
        if written:
            # A real provider fetch occurred, so this resolution counts as one
            # recovery attempt (attempt_count += 1), consistent with the
            # failed/re-deferred paths. Explicit, never a default.
            store.record_success(cand.symbol, cand.session_date, now=now,
                                 attempted=True)
            print(f"[DEFERRED RECOVERY] RESOLVED {cand.symbol} "
                  f"{cand.session_date}")
            resolved += 1
        else:
            store.record_deferral(cand.symbol, cand.session_date,
                                  "RECOVERY_PRIOR_STILL_UNCONFIRMED", now=now,
                                  attempted=True, retry_class=BACKOFF_ACTIVE)
            still += 1

    escalated = store.escalate_stale(now=now, recovery_days=recovery_days,
                                     max_attempts=max_attempts)
    _report_deferred_states(store, now=now)
    for row in escalated:
        print(f"[DEFERRED_HISTORY_UNRESOLVED] {row.symbol} "
              f"{row.session_date} attempts={row.attempt_count} "
              f"(retained in tracking)")

    return {"retried": len(candidates), "resolved": resolved,
            "still_deferred": still, "escalated": len(escalated)}


def _report_deferred_states(store, *, now, unavailable_keys=None):
    """One concise summary per state instead of per-symbol spam."""
    try:
        rows = [r for r in store.all_rows() if r.status != "RESOLVED"]
    except Exception:
        return
    buckets = {}
    for r in rows:
        b = _retry_policy.classify_bucket(
            r, now, unavailable_keys=unavailable_keys)
        buckets.setdefault(b, []).append(r)
    frozen = buckets.get("FROZEN", [])
    if frozen:
        legacy = sum(1 for r in frozen
                     if getattr(r, "symbol", "").startswith("US")
                     and getattr(r, "symbol", "").endswith("YT=X"))
        print(f"[EXPECTED FROZEN] legacy_treasury={legacy} "
              f"(no retry, intentional)")
    structural = buckets.get("STRUCTURAL", [])
    if structural:
        print(f"[DEFERRED STRUCTURAL] reason=PROVENANCE_UNVERIFIED "
              f"symbols={len(structural)} retry=manual")
    defect = buckets.get("RETRY_PATH_DEFECT", [])
    if defect:
        print(f"[DEFERRED DEFECT] retry_path_defect={len(defect)} "
              f"retry=after-code-fix")
    manual = buckets.get("MANUAL", [])
    if manual:
        print(f"[DEFERRED MANUAL] symbols={len(manual)} retry=manual")
    provider = buckets.get("PROVIDER_UNAVAILABLE", [])
    if provider:
        print(f"[DEFERRED BACKOFF] provider_unavailable={len(provider)}")
    backoff = buckets.get("TIME_BACKOFF", [])
    if backoff:
        print(f"[DEFERRED BACKOFF] active={len(backoff)}")
    blocked = buckets.get("PREREQUISITE_BLOCKED", [])
    if blocked:
        print(f"[DEFERRED DEPENDENCY] prerequisite_blocked={len(blocked)}")


def upsert_macro(engine, rows, *, mode="INGEST", prior_lookup=None,
                 prior_value_lookup=None, provenance_lookup=None,
                 trust_start=None,
                 treasury_published_dates=None, deferred_store=None,
                 batch_start=None, count_telemetry=True):
    """Write historical rows.

    ``mode="INGEST"`` (default) enforces the Phase A EOD write gate: only rows
    whose session is CONFIRMED_COMPLETED *and* whose immediately prior valid
    session is confirmed may be written. Deferred/rejected rows never reach SQL.

    ``mode="RECONCILE"`` dispatches to the Phase B reconciliation store path.
    It requires a pre-built ``CorrectionPlan`` and never calls normal INGEST
    duplicate filtering or ``ON CONFLICT DO UPDATE``.
    """
    if not rows:
        return 0

    if mode == "RECONCILE":
        from db_builder.reconciliation import CorrectionPlan, is_auto_correctable, Verdict, Reason
        from db_builder.reconciliation_store import (
            execute_correction_plan,
            record_detection_event,
        )

        plan = rows[0].get("_correction_plan") if isinstance(rows[0], dict) else None
        if plan is None or not isinstance(plan, CorrectionPlan):
            raise ValueError(
                "mode=RECONCILE requires a CorrectionPlan in rows[0]['_correction_plan']"
            )

        # Hard guard: only AUTO_CORRECT symbols may reach UPDATE
        if not is_auto_correctable(plan.symbol):
            store = _get_reconciliation_store(engine)
            record_detection_event(
                store=store,
                symbol=plan.symbol,
                session_date=plan.session_date,
                verdict=plan.verdict,
                reason=plan.reason,
                run_id=plan.evidence.get("run_id", "reconcile"),
                source_definition=plan.evidence.get("source_definition", ""),
                confirmation_source=plan.evidence.get("confirmation_source", ""),
            )
            return 0

        store = _get_reconciliation_store(engine)
        success, error = execute_correction_plan(
            store=store,
            plan=plan,
            run_id=plan.evidence.get("run_id", "reconcile"),
            confirmation_source=plan.evidence.get("confirmation_source", "treasury.gov"),
            source_definition=plan.evidence.get("source_definition", "treasury_gov_cmt"),
        )
        if not success:
            # Record detection event AFTER the repair transaction has aborted
            record_detection_event(
                store=store,
                symbol=plan.symbol,
                session_date=plan.session_date,
                verdict=Verdict.STALE_PLAN if error == "STALE_PLAN" else Verdict.MANUAL_REVIEW_REQUIRED,
                reason=Reason.STALE_PLAN if error == "STALE_PLAN" else Reason.MANUAL_REVIEW_REQUIRED,
                run_id=plan.evidence.get("run_id", "reconcile"),
                source_definition=plan.evidence.get("source_definition", ""),
                confirmation_source=plan.evidence.get("confirmation_source", ""),
            )
        return 1 if success else 0

    if mode == "INGEST":
        rows, _deferred, _rejected = route_historical_rows(
            engine, rows, prior_lookup=prior_lookup,
            prior_value_lookup=prior_value_lookup,
            provenance_lookup=provenance_lookup,
            trust_start=trust_start,
            treasury_published_dates=treasury_published_dates,
            treasury_published_values=_TREASURY_PUBLISHED_VALUES or None,
            deferred_store=deferred_store, batch_start=batch_start,
            count_telemetry=count_telemetry)
        if not rows:
            print("[GATE] no eligible historical rows after EOD gate")
            return 0

    # ``mode="MIRROR"`` is a destination-mirroring write: the input rows are
    # already-canonical, already-committed LOCAL rows being copied to another
    # database (Neon). It deliberately does NOT run the Phase A ingestion gate:
    # no provider eligibility, no provenance reclassification, no deferral, no
    # attempt_count, no C2 mutation. Universe/window safety is enforced by the
    # caller (sync_recent_macro_to_neon) before rows reach here.
    # Fall through to the same INSERT ... ON CONFLICT DO UPDATE below.

    df = pd.DataFrame(rows)
    df = df.where(pd.notna(df), None)

    sql = text("""
        INSERT INTO public.macro (
            date, symbol, name, asset_type,
            open, high, low, close, adj_close, volume,
            prev_close, change, pct_chg, amplitude
        )
        VALUES (
            :date, :symbol, :name, :asset_type,
            :open, :high, :low, :close, :adj_close, :volume,
            :prev_close, :change, :pct_chg, :amplitude
        )
        ON CONFLICT (date, symbol)
        DO UPDATE SET
            name = EXCLUDED.name,
            asset_type = EXCLUDED.asset_type,
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            adj_close = EXCLUDED.adj_close,
            volume = EXCLUDED.volume,
            prev_close = EXCLUDED.prev_close,
            change = EXCLUDED.change,
            pct_chg = EXCLUDED.pct_chg,
            amplitude = EXCLUDED.amplitude;
    """)

    with engine.begin() as conn:
        conn.execute(sql, df.to_dict(orient="records"))

    return len(df)


# ============================================================
# HELPERS
# ============================================================

def chunk_list(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def round_4(value):
    if pd.isna(value):
        return None
    return round(float(value), 4)


def get_close_config(symbol):
    if symbol not in SYMBOL_CLOSE_MAP:
        raise ValueError(f"Missing SYMBOL_CLOSE_MAP config for: {symbol}")

    return SYMBOL_CLOSE_MAP[symbol]


# 24/7 daily-session products. For these the daily bar labelled D spans
# D 00:00:00 UTC -> D+1 00:00:00 UTC, so there is no exchange close to use as
# the session boundary. The session boundary substitutes for the exchange close
# and the global "close + 30 minutes" rule is preserved unchanged:
#
#     session_end(D)  = D+1 00:00:00 UTC
#     EOD_ELIGIBLE(D) = D+1 00:30:00 UTC
#
# Consequently the current UTC calendar day's bar is never finalized.
# Membership is derived from the existing source definition (no new schema).
CRYPTO_24_7_SYMBOLS: frozenset = frozenset(
    sym for sym, sd in SOURCE_DEFINITION_BY_SYMBOL.items()
    if sd == "CRYPTO_24_7_DAILY"
)


def is_24_7_daily_session(symbol) -> bool:
    """True when ``symbol`` trades 24/7 and uses the UTC daily-session boundary."""
    return symbol in CRYPTO_24_7_SYMBOLS


def session_end_utc(symbol, bar_date):
    """Return the UTC instant at which the daily session labelled ``bar_date`` ends.

    Exchange-traded products: ``bar_date`` at the configured exchange close.
    CRYPTO_24_7_DAILY: ``bar_date + 1 day`` at 00:00 UTC.
    """
    cfg = get_close_config(symbol)

    if is_24_7_daily_session(symbol):
        return datetime.combine(
            bar_date + timedelta(days=1), datetime.min.time(), tzinfo=ZoneInfo("UTC")
        )

    tz = ZoneInfo(cfg["tz"])
    close_hour, close_minute = map(int, cfg["close_time"].split(":"))
    return datetime.combine(
        bar_date, datetime.min.time(), tzinfo=tz
    ).replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)


def symbol_date_has_closed(symbol, bar_date, now=None):
    """EOD eligibility test for a bar date: session end + 30 minutes, plus trading-day validity.

    H1: the original test was time-only, so a provider row labelled a holiday or
    weekend passed whenever ``now >= close_time``. A non-session date is now
    rejected before the time comparison.

    The trading-day test is calendar-aware and fail-closed: an unknown or
    unavailable calendar returns False (defer), never True. It is deliberately
    NOT a weekday-only rule, because crypto trades weekends and Globex has a
    Sunday session.

    24/7 products (``CRYPTO_24_7_DAILY``) use the UTC daily-session boundary in
    place of an exchange close: session end is ``D+1 00:00 UTC`` and the bar
    becomes eligible at ``D+1 00:30 UTC``. The global "session close + 30
    minutes" rule is unchanged; only the session-end instant differs. The
    current UTC calendar day's bar is therefore never finalized.

    ``now`` is injectable for deterministic tests; production callers omit it
    and get the current wall clock in the symbol's timezone.
    """
    cfg = get_close_config(symbol)

    if not adapter_for(symbol).is_trading_day(bar_date):
        return False

    if is_24_7_daily_session(symbol):
        # Compare in UTC so the decision is independent of the caller's clock.
        current = now if now is not None else datetime.now(ZoneInfo("UTC"))
        if current.tzinfo is None:
            current = current.replace(tzinfo=ZoneInfo("UTC"))
        else:
            current = current.astimezone(ZoneInfo("UTC"))
        return current >= session_end_utc(symbol, bar_date) + timedelta(
            minutes=GLOBAL_BUFFER_MINUTES
        )

    tz = ZoneInfo(cfg["tz"])
    now_local = now.astimezone(tz) if now is not None else datetime.now(tz)

    bar_close_dt = session_end_utc(symbol, bar_date)
    bar_close_dt += timedelta(minutes=GLOBAL_BUFFER_MINUTES)

    return now_local >= bar_close_dt


def validate_provider_bar_completeness(bar):
    """Check if a provider bar has all required OHLC fields valid.

    Returns:
      True if bar is complete and valid
      False if any required field is missing or invalid
    """
    import math as _math
    for field in ("Open", "High", "Low", "Close"):
        val = bar.get(field)
        if val is None:
            return False
        try:
            if pd.isna(val):
                return False
        except Exception:
            pass
        try:
            if not _math.isfinite(float(val)):
                return False
        except (TypeError, ValueError):
            return False
    # Validate OHLC relationship
    o, h, l, c = float(bar["Open"]), float(bar["High"]), float(bar["Low"]), float(bar["Close"])
    if not (h >= l and h >= o and h >= c and l <= o and l <= c):
        return False
    return True


# ---------------------------------------------------------------------------
# macro_live source validation for USnYT=X symbols
# ---------------------------------------------------------------------------
#
# USnYT=X symbols (US2YT=X ... US30YT=X) must only contain Investing.com
# market-yield data in macro_live. Treasury.gov CMT data belongs exclusively
# to USnYT_CMT symbols and must never appear under USnYT=X.

_US_YIELD_LIVE_SYMBOLS = frozenset(
    symbol for symbol in TREASURY_SYMBOLS
    if symbol.endswith("YT=X")
)

_VALID_US_YIELD_LIVE_SOURCES = frozenset({"investing.com"})

_WRONG_SOURCE_LIVE_PATTERNS = (
    "treasury",
    "cmt",
    "tnx",
    "tyx",
)


def validate_macro_live_source(symbol, source):
    """Validate that a macro_live row for USnYT=X has the correct source.

    USnYT=X symbols must only contain Investing.com market-yield data.
    Treasury.gov, CMT, ^TNX, ^TYX, and other source families are rejected.

    Args:
      symbol: The macro symbol (e.g. "US30YT=X").
      source: The source string stored in macro_live.

    Returns:
      (True, "OK") if the source is valid for this symbol.
      (False, reason) if the source is rejected.
    """
    if symbol not in _US_YIELD_LIVE_SYMBOLS:
        # Not a USnYT=X symbol — no restriction.
        return True, "OK (not USnYT=X)"

    source_lower = str(source).lower() if source else ""

    # Explicit allow-list first.
    if source_lower in _VALID_US_YIELD_LIVE_SOURCES:
        return True, "OK"

    # Reject known wrong-source families.
    for pattern in _WRONG_SOURCE_LIVE_PATTERNS:
        if pattern in source_lower:
            return False, (
                f"REJECTED: source '{source}' contains forbidden pattern "
                f"'{pattern}' for {symbol}"
            )

    # Reject anything that is not explicitly allowed.
    return False, (
        f"REJECTED: source '{source}' is not in the allowed set "
        f"{sorted(_VALID_US_YIELD_LIVE_SOURCES)} for {symbol}"
    )


def auto_clean_wrong_source_live_rows(engine):
    """Remove wrong-source macro_live rows for all USnYT=X symbols.

    For each USnYT=X symbol, if macro_live contains a row from the wrong
    source family (treasury.gov, CMT, etc.), remove it. This prevents
    stale wrong-source rows from surviving indefinitely.

    Args:
      engine: A SQLAlchemy engine connected to the local database.

    Returns:
      list of (symbol, old_source) tuples for each row that was removed.
    """
    removed = []
    with engine.connect() as conn:
        for symbol in sorted(_US_YIELD_LIVE_SYMBOLS):
            row = conn.execute(
                text("SELECT source FROM macro_live WHERE symbol = :sym"),
                {"sym": symbol},
            ).fetchone()
            if row is None:
                continue
            old_source = row[0]
            valid, reason = validate_macro_live_source(symbol, old_source)
            if not valid:
                conn.execute(
                    text("DELETE FROM macro_live WHERE symbol = :sym"),
                    {"sym": symbol},
                )
                removed.append((symbol, old_source))
                print(
                    f"[MACRO_LIVE AUTO-CLEAN] {symbol}: "
                    f"removed wrong-source row (source={old_source}) | {reason}"
                )
        conn.commit()
    return removed


def classify_market_session(symbol, provider_session_date, now=None, provider_bar=None):
    """Universal market-data finalization classifier.

    Returns one of:
      LIVE                — session is still open; write/update macro_live
      FINALIZED_EOD       — session has ended + buffer; write/update public.macro
      SOURCE_BLOCKED      — symbol is R22-protected or otherwise blocked
      NOT_AVAILABLE       — unknown symbol or future date
      PROVIDER_BAR_INCOMPLETE — session finalized but provider bar has missing/invalid OHLC

    Universal rule:
      FINALIZED_AT = PRODUCT_DAY_END + 30 minutes (GLOBAL_BUFFER_MINUTES)

    This is the single canonical classifier. All ingestion paths (normal
    ingestion, EOD refresh, Investing fetch, Yahoo fetch, Market Tape
    selection) must call this function. No path may implement its own
    finalization logic.
    """
    # Handle string dates
    if isinstance(provider_session_date, str):
        try:
            provider_session_date = date.fromisoformat(provider_session_date)
        except ValueError:
            return "NOT_AVAILABLE"

    # R22-protected symbols are source-blocked
    if _is_r22_protected(symbol, provider_session_date):
        return "SOURCE_BLOCKED"

    # 24/7 crypto sessions use UTC daily-session boundary
    if is_24_7_daily_session(symbol):
        current_utc = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo("UTC"))
        today_utc = current_utc.date()
        if provider_session_date > today_utc:
            return "NOT_AVAILABLE"
        if provider_session_date < today_utc:
            return "FINALIZED_EOD"
        session_end = session_end_utc(symbol, provider_session_date)
        return "FINALIZED_EOD" if current_utc >= session_end + timedelta(minutes=GLOBAL_BUFFER_MINUTES) else "LIVE"

    # Unknown symbols are not available
    try:
        cfg = get_close_config(symbol)
    except (KeyError, ValueError):
        return "NOT_AVAILABLE"

    # Use the symbol's local date for the future check
    tz = ZoneInfo(cfg["tz"])
    now_local = (now or datetime.now(ZoneInfo("UTC"))).astimezone(tz)
    today = now_local.date()

    # Future dates are not available
    if provider_session_date > today:
        return "NOT_AVAILABLE"

    # Previous day sessions are always finalized (provider returned the data)
    if provider_session_date < today:
        # Check completeness if provider bar is provided
        if provider_bar is not None and not validate_provider_bar_completeness(provider_bar):
            return "PROVIDER_BAR_INCOMPLETE"
        return "FINALIZED_EOD"

    # Same-day session: check close time
    close_time = cfg["close_time"]
    close_hour, close_minute = map(int, close_time.split(":"))
    session_end_local = datetime(provider_session_date.year, provider_session_date.month, provider_session_date.day, close_hour, close_minute, tzinfo=tz)
    session_end_as_utc = session_end_local.astimezone(ZoneInfo("UTC"))
    finalized_at = session_end_as_utc + timedelta(minutes=GLOBAL_BUFFER_MINUTES)

    current_utc = now_local.astimezone(ZoneInfo("UTC"))
    is_finalized = current_utc >= finalized_at
    
    if is_finalized:
        # Check completeness if provider bar is provided
        if provider_bar is not None and not validate_provider_bar_completeness(provider_bar):
            return "PROVIDER_BAR_INCOMPLETE"
        return "FINALIZED_EOD"
    else:
        return "LIVE"


def _is_r22_protected(symbol, session_date):
    """Check if (symbol, date) is R22-protected.

    R22 withdrawal covers 11 original futures symbols × 2 dates = 22 keys.
    Source: R22-TXN-001 immutable withdrawal evidence.

    Precedence: R22_PROTECTED before ordinary source/gap eligibility.
    Even if a symbol is independently blocked by SOURCE_DEFINITION_UNRESOLVED,
    R22 protection must remain independently true.
    """
    r22_futures = {
        "ES=F", "NQ=F", "YM=F", "RTY=F", "NIY=F",
        "GC=F", "SI=F", "HG=F", "NG=F", "BZ=F", "CL=F",
    }
    r22_dates = {date(2026, 10, 5), date(2026, 10, 6)}
    return symbol in r22_futures and session_date in r22_dates


def classify_session_state(symbol, session_date, *, now_utc=None):
    """Deterministic session-state classification.

    Returns one of:
      CURRENT_OPEN_SESSION          — market still trading
      CLOSED_WAITING_FINALIZATION_DELAY — closed but < +30m
      ELIGIBLE_TO_ATTEMPT           — closed >= +30m, provider fetch may be attempted
      NON_TRADING_DAY               — weekend/holiday for this symbol
      SOURCE_UNRESOLVED             — no approved source definition
      R22_PROTECTED                  — R22-withdrawn date
    """
    now = now_utc or datetime.now(ZoneInfo("UTC"))

    # Check if trading day
    if not adapter_for(symbol).is_trading_day(session_date):
        return "NON_TRADING_DAY"

    # Check R22 protection — MUST precede source/gap eligibility
    if _is_r22_protected(symbol, session_date):
        return "R22_PROTECTED"

    # Check if source definition is resolved
    source_def = SOURCE_DEFINITION_BY_SYMBOL.get(symbol)
    if source_def is None:
        return "SOURCE_UNRESOLVED"

    # Time-based close test
    cfg = get_close_config(symbol)
    tz = ZoneInfo(cfg["tz"])
    close_hour, close_minute = map(int, cfg["close_time"].split(":"))
    # For crypto with close_time="00:00", the session D closes at D 00:00 UTC
    # (which is D+1 00:00 in wall-clock terms).  For equities, close_time is
    # the wall-clock close on the same calendar date.
    if close_hour == 0 and close_minute == 0:
        # Crypto: session D closes at D 00:00 UTC = (D+1) 00:00 local
        close_date = session_date + timedelta(days=1)
    else:
        close_date = session_date
    bar_close_dt = datetime.combine(
        close_date, datetime.min.time(), tzinfo=tz,
    ).replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
    bar_close_dt += timedelta(minutes=GLOBAL_BUFFER_MINUTES)

    now_local = now.astimezone(tz)
    if now_local < bar_close_dt:
        # Check if market is still open or just closed
        market_close_dt = bar_close_dt - timedelta(minutes=GLOBAL_BUFFER_MINUTES)
        if now_local < market_close_dt:
            return "CURRENT_OPEN_SESSION"
        return "CLOSED_WAITING_FINALIZATION_DELAY"

    return "ELIGIBLE_TO_ATTEMPT"


def detect_historical_gaps(*, symbol, scan_start, scan_end, stored_dates):
    """Detect missing expected sessions for a symbol.

    Returns a sorted list of dates that are:
      - valid trading days for this symbol
      - within [scan_start, scan_end]
      - NOT in stored_dates
      - NOT R22-protected
      - NOT frozen/unresolved
    """
    adapter = adapter_for(symbol)
    gaps = []

    current = scan_start
    while current <= scan_end:
        if adapter.is_trading_day(current):
            if current not in stored_dates:
                state = classify_session_state(symbol, current)
                if state not in ("NON_TRADING_DAY", "SOURCE_FROZEN",
                                 "SOURCE_UNRESOLVED", "R22_PROTECTED"):
                    gaps.append(current)
        current += timedelta(days=1)

    return sorted(gaps)


def derive_historical_row(*, provider_bar, stored_prev_close, symbol, session_date):
    """Derive one historical row from provider bar + explicit stored P.close.

    Uses canonical production rounding: round(float(x), 4).
    Never uses provider-supplied previous close or positional iloc.
    """
    close = round(float(provider_bar["Close"]), 4)
    high = round(float(provider_bar["High"]), 4)
    low = round(float(provider_bar["Low"]), 4)
    open_ = round(float(provider_bar["Open"]), 4)
    volume = int(provider_bar["Volume"]) if pd.notna(provider_bar.get("Volume")) else None
    adj_close = round(float(provider_bar["Adj Close"]), 4) if "Adj Close" in provider_bar and pd.notna(provider_bar.get("Adj Close")) else None

    prev_close = round(float(stored_prev_close), 4)
    change = round(close - prev_close, 4)
    pct_chg = round(change / prev_close * 100, 4) if prev_close != 0 else None
    amplitude = round((high - low) / prev_close * 100, 4) if prev_close != 0 else None

    return {
        "date": session_date,
        "symbol": symbol,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "adj_close": adj_close,
        "volume": volume,
        "prev_close": prev_close,
        "change": change,
        "pct_chg": pct_chg,
        "amplitude": amplitude,
    }


def backfill_historical_gaps(*, symbol, gaps, provider_data, stored_dates, stored_pcloses):
    """Backfill missing historical gaps chronologically.

    For each gap D:
      1. Resolve expected prior session P
      2. Verify P is in stored_dates (already committed)
      3. Use stored P.close as prev_close (NOT provider P.close, NOT iloc)
      4. Derive row using derive_historical_row
      5. Commit D
      6. Add D to stored_dates + stored_pcloses (so D+1 can use it)

    stored_pcloses: dict mapping date -> committed close for this symbol.
    This ensures candidate-as-confirmed prohibition: uncommitted in-memory
    candidates never act as canonical P.

    Returns dict with:
      inserted: list of dates successfully inserted
      failed: list of (date, reason) tuples
    """
    inserted = []
    failed = []

    for d in gaps:
        source_def = SOURCE_DEFINITION_BY_SYMBOL.get(symbol)
        if source_def is None:
            failed.append((d, "SOURCE_UNRESOLVED"))
            continue

        prior = previous_valid_observation_date(symbol, d, source_def)
        if prior is None:
            failed.append((d, "PRIOR_NOT_FOUND"))
            continue

        if prior not in stored_dates:
            failed.append((d, f"PRIOR_NOT_STORED:{prior}"))
            continue

        # Get stored P.close — authoritative prior
        stored_pclose = stored_pcloses.get(prior)
        if stored_pclose is None:
            failed.append((d, f"PRIOR_CLOSE_NOT_FOUND:{prior}"))
            continue

        # Get provider bar for D
        df = provider_data.get(symbol)
        if df is None or df.empty:
            failed.append((d, "NO_PROVIDER_DATA"))
            continue

        bar_series = df[pd.to_datetime(df["Date"]).dt.date == d]
        if bar_series.empty:
            failed.append((d, "NO_PROVIDER_BAR"))
            continue

        provider_bar = bar_series.iloc[0]

        # Derive using explicit stored P.close — no positional fallback
        try:
            row = derive_historical_row(
                provider_bar=provider_bar,
                stored_prev_close=stored_pclose,
                symbol=symbol,
                session_date=d,
            )
            # Commit D: add to stored_dates and stored_pcloses
            stored_dates.add(d)
            stored_pcloses[d] = row["close"]
            inserted.append(d)
        except Exception as e:
            failed.append((d, str(e)))

    return {"inserted": inserted, "failed": failed}


def refresh_recent_history(*, symbol, provider_data, stored_rows, stored_pcloses, now_utc=None):
    """Refresh the last 5 valid completed sessions for a symbol.

    Uses the already-fetched normalized provider frame — never re-fetches.

    Returns dict with:
      no_change: count of sessions with no provider change
      revisions: list of revision dicts (one per changed session)
      provider_missing: count of sessions with no provider bar
      source_blocked: count of sessions blocked by source definition
      waiting_finalization: count of sessions still in finalization delay
    """
    now = now_utc or datetime.now(ZoneInfo("UTC"))
    source_def = SOURCE_DEFINITION_BY_SYMBOL.get(symbol)
    if source_def is None:
        return {"no_change": 0, "revisions": [], "provider_missing": 0,
                "source_blocked": len(stored_rows), "waiting_finalization": 0}

    # Filter to valid completed sessions that are ELIGIBLE_TO_ATTEMPT
    eligible_sessions = []
    for row in stored_rows:
        d = row["date"]
        state = classify_session_state(symbol, d, now_utc=now)
        if state == "ELIGIBLE_TO_ATTEMPT":
            eligible_sessions.append(row)

    # Take last 5
    recent = eligible_sessions[-RECENT_REFRESH_SESSIONS:]

    no_change = 0
    revisions = []
    provider_missing = 0
    source_blocked = 0
    waiting_finalization = 0

    df = provider_data.get(symbol)
    for row in recent:
        d = row["date"]
        state = classify_session_state(symbol, d, now_utc=now)
        if state == "CLOSED_WAITING_FINALIZATION_DELAY":
            waiting_finalization += 1
            continue
        if state == "SOURCE_FROZEN" or state == "SOURCE_UNRESOLVED":
            source_blocked += 1
            continue

        if df is None or df.empty:
            provider_missing += 1
            continue

        bar_series = df[pd.to_datetime(df["Date"]).dt.date == d]
        if bar_series.empty:
            provider_missing += 1
            continue

        provider_bar = bar_series.iloc[0]

        # Compare raw fields: open, high, low, close, adj_close, volume
        stored_open = row.get("open")
        stored_high = row.get("high")
        stored_low = row.get("low")
        stored_close = row.get("close")
        stored_adj = row.get("adj_close")
        stored_vol = row.get("volume")

        prov_open = round(float(provider_bar["Open"]), 4)
        prov_high = round(float(provider_bar["High"]), 4)
        prov_low = round(float(provider_bar["Low"]), 4)
        prov_close = round(float(provider_bar["Close"]), 4)
        prov_adj = round(float(provider_bar["Adj Close"]), 4) if "Adj Close" in provider_bar and pd.notna(provider_bar.get("Adj Close")) else None
        prov_vol = int(provider_bar["Volume"]) if pd.notna(provider_bar.get("Volume")) else None

        changed_fields = {}
        if stored_open != prov_open:
            changed_fields["open"] = {"old": stored_open, "new": prov_open}
        if stored_high != prov_high:
            changed_fields["high"] = {"old": stored_high, "new": prov_high}
        if stored_low != prov_low:
            changed_fields["low"] = {"old": stored_low, "new": prov_low}
        if stored_close != prov_close:
            changed_fields["close"] = {"old": stored_close, "new": prov_close}
        if stored_adj != prov_adj:
            changed_fields["adj_close"] = {"old": stored_adj, "new": prov_adj}
        if stored_vol != prov_vol:
            changed_fields["volume"] = {"old": stored_vol, "new": prov_vol}

        if not changed_fields:
            no_change += 1
        else:
            revisions.append({
                "symbol": symbol,
                "date": d,
                "changed_fields": changed_fields,
                "source_definition": source_def,
            })

    return {
        "no_change": no_change,
        "revisions": revisions,
        "provider_missing": provider_missing,
        "source_blocked": source_blocked,
        "waiting_finalization": waiting_finalization,
    }


def propagate_revision(*, symbol, d, new_d_close, stored_rows, stored_pcloses):
    """Propagate D.close change to D+1 derived fields.

    If D.close changes and D+1 exists in stored_rows:
      - update D+1.prev_close = new D.close
      - recompute D+1.change, pct_chg, amplitude

    Does NOT propagate to D+2 unless D+1.close itself changes.
    Does NOT touch D+1 if only D.adj_close changed.

    Returns list of D+1 repair dicts (empty if no repair needed).
    """
    # Find D+1 (next stored session after D)
    d_plus_1 = None
    for row in stored_rows:
        if row["date"] > d:
            d_plus_1 = row
            break

    if d_plus_1 is None:
        return []  # D+1 does not exist

    # Get D+1's stored values
    d1_prev_close = d_plus_1.get("prev_close")
    d1_close = d_plus_1.get("close")
    d1_high = d_plus_1.get("high")
    d1_low = d_plus_1.get("low")

    # New D+1 derived fields
    new_d1_prev_close = round(float(new_d_close), 4)
    new_d1_change = round(d1_close - new_d1_prev_close, 4)
    new_d1_pct_chg = round(new_d1_change / new_d1_prev_close * 100, 4) if new_d1_prev_close != 0 else None
    new_d1_amplitude = round((d1_high - d1_low) / new_d1_prev_close * 100, 4) if new_d1_prev_close != 0 else None

    # Check if anything actually changed
    if d1_prev_close == new_d1_prev_close:
        return []  # No change needed

    repairs = []
    if d1_prev_close != new_d1_prev_close:
        repairs.append({
            "symbol": symbol,
            "date": d_plus_1["date"],
            "field": "prev_close",
            "old_value": d1_prev_close,
            "new_value": new_d1_prev_close,
        })
    if d_plus_1.get("change") != new_d1_change:
        repairs.append({
            "symbol": symbol,
            "date": d_plus_1["date"],
            "field": "change",
            "old_value": d_plus_1.get("change"),
            "new_value": new_d1_change,
        })
    if d_plus_1.get("pct_chg") != new_d1_pct_chg:
        repairs.append({
            "symbol": symbol,
            "date": d_plus_1["date"],
            "field": "pct_chg",
            "old_value": d_plus_1.get("pct_chg"),
            "new_value": new_d1_pct_chg,
        })
    if d_plus_1.get("amplitude") != new_d1_amplitude:
        repairs.append({
            "symbol": symbol,
            "date": d_plus_1["date"],
            "field": "amplitude",
            "old_value": d_plus_1.get("amplitude"),
            "new_value": new_d1_amplitude,
        })

    return repairs


def _stale_plan_guard(*, engine, symbol, session_date, expected_old_values):
    """Stale-plan protection: verify stored values match expected before mutation.

    Returns True if safe to proceed, False if drift detected (STALE_PLAN).
    """
    if engine is None:
        return True  # No engine in test mode

    try:
        with engine.connect() as conn:
            # Lock the row for update
            result = conn.execute(
                text(f"""
                    SELECT * FROM {MACRO_TABLE}
                    WHERE symbol = :symbol AND date = :date
                    FOR UPDATE
                """),
                {"symbol": symbol, "date": session_date},
            )
            row = result.fetchone()
            if row is None:
                return False  # Row missing

            # Verify expected old values using null-safe comparisons
            for field, expected_val in expected_old_values.items():
                actual_val = getattr(row, field, None)
                if expected_val is None and actual_val is not None:
                    return False  # Stale: expected NULL but got value
                if expected_val is not None and actual_val is None:
                    return False  # Stale: expected value but got NULL
                if expected_val is not None and actual_val is not None:
                    if round(float(actual_val), 4) != round(float(expected_val), 4):
                        return False  # Stale: value drifted

            return True  # All checks passed
    except Exception:
        return False  # Fail closed on any error


def run_eod_5session_refresh(
    *,
    symbols: list,
    provider_data: dict,
    now_utc=None,
    dry_run: bool = False,
    engine=None,
    close_check=None,
) -> dict:
    """Run simplified EOD 5-session rolling refresh.

    For each symbol:
      1. Get latest 5 valid trading sessions from provider frame.
      2. Only process sessions where now >= close+30.
      3. Compare each eligible session against DB.
      4. Missing → INSERT with P.close as prev_close.
      5. Different → UPDATE raw fields, recompute derived.
      6. If D.close changed → repair D+1.

    Returns committed outcome counters (not predictions).
    Does NOT use C2 attempt_count. Does NOT depend on deferred recovery.

    ``engine`` and ``close_check`` are injectable for testing; production
    callers omit them and get ``local_engine`` / ``symbol_date_has_closed``.
    """
    from datetime import timezone as _tz

    now = now_utc or datetime.now(_tz.utc)
    _engine = engine if engine is not None else local_engine
    # One shared eligibility implementation. Production passes the same instant
    # the refresh itself resolved, so ingestion and refresh cannot disagree.
    _closed = close_check if close_check is not None else (
        lambda sym, d: symbol_date_has_closed(sym, d, now)
    )

    counters = {
        "symbols_checked": 0,
        "sessions_checked": 0,
        "not_yet_finalized": 0,
        "no_change": 0,
        "rows_inserted": 0,
        "rows_revised": 0,
        "close_revisions": 0,
        "open_revisions": 0,
        "high_revisions": 0,
        "low_revisions": 0,
        "volume_revisions": 0,
        "adj_close_revisions": 0,
        "d1_repairs": 0,
        "provider_missing": 0,
        "source_blocked": 0,
        "missing_close_time_mapping": 0,
    }

    for symbol in symbols:
        # Check close-time mapping exists
        if symbol not in SYMBOL_CLOSE_MAP:
            counters["missing_close_time_mapping"] += 1
            continue

        # Check source definition / anchor
        source_def = SOURCE_DEFINITION_BY_SYMBOL.get(symbol)
        if source_def is None:
            counters["source_blocked"] += 1
            continue

        trust_key = (symbol, source_def)
        if trust_key not in C3A_APPROVED_ANCHORS and trust_key not in SOURCE_DEFINITION_TRUST_START:
            counters["source_blocked"] += 1
            continue

        counters["symbols_checked"] += 1

        # Get provider frame
        df = provider_data.get(symbol)
        if df is None or df.empty:
            counters["provider_missing"] += 1
            continue

        # Get the LATEST 5 valid trading sessions (frame may be ascending)
        adapter = adapter_for(symbol)
        all_valid = []
        for d in df["Date"]:
            d_date = pd.to_datetime(d).date()
            if adapter.is_trading_day(d_date):
                all_valid.append(d_date)

        if not all_valid:
            counters["provider_missing"] += 1
            continue

        # Deduplicate and take the latest 5, then sort oldest→newest for
        # sequential processing (required: an uncommitted row must never be P).
        valid_dates = sorted(set(all_valid))[-RECENT_REFRESH_SESSIONS:]

        # Process each session
        for d in valid_dates:
            # Check close+30 eligibility
            if not _closed(symbol, d):
                counters["not_yet_finalized"] += 1
                continue

            counters["sessions_checked"] += 1

            # Check R22 protection
            if _is_r22_protected(symbol, d):
                counters["source_blocked"] += 1
                continue

            # Get provider bar
            bar = df[pd.to_datetime(df["Date"]).dt.date == d]
            if bar.empty:
                counters["provider_missing"] += 1
                continue
            bar = bar.iloc[0]

            # Get stored P (previous valid session)
            p = previous_valid_observation_date(symbol, d, source_def)

            # Canonical normalization — the SAME function normal ingestion uses.
            _n = canonical_normalize_daily_bar(bar)
            open_, high, low = _n["open"], _n["high"], _n["low"]
            close, adj_close, volume = _n["close"], _n["adj_close"], _n["volume"]

            # Check DB
            with _engine.begin() as conn:
                row = conn.execute(
                    text("SELECT * FROM public.macro WHERE symbol = :s AND date = :d"),
                    {"s": symbol, "d": d},
                ).fetchone()

            if row is None:
                # Missing → INSERT
                if p is None:
                    # No P available, cannot determine prev_close
                    counters["source_blocked"] += 1
                    continue

                # Get stored P.close
                with _engine.begin() as conn:
                    p_row = conn.execute(
                        text("SELECT close FROM public.macro WHERE symbol = :s AND date = :d"),
                        {"s": symbol, "d": p},
                    ).fetchone()

                if p_row is None:
                    counters["source_blocked"] += 1
                    continue

                p_close = float(p_row[0])
                change = round_4(close - p_close)
                pct_chg = round_4(change / p_close * 100) if p_close != 0 else None
                amplitude = round_4((high - low) / p_close * 100) if p_close != 0 else None

                if not dry_run:
                    with _engine.begin() as conn:
                        conn.execute(
                            text("""
                                INSERT INTO public.macro (date, symbol, open, high, low, close, adj_close, volume, prev_close, change, pct_chg, amplitude)
                                VALUES (:d, :s, :o, :h, :l, :c, :ac, :v, :pc, :ch, :pct, :amp)
                            """),
                            {"d": d, "s": symbol, "o": open_, "h": high, "l": low, "c": close,
                             "ac": adj_close, "v": volume, "pc": p_close, "ch": change,
                             "pct": pct_chg, "amp": amplitude},
                        )
                counters["rows_inserted"] += 1

            else:
                # Existing → compare and update if different
                stored_close = float(row.close)
                stored_open = float(row.open) if row.open is not None else None
                stored_high = float(row.high) if row.high is not None else None
                stored_low = float(row.low) if row.low is not None else None
                stored_adj = float(row.adj_close) if row.adj_close is not None else None
                stored_vol = round_4(row.volume) if row.volume is not None else None

                changed_fields = {}
                if stored_open != open_:
                    changed_fields["open"] = open_
                if stored_high != high:
                    changed_fields["high"] = high
                if stored_low != low:
                    changed_fields["low"] = low
                if stored_close != close:
                    changed_fields["close"] = close
                if stored_adj != adj_close:
                    changed_fields["adj_close"] = adj_close
                if stored_vol != volume:
                    changed_fields["volume"] = volume

                if not changed_fields:
                    counters["no_change"] += 1
                    continue

                close_changed = "close" in changed_fields
                hl_changed = "high" in changed_fields or "low" in changed_fields

                # Resolve P.close only when a derived field must be recomputed.
                if close_changed or hl_changed:
                    if p is not None:
                        with _engine.begin() as conn:
                            p_row = conn.execute(
                                text("SELECT close FROM public.macro WHERE symbol = :s AND date = :d"),
                                {"s": symbol, "d": p},
                            ).fetchone()
                        p_close = float(p_row[0]) if p_row else stored_close
                    else:
                        p_close = stored_close

                    # Recompute only the fields affected by the revision.
                    if close_changed:
                        new_change = round_4(close - p_close)
                        changed_fields["prev_close"] = p_close
                        changed_fields["change"] = new_change
                        changed_fields["pct_chg"] = (
                            round_4(new_change / p_close * 100) if p_close != 0 else None
                        )
                    if close_changed or hl_changed:
                        changed_fields["amplitude"] = (
                            round_4((high - low) / p_close * 100) if p_close != 0 else None
                        )

                if not dry_run:
                    set_clause = ", ".join(f"{k} = :{k}" for k in changed_fields)
                    with _engine.begin() as conn:
                        conn.execute(
                            text(f"UPDATE public.macro SET {set_clause} WHERE symbol = :s AND date = :d"),
                            {"s": symbol, "d": d, **changed_fields},
                        )

                counters["rows_revised"] += 1
                if close_changed:
                    counters["close_revisions"] += 1
                for _f, _ck in (("open", "open_revisions"), ("high", "high_revisions"),
                                ("low", "low_revisions"), ("volume", "volume_revisions"),
                                ("adj_close", "adj_close_revisions")):
                    if _f in changed_fields:
                        counters[_ck] += 1

                # D+1 repair only when D.close actually changed.
                if close_changed:
                    d1_candidates = [x for x in valid_dates if x > d]
                    if d1_candidates:
                        d1 = d1_candidates[0]
                        # Check D+1 exists in DB
                        with _engine.begin() as conn:
                            d1_row = conn.execute(
                                text("SELECT * FROM public.macro WHERE symbol = :s AND date = :d"),
                                {"s": symbol, "d": d1},
                            ).fetchone()
                        if d1_row:
                            d1_close = float(d1_row.close)
                            d1_high = float(d1_row.high)
                            d1_low = float(d1_row.low)
                            d1_new_change = round_4(d1_close - close)
                            d1_new_pct = round_4(d1_new_change / close * 100) if close != 0 else None
                            d1_new_amp = round_4((d1_high - d1_low) / close * 100) if close != 0 else None

                            if not dry_run:
                                with _engine.begin() as conn:
                                    conn.execute(
                                        text("""
                                            UPDATE public.macro
                                            SET prev_close = :pc, change = :ch, pct_chg = :pct, amplitude = :amp
                                            WHERE symbol = :s AND date = :d
                                        """),
                                        {"pc": close, "ch": d1_new_change, "pct": d1_new_pct,
                                         "amp": d1_new_amp, "s": symbol, "d": d1},
                                    )
                            counters["d1_repairs"] += 1

    # Print summary
    print(
        f"[DAILY HISTORY REFRESH] "
        f"symbols_checked={counters['symbols_checked']} "
        f"sessions_checked={counters['sessions_checked']} "
        f"not_yet_finalized={counters['not_yet_finalized']} "
        f"no_change={counters['no_change']} "
        f"rows_inserted={counters['rows_inserted']} "
        f"rows_revised={counters['rows_revised']} "
        f"close_revisions={counters['close_revisions']} "
        f"open_revisions={counters['open_revisions']} "
        f"high_revisions={counters['high_revisions']} "
        f"low_revisions={counters['low_revisions']} "
        f"volume_revisions={counters['volume_revisions']} "
        f"adj_close_revisions={counters['adj_close_revisions']} "
        f"d1_repairs={counters['d1_repairs']} "
        f"provider_missing={counters['provider_missing']} "
        f"source_blocked={counters['source_blocked']} "
        f"missing_close_time_mapping={counters['missing_close_time_mapping']}"
    )

    return counters


def sync_recent_macro_to_neon(*, symbols, local_engine_=None, neon_engine_=None,
                              window=RECENT_REFRESH_SESSIONS, dry_run=False):
    """Converge Neon's ``public.macro`` to the canonical LOCAL rows.

    Ordering contract (production)::

        provider fetch -> normal ingestion -> C2 recovery
          -> local EOD 5-session refresh -> LOCAL COMMIT
          -> local-to-Neon convergence sync -> downstream/API availability

    LOCAL is authoritative. This function reads the canonical, already-committed
    rows straight out of local ``public.macro``; it never fetches provider data
    and never recalculates a value. For the bounded recent window (the latest
    ``window`` sessions per symbol — the same window the EOD refresh maintains)
    it upserts every local row into Neon using the existing conflict key
    ``(date, symbol)`` with ``ON CONFLICT ... DO UPDATE``, so that BOTH new rows
    and revisions of existing rows propagate.

    Destination safety is enforced here (MIRROR bypasses ingestion *eligibility*,
    never universe controls):

    * only symbols present in the approved EOD/API universe;
    * R22-protected keys are never synchronized;
    * a bounded recent window only — no uncontrolled full-history mirror;
    * the destination engine must not be the local engine (MIRROR never writes
      back to local).

    Counters are truthful: ``committed_rows`` counts rows actually submitted to
    the upsert and accepted (never merely candidates).

    ``local_engine_`` / ``neon_engine_`` are injectable for tests; production
    callers omit them and get ``local_engine`` / ``neon_engine``.
    """
    _local = local_engine_ if local_engine_ is not None else local_engine
    _neon = neon_engine_ if neon_engine_ is not None else neon_engine

    if _local is None or _neon is None:
        raise ValueError("sync_recent_macro_to_neon requires both engines")
    if _neon is _local:
        raise ValueError(
            "MIRROR destination must not be the local engine: "
            "the convergence sync never writes back to local"
        )

    counters = {
        "candidate_rows": 0,
        "committed_rows": 0,
        "inserted_rows": 0,
        "updated_rows": 0,
        "unchanged_rows": 0,
        "failed_rows": 0,
        "skipped_rows": 0,
        "local_latest_date": None,
        "neon_latest_date": None,
        "window": window,
    }

    # ---- destination safety: approved universe, R22 excluded ----
    approved = []
    for s in symbols:
        sd = SOURCE_DEFINITION_BY_SYMBOL.get(s)
        if sd is None:
            counters["skipped_rows"] += 1
            continue
        key = (s, sd)
        if key not in C3A_APPROVED_ANCHORS and key not in SOURCE_DEFINITION_TRUST_START:
            counters["skipped_rows"] += 1
            continue
        approved.append(s)

    if not approved:
        print("[NEON MACRO SYNC] no approved symbols in scope")
        return counters

    # ---- read canonical rows from LOCAL (never from provider) ----
    with _local.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT date, symbol, name, asset_type,
                       open, high, low, close, adj_close, volume,
                       prev_close, change, pct_chg, amplitude
                FROM (
                    SELECT m.*,
                           row_number() OVER (
                               PARTITION BY symbol ORDER BY date DESC
                           ) AS _rn
                    FROM public.macro m
                    WHERE symbol = ANY(:symbols)
                ) ranked
                WHERE _rn <= :window
                ORDER BY symbol, date
            """),
            {"symbols": approved, "window": window},
        ).mappings().all()

    local_rows = []
    for r in rows:
        d = r["date"]
        # R22 exact-key protection (mirror must never restore withdrawn keys)
        if _is_r22_protected(r["symbol"], d):
            counters["skipped_rows"] += 1
            continue
        local_rows.append({k: r[k] for k in (
            "date", "symbol", "name", "asset_type",
            "open", "high", "low", "close", "adj_close", "volume",
            "prev_close", "change", "pct_chg", "amplitude")})

    counters["candidate_rows"] = len(local_rows)
    if local_rows:
        counters["local_latest_date"] = str(max(r["date"] for r in local_rows))

    if not local_rows:
        print(f"[NEON MACRO SYNC] candidate_rows=0 window={window}")
        return counters

    # ---- current Neon state (for truthful unchanged/committed accounting) ----
    neon_existing = {}
    try:
        with _neon.connect() as conn:
            ex = conn.execute(
                text("""
                    SELECT date, symbol, open, high, low, close, adj_close, volume,
                           prev_close, change, pct_chg, amplitude
                    FROM public.macro
                    WHERE symbol = ANY(:symbols)
                """),
                {"symbols": approved},
            ).mappings().all()
        for r in ex:
            neon_existing[(r["symbol"], str(r["date"]))] = r
            counters["neon_latest_date"] = str(max(
                counters["neon_latest_date"] or str(r["date"]), str(r["date"])))
    except Exception as exc:
        counters["failed_rows"] = len(local_rows)
        counters["committed_rows"] = 0
        print(f"[NEON MACRO SYNC FAILED] could not read Neon state: "
              f"{type(exc).__name__}: {exc}")
        print("[NEON MACRO SYNC] local DB remains authoritative; "
              "no local rollback performed")
        print(
            f"[NEON MACRO SYNC] candidate_rows={counters['candidate_rows']} "
            f"inserted_rows={counters['inserted_rows']} "
            f"updated_rows={counters['updated_rows']} "
            f"unchanged_rows={counters['unchanged_rows']} "
            f"committed_rows={counters['committed_rows']} "
            f"failed_rows={counters['failed_rows']} "
            f"skipped_rows={counters['skipped_rows']} "
            f"local_latest_date={counters['local_latest_date']} "
            f"neon_latest_date={counters['neon_latest_date']}"
        )
        return counters

    VALUE_FIELDS = ("open", "high", "low", "close", "adj_close", "volume",
                    "prev_close", "change", "pct_chg", "amplitude")

    def _eq(a, b):
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        try:
            return abs(float(a) - float(b)) < 1e-9
        except (TypeError, ValueError):
            return a == b

    to_submit = []
    for r in local_rows:
        key = (r["symbol"], str(r["date"]))
        prior = neon_existing.get(key)
        if prior is None:
            to_submit.append(r)
            counters["inserted_rows"] += 1
            continue
        differs = any(
            not _eq(r[f], prior.get(f)) for f in VALUE_FIELDS
        ) or (r.get("name") != prior.get("name")) \
           or (r.get("asset_type") != prior.get("asset_type"))
        if differs:
            to_submit.append(r)
            counters["updated_rows"] += 1
        else:
            counters["unchanged_rows"] += 1

    # ---- submit (MIRROR: direct upsert, no Phase A gate) ----
    # In production (no injected engine), use a FRESH Neon engine for the
    # write to avoid connection-pool staleness from the pre-sync read.
    # In test mode (injected engine), use the injected engine consistently.
    if to_submit and not dry_run:
        try:
            _write_neon = _neon if neon_engine_ is not None else make_neon_engine()
            with _write_neon.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO public.macro (
                            date, symbol, name, asset_type,
                            open, high, low, close, adj_close, volume,
                            prev_close, change, pct_chg, amplitude
                        )
                        VALUES (
                            :date, :symbol, :name, :asset_type,
                            :open, :high, :low, :close, :adj_close, :volume,
                            :prev_close, :change, :pct_chg, :amplitude
                        )
                        ON CONFLICT (date, symbol)
                        DO UPDATE SET
                            name = EXCLUDED.name,
                            asset_type = EXCLUDED.asset_type,
                            open = EXCLUDED.open,
                            high = EXCLUDED.high,
                            low = EXCLUDED.low,
                            close = EXCLUDED.close,
                            adj_close = EXCLUDED.adj_close,
                            volume = EXCLUDED.volume,
                            prev_close = EXCLUDED.prev_close,
                            change = EXCLUDED.change,
                            pct_chg = EXCLUDED.pct_chg,
                            amplitude = EXCLUDED.amplitude
                    """),
                    to_submit,
                )
            _write_neon.dispose()
            counters["committed_rows"] = len(to_submit)
        except Exception as exc:
            counters["failed_rows"] = len(to_submit)
            counters["committed_rows"] = 0
            print(f"[NEON MACRO SYNC FAILED] submitted={len(to_submit)} "
                  f"error={type(exc).__name__}: {exc}")
            print("[NEON MACRO SYNC] local DB remains authoritative; "
                  "no local rollback performed")
    elif to_submit and dry_run:
        print(f"[DRY RUN SKIP NEON MACRO SYNC] would_submit={len(to_submit)}")

    # refresh neon_latest_date view after commit
    if counters["committed_rows"]:
        counters["neon_latest_date"] = str(max(
            [counters["neon_latest_date"]] + [str(r["date"]) for r in to_submit]
            if counters["neon_latest_date"] else [str(r["date"]) for r in to_submit]
        ))

    # ---- post-sync convergence verification (read-only) ----
    # For every synchronized key, compare local vs Neon for OHLCV + derived.
    # Required: LOCAL_NEON_MISMATCH = 0 for the synchronized window.
    # CRITICAL: Use a FRESH Neon engine for verification to avoid reading
    # uncommitted/pooled state from the sync transaction.
    if counters["committed_rows"] and not dry_run:
        try:
            with _local.connect() as conn:
                verify_local = conn.execute(
                    text("""
                        SELECT date, symbol, open, high, low, close, adj_close, volume,
                               prev_close, change, pct_chg, amplitude
                        FROM (
                            SELECT m.*, row_number() OVER (
                                PARTITION BY symbol ORDER BY date DESC
                            ) AS _rn
                            FROM public.macro m
                            WHERE symbol = ANY(:symbols)
                        ) ranked WHERE _rn <= :window
                        ORDER BY symbol, date
                    """),
                    {"symbols": approved, "window": window},
                ).mappings().all()

            # Fresh Neon engine for verification (not the sync engine)
            # In test mode, use the injected engine consistently.
            _verify_neon = _neon if neon_engine_ is not None else make_neon_engine()
            with _verify_neon.connect() as conn:
                verify_neon_rows = conn.execute(
                    text("""
                        SELECT date, symbol, open, high, low, close, adj_close, volume,
                               prev_close, change, pct_chg, amplitude
                        FROM public.macro WHERE symbol = ANY(:symbols)
                    """),
                    {"symbols": approved},
                ).mappings().all()
            _verify_neon.dispose()
            verify_neon_map = {(r["symbol"], str(r["date"])): r for r in verify_neon_rows}

            mismatch_count = 0
            mismatch_details = []
            for lr in verify_local:
                key = (lr["symbol"], str(lr["date"]))
                nr = verify_neon_map.get(key)
                if nr is None:
                    mismatch_count += 1
                    mismatch_details.append(f"{key[0]}/{key[1]}:missing")
                    continue
                for f in VALUE_FIELDS:
                    if not _eq(lr[f], nr.get(f)):
                        mismatch_count += 1
                        mismatch_details.append(f"{key[0]}/{key[1]}.{f}")

            counters["convergence_mismatches"] = mismatch_count
            if mismatch_count:
                print(f"[NEON CONVERGENCE VERIFY FAILED] "
                      f"LOCAL_NEON_MISMATCH={mismatch_count} "
                      f"e.g. {mismatch_details[:5]}")
            else:
                print(f"[NEON CONVERGENCE VERIFY] LOCAL_NEON_MISMATCH=0 "
                      f"({len(verify_local)} keys verified)")
        except Exception as exc:
            counters["convergence_mismatches"] = -1
            print(f"[NEON CONVERGENCE VERIFY ERROR] {type(exc).__name__}: {exc}")

    print(
        f"[NEON MACRO SYNC] candidate_rows={counters['candidate_rows']} "
        f"inserted_rows={counters['inserted_rows']} "
        f"updated_rows={counters['updated_rows']} "
        f"unchanged_rows={counters['unchanged_rows']} "
        f"committed_rows={counters['committed_rows']} "
        f"failed_rows={counters['failed_rows']} "
        f"skipped_rows={counters['skipped_rows']} "
        f"local_latest_date={counters['local_latest_date']} "
        f"neon_latest_date={counters['neon_latest_date']}"
    )
    return counters


def run_c3a_gap_scan(*, symbols, stored_dates_map, stored_pcloses_map, provider_data, stored_rows_map=None):
    """Run C.3A gap scan, recent refresh, and backfill for approved symbols.

    For each symbol:
      1. Detect gaps in BACKFILL_SCAN_DAYS window
      2. Backfill chronologically using provider data + stored P.close
      3. Refresh last 5 sessions for revision detection
      4. Propagate any D.close changes to D+1

    Emits [HISTORY REFRESH] observability line with predicted counters.
    Does NOT increment C.2 attempt_count.
    Does NOT perform actual DB writes in dry-run mode.
    """
    results = {}
    scan_end = datetime.now().date()
    scan_start = scan_end - timedelta(days=BACKFILL_SCAN_DAYS)

    eligible_closed = 0
    recent_sessions_scanned = 0
    no_change_count = 0
    provider_rows_reused = 0
    raw_missing_dates = 0
    expected_session_missing = 0
    trusted_source_missing = 0
    provider_available_missing = 0
    eligible_gap_backfills = 0
    gaps_backfilled = 0
    predicted_revisions = 0
    predicted_derived_repairs = 0
    r22_blocked = 0
    source_blocked = 0
    waiting_finalization = 0
    provider_missing = 0

    for symbol in symbols:
        source_def = SOURCE_DEFINITION_BY_SYMBOL.get(symbol)
        if source_def is None:
            source_blocked += 1
            results[symbol] = {"status": "SOURCE_UNRESOLVED"}
            continue

        trust_key = (symbol, source_def)
        if trust_key not in C3A_APPROVED_ANCHORS and trust_key not in SOURCE_DEFINITION_TRUST_START:
            source_blocked += 1
            results[symbol] = {"status": "NO_ANCHOR"}
            continue

        stored = stored_dates_map.get(symbol, set())
        stored_pcloses = stored_pcloses_map.get(symbol, {})
        stored_rows = stored_rows_map.get(symbol, []) if stored_rows_map else []

        # Step 1: Recent refresh (last 5 sessions)
        refresh_result = refresh_recent_history(
            symbol=symbol,
            provider_data=provider_data,
            stored_rows=stored_rows,
            stored_pcloses=stored_pcloses,
        )
        recent_sessions_scanned += len(stored_rows[-RECENT_REFRESH_SESSIONS:]) if stored_rows else 0
        eligible_closed += refresh_result["no_change"] + len(refresh_result["revisions"])
        no_change_count += refresh_result["no_change"]
        predicted_revisions += len(refresh_result["revisions"])
        provider_missing += refresh_result["provider_missing"]
        source_blocked += refresh_result["source_blocked"]
        waiting_finalization += refresh_result["waiting_finalization"]

        # Step 2: Gap detection (funnel: raw_missing -> expected_session -> trusted_source -> provider -> eligible)
        adapter = adapter_for(symbol)
        _sym_raw_missing = 0
        _sym_expected_missing = 0
        _sym_trusted_source_missing = 0
        _sym_provider_available_missing = 0
        _sym_eligible_backfills = []

        _cur = scan_start
        while _cur <= scan_end:
            if not adapter.is_trading_day(_cur):
                _cur += timedelta(days=1)
                continue
            _cur += timedelta(days=1)

            _state = classify_session_state(symbol, _cur)
            if _state in ("NON_TRADING_DAY", "SOURCE_FROZEN", "SOURCE_UNRESOLVED", "R22_PROTECTED"):
                continue

            if _cur in stored:
                continue

            _sym_raw_missing += 1

            # Check trusted source: P must exist in stored
            _p = previous_valid_observation_date(symbol, _cur, source_def)
            if _p is None or _p not in stored:
                _sym_trusted_source_missing += 1
                continue

            _sym_expected_missing += 1

            # Check provider D availability
            _df = provider_data.get(symbol)
            if _df is None or _df.empty:
                _sym_provider_available_missing += 1
                continue

            _bar = _df[pd.to_datetime(_df["Date"]).dt.date == _cur]
            if _bar.empty:
                _sym_provider_available_missing += 1
                continue

            _sym_eligible_backfills.append(_cur)

        raw_missing_dates += _sym_raw_missing
        expected_session_missing += _sym_expected_missing
        trusted_source_missing += _sym_trusted_source_missing
        provider_available_missing += _sym_provider_available_missing
        eligible_gap_backfills += len(_sym_eligible_backfills)

        if not _sym_eligible_backfills:
            results[symbol] = {"status": "NO_GAPS", "recent_refresh": refresh_result}
            continue

        # Step 3: Chronological backfill (iterate to handle chains: D becomes P for D+1)
        all_inserted = []
        all_failed = []
        remaining_gaps = _sym_eligible_backfills

        while remaining_gaps:
            backfill_result = backfill_historical_gaps(
                symbol=symbol,
                gaps=remaining_gaps,
                provider_data=provider_data,
                stored_dates=stored,
                stored_pcloses=stored_pcloses,
            )
            all_inserted.extend(backfill_result["inserted"])
            all_failed.extend(backfill_result["failed"])
            gaps_backfilled += len(backfill_result["inserted"])

            if not backfill_result["inserted"]:
                break

            # Full re-scan: new commitments may make D+1 eligible
            new_eligible = []
            _cur2 = scan_start
            while _cur2 <= scan_end:
                if not adapter.is_trading_day(_cur2):
                    _cur2 += timedelta(days=1)
                    continue
                _state2 = classify_session_state(symbol, _cur2)
                if _state2 in ("NON_TRADING_DAY", "SOURCE_FROZEN", "SOURCE_UNRESOLVED", "R22_PROTECTED"):
                    _cur2 += timedelta(days=1)
                    continue
                if _cur2 in stored:
                    _cur2 += timedelta(days=1)
                    continue

                _p2 = previous_valid_observation_date(symbol, _cur2, source_def)
                if _p2 is not None and _p2 in stored:
                    _df2 = provider_data.get(symbol)
                    if _df2 is not None and not _df2.empty:
                        _bar2 = _df2[pd.to_datetime(_df2["Date"]).dt.date == _cur2]
                        if not _bar2.empty:
                            new_eligible.append(_cur2)
                _cur2 += timedelta(days=1)

            if not new_eligible:
                break
            remaining_gaps = new_eligible

        # Step 4: D+1 propagation for each inserted gap
        for d in all_inserted:
            new_close = stored_pcloses.get(d)
            if new_close is not None:
                repairs = propagate_revision(
                    symbol=symbol,
                    d=d,
                    new_d_close=new_close,
                    stored_rows=stored_rows,
                    stored_pcloses=stored_pcloses,
                )
                predicted_derived_repairs += len(repairs)

        results[symbol] = {
            "status": "BACKFILLED",
            "inserted": all_inserted,
            "failed": all_failed,
            "recent_refresh": refresh_result,
        }

        provider_rows_reused += len(_sym_eligible_backfills)  # Each gap reuses the provider frame

    print(
        f"[HISTORY REFRESH] "
        f"eligible_closed={eligible_closed} "
        f"recent_sessions_scanned={recent_sessions_scanned} "
        f"no_change={no_change_count} "
        f"provider_rows_reused={provider_rows_reused} "
        f"raw_missing_dates={raw_missing_dates} "
        f"expected_session_missing={expected_session_missing} "
        f"trusted_source_missing={trusted_source_missing} "
        f"provider_available_missing={provider_available_missing} "
        f"eligible_gap_backfills={eligible_gap_backfills} "
        f"gaps_backfilled={gaps_backfilled} "
        f"predicted_revisions={predicted_revisions} "
        f"predicted_derived_repairs={predicted_derived_repairs} "
        f"r22_blocked={r22_blocked} "
        f"source_blocked={source_blocked} "
        f"waiting_finalization={waiting_finalization} "
        f"provider_missing={provider_missing}"
    )
    return results


def remove_unfinished_bars(df, symbol, now=None):
    """Drop provider bars whose daily session has not yet finalized.

    Uses the single shared eligibility helper ``symbol_date_has_closed`` so that
    normal ingestion and the EOD refresh always agree — including the 24/7
    crypto session boundary.
    """
    if df.empty:
        return df

    df = df.sort_values("Date").copy()
    keep_mask = []

    for _, row in df.iterrows():
        bar_date = pd.to_datetime(row["Date"]).date()
        # Preserve the 2-argument call contract for existing callers/stubs;
        # only pass an explicit instant when one was supplied.
        if now is None:
            keep_mask.append(symbol_date_has_closed(symbol, bar_date))
        else:
            keep_mask.append(symbol_date_has_closed(symbol, bar_date, now))

    return df[keep_mask].copy()


def extract_latest_unfinished_row(df, symbol, name, asset_type, observed_at=None, source_label=None,
                                  allow_closed_session=False):
    """Return the latest incomplete daily bar as a transient live snapshot.

    Phase A dual-source rule: a *closed* session whose official EOD value is not
    yet published (e.g. Treasury.gov par yields) must still be observable live.
    ``allow_closed_session`` admits that case; the row stays labelled
    ``is_market_closed=False`` so it can never be mistaken for official EOD.
    """
    if df.empty or len(df) < 2:
        return None

    ordered = df.sort_values("Date").copy()
    latest_date = pd.to_datetime(ordered.iloc[-1]["Date"]).date()

    # Phase A: only a genuine trading day may produce a live snapshot. A
    # provider row labelled a holiday/weekend (e.g. a Saturday FX/futures row)
    # must not become a live observation either. Calendar-aware and fail-closed.
    if not adapter_for(symbol).is_trading_day(latest_date):
        return None

    if symbol_date_has_closed(symbol, latest_date) and not allow_closed_session:
        return None

    calculated = calculate_rows_for_symbol(
        df=ordered,
        symbol=symbol,
        name=name,
        asset_type=asset_type,
        allow_missing_prior=True,
    )
    if not calculated:
        return None

    latest = calculated[-1]
    if latest["date"] != latest_date:
        return None

    latest["market_date"] = latest.pop("date")
    latest["observed_at"] = observed_at or datetime.now(ZoneInfo("UTC"))
    latest["is_market_closed"] = False
    latest["source"] = source_label or "yfinance"
    return latest


def calculate_rows_for_symbol(df, symbol, name, asset_type,
                              *, defer_callback=None,
                              allow_missing_prior=False,
                              source_definition=None):
    """Build rows for ``symbol``.

    Phase A: ``prev_close`` is resolved from the immediately prior *valid
    session* for the instrument's own calendar -- never from a positional
    dataframe predecessor. A frame gap (e.g. D-1 absent) must not silently fall
    back to D-2. Such a bar is skipped here and reported as
    PREVIOUS_SESSION_NOT_CONFIRMED; the row is deferred upstream.

    ``allow_missing_prior`` is used only by the transient live-snapshot path:
    the observation is still emitted (so macro_live freshness is preserved) but
    with ``prev_close``/``change``/``pct_chg``/``amplitude`` set to NULL rather
    than a stale positional value. The historical path never sets this flag.
    """
    if df.empty or len(df) < 2:
        return []

    df = df.sort_values("Date").copy()
    df["_date"] = pd.to_datetime(df["Date"]).dt.date
    rows = []

    # Index closes by their actual session date so we can look up the exact
    # expected prior session rather than "whatever row came before".
    close_by_date = {
        rec_date: rec_close
        for rec_date, rec_close in zip(df["_date"], df["Close"])
    }

    for i in range(1, len(df)):
        latest = df.iloc[i]
        latest_date = latest["_date"]

        # Phase A.2A: prev_close is resolved against the *previous daily
        # observation date* for the candidate's approved source definition,
        # not the raw session date and not the symbol alone. For a Mon-Fri
        # labelled Globex source Monday's previous observation is Friday,
        # because no Sunday-labelled bar is published. An unknown or
        # unestablished definition fails closed (expected_prior is None ->
        # deferred).
        expected_prior = previous_valid_observation_date(
            symbol, latest_date, source_definition)

        prev_close = None
        if expected_prior is not None:
            prev_close = close_by_date.get(expected_prior)

        if prev_close is None and not allow_missing_prior:
            # D-1 missing (or calendar unavailable). Never use an older
            # positional value -- this is the confirmed incident.
            # Phase 6: Check trusted committed Local P before deferring.
            if expected_prior is not None:
                local_prev_close = _get_trusted_local_close(symbol, expected_prior)
                if local_prev_close is not None:
                    prev_close = local_prev_close
                    print(f"[PREVIOUS_SESSION_CONFIRMED_FROM_LOCAL] {symbol} {latest_date}: "
                          f"using trusted Local close for {expected_prior}")
            if prev_close is None:
                _report_prev_session_unconfirmed(symbol, latest_date, expected_prior,
                                                 defer_callback)
                continue

        open_price = latest.get("Open")
        high = latest.get("High")
        low = latest.get("Low")
        close = latest.get("Close")
        adj_close = latest.get("Adj Close")
        volume = latest.get("Volume")

        change = None
        pct_chg = None
        amplitude = None

        if pd.notna(close) and pd.notna(prev_close) and prev_close != 0:
            change = close - prev_close
            pct_chg = (change / prev_close) * 100

            if pd.notna(high) and pd.notna(low):
                amplitude = ((high - low) / prev_close) * 100

        # Single canonical normalization shared with the EOD 5-session refresh.
        _norm = canonical_normalize_daily_bar(latest)

        rows.append({
            "date": latest_date,
            "symbol": symbol,
            "name": name,
            "asset_type": asset_type,
            "source_definition": source_definition
            or SOURCE_DEFINITION_BY_SYMBOL.get(symbol),
            "open": _norm["open"],
            "high": _norm["high"],
            "low": _norm["low"],
            "close": _norm["close"],
            "adj_close": _norm["adj_close"],
            "volume": _norm["volume"],
            "prev_close": round_4(prev_close),
            "change": round_4(change),
            "pct_chg": round_4(pct_chg),
            "amplitude": round_4(amplitude),
        })

    return rows


def _get_trusted_local_close(symbol, expected_prior):
    """Query trusted committed Local close for symbol at expected_prior date.

    Returns the close value if a committed row exists in public.macro for the
    exact (symbol, expected_prior) key. Returns None if no such row exists or
    the value is null/invalid. Never synthesizes or estimates a value.
    """
    try:
        from db_builder.db import get_local_engine
        engine = get_local_engine()
        with engine.connect() as conn:
            row = conn.execute(
                "SELECT close FROM public.macro WHERE symbol = %s AND date = %s",
                (symbol, expected_prior)
            ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
    except Exception as exc:
        print(f"[TRUSTED_LOCAL_CLOSE_ERROR] {symbol} {expected_prior}: {exc}")
    return None


def _report_prev_session_unconfirmed(symbol, session_date, expected_prior,
                                     defer_callback=None):
    expected = expected_prior.isoformat() if expected_prior else "UNKNOWN"
    print(f"[PREVIOUS_SESSION_NOT_CONFIRMED] {symbol} {session_date}: "
          f"expected prior session {expected} not present in provider frame "
          f"-> deferred (positional fallback prohibited)")
    if defer_callback is not None:
        try:
            defer_callback(symbol, session_date, "PREVIOUS_SESSION_NOT_CONFIRMED")
        except Exception as exc:
            print(f"[DEFERRED WARN] callback failed for {symbol} "
                  f"{session_date}: {exc}")


def filter_rows_to_target_window(rows, target_start_date):
    return [
        row for row in rows
        if row["date"] >= target_start_date
    ]


def filter_duplicate_symbol_dates(rows, existing_df):
    if not rows:
        return []

    df_new = pd.DataFrame(rows)
    df_new["date"] = pd.to_datetime(df_new["date"]).dt.date
    df_new["symbol"] = df_new["symbol"].astype(str)

    if existing_df.empty:
        return df_new.to_dict(orient="records")

    existing_df = existing_df.copy()
    existing_df["date"] = pd.to_datetime(existing_df["date"]).dt.date
    existing_df["symbol"] = existing_df["symbol"].astype(str)

    df_new["_key"] = df_new["symbol"] + "|" + df_new["date"].astype(str)
    existing_df["_key"] = existing_df["symbol"] + "|" + existing_df["date"].astype(str)

    df_missing = df_new[~df_new["_key"].isin(existing_df["_key"])].copy()
    df_missing = df_missing.drop(columns=["_key"])

    return df_missing.to_dict(orient="records")


def upsert_new_macro_rows(rows, symbols, *, upsert_neon=True, dry_run=False,
                          provider_label="macro", deferred_store=None):
    if not rows:
        return 0

    rows_df = pd.DataFrame(rows)
    min_date = rows_df["date"].min()
    max_date = rows_df["date"].max()

    local_existing_df = fetch_existing_symbol_dates(
        local_engine,
        symbols=symbols,
        min_date=min_date,
        max_date=max_date,
    )

    print(
        f"[EXISTING LOCAL {provider_label}] rows={len(local_existing_df):,} "
        f"range={min_date} -> {max_date}"
    )

    local_missing_rows = filter_duplicate_symbol_dates(
        rows=rows,
        existing_df=local_existing_df,
    )

    if local_missing_rows:
        print(f"[{provider_label} LOCAL NEW ROWS] {len(local_missing_rows):,}")
        if dry_run:
            print(f"[DRY RUN SKIP LOCAL UPSERT {provider_label}] rows={len(local_missing_rows):,}")
        else:
            print(f"[UPSERT LOCAL {provider_label}] rows={len(local_missing_rows):,}")
            upsert_macro(local_engine, local_missing_rows,
                         deferred_store=deferred_store)
    else:
        print(f"[{provider_label} LOCAL NO NEW ROWS AFTER DEDUPE]")

    neon_missing_rows = []
    if upsert_neon:
        neon_existing_df = fetch_existing_symbol_dates(
            neon_engine,
            symbols=symbols,
            min_date=min_date,
            max_date=max_date,
        )
        print(
            f"[EXISTING NEON {provider_label}] rows={len(neon_existing_df):,} "
            f"range={min_date} -> {max_date}"
        )
        neon_missing_rows = filter_duplicate_symbol_dates(
            rows=rows,
            existing_df=neon_existing_df,
        )
        if neon_missing_rows:
            print(f"[{provider_label} NEON NEW ROWS] {len(neon_missing_rows):,}")
            if dry_run:
                print(f"[DRY RUN SKIP NEON UPSERT {provider_label}] rows={len(neon_missing_rows):,}")
            else:
                print(f"[UPSERT NEON {provider_label}] rows={len(neon_missing_rows):,}")
                upsert_macro(neon_engine, neon_missing_rows,
                             count_telemetry=False)
        else:
            print(f"[{provider_label} NEON NO NEW ROWS AFTER DEDUPE]")

    return max(len(local_missing_rows), len(neon_missing_rows))


# ============================================================
# INVESTINY
# ============================================================

def _investiny_date(value):
    return pd.to_datetime(value, format="%m/%d/%Y", errors="coerce")


# ============================================================
# INVESTING.COM PER-SYMBOL CIRCUIT BREAKER
# ============================================================
# Repeated HTTP 403 responses from Investing.com previously caused
# 20+ minutes of per-symbol exponential backoff across 12 symbols.
# After at most INVESTINY_CIRCUIT_BREAKER_TRIPS consecutive 403
# responses for a SPECIFIC symbol, only that symbol's breaker opens.
# Other Investing.com symbols continue fetching normally.
#
# BREAKER KEY = (provider, canonical_symbol)
# Example: ("investing.com", "BZ=F")
# Example: ("investing.com", "US2YT=X")
#
# This prevents one symbol's 403 from blocking unrelated symbols.
INVESTINY_CIRCUIT_BREAKER_TRIPS = 1

# Per-symbol circuit breaker state
# Key: (provider, symbol) -> {"open": bool, "reason": str, "tripped_at": datetime}
_symbol_breakers: dict[tuple[str, str], dict] = {}
_provider_requests_count = 0
_provider_failure_count = 0


def is_investiny_symbol_available(symbol: str) -> bool:
    """Check if Investing.com is available for a specific symbol.
    
    Returns True if the symbol's breaker is closed (available).
    Returns False if the symbol's breaker is open (unavailable).
    """
    key = ("investing.com", symbol)
    return not _symbol_breakers.get(key, {}).get("open", False)


def get_investiny_provider_state():
    """Return the current per-symbol breaker state dict for cycle metadata."""
    open_symbols = [
        {"symbol": key[1], "reason": state.get("reason"), "tripped_at": state.get("tripped_at").isoformat() if state.get("tripped_at") else None}
        for key, state in _symbol_breakers.items()
        if state.get("open")
    ]
    return {
        "provider_available": len(open_symbols) == 0,
        "breaker_tripped": len(open_symbols) > 0,
        "open_symbols": open_symbols,
        "requests_made": _provider_requests_count,
        "failures": _provider_failure_count,
    }


def record_investiny_symbol_failure(symbol: str, reason: str):
    """Record a per-symbol failure and open the symbol's breaker if threshold reached.
    
    This is called by the run_daily_update loop when a symbol encounters
    a provider error (403, 429, 5xx, connection failure).
    
    Only the specific symbol's breaker is opened. Other symbols continue normally.
    """
    global _provider_failure_count, _provider_requests_count
    _provider_failure_count += 1
    _provider_requests_count += 1
    
    # Check if this is a provider error (403/429/5xx/connection)
    is_provider_error = any(code in str(reason) for code in ["403", "429", "500", "502", "503", "504", "Connection", "SSL", "DNS", "timeout"])
    
    if is_provider_error:
        key = ("investing.com", symbol)
        _symbol_breakers[key] = {
            "open": True,
            "reason": str(reason),
            "tripped_at": datetime.now(ZoneInfo("UTC")),
        }
        print(f"[INVESTINY SYMBOL BREAKER OPEN] provider=investing.com symbol={symbol} reason={reason}")
        return True  # Breaker opened for this symbol
    return False  # Transient error, breaker not opened


def reset_investiny_provider_state():
    """Reset all per-symbol breaker states for a new cycle."""
    global _provider_requests_count, _provider_failure_count
    _symbol_breakers.clear()
    _provider_requests_count = 0
    _provider_failure_count = 0



class InvestinyProviderUnavailable(RuntimeError):
    """Raised internally when the Investing.com circuit breaker is open.

    Carries the last observed 403 error text so callers can record the
    provider-level failure reason in cycle metadata.
    """

    def __init__(self, message: str):
        super().__init__(message)


def _is_investiny_403(error: BaseException) -> bool:
    return "403" in str(error)


def _investiny_retry_delay(error, attempt):
    """Return a longer cooldown when Investing.com throttles with HTTP 403."""
    if _is_investiny_403(error):
        return min(60.0, 8.0 * (2 ** (attempt - 1))) + random.uniform(0.0, 3.0)
    return random.uniform(3.0, 8.0)


def fetch_investiny_symbol_df(symbol, *, start_date=None, end_date=None, max_retries=2):
    """Fetch one Investing.com symbol by scraping its historical-data HTML page.

    HTML-only by design: the Investing.com JSON API is blocked for this project
    (HTTP 403) and is explicitly out of scope. The scraper uses a browser TLS
    fingerprint that passes Investing.com's WAF.

    Returns the same frame shape as the rest of the pipeline:
        Date, Open, High, Low, Close, Adj Close, Volume

    Volume is never fabricated - the public HTML page does not expose a
    trustworthy per-day volume for these instruments, so it is left NULL.
    """
    source_symbol = INVESTING_SOURCE_SYMBOLS.get(symbol, symbol)
    target_start_date = start_date or (datetime.now().date() - timedelta(days=LOOKBACK_DAYS))
    fetch_start_date = target_start_date - timedelta(days=CALC_BUFFER_DAYS)
    fetch_end_date = end_date or (datetime.now().date() + timedelta(days=1))

    print(
        f"[INVESTING.COM FETCH WINDOW] {symbol} source_symbol={source_symbol} "
        f"mode=html-scrape "
        f"target_start={target_start_date} | end={fetch_end_date}"
    )

    last_error: BaseException | None = None
    for attempt in range(1, max_retries + 1):
        try:
            time.sleep(random.uniform(0.8, 2.0))
            rows = _investing_html.fetch_historical_data(symbol)
            if not rows:
                # Empty page: retry once through the loop, then surface it.
                raise RuntimeError(
                    f"Investing.com HTML returned no rows for {symbol}"
                )

            df = pd.DataFrame(rows)
            df = df.rename(
                columns={
                    "date": "Date",
                    "price": "Close",
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                }
            )
            # The HTML scraper emits ISO date strings; the pipeline needs
            # datetimes for the weekday filter and downstream date math.
            df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
            df["Adj Close"] = df["Close"]
            # Volume is deliberately NULL: never fabricated.
            df["Volume"] = None
            df = df.dropna(subset=["Date", "Close"]).sort_values("Date")
            df = df[df["Date"].dt.weekday < 5].copy()
            return df[["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"]], target_start_date
        except Exception as e:
            last_error = e
            print(f"[INVESTINY RETRY {attempt}/{max_retries}] {symbol}: {e}")
            if attempt == max_retries:
                # Surface as a provider-level failure so the caller can open this
                # symbol's circuit breaker and route to its fallback provider.
                raise InvestinyProviderUnavailable(
                    f"Investing.com unavailable for {symbol} after "
                    f"{max_retries} attempts: {e}"
                ) from e
            delay = _investiny_retry_delay(e, attempt)
            print(f"[INVESTINY COOLDOWN] {symbol}: {delay:.1f}s")
            time.sleep(delay)
    if last_error:
        raise InvestinyProviderUnavailable(
            f"Investing.com unavailable for {symbol}: {last_error}"
        ) from last_error
    return pd.DataFrame(), target_start_date


def process_investiny_symbol(symbol, meta, *, start_date=None, observed_at=None):
    symbol_df, target_start_date = fetch_investiny_symbol_df(symbol, start_date=start_date)
    rows_fetched = len(symbol_df)
    latest_observation = None
    try:
        rows_fetched, latest_observation = observation_metadata(symbol_df)

        print(
            f"[INVESTINY DATES RAW] {symbol} | "
            f"{pd.to_datetime(symbol_df['Date']).dt.date.tolist()}"
        )

        # Phase A dual-source: keep serving a live observation for a closed
        # session whose official EOD value is not yet published. The row remains
        # is_market_closed=False and is never labelled official EOD.
        live_row = extract_latest_unfinished_row(
            df=symbol_df,
            symbol=symbol,
            name=meta["name"],
            asset_type=meta["asset_type"],
            observed_at=observed_at,
            allow_closed_session=True,
        )
        if live_row:
            live_row["source"] = "investing.com"

        symbol_df = remove_unfinished_bars(symbol_df, symbol)
        if symbol_df.empty:
            print(f"[INVESTINY NO FINISHED BARS] {symbol}")
            return [], live_row, rows_fetched, latest_observation

        rows = calculate_rows_for_symbol(
            df=symbol_df,
            symbol=symbol,
            name=meta["name"],
            asset_type=meta["asset_type"],
            source_definition=SOURCE_DEF_INVESTING_CONTINUOUS_FUTURES_DAILY,
        )
        rows = filter_rows_to_target_window(rows=rows, target_start_date=target_start_date)
        if rows:
            print(f"[INVESTINY ROW DATES TARGET] {symbol} | {[row['date'] for row in rows]}")
        else:
            print(f"[INVESTINY NO VALID ROWS IN TARGET WINDOW] {symbol}")
        return rows, live_row, rows_fetched, latest_observation
    except Exception as exc:
        raise SymbolProcessingError(
            f"{type(exc).__name__}: {exc}",
            rows_fetched=rows_fetched,
            latest_observation=latest_observation,
        ) from exc


# ============================================================
# YFINANCE
# ============================================================

def download_recent_batch(symbols, max_retries=5, start_date=None):
    end_date = datetime.now().date() + timedelta(days=1)

    target_start_date = start_date or (datetime.now().date() - timedelta(days=LOOKBACK_DAYS))
    fetch_start_date = target_start_date - timedelta(days=CALC_BUFFER_DAYS)

    print(
        f"[YF FETCH WINDOW] fetch_start={fetch_start_date} | "
        f"target_start={target_start_date} | end={end_date}"
    )

    for attempt in range(1, max_retries + 1):
        try:
            time.sleep(random.uniform(1.0, 3.0))

            df = yf.download(
                tickers=" ".join(symbols),
                start=fetch_start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=False,
                repair=False,
                timeout=30,
            )

            if df.empty:
                print("[EMPTY BATCH]")
                return pd.DataFrame(), target_start_date

            return df, target_start_date

        except Exception as e:
            print(f"[BATCH RETRY {attempt}/{max_retries}] {e}")

            if attempt == max_retries:
                raise

            time.sleep(random.uniform(5.0, 15.0))


def extract_symbol_df_from_batch(batch_df, symbol):
    if batch_df.empty:
        return pd.DataFrame()

    if isinstance(batch_df.columns, pd.MultiIndex):
        symbols_in_df = batch_df.columns.get_level_values(0)

        if symbol not in symbols_in_df:
            print(f"[MISSING IN BATCH] {symbol}")
            return pd.DataFrame()

        df = batch_df[symbol].copy()
    else:
        df = batch_df.copy()

    if df.empty:
        return pd.DataFrame()

    df = df.reset_index()

    close_col = None
    for col in df.columns:
        if str(col).lower() == "close":
            close_col = col
            break

    if close_col is None:
        print(f"[NO CLOSE COLUMN] {symbol}")
        return pd.DataFrame()

    df = df.dropna(subset=[close_col], how="all")

    return df


def validate_fallback_observations(symbol_df):
    """Return fallback rows whose dates and closing prices are finite and usable."""
    if symbol_df is None or symbol_df.empty:
        raise ValueError("empty provider response")
    if "Date" not in symbol_df.columns or "Close" not in symbol_df.columns:
        raise ValueError("malformed provider response: Date/Close columns required")

    dates = pd.to_datetime(symbol_df["Date"], errors="coerce")
    closes = pd.to_numeric(symbol_df["Close"], errors="coerce")
    finite_closes = closes.map(
        lambda value: pd.notna(value) and math.isfinite(float(value))
    )
    valid_mask = dates.notna() & finite_closes
    valid_rows = symbol_df.loc[valid_mask].copy()
    if valid_rows.empty:
        raise ValueError(
            "malformed provider response: no finite Date/Close observations"
        )
    if len(valid_rows) < 2:
        raise ValueError(
            "insufficient provider observations: at least 2 finite observations required"
        )

    valid_rows["Date"] = dates.loc[valid_mask]
    valid_rows["Close"] = closes.loc[valid_mask]
    return valid_rows, len(valid_rows), valid_rows["Date"].max().date()


def canonical_normalize_daily_bar(bar, *, source_definition=None):
    """THE canonical daily-bar normalization for public.macro.

    Both normal ingestion and the EOD 5-session refresh MUST call this so the
    same provider frame always yields byte-identical field values.

    Semantics (must reproduce existing intended production behavior):
      - open/high/low/close/adj_close: ``round_4`` (Python round(float(x), 4))
      - volume: ``round_4`` (public.macro.volume is numeric)
    """
    def _g(name):
        try:
            v = bar.get(name)
        except Exception:
            v = None
        return v

    def _num(v):
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except Exception:
            pass
        return round_4(v)

    return {
        "open": _num(_g("Open")),
        "high": _num(_g("High")),
        "low": _num(_g("Low")),
        "close": _num(_g("Close")),
        "adj_close": _num(_g("Adj Close")),
        "volume": _num(_g("Volume")),
    }


def fetch_yfinance_daily_frame(symbols, *, start_date=None):
    """THE canonical Yahoo daily-bar provider route for public.macro.

    Both normal ingestion and the EOD 5-session refresh MUST fetch through this
    function so a provider bar can never be seen differently by the two paths.

    Delegates to ``download_recent_batch`` (the production route) and returns
    per-symbol finished-bar frames keyed by symbol.
    """
    batch_df, target_start_date = download_recent_batch(list(symbols), start_date=start_date)
    frames = {}
    for sym in symbols:
        try:
            f = extract_symbol_df_from_batch(batch_df, sym)
            if f is None or f.empty:
                continue
            f = remove_unfinished_bars(f, sym)
            if f is None or f.empty:
                continue
            frames[sym] = f
        except Exception:
            continue
    return frames, target_start_date


def process_asia_index_yfinance_fallback_symbol(symbol, meta, *, start_date=None, observed_at=None):
    """Fetch one Asia futures symbol via its yfinance cash-index proxy.

    HK50 -> ^HSI (Hang Seng Index), KOR200c1 -> ^KS11 (KOSPI),
    CIHc1 -> 000016.SS (SSE 50). The proxy is a CASH INDEX, not a futures
    contract: level/direction/trend are representative but basis/spread
    versus the futures are not comparable. Rows are written under the
    canonical DB symbol with source='yfinance (cash index proxy)'.
    """
    if symbol not in ASIA_INDEX_YFINANCE_FALLBACK_MAP:
        raise ValueError(f"Asia index yfinance fallback is not enabled for {symbol}")

    proxy = ASIA_INDEX_YFINANCE_FALLBACK_MAP[symbol]
    proxy_symbol = proxy["symbol"]
    proxy_name = proxy["name"]

    rows_fetched = 0
    latest_observation = None
    try:
        batch_df, target_start_date = download_recent_batch(
            [proxy_symbol],
            start_date=start_date,
        )
        symbol_df = extract_symbol_df_from_batch(batch_df, proxy_symbol)
        rows_fetched = len(symbol_df)
        symbol_df, rows_fetched, latest_observation = validate_fallback_observations(
            symbol_df
        )

        # Asia sessions close before the US session, so require at least the
        # prior completed NYSE session rather than the latest one.
        required_latest = latest_completed_nyse_session_date(now=observed_at)
        if latest_observation < required_latest:
            # Allow one extra calendar day for regional holidays.
            from datetime import timedelta as _td

            if latest_observation < required_latest - _td(days=1):
                raise ValueError(
                    "stale Asia proxy response: "
                    f"latest observation {latest_observation} is more than one "
                    f"day before required {required_latest}"
                )
            print(
                f"[ASIA PROXY FRESHNESS TOLERANCE] {symbol} | "
                f"latest={latest_observation} required={required_latest} "
                f"(regional holiday tolerance applied)"
            )

        print(
            f"[ASIA PROXY FALLBACK] {symbol} <- {proxy_symbol} ({proxy_name}) | "
            f"latest={latest_observation}"
        )

        live_row = extract_latest_unfinished_row(
            df=symbol_df,
            symbol=symbol,
            name=meta["name"],
            asset_type=meta["asset_type"],
            observed_at=observed_at,
            source_label="yfinance (cash index proxy)",
        )

        finished_df = remove_unfinished_bars(symbol_df, proxy_symbol)
        if finished_df.empty:
            print(f"[ASIA PROXY NO FINISHED BARS] {symbol}")
            return [], live_row, rows_fetched, latest_observation

        # The candidate's HISTORICAL source definition is the symbol's approved
        # definition, not this fallback provider. Passing the symbol lets the
        # registry supply it, so an unresolved definition still fails closed
        # instead of inheriting the proxy provider's convention.
        rows = calculate_rows_for_symbol(
            df=finished_df,
            symbol=symbol,
            name=meta["name"],
            asset_type=meta["asset_type"],
        )
        rows = filter_rows_to_target_window(
            rows=rows,
            target_start_date=target_start_date,
        )
        if rows:
            print(f"[ASIA PROXY ROW DATES TARGET] {symbol} | {[row['date'] for row in rows]}")
        else:
            print(f"[ASIA PROXY NO VALID ROWS IN TARGET WINDOW] {symbol}")
        return rows, live_row, rows_fetched, latest_observation
    except Exception as exc:
        raise SymbolProcessingError(
            f"{type(exc).__name__}: {exc}",
            rows_fetched=rows_fetched,
            latest_observation=latest_observation,
        ) from exc


        # ---------------------------------------------------------------------------
# P1B: U.S. Treasury official par-yield fallback
# ---------------------------------------------------------------------------


def fetch_treasury_par_yields(year: int) -> pd.DataFrame:
    """Fetch (with per-run caching) the Treasury daily par yield curve XML.

    The year-scoped document is fetched ONCE per run and shared across all
    seven UST fallback symbols. Returns a DataFrame indexed by date with one
    column per BC_*YEAR element.
    """
    if year in _treasury_xml_cache:
        return _treasury_xml_cache[year]

    import requests
    import xml.etree.ElementTree as ET

    url = TREASURY_XML_URL_TEMPLATE.format(year=year)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Connection": "close",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()

    # Treasury XML uses Atom-style entries with d: namespace prefixes.
    ns = {"d": "http://schemas.microsoft.com/ado/2007/08/dataservices"}
    entries = ET.fromstring(resp.content).findall(".//d:entry", ns) or \
        ET.fromstring(resp.content).findall(".//{http://www.w3.org/2005/Atom}entry")

    records = []
    for entry in entries:
        props = entry.find(".//d:properties", ns) or entry.find(
            ".//{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}properties"
        )
        if props is None:
            continue
        row = {}
        for child in props:
            tag = child.tag.split("}")[-1]  # strip namespace
            row[tag] = child.text
        if "NEW_DATE" in row:
            records.append(row)

    if not records:
        raise ValueError("empty treasury.gov response: no yield-curve entries")

    df = pd.DataFrame(records)
    df["NEW_DATE"] = pd.to_datetime(df["NEW_DATE"], errors="coerce", utc=True)
    df = df.dropna(subset=["NEW_DATE"]).sort_values("NEW_DATE")
    df = df.drop_duplicates(subset=["NEW_DATE"], keep="last").set_index("NEW_DATE")

    _treasury_xml_cache[year] = df
    return df


def validate_treasury_observations(symbol, yields_df):
    """Validate official par-yield observations for one canonical UST symbol.

    Returns (symbol_df, rows_fetched, latest_observation) where symbol_df has
    Date/Close columns consumable by the row-construction helpers.
    """
    element = TREASURY_XML_ELEMENT_MAP[symbol]
    if element not in yields_df.columns:
        raise ValueError(
            f"malformed official data: element {element} missing from treasury.gov response"
        )

    series = pd.to_numeric(yields_df[element], errors="coerce")
    dates = yields_df.index

    valid_mask = series.notna() & series.map(
        lambda v: math.isfinite(float(v))
        and TREASURY_YIELD_MIN <= float(v) <= TREASURY_YIELD_MAX
    )
    if not valid_mask.any():
        raise ValueError(
            "malformed official data: no plausible finite yields "
            f"(expected {TREASURY_YIELD_MIN}-{TREASURY_YIELD_MAX} percent)"
        )
    if int(valid_mask.sum()) < 2:
        raise ValueError(
            "insufficient official observations: at least 2 valid yields required"
        )

    symbol_df = pd.DataFrame(
        {
            "Date": dates[valid_mask],
            "Open": series[valid_mask],
            "High": series[valid_mask],
            "Low": series[valid_mask],
            "Close": series[valid_mask],
            "Adj Close": series[valid_mask],
            "Volume": None,
        }
    ).reset_index(drop=True)

    # Phase A: record the official publication dates actually observed so the
    # EOD gate can require confirmation for the *specific* session.
    try:
        _TREASURY_PUBLISHED_DATES.update(
            pd.to_datetime(symbol_df["Date"]).dt.date.tolist())
        # Phase A: record the official VALUE per session so the EOD gate can
        # reject a row whose value did not come from the official series.
        for _d, _v in zip(pd.to_datetime(symbol_df["Date"]).dt.date,
                          symbol_df["Close"]):
            _TREASURY_PUBLISHED_VALUES[(symbol, _d)] = float(_v)
    except Exception as exc:
        print(f"[UST GATE WARN] could not record publication dates: {exc}")

    return symbol_df, len(symbol_df), symbol_df["Date"].max().date()


def classify_treasury_freshness(latest_observation, observed_at):
    """Three-state publication-aware freshness classification.

    Returns (status, expected_date) where status is one of:
      - 'CURRENT'           latest >= expected publication
      - 'PUBLICATION_DELAY' one publication day behind expected (WARN)
      - 'STALE'             beyond one publication day behind (reject)
    """
    from db_builder.trading_calendar import (
        expected_latest_treasury_publication,
        previous_treasury_trading_day,
    )

    expected = expected_latest_treasury_publication(now=observed_at)
    if latest_observation >= expected:
        return "CURRENT", expected
    if latest_observation >= previous_treasury_trading_day(expected):
        return "PUBLICATION_DELAY", expected
    return "STALE", expected


def process_treasury_fallback_symbol(symbol, meta, *, start_date=None, observed_at=None):
    """Fetch and transform one UST symbol via the official Treasury par-yield curve.

    Fallback rows are SYNTHETIC single-point daily observations, not true OHLC
    bars: open = high = low = close = official par yield, volume = NULL.
    """
    if symbol not in UST_TREASURY_FALLBACK_SYMBOLS:
        raise ValueError(f"treasury.gov fallback is not enabled for {symbol}")

    rows_fetched = 0
    latest_observation = None
    try:
        from db_builder.trading_calendar import previous_treasury_trading_day

        now = observed_at or datetime.now(ZoneInfo("UTC"))
        # Fetch the current year and, when January may need prior-year context,
        # also the prior year (both cached; at most two documents per run).
        years = {now.year}
        if start_date is not None:
            years.add(start_date.year)
        else:
            years.add(now.year - 1)

        combined = pd.concat(
            [fetch_treasury_par_yields(y) for y in sorted(years)]
        ).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]

        symbol_df, rows_fetched, latest_observation = validate_treasury_observations(
            symbol, combined
        )

        status, expected = classify_treasury_freshness(latest_observation, now)
        if status == "STALE":
            raise ValueError(
                f"stale official publication: latest observation {latest_observation} "
                f"is before tolerated {previous_treasury_trading_day(expected)}"
            )
        if status == "PUBLICATION_DELAY":
            print(
                f"[UST FALLBACK PUBLICATION DELAY] {symbol} | "
                f"latest={latest_observation} expected={expected} "
                f"(official source one publication day behind — accepted with warning)"
            )
        else:
            print(
                f"[UST FALLBACK FRESHNESS OK] {symbol} | "
                f"latest={latest_observation} expected={expected}"
            )

        # Synthetic single-point rows: Date/Close only; OHLC all equal.
        target_start = start_date or (now.date() - timedelta(days=LOOKBACK_DAYS + CALC_BUFFER_DAYS))

        # H2 fix: this path previously called calculate_rows_for_symbol WITHOUT
        # remove_unfinished_bars, bypassing the close gate. The published
        # series is official, but the session must still be a completed,
        # calendar-valid session and the prior session must be resolvable.
        symbol_df = remove_unfinished_bars(symbol_df, symbol)

        # The candidate's HISTORICAL source definition is the symbol's approved
        # definition, not this fallback provider (for the legacy UST symbols
        # they coincide; passing the symbol keeps the rule uniform).
        rows = calculate_rows_for_symbol(
            df=symbol_df,
            symbol=symbol,
            name=meta["name"],
            asset_type=meta["asset_type"],
        )
        rows = filter_rows_to_target_window(rows=rows, target_start_date=target_start)
        if rows:
            print(
                f"[UST FALLBACK ROW DATES TARGET] {symbol} | "
                f"{[row['date'] for row in rows]}"
            )
        else:
            print(f"[UST FALLBACK NO VALID ROWS IN TARGET WINDOW] {symbol}")

        # Live snapshot. ``is_market_closed`` describes MARKET-CLOSURE state
        # only -- it must not be used to signal "official value published".
        # Downstream consumers filter on FALSE to serve intraday quotes, so an
        # official value that is itself the latest observation stays FALSE.
        # Protection against unofficial values entering historical storage is
        # provided independently by the EOD gate (official value matching).
        live_row = None
        if rows:
            latest = dict(rows[-1])
            latest["market_date"] = latest.pop("date")
            latest["observed_at"] = observed_at or datetime.now(ZoneInfo("UTC"))
            latest["is_market_closed"] = False
            latest["source"] = "treasury.gov"
            live_row = latest

        return rows, live_row, rows_fetched, latest_observation
    except Exception as exc:
        raise SymbolProcessingError(
            f"{type(exc).__name__}: {exc}",
            rows_fetched=rows_fetched,
            latest_observation=latest_observation,
        ) from exc


def provider_for_symbol(symbol):
    if symbol in CMT_SYMBOLS:
        return "treasury.gov"
    if symbol in OILPRICEAPI_ASSETS:
        return "oilpriceapi.com"
    return "investing.com" if symbol in INVESTINY_ASSETS else "yfinance"


def fetch_oilpriceapi_history(commodity_code, pages=8):
    """Fetch daily spot prices for one OilPriceAPI commodity.

    Only /prices/past_year is deep enough to be useful; the API caps history at
    ~365 days, so the earliest available observation bounds what can be stored.
    Returns a list of (as_of_date, price) sorted ascending.
    """
    api_key = os.getenv("OIL_PRICE_API_KEY", "")
    if not api_key:
        raise ValueError("OIL_PRICE_API_KEY is not set")

    out = []
    for page in range(1, pages + 1):
        url = (
            f"{OILPRICEAPI_BASE_URL}/prices/past_year"
            f"?by_code={commodity_code}&per_page=100&page={page}"
        )
        request = urllib.request.Request(
            url, headers={"Authorization": f"Token {api_key}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise ValueError(
                f"OilPriceAPI HTTP {exc.code} for {commodity_code} page {page}"
            ) from exc
        prices = (payload.get("data") or {}).get("prices") or []
        if not prices:
            break
        for item in prices:
            as_of = item.get("as_of") or item.get("observed_at")
            price = item.get("price")
            if as_of is None or price is None:
                continue
            out.append((as_of[:10], float(price)))
        if len(prices) < 100:
            break
        time.sleep(OILPRICEAPI_RATE_LIMIT_SECONDS)
    out.sort()
    return out


def process_oilpriceapi_symbol(symbol, meta, *, start_date=None, observed_at=None):
    """Fetch and transform one OilPriceAPI spot-price symbol.

    Rows are SYNTHETIC single-point observations, not true OHLC bars:
    open = high = low = close = spot price, volume = NULL.
    """
    if symbol not in OILPRICEAPI_ASSETS:
        raise ValueError(f"oilpriceapi provider is not enabled for {symbol}")

    commodity_code = OILPRICEAPI_MAPPING[symbol]
    rows_fetched = 0
    latest_observation = None
    try:
        observations = fetch_oilpriceapi_history(commodity_code)
        if not observations:
            raise ValueError(f"empty OilPriceAPI response for {commodity_code}")
        rows_fetched = len(observations)
        latest_observation = date.fromisoformat(observations[-1][0])

        symbol_df = pd.DataFrame(
            {
                "Date": pd.to_datetime([obs[0] for obs in observations]),
                "Open": [obs[1] for obs in observations],
                "High": [obs[1] for obs in observations],
                "Low": [obs[1] for obs in observations],
                "Close": [obs[1] for obs in observations],
                "Adj Close": [obs[1] for obs in observations],
                "Volume": [None] * len(observations),
            }
        )
        symbol_df = remove_unfinished_bars(symbol_df, symbol)

        target_start = start_date or (
            (observed_at or datetime.now(ZoneInfo("UTC"))).date()
            - timedelta(days=LOOKBACK_DAYS + CALC_BUFFER_DAYS)
        )
        rows = calculate_rows_for_symbol(
            df=symbol_df,
            symbol=symbol,
            name=meta["name"],
            asset_type=meta["asset_type"],
        )
        rows = filter_rows_to_target_window(rows=rows, target_start_date=target_start)
        if rows:
            print(
                f"[OILPRICEAPI ROW DATES TARGET] {symbol} | "
                f"{rows[0]['date']} .. {rows[-1]['date']} ({len(rows)} rows)"
            )
        else:
            print(f"[OILPRICEAPI NO VALID ROWS IN TARGET WINDOW] {symbol}")

        live_row = None
        if rows:
            latest = dict(rows[-1])
            latest["market_date"] = latest.pop("date")
            latest["observed_at"] = observed_at or datetime.now(ZoneInfo("UTC"))
            latest["is_market_closed"] = False
            latest["source"] = "oilpriceapi.com"
            live_row = latest

        return rows, live_row, rows_fetched, latest_observation
    except Exception as exc:
        raise SymbolProcessingError(
            f"{type(exc).__name__}: {exc}",
            rows_fetched=rows_fetched,
            latest_observation=latest_observation,
        ) from exc
    return "investing.com" if symbol in INVESTINY_ASSETS else "yfinance"


def observation_metadata(symbol_df):
    """Return usable row count and latest daily observation date."""
    if symbol_df is None or symbol_df.empty:
        raise ValueError("empty provider response")
    if "Date" not in symbol_df.columns or "Close" not in symbol_df.columns:
        raise ValueError("malformed provider response: Date/Close columns required")

    dates = pd.to_datetime(symbol_df["Date"], errors="coerce")
    valid_rows = symbol_df.loc[dates.notna() & symbol_df["Close"].notna()]
    if valid_rows.empty:
        raise ValueError("malformed provider response: no valid Date/Close observations")
    if len(valid_rows) < 2:
        raise ValueError("insufficient provider observations: at least 2 required")
    return len(valid_rows), pd.to_datetime(valid_rows["Date"]).max().date()


def summarize_outcomes(outcomes, minimum_coverage_pct=MIN_COVERAGE_PCT):
    ordered = tuple(sorted(outcomes, key=lambda item: item.symbol))
    configured_count = len(ordered)
    successful_count = sum(item.success for item in ordered)
    failed = tuple(item for item in ordered if not item.success)
    raw_coverage_pct = (
        (successful_count / configured_count) * 100.0 if configured_count else 0.0
    )
    coverage_pct = round(raw_coverage_pct, 2)
    required_failed = tuple(item.symbol for item in failed if item.required)
    providers = {
        provider
        for item in ordered
        for provider in (item.provider, item.primary_provider, item.fallback_provider)
        if provider
    }
    provider_failure_counts = []
    for provider in sorted(providers):
        failure_count = 0
        for item in ordered:
            if item.primary_provider:
                # Count provider failures even when success=True (e.g., CURRENT_BUT_PROVIDER_UNAVAILABLE)
                # because the provider did fail; the symbol was covered by existing Local data.
                failure_count += int(
                    item.primary_provider == provider
                    and item.primary_failure_reason is not None
                )
                failure_count += int(
                    item.fallback_provider == provider
                    and item.fallback_failure_reason is not None
                )
            elif item.provider == provider and not item.success:
                failure_count += 1
        provider_failure_counts.append((provider, failure_count))
    coverage_satisfied = (
        configured_count > 0
        and not required_failed
        and raw_coverage_pct >= minimum_coverage_pct
    )
    # Classify historical deferrals into mutually exclusive buckets:
    # - EXPECTED_FROZEN: intentional long-term freeze (legacy Treasury)
    # - REQUIRED_HISTORICAL_DEGRADED: required symbols with historical deferrals
    # - SUPPLEMENTARY_HISTORICAL_DEGRADED: supplementary symbols with historical deferrals
    # A symbol is counted in exactly one bucket per condition.
    expected_frozen = set()
    required_historical_deferred = set()
    supplementary_historical_deferred = set()
    for rec in _PHASE_A_ACTIONABLE_DEFERRALS:
        sym = rec.get("symbol")
        reason = rec.get("reason")
        if reason == "LEGACY_EOD_DEFINITION_UNRESOLVED":
            expected_frozen.add(sym)
        elif sym in REQUIRED_SYMBOLS:
            required_historical_deferred.add(sym)
        else:
            supplementary_historical_deferred.add(sym)
    return MacroCoverageSummary(
        outcomes=ordered,
        configured_count=configured_count,
        successful_count=successful_count,
        failed_count=len(failed),
        coverage_pct=coverage_pct,
        required_failed_symbols=required_failed,
        provider_failure_counts=tuple(provider_failure_counts),
        coverage_satisfied=coverage_satisfied,
        required_historical_deferred_symbols=tuple(sorted(required_historical_deferred)),
        supplementary_historical_deferred_symbols=tuple(sorted(supplementary_historical_deferred)),
        expected_frozen_symbols=tuple(sorted(expected_frozen)),
    )


def log_coverage_summary(summary):
    for outcome in summary.outcomes:
        status = "OK" if outcome.success else "FAILED"
        latest = outcome.latest_observation.isoformat() if outcome.latest_observation else "none"
        reason = outcome.failure_reason or "none"
        print(
            f"[MACRO SYMBOL {status}] symbol={outcome.symbol} name={outcome.name} "
            f"provider={outcome.provider} "
            f"required={'yes' if outcome.required else 'no'} rows_fetched={outcome.rows_fetched} "
            f"latest_observation={latest} reason={reason} "
            f"primary_provider={outcome.primary_provider or 'none'} "
            f"primary_failure={outcome.primary_failure_reason or 'none'} "
            f"fallback_provider={outcome.fallback_provider or 'none'} "
            f"fallback_failure={outcome.fallback_failure_reason or 'none'}"
        )

    failed_symbols = [
        f"{item.symbol}({item.name})" for item in summary.outcomes if not item.success
    ]
    print(
        f"[MACRO COVERAGE] configured={summary.configured_count} "
        f"successful={summary.successful_count} failed={summary.failed_count} "
        f"coverage={summary.coverage_pct:.2f}% threshold={MIN_COVERAGE_PCT:.2f}%"
    )
    print(
        "[MACRO FAILED SYMBOLS] "
        + (",".join(failed_symbols) if failed_symbols else "none")
    )
    print(
        "[MACRO REQUIRED FAILURES] "
        + (
            ",".join(summary.required_failed_symbols)
            if summary.required_failed_symbols
            else "none"
        )
    )
    for provider, failure_count in summary.provider_failure_counts:
        print(f"[MACRO PROVIDER FAILURES] provider={provider} failed={failure_count}")


# ============================================================
# DAILY UPDATE
# ============================================================

def run_etf_flow_update_after_macro(*, start_date=None, tickers=None, dry_run=False):
    """Refresh local ETF daily flow data after the macro market refresh.

    ETF daily flow data is maintained locally because the Neon sync contract
    currently covers macro/live market data separately. The derived positioning
    signals are refreshed from the latest local ETF rows.
    """
    from db_builder.etf_flow.run import run_etf_flow_analytics
    from db_builder.etf_flows import ETF_FLOW_UNIVERSE, plan_etf_flow_missing_fetch, run_etf_flow_fetch
    from db_builder.positioning_flow_signals import run_positioning_flow_signal_update, setup_flow_tables

    print("\n[ETF FLOW UPDATE]")
    selected_tickers = [ticker.strip().upper() for ticker in tickers if ticker.strip()] if tickers else None
    print(f"[ETF FLOW UNIVERSE] tickers={len(selected_tickers) if selected_tickers else len(ETF_FLOW_UNIVERSE)}")
    if selected_tickers:
        print(f"[ETF FLOW TICKERS] {','.join(selected_tickers)}")
    if start_date:
        print(f"[ETF FLOW START DATE] {start_date}")
    if dry_run:
        print("[ETF FLOW DRY RUN] No rows will be upserted.")

    if not dry_run:
        setup_flow_tables(local_engine)

    plan = plan_etf_flow_missing_fetch(
        local_engine,
        tickers=selected_tickers,
        start_date=start_date,
    )
    print(
        f"[ETF FLOW TARGET] session={plan['target_date']} "
        f"missing_tickers={len(plan['tickers'])} skipped_current={len(plan['skipped_tickers'])}"
    )
    if plan.get("unsupported_tickers"):
        print(f"[ETF FLOW UNSUPPORTED ISSUER HISTORY] {','.join(plan['unsupported_tickers'])}")
    if plan["latest_dates"]:
        latest_preview = sorted(plan["latest_dates"].items())[:8]
        print(f"[ETF FLOW LATEST PREVIEW] {latest_preview}")
    if not plan["tickers"]:
        print("[ETF FLOW SKIP] Issuer-backed ETF flow data already covers the target session.")
        return {
            "rows": 0,
            "upserted": 0,
            "recomputed": 0,
            "sample": [],
            "plan": plan,
            "skipped": True,
        }

    result = run_etf_flow_fetch(
        local_engine,
        tickers=plan["tickers"],
        dry_run=dry_run,
        start_date=plan["start_date"],
        end_date=plan["target_date"],
        allow_yfinance_fallback=False,
    )
    result["plan"] = plan
    print(
        f"[ETF FLOW RESULT] snapshots={result['rows']:,} "
        f"upserted={result['upserted']:,} recomputed={result['recomputed']:,}"
    )
    if not dry_run:
        analytics_result = run_etf_flow_analytics(
            local_engine,
            as_of_date=plan["target_date"],
            # Analytics needs prior rows for lag/rolling fields. The issuer
            # fetch above remains incremental, but the analytics refresh must
            # include full available history so Monday values can use Friday
            # as the previous session and rolling windows are not truncated.
            start_date=None,
            dry_run=False,
            write_report_output=True,
        )
        print(
            f"[ETF FLOW ANALYTICS] as_of={analytics_result['as_of_date']} "
            f"raw={analytics_result['raw_rows']:,} features={analytics_result['feature_rows']:,} "
            f"signals={analytics_result['representative_signal_rows']:,}"
        )
        signal_result = run_positioning_flow_signal_update(local_engine, dry_run=False)
        print(f"[ETF FLOW SIGNALS] positioning_flow_signals upserted={signal_result['upserted']:,}")
    return result


def _check_local_current_for_provider_outage(symbol: str, meta: dict) -> bool:
    """Check if Local data is still current for a symbol whose provider fetch failed.
    
    Returns True if:
    - Local has previously validated canonical data for this symbol
    - No newer finalized session is expected per market calendar
    - The latest Local finalized date matches the expected latest finalized date
    
    Returns False if:
    - No Local data exists
    - A newer finalized session should exist but Local is behind
    """
    try:
        from db_builder.config import local_engine
        from sqlalchemy import text
        
        with local_engine().connect() as conn:
            result = conn.execute(
                text("""
                    SELECT MAX(session_date) as latest_date
                    FROM public.macro
                    WHERE symbol = :symbol
                """),
                {"symbol": symbol}
            )
            row = result.fetchone()
            if row is None or row[0] is None:
                return False
            
            local_latest = row[0]
            
            # Check if we expect a newer finalized session
            # For most symbols, if Local has data within the last 1-2 business days,
            # it's considered current (market may not have produced a new session yet)
            from datetime import date, timedelta
            today = date.today()
            
            # Allow 2 business days of grace period
            # If local_latest is within 2 business days of today, consider it current
            business_days_back = 0
            check_date = today
            while business_days_back < 2:
                check_date -= timedelta(days=1)
                if check_date.weekday() < 5:  # Monday=0, Friday=4
                    business_days_back += 1
            
            return local_latest >= check_date
            
    except Exception as e:
        print(f"[LOCAL CURRENT CHECK ERROR] {symbol}: {e}")
        return False


def _process_investiny_fallback_for_symbol(
    *,
    symbol,
    meta,
    primary_failure_reason,
    start_date=None,
    observed_at=None,
    outcomes,
    all_live_rows,
    upsert_neon=True,
    dry_run=False,
    deferred_store=None,
):
    """Route one Investing.com-routed symbol to its fallback provider.

    Fallback order:
      - US*YT_CMT              -> Treasury.gov par-yield XML
      - HK50/KOR200c1/CIHc1    -> yfinance cash-index proxy (Asia fallback)
      - anything else          -> recorded as failed (DEGRADED) — never silently
                                   dropped; the outcome carries the provider
                                   failure reason for cycle metadata.

    USnYT=X is deliberately absent: it is an Investing.com-only series and must
    never cross into Treasury.gov (which owns the US*YT_CMT series).
    """
    fallback_fn = None
    fallback_provider = None
    if symbol in UST_TREASURY_FALLBACK_SYMBOLS:
        fallback_fn = process_treasury_fallback_symbol
        fallback_provider = "treasury.gov"
    elif symbol in ASIA_INDEX_YFINANCE_FALLBACK_SYMBOLS:
        fallback_fn = process_asia_index_yfinance_fallback_symbol
        fallback_provider = "yfinance (cash index proxy)"

    if fallback_fn is None:
        # No fallback configured. Check if Local data is still current.
        # If Local retains previously validated canonical data and no newer
        # finalized session is expected, classify as CURRENT_BUT_PROVIDER_UNAVAILABLE.
        local_is_current = _check_local_current_for_provider_outage(symbol, meta)
        if local_is_current:
            print(
                f"[INVESTINY FALLBACK NONE] {symbol} | no fallback configured; "
                f"Local data still current | primary_failure={primary_failure_reason}"
            )
            outcomes[symbol] = SymbolFetchOutcome(
                symbol=symbol,
                name=meta["name"],
                provider="investing.com",
                required=symbol in REQUIRED_SYMBOLS,
                success=True,  # Coverage passes because Local is current
                rows_fetched=0,
                latest_observation=None,
                failure_reason=None,
                primary_provider="investing.com",
                primary_failure_reason=primary_failure_reason,
                data_freshness_state="CURRENT_BUT_PROVIDER_UNAVAILABLE",
                local_is_current=True,
            )
            return True
        else:
            print(
                f"[INVESTINY FALLBACK NONE] {symbol} | no fallback configured; "
                f"Local data stale or unconfirmed | primary_failure={primary_failure_reason}"
            )
            outcomes[symbol] = SymbolFetchOutcome(
                symbol=symbol,
                name=meta["name"],
                provider="investing.com",
                required=symbol in REQUIRED_SYMBOLS,
                success=False,
                rows_fetched=0,
                latest_observation=None,
                failure_reason=(
                    f"primary investing.com: {primary_failure_reason}; "
                    f"no fallback source configured; Local data stale or unconfirmed"
                ),
                primary_provider="investing.com",
                primary_failure_reason=primary_failure_reason,
                data_freshness_state="STALE_OR_UNCONFIRMED_PROVIDER_OUTAGE",
                local_is_current=False,
            )
            return False

    print(
        f"[INVESTINY FALLBACK ATTEMPT] {symbol} -> {fallback_provider} | "
        f"primary_failure={primary_failure_reason}"
    )
    try:
        rows, live_row, rows_fetched, latest_observation = fallback_fn(
            symbol,
            meta,
            start_date=start_date,
            observed_at=observed_at,
        )
    except Exception as fallback_error:
        print(f"[INVESTINY FALLBACK ERROR] {symbol}: {fallback_error}")
        rows_fetched = getattr(fallback_error, "rows_fetched", 0)
        latest_observation = getattr(fallback_error, "latest_observation", None)
        fallback_failure_reason = (
            str(fallback_error)
            if isinstance(fallback_error, SymbolProcessingError)
            else f"{type(fallback_error).__name__}: {fallback_error}"
        )
        outcomes[symbol] = SymbolFetchOutcome(
            symbol=symbol,
            name=meta["name"],
            provider="investing.com",
            required=symbol in REQUIRED_SYMBOLS,
            success=False,
            rows_fetched=rows_fetched,
            latest_observation=latest_observation,
            failure_reason=(
                f"primary investing.com: {primary_failure_reason}; "
                f"fallback {fallback_provider}: {fallback_failure_reason}"
            ),
            primary_provider="investing.com",
            primary_failure_reason=primary_failure_reason,
            fallback_provider=fallback_provider,
            fallback_failure_reason=fallback_failure_reason,
        )
        return False

    if live_row:
        all_live_rows.append(live_row)
        print(
            f"[INVESTINY FALLBACK LIVE SNAPSHOT] {symbol} | "
            f"market_date={live_row.get('market_date')} close={live_row.get('close')} "
            f"source={live_row.get('source')}"
        )
    # Keep database errors fatal, as in both primary provider paths.
    upsert_new_macro_rows(
        rows,
        [symbol],
        upsert_neon=upsert_neon,
        dry_run=dry_run,
        provider_label=f"{fallback_provider.upper()} FALLBACK",
        deferred_store=deferred_store,
    )
    outcomes[symbol] = SymbolFetchOutcome(
        symbol=symbol,
        name=meta["name"],
        provider=fallback_provider,
        required=symbol in REQUIRED_SYMBOLS,
        success=True,
        rows_fetched=rows_fetched,
        latest_observation=latest_observation,
        primary_provider="investing.com",
        primary_failure_reason=primary_failure_reason,
        fallback_provider=fallback_provider,
    )
    print(
        f"[INVESTINY FALLBACK OK] {symbol} | provider={fallback_provider} | "
        f"latest_observation={latest_observation}"
    )
    return True



# ============================================================
# SHARED HISTORICAL FETCH ROUTING (Phase A.2B)
# ============================================================
# Single source of truth for the approved historical fetch path. The main
# ingestion cycle and the bounded deferred-recovery path MUST agree on a
# symbol's source identity; recovery may change the fetch window/date
# selection but never the source. Keeping one dispatcher prevents the two
# paths from diverging again (the D2 hotfix defect routed every non-Treasury,
# non-Investing symbol into the oil-only yfinance fallback, raising
# "yfinance fallback is not enabled for <symbol>").
ROUTE_TREASURY_OFFICIAL = "TREASURY_OFFICIAL"
ROUTE_INVESTING = "INVESTING"
ROUTE_YFINANCE_HISTORICAL = "YFINANCE_HISTORICAL"
ROUTE_FROZEN = "FROZEN"        # no approved historical route -> fail closed


def historical_fetch_route(symbol, *, source_definition=None):
    """Return ``(route_kind, source_definition)`` for a symbol.

    Mirrors the main-ingestion routing exactly. A symbol whose historical
    source definition is unresolved (HK50/KOR200c1/CIHc1) or absent fails
    closed -- no source inference is performed.
    """
    from db_builder.session_calendar import (
        SOURCE_DEFINITION_BY_SYMBOL, SOURCE_DEFINITION_UNRESOLVED,
    )
    sd = source_definition or SOURCE_DEFINITION_BY_SYMBOL.get(symbol)
    # Unresolved / unknown definition -> fail closed (never guess a source).
    if symbol in SOURCE_DEFINITION_UNRESOLVED or sd is None:
        return ROUTE_FROZEN, sd
    # Treasury.gov official par-yield (CMT series and legacy US*YT=X).
    if symbol in CMT_SYMBOLS or symbol in TREASURY_PUBLICATION_SYMBOLS:
        return ROUTE_TREASURY_OFFICIAL, sd
    # Investing.com continuous front-contract futures (BZ=F/CL=F).
    if symbol in INVESTINY_ASSETS:
        return ROUTE_INVESTING, sd
    # FX / crypto / cash equity / cash indices: approved yfinance historical.
    return ROUTE_YFINANCE_HISTORICAL, sd


def _yfinance_historical_rows(symbol, meta, dates, *, source_definition=None):
    """Approved yfinance historical fetch, mirroring the main cycle.

    Uses the same extract -> remove-unfinished -> calculate_rows_for_symbol
    path as normal ingestion, so a recovered row is never sourced differently.
    """
    wanted = {pd.to_datetime(d).date() for d in dates}
    batch_df, target_start_date = download_recent_batch([symbol])
    symbol_df = extract_symbol_df_from_batch(batch_df, symbol)
    symbol_df = remove_unfinished_bars(symbol_df, symbol)
    if symbol_df.empty:
        return []
    rows = calculate_rows_for_symbol(
        df=symbol_df, symbol=symbol, name=meta["name"],
        asset_type=meta["asset_type"], source_definition=source_definition)
    rows = filter_rows_to_target_window(rows=rows,
                                        target_start_date=target_start_date)
    return [r for r in (rows or [])
            if pd.to_datetime(r["date"]).date() in wanted]


def _recovery_fetch_symbol_rows(symbol, dates):
    """Bounded deferred-history recovery fetch.

    Routes through :func:`historical_fetch_route` -- the SAME approved source
    decision the normal historical ingestion uses. Recovery changes only the
    fetch window/date selection, never the source identity.
    """
    meta = {
        "name": dict((s, n) for s, n, _a in ASSETS).get(symbol, symbol),
        "asset_type": dict((s, a) for s, _n, a in ASSETS).get(
            symbol, "unknown"),
    }
    route, sd = historical_fetch_route(symbol)
    if route == ROUTE_FROZEN:
        # No approved historical route (unresolved source definition): fail
        # closed. The historical gate independently refuses the write; the
        # retry policy keeps such rows non-retry-eligible.
        return []
    if route == ROUTE_TREASURY_OFFICIAL:
        rows, _live, _fetched, _latest = process_treasury_fallback_symbol(
            symbol, meta)
    elif route == ROUTE_INVESTING:
        rows, _live, _fetched, _latest = process_investiny_symbol(
            symbol, meta)
    else:  # ROUTE_YFINANCE_HISTORICAL
        return _yfinance_historical_rows(symbol, meta, dates,
                                         source_definition=sd)
    wanted = {pd.to_datetime(d).date() for d in dates}
    return [r for r in (rows or [])
            if pd.to_datetime(r["date"]).date() in wanted]


def run_production_deferred_recovery(now_utc=None):
    """D2: bounded deferred-history recovery inside the normal cycle.

    Deferred sessions are retried in bounded batches (RECOVERY_BATCH) so an
    hourly run never becomes a historical backfill. Rows that can now be built
    are written through the normal EOD gate and marked RESOLVED; rows still
    missing their prior session stay DEFERRED and escalate after repeated
    failure without disappearing from tracking.
    """
    store = production_deferred_store()
    provider_backoff = None
    try:
        provider_backoff = ProviderBackoffStore(local_engine)
    except Exception as exc:
        print(f"[DEFERRED BACKOFF] provider store unavailable: "
              f"{type(exc).__name__}: {exc}")
    try:
        result = run_deferred_recovery(
            local_engine, store,
            fetch_symbol_rows=_recovery_fetch_symbol_rows,
            now_utc=now_utc,
            provider_backoff=provider_backoff,
        )
    except Exception as exc:
        print(f"[DEFERRED RECOVERY] skipped: {type(exc).__name__}: {exc}")
        return None
    print(
        f"[DEFERRED RECOVERY] retried={result['retried']} "
        f"resolved={result['resolved']} "
        f"still_deferred={result['still_deferred']} "
        f"escalated={result['escalated']}"
    )
    return result




def _get_reconciliation_store(engine):
    """Return a ReconciliationStore bound to the given engine."""
    from db_builder.reconciliation_store import SqlReconciliationStore
    return SqlReconciliationStore(engine)


# Phase B bounded limits
RECONCILIATION_LOOKBACK = 5
RECONCILIATION_MAX_SYMBOLS = 40
RECONCILIATION_MAX_ROWS = 200
RECONCILIATION_RUNTIME_BUDGET = 120  # seconds
RECONCILIATION_MAX_CORRECTIONS = 25


def run_reconciliation_cycle(
    engine=None,
    run_id=None,
    now_utc=None,
    dry_run=False,
):
    """S8: Bounded Phase B reconciliation cycle.

    Builds the candidate universe, distinguishes AUTO_CORRECT vs DETECT_ONLY,
    fetches confirmation evidence, processes in dependency-safe order, and
    applies corrections only to Treasury CMT. Detect-only symbols produce
    DETECTION events only.

    NOT called from run_daily_update. Requires explicit invocation.
    """
    import time
    import uuid
    from datetime import timezone

    start_time = time.monotonic()
    run_id = run_id or f"recon_{uuid.uuid4().hex[:8]}"
    now = now_utc or datetime.now(timezone.utc)

    if engine is None:
        engine = make_local_engine()

    store = _get_reconciliation_store(engine)

    # 1. Build bounded candidate universe
    # v1 RUNTIME: only AUTO_CORRECT symbols (CMT) are processed.
    # DETECT_ONLY and EXCLUDED symbols are DORMANT until a real confirmation
    # fetcher is implemented. Do not generate CONFIRMATION_UNAVAILABLE events
    # for symbols that have no fetcher.
    auto_correct = sorted(AUTO_CORRECT_SYMBOLS)
    detect_only = []  # DORMANT in v1

    print(f"[RECONCILE] run_id={run_id} | auto_correct={len(auto_correct)} detect_only={len(detect_only)} (dormant)")

    summary = {
        "run_id": run_id,
        "candidates_examined": 0,
        "verdict_counts": {},
        "detection_events": 0,
        "correction_plans": 0,
        "corrections_committed": 0,
        "stale_plans": 0,
        "dependency_blocks": 0,
        "manual_reviews": 0,
        "confirmation_unavailable": 0,
        "report_only_mismatches": 0,
        "runtime_seconds": 0.0,
        "budget_exhausted": False,
    }

    corrections_committed = 0
    rows_examined = 0

    # 3. Process AUTO_CORRECT symbols
    for symbol in auto_correct:
        if corrections_committed >= RECONCILIATION_MAX_CORRECTIONS:
            summary["budget_exhausted"] = True
            break
        if rows_examined >= RECONCILIATION_MAX_ROWS:
            summary["budget_exhausted"] = True
            break
        elapsed = time.monotonic() - start_time
        if elapsed > RECONCILIATION_RUNTIME_BUDGET:
            summary["budget_exhausted"] = True
            break

        is_cmt = symbol in AUTO_CORRECT_SYMBOLS
        stored_rows = _get_stored_rows_for_symbol(engine, symbol, now, RECONCILIATION_LOOKBACK)
        if not stored_rows:
            continue

        sessions_to_process = sorted(stored_rows.keys(), reverse=True)

        for session_date in sessions_to_process:
            if corrections_committed >= RECONCILIATION_MAX_CORRECTIONS:
                summary["budget_exhausted"] = True
                break
            if rows_examined >= RECONCILIATION_MAX_ROWS:
                summary["budget_exhausted"] = True
                break
            elapsed = time.monotonic() - start_time
            if elapsed > RECONCILIATION_RUNTIME_BUDGET:
                summary["budget_exhausted"] = True
                break

            rows_examined += 1
            summary["candidates_examined"] += 1
            stored = stored_rows[session_date]

            confirmed = _fetch_confirmation_observation(symbol, session_date, now)
            if confirmed is None:
                summary["confirmation_unavailable"] += 1
                _record_detection(store, symbol, session_date,
                                  Verdict.CONFIRMATION_UNAVAILABLE,
                                  Reason.CONFIRMATION_UNAVAILABLE, run_id)
                continue

            prior_session = previous_valid_session(symbol, session_date)
            confirmed_prior = None
            stored_prior = None
            if prior_session:
                confirmed_prior = _fetch_confirmation_observation(symbol, prior_session, now)
                stored_prior = _get_stored_row(engine, symbol, prior_session)

            if prior_session and is_cmt:
                dep_ok, dep_reason = check_prior_dependency(
                    symbol, session_date, stored_prior, confirmed_prior, prior_session
                )
                if not dep_ok:
                    summary["dependency_blocks"] += 1
                    _record_detection(store, symbol, session_date,
                                      Verdict.DEPENDENCY_RECONCILE_REQUIRED,
                                      dep_reason, run_id)
                    continue

            d1_session = _next_valid_session(symbol, session_date)
            stored_d1 = _get_stored_row(engine, symbol, d1_session) if d1_session else None
            d2_session = _next_valid_session(symbol, d1_session) if d1_session else None
            stored_d2 = _get_stored_row(engine, symbol, d2_session) if d2_session else None

            result = decide(symbol, session_date, stored, confirmed,
                            confirmed_prior, prior_session, is_cmt=is_cmt)

            summary["verdict_counts"][result.verdict.value] = \
                summary["verdict_counts"].get(result.verdict.value, 0) + 1

            if result.verdict == Verdict.MATCH:
                continue
            elif result.verdict == Verdict.ROUNDING_ONLY:
                continue
            elif result.verdict == Verdict.CONFIRMATION_UNAVAILABLE:
                summary["confirmation_unavailable"] += 1
                _record_detection(store, symbol, session_date,
                                  result.verdict, result.reason, run_id)
                continue
            elif result.verdict == Verdict.SOURCE_DEFINITION_MISMATCH:
                _record_detection(store, symbol, session_date,
                                  result.verdict, result.reason, run_id)
                continue
            elif result.verdict == Verdict.MANUAL_REVIEW_REQUIRED:
                summary["manual_reviews"] += 1
                _record_detection(store, symbol, session_date,
                                  result.verdict, result.reason, run_id)
                continue
            elif result.verdict == Verdict.CORRECTION_REQUIRED:
                plan = build_correction_plan(
                    symbol, session_date, stored, confirmed, confirmed_prior,
                    stored_d1=stored_d1, previous_valid_session_d1=session_date if stored_d1 else None,
                    stored_d2=stored_d2, is_cmt=is_cmt,
                )
                plan.evidence["run_id"] = run_id
                plan.evidence["source_definition"] = "treasury_gov_cmt" if is_cmt else ""
                plan.evidence["confirmation_source"] = "treasury.gov" if is_cmt else ""

                summary["correction_plans"] += 1

                if dry_run:
                    continue

                success, error = execute_correction_plan(
                    store=store,
                    plan=plan,
                    run_id=run_id,
                    confirmation_source="treasury.gov" if is_cmt else "",
                    source_definition="treasury_gov_cmt" if is_cmt else "",
                )
                if success:
                    corrections_committed += 1
                    summary["corrections_committed"] += 1
                elif error == "STALE_PLAN":
                    summary["stale_plans"] += 1
                    _record_detection(store, symbol, session_date,
                                      Verdict.STALE_PLAN, Reason.STALE_PLAN, run_id)
                else:
                    summary["manual_reviews"] += 1
                    _record_detection(store, symbol, session_date,
                                      Verdict.MANUAL_REVIEW_REQUIRED,
                                      Reason.MANUAL_REVIEW_REQUIRED, run_id)

    # 4. DETECT_ONLY and EXCLUDED symbols are DORMANT in v1.
    # They are not processed until a real confirmation fetcher is implemented.
    # No CONFIRMATION_UNAVAILABLE events are generated for dormant symbols.

    summary["runtime_seconds"] = round(time.monotonic() - start_time, 2)
    print(f"[RECONCILE] done | {summary}")
    return summary


def _get_stored_rows_for_symbol(engine, symbol, now, lookback):
    """Get stored rows for a symbol within the lookback window."""
    from sqlalchemy import text
    cutoff = (now.date() - timedelta(days=lookback * 3)).isoformat()
    sql = text("""
        SELECT date, symbol, open, high, low, close, adj_close, volume,
               prev_close, change, pct_chg, amplitude, asset_type
          FROM public.macro
         WHERE symbol = :symbol AND date >= :cutoff
         ORDER BY date DESC
         LIMIT :lookback
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"symbol": symbol, "cutoff": cutoff, "lookback": lookback}).mappings().all()
    result = {}
    for r in rows:
        result[r["date"]] = Row(
            symbol=r["symbol"], date=r["date"],
            open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], adj_close=r["adj_close"], volume=r["volume"],
            prev_close=r["prev_close"], change=r["change"],
            pct_chg=r["pct_chg"], amplitude=r["amplitude"],
            source=None, asset_type=r["asset_type"],
        )
    return result


def _get_stored_row(engine, symbol, date):
    """Get a single stored row."""
    if date is None:
        return None
    from sqlalchemy import text
    sql = text("""
        SELECT date, symbol, open, high, low, close, adj_close, volume,
               prev_close, change, pct_chg, amplitude, asset_type
          FROM public.macro
         WHERE symbol = :symbol AND date = :date
         LIMIT 1
    """)
    with engine.connect() as conn:
        r = conn.execute(sql, {"symbol": symbol, "date": date}).mappings().first()
    if r is None:
        return None
    return Row(
        symbol=r["symbol"], date=r["date"],
        open=r["open"], high=r["high"], low=r["low"],
        close=r["close"], adj_close=r["adj_close"], volume=r["volume"],
        prev_close=r["prev_close"], change=r["change"],
        pct_chg=r["pct_chg"], amplitude=r["amplitude"],
        source=None, asset_type=r["asset_type"],
    )


def _fetch_confirmation_observation(symbol, session_date, now):
    """Fetch a confirmed same-definition observation for reconciliation."""
    if symbol in AUTO_CORRECT_SYMBOLS:
        return _fetch_treasury_confirmation(symbol, session_date, now)
    return _fetch_provider_confirmation(symbol, session_date, now)


def _fetch_treasury_confirmation(symbol, session_date, now):
    """Fetch Treasury.gov official CMT value for a specific date."""
    try:
        from db_builder.session_calendar import TENOR_BY_CMT_SYMBOL
        tenor = TENOR_BY_CMT_SYMBOL.get(symbol)
        if tenor is None:
            return None
        # Map tenor (e.g. "10Y") to Treasury.gov column (e.g. "BC_10YEAR")
        element = f"BC_{tenor}EAR"
        year = session_date.year
        df = fetch_treasury_par_yields(year)
        if element not in df.columns:
            return None
        dates_in_index = [d.date() for d in df.index]
        if session_date not in dates_in_index:
            return None
        idx = dates_in_index.index(session_date)
        val = df[element].iloc[idx]
        if pd.isna(val):
            return None
        val = float(val)
        return Row(
            symbol=symbol, date=session_date,
            open=val, high=val, low=val, close=val, adj_close=val,
            volume=None, source="treasury.gov", asset_type="ust_yield_cmt",
        )
    except Exception:
        return None


def _fetch_provider_confirmation(symbol, session_date, now):
    """Fetch same-provider confirmation for non-CMT symbols."""
    return None


def _next_valid_session(symbol, session_date):
    """Get the next valid session after session_date."""
    if session_date is None:
        return None
    try:
        from db_builder.session_calendar import adapter_for
        cal = adapter_for(symbol)
        return cal.next_session(session_date)
    except Exception:
        return None


def _record_detection(store, symbol, session_date, verdict, reason, run_id):
    """Record a DETECTION event."""
    record_detection_event(
        store=store,
        symbol=symbol,
        session_date=session_date,
        verdict=verdict,
        reason=reason,
        run_id=run_id,
    )
def run_daily_update(
    symbol_filter=None,
    start_date=None,
    upsert_neon=True,
    dry_run=False,
    update_etf_flows=True,
    etf_flow_start_date=None,
    etf_flow_tickers=None,
    update_live=True,
):
    # D2: one shared persistent deferred-history store for the whole cycle.
    # Deferrals are recorded in public.macro_deferred_history, never only in
    # memory. A dry run performs no writes, so it gets no store.
    deferred_store = None if dry_run else production_deferred_store()

    if not dry_run:
        create_macro_table(local_engine)
        create_macro_live_table(local_engine)
        if upsert_neon:
            create_macro_table(neon_engine)
            create_macro_live_table(neon_engine)

    asset_map = {
        symbol: {
            "name": name,
            "asset_type": asset_type,
        }
        for symbol, name, asset_type in ASSETS
    }

    symbols = list(asset_map.keys())
    if symbol_filter:
        requested = set(symbol_filter)
        unknown = sorted(requested - set(symbols))
        if unknown:
            raise ValueError(f"Unknown macro symbols requested: {unknown}")
        symbols = [symbol for symbol in symbols if symbol in requested]

    # ------------------------------------------------------------------
    # macro_live source guard: remove any wrong-source rows for USnYT=X
    # symbols before the scheduled pipeline writes new live rows. This
    # prevents stale Treasury.gov/CMT rows from surviving indefinitely
    # under USnYT=X symbols.
    # ------------------------------------------------------------------
    if not dry_run:
        removed_live = auto_clean_wrong_source_live_rows(local_engine)
        if removed_live:
            print(
                f"[MACRO_LIVE SOURCE GUARD] Removed "
                f"{len(removed_live)} wrong-source live rows: "
                f"{[s for s, _ in removed_live]}"
            )

    # Phase A (additive): the official CMT series is fetched from Treasury.gov
    # only, so it is excluded from both the yfinance batch and the
    # Investing.com loop and handled by its own phase below.
    yfinance_symbols = [
        symbol for symbol in symbols
        if symbol not in INVESTINY_ASSETS
        and symbol not in CMT_SYMBOLS
        and symbol not in OILPRICEAPI_ASSETS
    ]
    investiny_symbols = [
        symbol for symbol in symbols
        if symbol in INVESTINY_ASSETS and symbol not in CMT_SYMBOLS
    ]

    print(
        f"Running macro update for {len(symbols)} symbols "
        f"({len(yfinance_symbols)} yfinance, {len(investiny_symbols)} Investing.com) "
        f"in yfinance batches of {BATCH_SIZE}..."
    )
    if start_date:
        print(f"[START DATE OVERRIDE] target_start={start_date}")
    if dry_run:
        print("[DRY RUN] No rows will be upserted.")
    if not upsert_neon:
        print("[LOCAL ONLY] Neon upsert disabled.")

    all_live_rows = []
    outcomes = {}
    reset_investiny_provider_state()  # Reset provider breaker state for new cycle
    observed_at = datetime.now(ZoneInfo("UTC"))

    for batch in chunk_list(yfinance_symbols, BATCH_SIZE):
        print(f"\n[BATCH] {batch[0]} -> {batch[-1]} | {len(batch)} symbols")

        try:
            batch_df, target_start_date = download_recent_batch(batch, start_date=start_date)
        except Exception as e:
            print(f"[BATCH ERROR] {e}")
            for symbol in batch:
                meta = asset_map[symbol]
                outcomes[symbol] = SymbolFetchOutcome(
                    symbol=symbol,
                    name=meta["name"],
                    provider="yfinance",
                    required=symbol in REQUIRED_SYMBOLS,
                    success=False,
                    rows_fetched=0,
                    latest_observation=None,
                    failure_reason=f"{type(e).__name__}: {e}",
                )
            time.sleep(random.uniform(5.0, 15.0))
            continue

        all_yf_rows = []

        for symbol in batch:
            rows_fetched = 0
            latest_observation = None
            try:
                meta = asset_map[symbol]
                print(f"[PROCESS YF] {symbol}")

                symbol_df = extract_symbol_df_from_batch(batch_df, symbol)
                rows_fetched = len(symbol_df)
                rows_fetched, latest_observation = observation_metadata(symbol_df)

                print(
                    f"[YF DATES RAW] {symbol} | "
                    f"{pd.to_datetime(symbol_df['Date']).dt.date.tolist()}"
                )

                live_row = extract_latest_unfinished_row(
                    df=symbol_df,
                    symbol=symbol,
                    name=meta["name"],
                    asset_type=meta["asset_type"],
                    observed_at=observed_at,
                )
                if live_row:
                    all_live_rows.append(live_row)
                    print(
                        f"[LIVE SNAPSHOT] {symbol} | "
                        f"market_date={live_row['market_date']} "
                        f"close={live_row['close']}"
                    )

                symbol_df = remove_unfinished_bars(symbol_df, symbol)

                if symbol_df.empty:
                    print(f"[NO FINISHED BARS] {symbol}")
                    outcomes[symbol] = SymbolFetchOutcome(
                        symbol=symbol,
                        name=meta["name"],
                        provider="yfinance",
                        required=symbol in REQUIRED_SYMBOLS,
                        success=True,
                        rows_fetched=rows_fetched,
                        latest_observation=latest_observation,
                    )
                    continue

                rows = calculate_rows_for_symbol(
                    df=symbol_df,
                    symbol=symbol,
                    name=meta["name"],
                    asset_type=meta["asset_type"],
                )

                rows = filter_rows_to_target_window(
                    rows=rows,
                    target_start_date=target_start_date,
                )

                if not rows:
                    print(f"[NO VALID ROWS IN TARGET WINDOW] {symbol}")
                    outcomes[symbol] = SymbolFetchOutcome(
                        symbol=symbol,
                        name=meta["name"],
                        provider="yfinance",
                        required=symbol in REQUIRED_SYMBOLS,
                        success=True,
                        rows_fetched=rows_fetched,
                        latest_observation=latest_observation,
                    )
                    continue

                print(
                    f"[YF ROW DATES TARGET] {symbol} | "
                    f"{[row['date'] for row in rows]}"
                )

                all_yf_rows.extend(rows)
                outcomes[symbol] = SymbolFetchOutcome(
                    symbol=symbol,
                    name=meta["name"],
                    provider="yfinance",
                    required=symbol in REQUIRED_SYMBOLS,
                    success=True,
                    rows_fetched=rows_fetched,
                    latest_observation=latest_observation,
                )

            except Exception as e:
                print(f"[PROCESS ERROR] {symbol}: {e}")
                meta = asset_map[symbol]
                outcomes[symbol] = SymbolFetchOutcome(
                    symbol=symbol,
                    name=meta["name"],
                    provider="yfinance",
                    required=symbol in REQUIRED_SYMBOLS,
                    success=False,
                    rows_fetched=rows_fetched,
                    latest_observation=latest_observation,
                    failure_reason=f"{type(e).__name__}: {e}",
                )
                time.sleep(random.uniform(2.0, 5.0))

        if not all_yf_rows:
            print("[BATCH NO YF ROWS]")
            continue

        upsert_new_macro_rows(
            all_yf_rows,
            batch,
            upsert_neon=upsert_neon,
            dry_run=dry_run,
            provider_label="YF",
            deferred_store=deferred_store,
        )

        print("[BATCH DONE]")

        time.sleep(random.uniform(1.0, 3.0))

    if investiny_symbols:
        print(f"\n[INVESTING.COM SYMBOLS] {investiny_symbols}")
    for symbol in investiny_symbols:
        meta = asset_map[symbol]

        # ------------------------------------------------------------------
        # Per-symbol circuit breaker: check if THIS symbol's breaker is open.
        # Other symbols continue fetching normally even if this symbol is blocked.
        # ------------------------------------------------------------------
        if not is_investiny_symbol_available(symbol):
            print(
                f"[INVESTINY CIRCUIT BREAKER OPEN] provider=investing.com symbol={symbol} | "
                f"skipping Investing.com; routing to fallback"
            )
            rows = live_row = None
            rows_fetched = 0
            latest_observation = None
            primary_failure_reason = (
                f"investing.com provider unavailable (per-symbol circuit breaker)"
            )
            _process_investiny_fallback_for_symbol(
                symbol=symbol,
                meta=meta,
                primary_failure_reason=primary_failure_reason,
                start_date=start_date,
                observed_at=observed_at,
                outcomes=outcomes,
                all_live_rows=all_live_rows,
                upsert_neon=upsert_neon,
                dry_run=dry_run,
                deferred_store=deferred_store,
            )
            continue

        try:
            print(f"[PROCESS INVESTING.COM] {symbol}")
            rows, live_row, rows_fetched, latest_observation = process_investiny_symbol(
                symbol,
                meta,
                start_date=start_date,
                observed_at=observed_at,
            )
            investiny_403_streak = 0
        except InvestinyProviderUnavailable as e:
            # Per-symbol 403: open this symbol's breaker only.
            # Other Investing.com symbols continue fetching normally.
            # Use current symbol name, not the exception message which may
            # reference a different symbol from a prior 403.
            record_investiny_symbol_failure(symbol, f"HTTP 403 for {symbol}")
            rows = live_row = None
            rows_fetched = 0
            latest_observation = None
            primary_failure_reason = (
                f"investing.com provider blocked (HTTP 403) for {symbol}"
            )
            _process_investiny_fallback_for_symbol(
                symbol=symbol,
                meta=meta,
                primary_failure_reason=primary_failure_reason,
                start_date=start_date,
                observed_at=observed_at,
                outcomes=outcomes,
                all_live_rows=all_live_rows,
                upsert_neon=upsert_neon,
                dry_run=dry_run,
                deferred_store=deferred_store,
            )
            continue
        except Exception as e:
            print(f"[INVESTINY PROCESS ERROR] {symbol}: {e}")
            rows_fetched = getattr(e, "rows_fetched", 0)
            latest_observation = getattr(e, "latest_observation", None)
            primary_failure_reason = (
                str(e)
                if isinstance(e, SymbolProcessingError)
                else f"{type(e).__name__}: {e}"
            )

            # Shared fallback dispatch. OIL -> yfinance, UST/CMT -> treasury.gov,
            # Asia -> yfinance cash-index proxy. Runs on ANY primary failure, not
            # only HTTP 403 (the 403 path is handled above via
            # InvestinyProviderUnavailable). This replaces the three per-provider
            # copies of the same logic that used to live here.
            if (symbol in UST_TREASURY_FALLBACK_SYMBOLS
                    or symbol in ASIA_INDEX_YFINANCE_FALLBACK_SYMBOLS):
                _process_investiny_fallback_for_symbol(
                    symbol=symbol,
                    meta=meta,
                    primary_failure_reason=primary_failure_reason,
                    start_date=start_date,
                    observed_at=observed_at,
                    outcomes=outcomes,
                    all_live_rows=all_live_rows,
                    upsert_neon=upsert_neon,
                    dry_run=dry_run,
                    deferred_store=deferred_store,
                )
                continue
            else:
                # No fallback is configured for this symbol. Record the primary
                # failure and skip the success path -- otherwise `live_row` is
                # unbound here (the primary raised before producing one).
                outcomes[symbol] = SymbolFetchOutcome(
                    symbol=symbol,
                    name=meta["name"],
                    provider="investing.com",
                    required=symbol in REQUIRED_SYMBOLS,
                    success=False,
                    rows_fetched=rows_fetched,
                    latest_observation=latest_observation,
                    failure_reason=primary_failure_reason,
                    primary_provider="investing.com",
                    primary_failure_reason=primary_failure_reason,
                )
                print(f"[MACRO SYMBOL FAILED] symbol={symbol} provider=investing.com "
                      f"rows_fetched={rows_fetched} reason={primary_failure_reason}")
                time.sleep(random.uniform(2.0, 5.0))
                continue

        if live_row:
            all_live_rows.append(live_row)
            print(
                f"[INVESTING.COM LIVE SNAPSHOT] {symbol} | "
                f"market_date={live_row['market_date']} close={live_row['close']}"
            )
        # Database write errors are fatal and must not be reclassified as an
        # accepted supplementary provider failure.
        upsert_new_macro_rows(
            rows,
            [symbol],
            upsert_neon=upsert_neon,
            dry_run=dry_run,
            provider_label="INVESTING.COM",
            deferred_store=deferred_store,
        )
        outcomes[symbol] = SymbolFetchOutcome(
            symbol=symbol,
            name=meta["name"],
            provider="investing.com",
            required=symbol in REQUIRED_SYMBOLS,
            success=True,
            rows_fetched=rows_fetched,
            latest_observation=latest_observation,
            primary_provider="investing.com",
        )
        # Investing.com intermittently returns 403 when symbols are fetched
        # back-to-back, even though each individual instrument is valid.
        time.sleep(random.uniform(3.0, 6.0))

    # ------------------------------------------------------------------
    # Phase A (additive): official Treasury.gov CMT series.
    # Fetched directly from the official par-yield publication only; the
    # Investing.com provider is never consulted for these symbols. Rows pass
    # the EOD gate like any other historical row.
    # ------------------------------------------------------------------
    cmt_symbols = [s for s in symbols if s in CMT_SYMBOLS]
    if cmt_symbols:
        print(f"\n[CMT OFFICIAL SERIES] {cmt_symbols}")
    for symbol in cmt_symbols:
        meta = asset_map[symbol]
        try:
            rows, live_row, rows_fetched, latest_observation = (
                process_treasury_fallback_symbol(
                    symbol, meta, start_date=start_date, observed_at=observed_at)
            )
            if live_row:
                all_live_rows.append(live_row)
                print(
                    f"[CMT LIVE SNAPSHOT] {symbol} | "
                    f"market_date={live_row.get('market_date')} "
                    f"close={live_row.get('close')} source=treasury.gov"
                )
            upsert_new_macro_rows(
                rows,
                [symbol],
                upsert_neon=upsert_neon,
                dry_run=dry_run,
                provider_label="CMT",
                deferred_store=deferred_store,
            )
            outcomes[symbol] = SymbolFetchOutcome(
                symbol=symbol,
                name=meta["name"],
                provider="treasury.gov",
                required=symbol in REQUIRED_SYMBOLS,
                success=True,
                rows_fetched=rows_fetched,
                latest_observation=latest_observation,
                primary_provider="treasury.gov",
                fallback_provider=None,
            )
            print(f"[CMT OK] {symbol} | latest_observation={latest_observation}")
        except Exception as cmt_error:
            rows_fetched = getattr(cmt_error, "rows_fetched", 0)
            latest_observation = getattr(cmt_error, "latest_observation", None)
            outcomes[symbol] = SymbolFetchOutcome(
                symbol=symbol,
                name=meta["name"],
                provider="treasury.gov",
                required=symbol in REQUIRED_SYMBOLS,
                success=False,
                rows_fetched=rows_fetched,
                latest_observation=latest_observation,
                failure_reason=(
                    str(cmt_error)
                    if isinstance(cmt_error, SymbolProcessingError)
                    else f"{type(cmt_error).__name__}: {cmt_error}"
                ),
                primary_provider="treasury.gov",
            )
            print(f"[CMT ERROR] {symbol}: {cmt_error}")
        time.sleep(random.uniform(0.5, 1.5))

    # --------------------------------------------------------------------------
    # OilPriceAPI spot/indicative series. Fetched only from OilPriceAPI; neither
    # yfinance nor Investing.com is consulted for these symbols.
    # --------------------------------------------------------------------------
    oilpriceapi_symbols = [s for s in symbols if s in OILPRICEAPI_ASSETS]
    if oilpriceapi_symbols:
        print(f"\n[OILPRICEAPI SPOT SERIES] {oilpriceapi_symbols}")
    for symbol in oilpriceapi_symbols:
        meta = asset_map[symbol]
        try:
            rows, live_row, rows_fetched, latest_observation = (
                process_oilpriceapi_symbol(
                    symbol, meta, start_date=start_date, observed_at=observed_at)
            )
            if live_row:
                all_live_rows.append(live_row)
                print(
                    f"[OILPRICEAPI LIVE SNAPSHOT] {symbol} | "
                    f"market_date={live_row.get('market_date')} "
                    f"close={live_row.get('close')} source=oilpriceapi.com"
                )
            upsert_new_macro_rows(
                rows,
                [symbol],
                upsert_neon=upsert_neon,
                dry_run=dry_run,
                provider_label="OILPRICEAPI",
                deferred_store=deferred_store,
            )
            outcomes[symbol] = SymbolFetchOutcome(
                symbol=symbol,
                name=meta["name"],
                provider="oilpriceapi.com",
                required=symbol in REQUIRED_SYMBOLS,
                success=True,
                rows_fetched=rows_fetched,
                latest_observation=latest_observation,
                primary_provider="oilpriceapi.com",
                fallback_provider=None,
            )
            print(f"[OILPRICEAPI OK] {symbol} | latest_observation={latest_observation}")
        except Exception as oil_error:
            rows_fetched = getattr(oil_error, "rows_fetched", 0)
            latest_observation = getattr(oil_error, "latest_observation", None)
            outcomes[symbol] = SymbolFetchOutcome(
                symbol=symbol,
                name=meta["name"],
                provider="oilpriceapi.com",
                required=symbol in REQUIRED_SYMBOLS,
                success=False,
                rows_fetched=rows_fetched,
                latest_observation=latest_observation,
                failure_reason=(
                    str(oil_error)
                    if isinstance(oil_error, SymbolProcessingError)
                    else f"{type(oil_error).__name__}: {oil_error}"
                ),
                primary_provider="oilpriceapi.com",
            )
            print(f"[OILPRICEAPI ERROR] {symbol}: {oil_error}")
        time.sleep(OILPRICEAPI_RATE_LIMIT_SECONDS)

    for symbol in symbols:
        if symbol not in outcomes:
            meta = asset_map[symbol]
            outcomes[symbol] = SymbolFetchOutcome(
                symbol=symbol,
                name=meta["name"],
                provider=provider_for_symbol(symbol),
                required=symbol in REQUIRED_SYMBOLS,
                success=False,
                rows_fetched=0,
                latest_observation=None,
                failure_reason="symbol was not attempted",
            )

    summary = summarize_outcomes(outcomes.values())
    log_coverage_summary(summary)

    successful_symbols = {
        outcome.symbol for outcome in summary.outcomes if outcome.success
    }
    is_complete_registry_refresh = (
        summary.failed_count == 0 and set(symbols) == set(asset_map)
    )

    print(f"\n[LIVE SNAPSHOT TOTAL] rows={len(all_live_rows):,}")
    if not update_live:
        print("[LIVE REPLACE SKIP] Historical backfill mode.")
    elif dry_run:
        print("[DRY RUN SKIP LIVE REPLACE]")
    elif is_complete_registry_refresh:
        print(f"[REPLACE LOCAL LIVE] rows={len(all_live_rows):,}")
        replace_macro_live(local_engine, all_live_rows)
        if upsert_neon:
            print(f"[REPLACE NEON LIVE] rows={len(all_live_rows):,}")
            replace_macro_live(neon_engine, all_live_rows)
    else:
        print(
            f"[MERGE LOCAL LIVE] rows={len(all_live_rows):,} "
            f"successful_symbols={len(successful_symbols):,} retained_failed={summary.failed_count:,}"
        )
        merge_macro_live(local_engine, all_live_rows, successful_symbols)
        if upsert_neon:
            print(
                f"[MERGE NEON LIVE] rows={len(all_live_rows):,} "
                f"successful_symbols={len(successful_symbols):,} retained_failed={summary.failed_count:,}"
            )
            merge_macro_live(neon_engine, all_live_rows, successful_symbols)

    # D2: bounded deferred-history recovery. Runs after ingestion so it can
    # retry sessions deferred by this cycle and by earlier cycles.
    if not dry_run:
        run_production_deferred_recovery()

    # ------------------------------------------------------------------
    # Simplified EOD 5-Session Rolling Refresh.
    # Runs after normal ingestion + deferred recovery so it can reuse
    # the same normalized provider frames.
    # Directly owns DB mutation. Does NOT use C2 attempt_count.
    # Failures are logged but never fatal — existing pipeline behavior unchanged.
    # ------------------------------------------------------------------
    try:
        eod_symbols = [
            s for s in symbols
            if s not in CMT_SYMBOLS
            and (
                (s, SOURCE_DEFINITION_BY_SYMBOL.get(s, "")) in C3A_APPROVED_ANCHORS
                or (s, SOURCE_DEFINITION_BY_SYMBOL.get(s, "")) in SOURCE_DEFINITION_TRUST_START
            )
        ]
        if eod_symbols:
            # Canonical provider route — the SAME fetch normal ingestion uses.
            provider_data, _ = fetch_yfinance_daily_frame(eod_symbols)
            if provider_data:
                run_eod_5session_refresh(
                    symbols=list(provider_data.keys()),
                    provider_data=provider_data,
                    dry_run=dry_run,
                )
            else:
                print("[EOD REFRESH SKIP] No provider frames available")
    except Exception as eod_error:
        print(f"[EOD REFRESH ERROR] {eod_error}")

    # ---- local-to-Neon convergence sync (after LOCAL COMMIT) ----
    # Ordering: provider fetch -> normal ingestion -> C2 recovery
    #   -> local EOD 5-session refresh -> LOCAL COMMIT
    #   -> local-to-Neon convergence sync -> downstream/API availability
    # LOCAL is authoritative; this reads canonical local rows and never
    # fetches provider data. MIRROR bypasses the Phase A ingestion gate but
    # keeps universe/window/R22 destination safety.
    if not dry_run:
        try:
            sync_recent_macro_to_neon(symbols=eod_symbols)
        except Exception as sync_error:
            # Local DB remains authoritative; never roll back valid local data.
            print(f"[NEON MACRO SYNC ERROR] {type(sync_error).__name__}: {sync_error}")
            print("[NEON MACRO SYNC] local DB remains authoritative; "
                  "no local rollback performed")

    if update_etf_flows:
        try:
            run_etf_flow_update_after_macro(
                start_date=etf_flow_start_date,
                tickers=etf_flow_tickers,
                dry_run=dry_run,
            )
        except Exception as e:
            print(f"[ETF FLOW ERROR] {e}")
            raise
    else:
        print("[ETF FLOW SKIP] ETF daily flow refresh disabled.")

    _render_pipeline_status_message(summary)
    return summary


def _render_pipeline_status_message(summary):
    """Render the final pipeline-state message for a coverage summary.

    SUCCESS: coverage satisfied, no genuine degradation (only intentional
    EXPECTED_FROZEN exclusions may remain).
    DEGRADED_SUCCESS: coverage satisfied but genuine operational degradation
    exists (required/supplementary historical deferrals).
    FAILURE: coverage not satisfied.
    """
    req_hist = len(summary.required_historical_deferred_symbols)
    supp_hist = len(summary.supplementary_historical_deferred_symbols)
    expected_frozen = len(summary.expected_frozen_symbols)
    if not summary.coverage_satisfied:
        print(
            "[MACRO UPDATE FAILURE] Required coverage not satisfied; "
            "returning non-zero."
        )
    elif req_hist or supp_hist:
        # Genuine operational degradation exists — unsafe historical rows withheld.
        print(
            f"[MACRO UPDATE DEGRADED SUCCESS] coverage={summary.coverage_pct:.2f}%; "
            f"required historical degraded={req_hist}; "
            f"supplementary historical degraded={supp_hist}; "
            f"expected frozen={expected_frozen}; "
            f"unsafe historical rows withheld; downstream continuing."
        )
    elif expected_frozen:
        # Only intentional EXPECTED_FROZEN exclusions remain — this is SUCCESS, not degraded.
        print(
            f"[MACRO UPDATE SUCCESS] coverage={summary.coverage_pct:.2f}%; "
            f"expected frozen={expected_frozen} legacy Treasury historical symbols."
        )
    else:
        print(
            "[MACRO UPDATE SUCCESS] Required coverage satisfied; "
            "all configured symbols succeeded."
        )


def _classify_deferral(reason, *, expected_prior, batch_start):
    """D1: is a deferral a benign window-boundary deferral?

    A deferral is non-fatal ONLY when the expected prior valid session is
    demonstrably outside the current fetch/lookback candidate window, i.e.
    ``expected_prior < batch_start`` (with a small tolerance for
    weekend/holiday gaps). The decision uses real session/window context,
    never the reason string alone.

    Everything else stays actionable: a prior session that should be inside
    the fetch range but is missing, an unavailable calendar, an unresolved
    source definition, an unexpected Treasury/CMT confirmation failure, an
    escalated recovery, or any other fail-closed integrity condition.
    """
    if reason == "PRIOR_SESSION_LOOKUP_FAILED":
        # The lookup itself failed, so the window cannot be trusted.
        return "actionable"
    if expected_prior is None:
        return "actionable"
    if batch_start is None:
        return "actionable"
    cutoff = batch_start - timedelta(days=_BOUNDARY_WINDOW_TOLERANCE_DAYS)
    if expected_prior < cutoff:
        return "boundary"
    return "actionable"


def _classify_unavailable_symbol(symbol, reason):
    """Classify an unavailable symbol as PIPELINE_FATAL, DEGRADED, or EXPECTED_FROZEN.

    PIPELINE_FATAL:
    - required current/live symbol unavailable
    - core provider failure leaves required current data unavailable
    - internal DB/gate failure

    DEGRADED:
    - supplementary source unavailable
    - required historical confirmation unavailable while safe live/current data exists

    EXPECTED_FROZEN:
    - intentional long-term freeze (legacy Treasury EOD definition unresolved)
    - not an operational degradation; does not count toward DEGRADED_SUCCESS
    """
    # Legacy Treasury freeze is intentional by design — never pipeline-fatal
    if reason == "LEGACY_EOD_DEFINITION_UNRESOLVED":
        return "EXPECTED_FROZEN"

    # Required symbol unavailable is pipeline-fatal
    if symbol in REQUIRED_SYMBOLS:
        return "PIPELINE_FATAL"

    # Supplementary symbol unavailable is degraded
    return "DEGRADED"


def _phase_a_safety_exit_code(summary):
    """Return a non-zero exit code only for pipeline-fatal conditions.

    Three-state model:
    - SUCCESS: coverage satisfied, no deferrals, no fatal conditions
    - DEGRADED_SUCCESS: coverage satisfied, historical deferrals exist but safe
    - FAIL_CLOSED: coverage not satisfied, or required live data failed, or fatal condition

    Symbol-level fail-closed behavior is unchanged: historical rows that cannot
    be safely confirmed remain deferred. This function only determines whether
    those deferrals should abort the entire pipeline.

    Pipeline-fatal positive set:
    1. Required coverage below threshold
    2. Required current/live data unavailable
    3. Deferred-store persistence failure
    4. DB write/transaction failure
    5. Phase A gate/internal exception
    6. Calendar/session engine failure affecting required processing
    7. Live-as-historical write risk
    8. Positional previous-row fallback attempt
    9. Source-definition contamination
    10. Written-row arithmetic/invariant failure
    11. Systemic failure that makes the run's integrity unknowable

    NOT pipeline-fatal:
    - Supplementary historical deferrals with coverage satisfied
    - Required symbols with healthy live data but deferred historical rows
    - Legacy Treasury intentionally frozen (LEGACY_EOD_DEFINITION_UNRESOLVED)
    """
    # Pipeline-fatal: coverage not satisfied
    if summary is None:
        return 1
    if not summary.coverage_satisfied:
        return 1

    # Pipeline-fatal: required symbol live data failed
    if summary.required_failed_symbols:
        return 1

    # Pipeline-fatal: systemic confirmation failure (>50% of required symbols)
    required_total = len([s for s in summary.outcomes if s.required])
    required_failed = len(summary.required_failed_symbols)
    if required_total > 0 and required_failed > required_total * 0.5:
        return 1

    # Pipeline-fatal: explicit fatal conditions recorded during the run
    if _PIPELINE_FATAL_CONDITIONS:
        return 1

    # Pipeline-fatal: required current/live symbol unavailable
    for rec in _PHASE_A_UNAVAILABLE_SYMBOLS:
        if _classify_unavailable_symbol(rec.get("symbol"), rec.get("reason")) == "PIPELINE_FATAL":
            return 1

    # NOT pipeline-fatal: historical deferrals with coverage satisfied
    # These remain actionable at the symbol level but don't abort the pipeline
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Fetch macro/index/ETF data into public.macro.")
    parser.add_argument("--symbols", default=None, help="Comma-separated symbol subset.")
    parser.add_argument("--start-date", default=None, help="Target start date in YYYY-MM-DD format.")
    parser.add_argument("--local-only", action="store_true", help="Upsert local PostgreSQL only.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and calculate rows without upserting.")
    parser.add_argument("--skip-etf-flows", action="store_true", help="Skip local ETF daily flow refresh after macro update.")
    parser.add_argument("--skip-live-replace", action="store_true", help="Do not replace public.macro_live; useful for historical symbol-subset backfills.")
    parser.add_argument("--etf-flow-start-date", default=None, help="Optional ETF flow backfill start date in YYYY-MM-DD format.")
    parser.add_argument("--etf-flow-tickers", default=None, help="Optional comma-separated ETF subset for ETF flow refresh.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    requested_symbols = [symbol.strip() for symbol in args.symbols.split(",") if symbol.strip()] if args.symbols else None
    start = datetime.strptime(args.start_date, "%Y-%m-%d").date() if args.start_date else None
    etf_flow_start = datetime.strptime(args.etf_flow_start_date, "%Y-%m-%d").date() if args.etf_flow_start_date else None
    etf_flow_tickers = [ticker.strip().upper() for ticker in args.etf_flow_tickers.split(",") if ticker.strip()] if args.etf_flow_tickers else None
    summary = run_daily_update(
        symbol_filter=requested_symbols,
        start_date=start,
        upsert_neon=not args.local_only,
        dry_run=args.dry_run,
        update_etf_flows=not args.skip_etf_flows,
        etf_flow_start_date=etf_flow_start,
        etf_flow_tickers=etf_flow_tickers,
        update_live=not args.skip_live_replace,
    )
    exit_code = summary.exit_code or _phase_a_safety_exit_code(summary)
    if exit_code == 0 and summary.coverage_satisfied:
        req_hist = len(summary.required_historical_deferred_symbols)
        supp_hist = len(summary.supplementary_historical_deferred_symbols)
        expected_frozen = len(summary.expected_frozen_symbols)
        if req_hist or supp_hist:
            print(
                f"[PHASE A WARN] historical ingestion degraded. "
                f"required_historical_deferred={req_hist} "
                f"supplementary_historical_deferred={supp_hist} "
                f"expected_frozen={expected_frozen}. "
                f"Pipeline continues; unsafe historical rows withheld."
            )
        elif expected_frozen:
            print(
                f"[PHASE A INFO] expected_frozen={expected_frozen} legacy Treasury historical symbols."
            )
        # Per-class freshness reporting
        _report_per_class_freshness(summary)
    return exit_code


def _report_per_class_freshness(summary):
    """Report per-class freshness status during DEGRADED_SUCCESS.

    Pipeline success must never imply every asset class is fresh.
    Each class is reported independently: FRESH, STALE, DEGRADED, EXPECTED_FROZEN, or UNAVAILABLE.
    """
    from datetime import date as _date
    today = _date.today()

    # Define asset classes and their symbols
    classes = {
        "equity_indices": ["^GSPC", "^NDX", "^DJI", "^RUT", "^VIX", "^FTSE", "^GDAXI", "^FCHI", "^HSI", "^N225", "^KS11"],
        "fx": ["EURUSD=X", "GBPUSD=X", "AUDUSD=X", "USDJPY=X", "CNY=X", "CHFUSD=X", "NZDUSD=X", "HKD=X", "SGD=X"],
        "futures": ["BZ=F", "CL=F", "ES=F", "GC=F", "HG=F", "NG=F", "NIY=F", "NQ=F", "RTY=F", "SI=F", "YM=F"],
        "treasury_cmt": ["US2YT_CMT", "US3YT_CMT", "US5YT_CMT", "US7YT_CMT", "US10YT_CMT", "US20YT_CMT", "US30YT_CMT"],
        "treasury_legacy": ["US2YT=X", "US3YT=X", "US5YT=X", "US7YT=X", "US10YT=X", "US20YT=X", "US30YT=X"],
        "yield_indices": ["^FVX", "^TNX", "^TYX"],
        "dollar_index": ["DX-Y.NYB"],
        "crypto": ["BTC-USD", "ETH-USD"],
    }

    # Build lookup from summary outcomes
    outcome_map = {o.symbol: o for o in summary.outcomes}
    expected_frozen_set = set(summary.expected_frozen_symbols)

    print("\n[PER-CLASS FRESHNESS]")
    for class_name, symbols in classes.items():
        class_outcomes = [outcome_map[s] for s in symbols if s in outcome_map]
        if not class_outcomes:
            continue

        successful = [o for o in class_outcomes if o.success]
        failed = [o for o in class_outcomes if not o.success]

        # Check if this class is entirely expected frozen
        class_symbols_set = set(symbols)
        if class_symbols_set and class_symbols_set.issubset(expected_frozen_set):
            status = "EXPECTED_FROZEN"
            print(f"  {class_name}: {status} (live/current successful={len(successful)}/{len(class_outcomes)}; historical frozen={len(class_outcomes)})")
        elif not successful:
            status = "UNAVAILABLE"
            print(f"  {class_name}: {status} (successful={len(successful)}/{len(class_outcomes)})")
        elif failed:
            status = "DEGRADED"
            print(f"  {class_name}: {status} (successful={len(successful)}/{len(class_outcomes)})")
        else:
            # Check if latest observation is recent (within 2 days)
            latest_dates = [o.latest_observation for o in successful if o.latest_observation]
            if latest_dates:
                max_date = max(latest_dates)
                if (today - max_date).days <= 2:
                    status = "FRESH"
                else:
                    status = "STALE"
            else:
                status = "DEGRADED"

            print(f"  {class_name}: {status} (successful={len(successful)}/{len(class_outcomes)})")


if __name__ == "__main__":
    sys.exit(main())
