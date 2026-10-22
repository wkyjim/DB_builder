"""Exchange-aware session calendar adapter for macro EOD ingestion.

Phase A integrity controls. This module is deliberately self-contained and has
no imports from ``macro_data_fetch`` (that module imports this one).

Design rules (Phase A):
  * Session eligibility is decided per-instrument by an exchange-aware calendar.
  * Unknown / unavailable calendar  -> CLOSED_UNCONFIRMED -> defer. Never guess.
  * ``previous_valid_session`` returns the immediately preceding *session*.
    Positional dataframe predecessors are NOT an acceptable source.
  * A weekday-only rule must never be used to validate EU/Asia, FX, futures or
    crypto sessions (crypto trades weekends; Globex has a Sunday session; FX
    opens Sunday evening).

``exchange_calendars`` is the primary backend. It is imported lazily and its
absence is fail-closed (every historical write defers) rather than fatal.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Set

try:  # primary backend
    import exchange_calendars as _xc
    import pandas as _pd
    _XC_IMPORT_ERROR: Optional[BaseException] = None
except BaseException as _exc:  # pragma: no cover - exercised via monkeypatch
    _xc = None
    _pd = None
    _XC_IMPORT_ERROR = _exc

# Minutes after the scheduled close before a session is treated as final.
CALENDAR_CLOSE_BUFFER_MINUTES = 30


class SessionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED_UNCONFIRMED = "CLOSED_UNCONFIRMED"
    CONFIRMED_COMPLETED = "CONFIRMED_COMPLETED"
    HOLIDAY = "HOLIDAY"
    NON_TRADING_DAY = "NON_TRADING_DAY"
    # Uncertain weekend/roll rows (e.g. FX Sunday weekly open) must not be
    # silently accepted as a completed daily session, nor deleted.
    WEEKEND_SESSION_REVIEW_REQUIRED = "WEEKEND_SESSION_REVIEW_REQUIRED"


class CalendarUnavailable(RuntimeError):
    """Raised when a calendar backend is required but unavailable."""


# ---------------------------------------------------------------- inventory

# Exchange-backed cash indices and US-listed ETFs.
EXCHANGE_BY_SYMBOL: Dict[str, str] = {
    "^GSPC": "XNYS", "^IXIC": "XNYS", "^DJI": "XNYS", "^NDX": "XNYS",
    "^RUT": "XNYS",
    "IEF": "XNYS", "SHY": "XNYS", "TLT": "XNYS", "HYG": "XNYS",
    "JNK": "XNYS", "LQD": "XNYS", "IWD": "XNYS", "IWF": "XNYS", "RSP": "XNYS",
    "^HSI": "XHKG", "^KS11": "XKRX", "000001.SS": "XSHG",
    "^N225": "XTKS", "^FTSE": "XLON", "^GDAXI": "XETR", "^FCHI": "XPAR",
    # Investing.com HTML-crawler symbols. They mirror the cash-index proxies
    # above: US yields on XNYS, HK50 on XHKG, KOR200c1 on XKRX, CIHc1 on XSHG.
    "US2YT=X": "XNYS", "US3YT=X": "XNYS", "US5YT=X": "XNYS", "US7YT=X": "XNYS",
    "US10YT=X": "XNYS", "US20YT=X": "XNYS", "US30YT=X": "XNYS",
    "HK50": "XHKG", "KOR200c1": "XKRX", "CIHc1": "XSHG",
}

# CBOE / ICE indices: they follow the NYSE trading-day calendar; the precise
# close *time* already lives in SYMBOL_CLOSE_MAP. "XCBO" is not a valid
# exchange_calendars name, so XNYS is used for the session-day test.
CBOE_ICE_SYMBOLS: Set[str] = {"^VIX", "^SKEW", "^MOVE", "^FVX", "^TNX", "^TYX"}

CRYPTO_SYMBOLS: Set[str] = {"BTC-USD", "ETH-USD"}
OILPRICEAPI_SYMBOLS: Set[str] = {"BRENT_OILPRICE", "WTI_OILPRICE"}

FX_SYMBOLS: Set[str] = {
    "AUDUSD=X", "CHFUSD=X", "CNY=X", "EURUSD=X", "GBPUSD=X",
    "HKD=X", "JPY=X", "NZDUSD=X", "SGD=X", "DX-Y.NYB",
}

# USnYT=X series are owned ONLY by Investing.com market yield.
# Treasury.gov CMT series are the separate USnYT_CMT symbols below.
TREASURY_SYMBOLS: Set[str] = set()  # Empty: USnYT=X uses Investing, not Treasury

# Phase A (additive): the official Treasury.gov constant-maturity par-yield
# series, kept separate from the legacy Investing.com-fed ``USnYT=X`` symbols.
# Same tenor calendar as the legacy symbols, but a distinct identity so the two
# series can never share rows, metadata or a previous-close chain.
TENOR_BY_CMT_SYMBOL: Dict[str, str] = {
    "US2YT_CMT": "2Y",
    "US3YT_CMT": "3Y",
    "US5YT_CMT": "5Y",
    "US7YT_CMT": "7Y",
    "US10YT_CMT": "10Y",
    "US20YT_CMT": "20Y",
    "US30YT_CMT": "30Y",
}
CMT_SYMBOLS: Set[str] = set(TENOR_BY_CMT_SYMBOL)

# Reserved: the proposed 17:00 ET market reference series. NOT enabled; no rows
# are produced for these symbols, so nothing is ever enqueued for them.
MARKET_REFERENCE_SYMBOLS: Set[str] = {
    "US2YT_MKT", "US3YT_MKT", "US5YT_MKT", "US7YT_MKT",
    "US10YT_MKT", "US20YT_MKT", "US30YT_MKT",
}

# Every symbol whose session confirmation comes from the official Treasury.gov
# publication rather than a market calendar.
TREASURY_PUBLICATION_SYMBOLS: Set[str] = TREASURY_SYMBOLS | CMT_SYMBOLS

GLOBEX_SYMBOLS: Set[str] = {
    "ES=F", "NQ=F", "YM=F", "RTY=F", "NIY=F",
    "GC=F", "SI=F", "HG=F", "CL=F", "BZ=F", "NG=F",
}

# ---------------------------------------------------------------------------
# Daily observation-date conventions (Phase A.2A)
#
# A *session* calendar and a *daily-bar observation-date* convention are two
# different things and must not be conflated:
#
#   * the session calendar answers "is the market trading, and when does the
#     session close?" -- used for live market-state, session open/closed and
#     Sunday-evening trading;
#   * the observation-date convention answers "which daily bar does this
#     source publish before D?" -- used for the historical prev_close chain.
#
# Globex is the motivating case: Sunday 17:00 ET *is* a real trading session,
# but a source that publishes Mon-Fri daily bars does not publish a
# Sunday-labelled daily bar. The daily observation before Monday is therefore
# Friday, not Sunday.
#
# The convention is keyed by the EXACT approved *historical source definition*,
# never by symbol alone: the same symbol can be served by different sources
# (e.g. an Investing.com continuous futures series vs. a yfinance fallback),
# and those sources may label daily bars differently. Provider date-label
# behaviour is NOT transferable between source definitions, and symbol identity
# alone does not establish a convention.
#
# ``None`` / absent means "not established for that source definition" and
# fails closed (the historical gate defers). The convention is never inferred
# from fallback-provider success.
# ---------------------------------------------------------------------------

# Source-definition identifiers. A source definition is the tuple
# (provider, instrument definition, daily-bar convention, continuous-contract
# construction) -- not a vague provider name. A single provider may legitimately
# carry more than one definition.
SOURCE_DEF_INVESTING_CONTINUOUS_FUTURES_DAILY = "INVESTING_CONTINUOUS_FUTURES_DAILY"
SOURCE_DEF_INVESTING_HK40_FUTURES_DAILY = "INVESTING_HK40_FUTURES_DAILY"
SOURCE_DEF_INVESTING_KOSPI200_FUTURES_DAILY = "INVESTING_KOSPI200_FUTURES_DAILY"
SOURCE_DEF_INVESTING_SSE50_FUTURES_DAILY = "INVESTING_SSE50_FUTURES_DAILY"
# USnYT=X series are owned ONLY by Investing.com market yield.
SOURCE_DEF_INVESTING_US2Y_BOND_YIELD_DAILY = "INVESTING_US2Y_BOND_YIELD_DAILY"
SOURCE_DEF_INVESTING_US3Y_BOND_YIELD_DAILY = "INVESTING_US3Y_BOND_YIELD_DAILY"
SOURCE_DEF_INVESTING_US5Y_BOND_YIELD_DAILY = "INVESTING_US5Y_BOND_YIELD_DAILY"
SOURCE_DEF_INVESTING_US7Y_BOND_YIELD_DAILY = "INVESTING_US7Y_BOND_YIELD_DAILY"
SOURCE_DEF_INVESTING_US10Y_BOND_YIELD_DAILY = "INVESTING_US10Y_BOND_YIELD_DAILY"
SOURCE_DEF_INVESTING_US20Y_BOND_YIELD_DAILY = "INVESTING_US20Y_BOND_YIELD_DAILY"
SOURCE_DEF_INVESTING_US30Y_BOND_YIELD_DAILY = "INVESTING_US30Y_BOND_YIELD_DAILY"
SOURCE_DEF_YFINANCE_CONTINUOUS_FUTURES_DAILY = "YFINANCE_CONTINUOUS_FUTURES_DAILY"
SOURCE_DEF_YFINANCE_CASH_INDEX_DAILY = "YFINANCE_CASH_INDEX_DAILY"
SOURCE_DEF_TREASURY_GOV_PAR_YIELD = "TREASURY_GOV_PAR_YIELD_DAILY"
SOURCE_DEF_CRYPTO_24_7_DAILY = "CRYPTO_24_7_DAILY"
SOURCE_DEF_YFINANCE_FX_DAILY = "YFINANCE_FX_DAILY"
SOURCE_DEF_YFINANCE_CASH_EQUITY_DAILY = "YFINANCE_CASH_EQUITY_DAILY"
SOURCE_DEF_MASSIVE_UST_EOD = "MASSIVE_UST_EOD"
SOURCE_DEF_OILPRICEAPI_SPOT = "OILPRICEAPI_SPOT_INDICATIVE_USD_PER_BARREL"

# Per-symbol *primary* historical source definition. This records which source
# definition the stored series belongs to; it is not itself the convention.
# Populated for every class so an unresolvable definition fails closed rather
# than silently defaulting.
SOURCE_DEFINITION_BY_SYMBOL: Dict[str, str] = {
    # Investing.com continuous front-contract futures (ids 8833/8849).
    "BZ=F": SOURCE_DEF_YFINANCE_CONTINUOUS_FUTURES_DAILY,
    "CL=F": SOURCE_DEF_YFINANCE_CONTINUOUS_FUTURES_DAILY,
    # yfinance continuous futures.
    **{s: SOURCE_DEF_YFINANCE_CONTINUOUS_FUTURES_DAILY
       for s in ("ES=F", "NQ=F", "YM=F", "RTY=F", "NIY=F",
                 "GC=F", "SI=F", "HG=F", "NG=F")},
    # Daily bars labelled by session date for these classes.
    **{s: SOURCE_DEF_TREASURY_GOV_PAR_YIELD
       for s in TREASURY_SYMBOLS | CMT_SYMBOLS},
    **{s: SOURCE_DEF_CRYPTO_24_7_DAILY for s in CRYPTO_SYMBOLS},
    **{s: SOURCE_DEF_YFINANCE_FX_DAILY for s in FX_SYMBOLS},
    **{s: SOURCE_DEF_YFINANCE_CASH_EQUITY_DAILY
       for s in CBOE_ICE_SYMBOLS | set(EXCHANGE_BY_SYMBOL)},
    # Special Investing.com futures (HK50, KOR200c1, CIHc1).
    "HK50": SOURCE_DEF_INVESTING_HK40_FUTURES_DAILY,
    "KOR200c1": SOURCE_DEF_INVESTING_KOSPI200_FUTURES_DAILY,
    "CIHc1": SOURCE_DEF_INVESTING_SSE50_FUTURES_DAILY,
    # USnYT=X series are owned ONLY by Investing.com market yield.
    "US2YT=X": SOURCE_DEF_INVESTING_US2Y_BOND_YIELD_DAILY,
    "US3YT=X": SOURCE_DEF_INVESTING_US3Y_BOND_YIELD_DAILY,
    "US5YT=X": SOURCE_DEF_INVESTING_US5Y_BOND_YIELD_DAILY,
    "US7YT=X": SOURCE_DEF_INVESTING_US7Y_BOND_YIELD_DAILY,
    "US10YT=X": SOURCE_DEF_INVESTING_US10Y_BOND_YIELD_DAILY,
    "US20YT=X": SOURCE_DEF_INVESTING_US20Y_BOND_YIELD_DAILY,
    "US30YT=X": SOURCE_DEF_INVESTING_US30Y_BOND_YIELD_DAILY,
    # New independent Massive UST EOD series (additive, not replacement)
    # Only 4 tenors available from Massive Economy API: 2Y, 5Y, 10Y, 30Y
    # 3Y, 7Y, 20Y are NOT available in Massive response — registrations removed
    "US2YT_MASSIVE": SOURCE_DEF_MASSIVE_UST_EOD,
    "US5YT_MASSIVE": SOURCE_DEF_MASSIVE_UST_EOD,
    "US10YT_MASSIVE": SOURCE_DEF_MASSIVE_UST_EOD,
    "US30YT_MASSIVE": SOURCE_DEF_MASSIVE_UST_EOD,
    # New independent OilPriceAPI spot price series (additive, not replacement)
    "BRENT_OILPRICE": SOURCE_DEF_OILPRICEAPI_SPOT,
    "WTI_OILPRICE": SOURCE_DEF_OILPRICEAPI_SPOT,
}

# Every source definition this build recognises. A candidate carrying an
# identifier outside this set is rejected (fail closed) -- an unknown
# definition must never inherit another definition's convention.
KNOWN_SOURCE_DEFINITIONS: Set[str] = {
    SOURCE_DEF_INVESTING_CONTINUOUS_FUTURES_DAILY,
    SOURCE_DEF_INVESTING_HK40_FUTURES_DAILY,
    SOURCE_DEF_INVESTING_KOSPI200_FUTURES_DAILY,
    SOURCE_DEF_INVESTING_SSE50_FUTURES_DAILY,
    SOURCE_DEF_YFINANCE_CONTINUOUS_FUTURES_DAILY,
    SOURCE_DEF_YFINANCE_CASH_INDEX_DAILY,
    SOURCE_DEF_TREASURY_GOV_PAR_YIELD,
    SOURCE_DEF_CRYPTO_24_7_DAILY,
    SOURCE_DEF_YFINANCE_FX_DAILY,
    SOURCE_DEF_YFINANCE_CASH_EQUITY_DAILY,
    SOURCE_DEF_MASSIVE_UST_EOD,
    SOURCE_DEF_OILPRICEAPI_SPOT,
}

# Definitions whose daily bars are explicitly known to be labelled by session
# date: for these the previous *session* IS the previous observation.
#
# This list is deliberately an allow-list. Any recognised definition NOT listed
# here requires an independently established daily-observation convention, and
# its absence fails closed. A newly added source definition therefore cannot
# silently inherit another definition's convention -- the safe default is
# always "defer until the convention is established".
SESSION_DATE_LABELLED_DEFINITIONS: Set[str] = {
    SOURCE_DEF_TREASURY_GOV_PAR_YIELD,
    SOURCE_DEF_CRYPTO_24_7_DAILY,
    SOURCE_DEF_YFINANCE_FX_DAILY,
    SOURCE_DEF_YFINANCE_CASH_EQUITY_DAILY,
    SOURCE_DEF_YFINANCE_CASH_INDEX_DAILY,
    # OilPriceAPI daily_average bars carry a US-business-day date label (0 weekend
    # rows across 261 observations; it does publish on US market holidays, so the
    # previous observation for any row is the previous NYSE session).
    SOURCE_DEF_OILPRICEAPI_SPOT,
}

# An *explicitly established* observation convention, keyed by source
# definition. Only entries present here are authoritative.
#
# NOTE (Phase A.2A): for the Investing.com continuous futures definition the
# provider's daily date-label convention has NOT been independently
# established, so it is deliberately absent -> the historical gate fails
# closed for that definition. This is intentional: yfinance's Mon-Fri label
# behaviour must not be assumed to hold for Investing.com's continuous series,
# and the investigation already showed the two are not source-equivalent.
OBSERVATION_CONVENTION_BY_SOURCE_DEFINITION: Dict[str, Optional[str]] = {
    SOURCE_DEF_YFINANCE_CONTINUOUS_FUTURES_DAILY: "us_futures",
    SOURCE_DEF_YFINANCE_CASH_EQUITY_DAILY: "XNYS",
}

# Human-readable record of what was established, for diagnostics/audit.
OBSERVATION_CONVENTION_EVIDENCE: Dict[str, Dict[str, str]] = {
    SOURCE_DEF_YFINANCE_CONTINUOUS_FUTURES_DAILY: {
        "provider": "yfinance",
        "instrument_definition": "continuous front-month futures",
        "daily_bar_convention": "Mon-Fri labelled; no Sunday-labelled bar",
        "continuous_construction": "provider default (front contract)",
        "sunday_daily_bar_produced": "no",
        "monday_bar_includes_sunday_evening": "known",
        "approved_convention": "CME us_futures session calendar "
                               "(previous daily observation; Sunday excluded)",
    },
    SOURCE_DEF_INVESTING_CONTINUOUS_FUTURES_DAILY: {
        "provider": "investing.com (investiny)",
        "instrument_definition": "continuous front-contract futures "
                                  "(Brent LCOV6 / WTI OIL)",
        "daily_bar_convention": "NOT ESTABLISHED",
        "continuous_construction": "provider continuous front contract",
        "sunday_daily_bar_produced": "unknown",
        "monday_bar_includes_sunday_evening": "unknown",
        "approved_convention": "NONE -- fails closed pending independent "
                               "establishment for this exact definition",
    },
}


# Investing.com continuous futures whose identity/source definition is
# unresolved. Historical writes for these symbols are deferred; the EOD gate
# is never bypassed for them.
SOURCE_DEFINITION_UNRESOLVED: Set[str] = set()

# Treasury par yields publish on the US business-day calendar. XNYS is used as
# the session-day approximation; the *authoritative* confirmation is the actual
# Treasury.gov publication for that specific session (see
# ``treasury_confirmation_required``).
_TREASURY_APPROX_EXCHANGE = "XNYS"

# US futures / FX weekly roll reference timezone (Globex + FX roll at 17:00 ET).
_ET = dt.timezone(dt.timedelta(hours=-5), "ET-fixed")  # conservative fixed offset
_UTC = dt.timezone.utc


def _as_date(value) -> Optional[dt.date]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return _pd.Timestamp(value).date()
    except Exception:
        return None


# ---------------------------------------------------------------- adapters

class CalendarAdapter:
    """Interface every session calendar must implement."""

    name = "abstract"
    #: True when the caller must additionally confirm an official publication
    #: for the specific session (Treasury).
    requires_publication_confirmation = False

    def is_trading_day(self, d: dt.date) -> bool:
        raise NotImplementedError

    def previous_valid_session(self, d: dt.date) -> Optional[dt.date]:
        raise NotImplementedError

    def session_close_utc(self, d: dt.date) -> Optional[dt.datetime]:
        raise NotImplementedError

    def session_status(self, d: dt.date, now_utc: dt.datetime) -> SessionStatus:
        close = self.session_close_utc(d)
        if close is None:
            if not self.is_trading_day(d):
                return self._non_session_status(d)
            return SessionStatus.CLOSED_UNCONFIRMED
        if not self.is_trading_day(d):
            return self._non_session_status(d)
        if now_utc >= close + dt.timedelta(minutes=CALENDAR_CLOSE_BUFFER_MINUTES):
            return SessionStatus.CONFIRMED_COMPLETED
        return SessionStatus.OPEN

    @staticmethod
    def _non_session_status(d: dt.date) -> SessionStatus:
        return (SessionStatus.NON_TRADING_DAY if d.weekday() >= 5
                else SessionStatus.HOLIDAY)


class UnknownCalendarAdapter(CalendarAdapter):
    """Fail-closed adapter: nothing is ever eligible."""

    name = "unknown"

    def __init__(self, reason: str = "CALENDAR_UNAVAILABLE"):
        self.reason = reason

    def is_trading_day(self, d: dt.date) -> bool:
        return False

    def previous_valid_session(self, d: dt.date) -> Optional[dt.date]:
        return None

    def previous_valid_observation_date(self, d: dt.date) -> Optional[dt.date]:
        return None

    def session_close_utc(self, d: dt.date) -> Optional[dt.datetime]:
        return None

    def session_status(self, d: dt.date, now_utc: dt.datetime) -> SessionStatus:
        return SessionStatus.CLOSED_UNCONFIRMED


class DailyObservationAdapter:
    """Resolve the previous *daily observation date* for a source series.

    Distinct from ``CalendarAdapter``: this answers which daily bar the source
    publishes before ``D``, not whether the market is trading. Backed by an
    ``exchange_calendars`` exchange (``us_futures`` for Globex daily bars),
    which is holiday-aware and excludes the Sunday session.
    """

    def __init__(self, exchange: str):
        if _xc is None:
            raise CalendarUnavailable(
                f"exchange_calendars unavailable: {_XC_IMPORT_ERROR!r}")
        try:
            self._cal = _xc.get_calendar(exchange)
        except Exception as exc:
            raise CalendarUnavailable(f"{exchange}: {exc}") from exc
        self.name = exchange

    def is_observation_day(self, d: dt.date) -> bool:
        try:
            return bool(self._cal.is_session(d))
        except Exception:
            return False

    def previous_valid_observation_date(self, d: dt.date) -> Optional[dt.date]:
        """Previous published daily observation strictly before ``d``.

        Fails closed (``None``) when the backend cannot answer, so an
        unestablished convention can never authorise a historical write.
        """
        try:
            return _as_date(self._cal.previous_session(d))
        except Exception:
            pass
        try:
            anchor = _pd.Timestamp(d) - _pd.Timedelta(days=1)
            return _as_date(
                self._cal.date_to_session(anchor, direction="previous"))
        except Exception:
            return None


class ExchangeCalendarAdapter(CalendarAdapter):
    """Cash index / ETF sessions via exchange_calendars."""

    def __init__(self, exchange: str):
        if _xc is None:
            raise CalendarUnavailable(
                f"exchange_calendars unavailable: {_XC_IMPORT_ERROR!r}")
        try:
            self._cal = _xc.get_calendar(exchange)
        except Exception as exc:
            raise CalendarUnavailable(f"{exchange}: {exc}") from exc
        self.name = exchange

    def is_trading_day(self, d: dt.date) -> bool:
        try:
            return bool(self._cal.is_session(d))
        except Exception:
            return False

    def previous_valid_session(self, d: dt.date) -> Optional[dt.date]:
        # Preferred API (verified in production py3.14/pandas3.0.3).
        try:
            return _as_date(self._cal.previous_session(d))
        except Exception:
            pass
        # Fallback for inputs that are not themselves a session.
        try:
            anchor = _pd.Timestamp(d) - _pd.Timedelta(days=1)
            return _as_date(self._cal.date_to_session(anchor, direction="previous"))
        except Exception:
            return None

    def session_close_utc(self, d: dt.date) -> Optional[dt.datetime]:
        try:
            return self._cal.session_close(d).to_pydatetime()
        except Exception:
            return None


class CryptoCalendarAdapter(CalendarAdapter):
    """24x7 UTC. Every calendar day is a session, weekends included."""

    name = "crypto-24x7"

    def is_trading_day(self, d: dt.date) -> bool:
        return True

    def previous_valid_session(self, d: dt.date) -> Optional[dt.date]:
        return d - dt.timedelta(days=1)

    def session_close_utc(self, d: dt.date) -> Optional[dt.datetime]:
        return dt.datetime.combine(d, dt.time(23, 59), tzinfo=_UTC)

    def session_status(self, d: dt.date, now_utc: dt.datetime) -> SessionStatus:
        close = self.session_close_utc(d)
        if now_utc >= close + dt.timedelta(minutes=CALENDAR_CLOSE_BUFFER_MINUTES):
            return SessionStatus.CONFIRMED_COMPLETED
        return SessionStatus.OPEN


class FxCalendarAdapter(CalendarAdapter):
    """24x5. Weekday sessions roll at 17:00 ET; Saturday closed.

    Sunday is the weekly *open* (17:00 ET), not a completed daily session, so it
    is classified WEEKEND_SESSION_REVIEW_REQUIRED rather than accepted.
    """

    name = "fx-24x5"

    def is_trading_day(self, d: dt.date) -> bool:
        return d.weekday() < 5

    def previous_valid_session(self, d: dt.date) -> Optional[dt.date]:
        cur = d - dt.timedelta(days=1)
        for _ in range(7):
            if cur.weekday() < 5:
                return cur
            cur -= dt.timedelta(days=1)
        return None

    def session_close_utc(self, d: dt.date) -> Optional[dt.datetime]:
        if d.weekday() >= 5:
            return None
        return dt.datetime.combine(d, dt.time(17, 0), tzinfo=_ET).astimezone(_UTC)

    def session_status(self, d: dt.date, now_utc: dt.datetime) -> SessionStatus:
        if d.weekday() == 5:                       # Saturday
            return SessionStatus.NON_TRADING_DAY
        if d.weekday() == 6:                       # Sunday weekly open
            return SessionStatus.WEEKEND_SESSION_REVIEW_REQUIRED
        close = self.session_close_utc(d)
        if now_utc >= close + dt.timedelta(minutes=CALENDAR_CLOSE_BUFFER_MINUTES):
            return SessionStatus.CONFIRMED_COMPLETED
        return SessionStatus.OPEN


class GlobexCalendarAdapter(CalendarAdapter):
    """Globex futures: Sunday-Friday, 17:00 ET session boundary.

    Sunday IS a legitimate session (weekly open), so it is a trading day here --
    unlike FX, where Sunday is only the roll into Monday.
    """

    name = "globex-sun-fri"

    def is_trading_day(self, d: dt.date) -> bool:
        return d.weekday() != 5                   # everything but Saturday

    def previous_valid_session(self, d: dt.date) -> Optional[dt.date]:
        cur = d - dt.timedelta(days=1)
        for _ in range(7):
            if cur.weekday() != 5:
                return cur
            cur -= dt.timedelta(days=1)
        return None

    def session_close_utc(self, d: dt.date) -> Optional[dt.datetime]:
        if d.weekday() == 5:
            return None
        return dt.datetime.combine(d, dt.time(17, 0), tzinfo=_ET).astimezone(_UTC)

    def session_status(self, d: dt.date, now_utc: dt.datetime) -> SessionStatus:
        if d.weekday() == 5:
            return SessionStatus.NON_TRADING_DAY
        close = self.session_close_utc(d)
        if now_utc >= close + dt.timedelta(minutes=CALENDAR_CLOSE_BUFFER_MINUTES):
            return SessionStatus.CONFIRMED_COMPLETED
        return SessionStatus.OPEN


class TreasuryCalendarAdapter(CalendarAdapter):
    """Official Treasury par yields.

    Session-day test uses the US business-day calendar (XNYS approximation).
    ``requires_publication_confirmation`` is True: the caller must additionally
    confirm that the Treasury.gov publication for *this specific session* is
    available. An unavailable calendar fails closed.
    """

    name = "treasury-publication"
    requires_publication_confirmation = True

    def __init__(self):
        if _xc is None:
            raise CalendarUnavailable(
                f"exchange_calendars unavailable: {_XC_IMPORT_ERROR!r}")
        try:
            self._cal = _xc.get_calendar(_TREASURY_APPROX_EXCHANGE)
        except Exception as exc:
            raise CalendarUnavailable(
                f"{_TREASURY_APPROX_EXCHANGE}: {exc}") from exc

    def is_trading_day(self, d: dt.date) -> bool:
        try:
            return bool(self._cal.is_session(d))
        except Exception:
            return False

    def previous_valid_session(self, d: dt.date) -> Optional[dt.date]:
        try:
            return _as_date(self._cal.previous_session(d))
        except Exception:
            pass
        try:
            anchor = _pd.Timestamp(d) - _pd.Timedelta(days=1)
            return _as_date(self._cal.date_to_session(anchor, direction="previous"))
        except Exception:
            return None

    def session_close_utc(self, d: dt.date) -> Optional[dt.datetime]:
        # Official par-yield publication lands mid-afternoon ET. 21:30 UTC is a
        # conservative post-publication instant used only for the time gate; the
        # authoritative check is ``treasury_publication_confirmed``.
        if not self.is_trading_day(d):
            return None
        return dt.datetime.combine(d, dt.time(21, 30), tzinfo=_UTC)


# ---------------------------------------------------------------- factory

_ADAPTER_CACHE: Dict[str, CalendarAdapter] = {}


def adapter_for(symbol: str) -> CalendarAdapter:
    """Return the session adapter for ``symbol`` (fail-closed on unknown)."""
    if symbol in _ADAPTER_CACHE:
        return _ADAPTER_CACHE[symbol]

    adapter: CalendarAdapter
    try:
        if symbol in SOURCE_DEFINITION_UNRESOLVED:
            adapter = UnknownCalendarAdapter("SOURCE_DEFINITION_UNRESOLVED")
        elif symbol in TREASURY_PUBLICATION_SYMBOLS:
            adapter = TreasuryCalendarAdapter()
        elif symbol in CRYPTO_SYMBOLS:
            adapter = CryptoCalendarAdapter()
        elif symbol in FX_SYMBOLS:
            adapter = FxCalendarAdapter()
        elif symbol in CBOE_ICE_SYMBOLS:
            adapter = ExchangeCalendarAdapter("XNYS")
        elif symbol in OILPRICEAPI_SYMBOLS:
            adapter = ExchangeCalendarAdapter("XNYS")
        elif symbol in GLOBEX_SYMBOLS:
            adapter = GlobexCalendarAdapter()
        elif symbol in EXCHANGE_BY_SYMBOL:
            adapter = ExchangeCalendarAdapter(EXCHANGE_BY_SYMBOL[symbol])
        else:
            adapter = UnknownCalendarAdapter("UNMAPPED_SYMBOL")
    except CalendarUnavailable as exc:
        adapter = UnknownCalendarAdapter(f"CALENDAR_UNAVAILABLE: {exc}")
    except Exception as exc:                       # never raise out of the gate
        adapter = UnknownCalendarAdapter(f"CALENDAR_ERROR: {type(exc).__name__}")

    _ADAPTER_CACHE[symbol] = adapter
    return adapter


def reset_adapter_cache() -> None:
    """Test hook: drop cached adapters (e.g. after toggling the backend)."""
    _ADAPTER_CACHE.clear()
    _OBSERVATION_ADAPTER_CACHE.clear()


# ------------------------------------------------- observation-date convention

_OBSERVATION_ADAPTER_CACHE: Dict[str, Optional[DailyObservationAdapter]] = {}


def source_definition_for(symbol: str,
                          source_definition: Optional[str] = None) -> Optional[str]:
    """Return the approved historical source definition for a candidate row.

    ``source_definition`` is carried by the candidate row when known (it is the
    definition the row's provider actually produced). When absent, fall back to
    the symbol's registered primary definition. ``None`` means the definition
    could not be resolved -> the gate must fail closed.
    """
    if source_definition:
        return source_definition
    return SOURCE_DEFINITION_BY_SYMBOL.get(symbol)


def observation_adapter_for(
        symbol: str,
        source_definition: Optional[str] = None,
) -> Optional[DailyObservationAdapter]:
    """Return the daily observation-date adapter for a *source definition*.

    The convention is resolved from the approved historical source definition,
    never from the symbol alone: the same symbol may be served by different
    sources with different daily-bar labelling, and provider label behaviour is
    not transferable between them.

    ``None`` means the convention is not established for that exact source
    definition -> the historical gate must defer (fail closed). Never infer a
    convention from provider success, from another source definition, or from
    the symbol's identity.
    """
    definition = source_definition_for(symbol, source_definition)
    if definition is None:
        return None

    cache_key = (symbol, definition)
    if cache_key in _OBSERVATION_ADAPTER_CACHE:
        return _OBSERVATION_ADAPTER_CACHE[cache_key]

    exchange = OBSERVATION_CONVENTION_BY_SOURCE_DEFINITION.get(definition)
    adapter: Optional[DailyObservationAdapter]
    if exchange is None:
        adapter = None
    else:
        try:
            adapter = DailyObservationAdapter(exchange)
        except CalendarUnavailable:
            adapter = None
        except Exception:
            adapter = None

    _OBSERVATION_ADAPTER_CACHE[cache_key] = adapter
    return adapter


def previous_valid_observation_date(
        symbol: str,
        d: dt.date,
        source_definition: Optional[str] = None,
) -> Optional[dt.date]:
    """Previous daily observation date strictly before ``d``.

    Resolved from the *source definition* of the candidate row. Distinct from
    ``previous_valid_session``: a Globex Sunday is a real trading session but a
    Mon-Fri source publishes no Sunday-labelled daily bar, so Monday's previous
    observation is Friday.

    Resolution order (fail closed):
      1. unknown / unresolved source definition            -> None
      2. definition whose bars are session-date-labelled    -> previous session
      3. otherwise an independently established convention is REQUIRED:
           established      -> that convention's previous observation
           not established  -> None (never inherit another definition's)

    A fallback provider succeeding does not change this: the convention belongs
    to the candidate row's historical source definition.
    """
    definition = source_definition_for(symbol, source_definition)
    if definition is None or definition not in KNOWN_SOURCE_DEFINITIONS:
        return None

    if definition in SESSION_DATE_LABELLED_DEFINITIONS:
        return previous_valid_session(symbol, d)

    # A non-session-date-labelled definition must have an established
    # convention; absence fails closed.
    adapter = observation_adapter_for(symbol, definition)
    if adapter is None:
        return None
    return adapter.previous_valid_observation_date(d)


def observation_convention_established(
        symbol: str,
        source_definition: Optional[str] = None,
) -> bool:
    return observation_adapter_for(symbol, source_definition) is not None


def register_observation_convention(source_definition: str,
                                    exchange: Optional[str]) -> None:
    """Attach (or clear) the approved daily observation convention for a
    *source definition*.

    Called only when an approved source definition's daily date labels have
    been independently established. ``None`` clears the entry so the gate fails
    closed again. Never infer a convention from provider success, from another
    source definition, or from symbol identity.
    """
    OBSERVATION_CONVENTION_BY_SOURCE_DEFINITION[source_definition] = exchange
    for key in [k for k in _OBSERVATION_ADAPTER_CACHE
                if k[1] == source_definition]:
        _OBSERVATION_ADAPTER_CACHE.pop(key, None)


def register_symbol_source_definition(symbol: str,
                                      source_definition: Optional[str]) -> None:
    """Attach (or clear) the approved historical source definition for a symbol.

    Test/operational hook for source definitions established after import.
    """
    if source_definition is None:
        SOURCE_DEFINITION_BY_SYMBOL.pop(symbol, None)
    else:
        SOURCE_DEFINITION_BY_SYMBOL[symbol] = source_definition
    for key in [k for k in _OBSERVATION_ADAPTER_CACHE if k[0] == symbol]:
        _OBSERVATION_ADAPTER_CACHE.pop(key, None)


# ---------------------------------------------------------------- helpers

def session_status(symbol: str, d: dt.date,
                   now_utc: Optional[dt.datetime] = None) -> SessionStatus:
    return adapter_for(symbol).session_status(
        d, now_utc or dt.datetime.now(_UTC))


def previous_valid_session(symbol: str, d: dt.date) -> Optional[dt.date]:
    return adapter_for(symbol).previous_valid_session(d)


def treasury_publication_confirmed(d: dt.date,
                                   published_dates: Optional[Iterable] = None,
                                   now_utc: Optional[dt.datetime] = None) -> bool:
    """True only when the official publication for session ``d`` is available.

    ``published_dates`` are the session dates actually present in the fetched
    Treasury.gov par-yield series. When it is not supplied the caller has not
    established official confirmation and the answer is False (fail closed).
    """
    if published_dates is None:
        return False
    wanted = _as_date(d)
    for item in published_dates:
        if _as_date(item) == wanted:
            return True
    return False


# ---------------------------------------------------------------- gate

ROUTE_HISTORICAL = "HISTORICAL"
ROUTE_LIVE = "LIVE"
ROUTE_DEFERRED = "DEFERRED_HISTORY"
ROUTE_REJECT = "REJECT"


@dataclass
class Eligibility:
    symbol: str
    session_date: dt.date
    eligible: bool
    routing: str
    reason: str
    session_status: SessionStatus
    expected_prior: Optional[dt.date] = None
    calendar: str = ""
    extras: dict = field(default_factory=dict)


def historical_eligibility(
    symbol: str,
    d: dt.date,
    *,
    known_prior_sessions: Optional[Set[dt.date]] = None,
    now_utc: Optional[dt.datetime] = None,
    treasury_published_dates: Optional[Iterable] = None,
    source_definition: Optional[str] = None,
) -> Eligibility:
    """Decide whether session ``d`` for ``symbol`` may enter historical EOD.

    ``known_prior_sessions`` is the set of session dates already confirmed in
    historical storage for this symbol. A completed bar whose prior session is
    missing is DEFERRED -- it is never routed to the live table and never
    written historically with an older positional value.

    ``source_definition`` is the approved historical source definition of the
    candidate row. The previous *observation date* is resolved from that
    definition; an unestablished definition fails closed.
    """
    now = now_utc or dt.datetime.now(_UTC)
    adapter = adapter_for(symbol)
    status = adapter.session_status(d, now)
    # Phase A.2A: the *session* calendar answers "is the market trading and is
    # the session complete?". The *observation-date* convention answers "which
    # daily bar does this source publish before D?" -- they are different
    # questions, and the latter is keyed by the exact approved source
    # definition, not by the symbol.
    prior = previous_valid_observation_date(symbol, d, source_definition)

    def _result(eligible, routing, reason, prior_=prior):
        return Eligibility(symbol=symbol, session_date=d, eligible=eligible,
                           routing=routing, reason=reason, session_status=status,
                           expected_prior=prior_, calendar=adapter.name)

    if status == SessionStatus.OPEN:
        return _result(False, ROUTE_LIVE, "SESSION_OPEN")

    if status in (SessionStatus.HOLIDAY, SessionStatus.NON_TRADING_DAY):
        return _result(False, ROUTE_REJECT, status.value)

    if status == SessionStatus.WEEKEND_SESSION_REVIEW_REQUIRED:
        return _result(False, ROUTE_DEFERRED, "WEEKEND_SESSION_REVIEW_REQUIRED")

    if status == SessionStatus.CLOSED_UNCONFIRMED:
        return _result(False, ROUTE_DEFERRED, "CLOSED_UNCONFIRMED")

    # status == CONFIRMED_COMPLETED
    if adapter.requires_publication_confirmation:
        if not treasury_publication_confirmed(d, treasury_published_dates, now):
            return _result(False, ROUTE_DEFERRED,
                           "TREASURY_PUBLICATION_NOT_CONFIRMED")

    if prior is None:
        return _result(False, ROUTE_DEFERRED, "PREVIOUS_SESSION_NOT_CONFIRMED")

    if known_prior_sessions is not None and prior not in known_prior_sessions:
        return _result(False, ROUTE_DEFERRED, "PREVIOUS_SESSION_NOT_CONFIRMED")

    # Phase A dual-source rule: on an official-EOD symbol, ``prev_close`` must
    # come from the same official series. If the prior session's official value
    # is not published yet, defer rather than mix an unofficial prior close with
    # an official close.
    if adapter.requires_publication_confirmation:
        if not treasury_publication_confirmed(
                prior, treasury_published_dates, now):
            return _result(False, ROUTE_DEFERRED,
                           "PREVIOUS_SESSION_OFFICIAL_NOT_CONFIRMED")

    return _result(True, ROUTE_HISTORICAL, "OK")


def classify_weekend_rows(
    symbol: str,
    dates: Sequence[dt.date],
) -> Dict[dt.date, str]:
    """Classify weekend-dated rows without repairing or deleting anything.

    Legitimate weekend sessions (crypto; Globex Sunday) are *not* corruption.
    Uncertain ones are reported as WEEKEND_SESSION_REVIEW_REQUIRED.
    """
    out: Dict[dt.date, str] = {}
    adapter = adapter_for(symbol)
    for d in dates:
        d = _as_date(d)
        if d is None:
            continue
        if d.weekday() < 5:
            continue
        if adapter.is_trading_day(d):
            out[d] = "LEGITIMATE_WEEKEND_SESSION"
        else:
            out[d] = "WEEKEND_SESSION_REVIEW_REQUIRED"
    return out
