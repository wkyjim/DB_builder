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
    "^HSI": "XHKG", "^KS11": "XKRX", "^KS200": "XKRX", "000001.SS": "XSHG",
    "^N225": "XTKS", "^FTSE": "XLON", "^GDAXI": "XETR", "^FCHI": "XPAR",
}

# CBOE / ICE indices: they follow the NYSE trading-day calendar; the precise
# close *time* already lives in SYMBOL_CLOSE_MAP. "XCBO" is not a valid
# exchange_calendars name, so XNYS is used for the session-day test.
CBOE_ICE_SYMBOLS: Set[str] = {"^VIX", "^SKEW", "^MOVE", "^FVX", "^TNX", "^TYX"}

CRYPTO_SYMBOLS: Set[str] = {"BTC-USD", "ETH-USD"}

FX_SYMBOLS: Set[str] = {
    "AUDUSD=X", "CHFUSD=X", "CNY=X", "EURUSD=X", "GBPUSD=X",
    "HKD=X", "JPY=X", "NZDUSD=X", "SGD=X", "DX-Y.NYB",
}

TREASURY_SYMBOLS: Set[str] = {
    "US2YT=X", "US3YT=X", "US5YT=X", "US7YT=X",
    "US10YT=X", "US20YT=X", "US30YT=X",
}

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

# Investing.com continuous futures whose identity/source definition is
# unresolved. Historical writes for these symbols are deferred; the EOD gate
# is never bypassed for them.
SOURCE_DEFINITION_UNRESOLVED: Set[str] = {"HK50", "KOR200c1", "CIHc1"}

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

    def session_close_utc(self, d: dt.date) -> Optional[dt.datetime]:
        return None

    def session_status(self, d: dt.date, now_utc: dt.datetime) -> SessionStatus:
        return SessionStatus.CLOSED_UNCONFIRMED


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
) -> Eligibility:
    """Decide whether session ``d`` for ``symbol`` may enter historical EOD.

    ``known_prior_sessions`` is the set of session dates already confirmed in
    historical storage for this symbol. A completed bar whose prior session is
    missing is DEFERRED -- it is never routed to the live table and never
    written historically with an older positional value.
    """
    now = now_utc or dt.datetime.now(_UTC)
    adapter = adapter_for(symbol)
    status = adapter.session_status(d, now)
    prior = adapter.previous_valid_session(d)

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
