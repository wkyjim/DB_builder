"""Phase B — T+1 / later historical reconciliation engine (PURE).

No I/O, no DB, no production wiring. All side effects are injected.

v1 scope: AUTO-CORRECT = Treasury CMT only. Everything else = detect/report.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Allow-lists
# ---------------------------------------------------------------------------

CMT_SYMBOLS: Set[str] = {
    "US2YT_CMT", "US3YT_CMT", "US5YT_CMT", "US7YT_CMT",
    "US10YT_CMT", "US20YT_CMT", "US30YT_CMT",
}

AUTO_CORRECT_SYMBOLS: Set[str] = CMT_SYMBOLS

DETECT_ONLY_SYMBOLS: Set[str] = {
    # US equity indices
    "^GSPC", "^NDX", "^IXIC", "^DJI", "^RUT",
    # Europe equity indices
    "^FTSE", "^GDAXI", "^FCHI",
    # Asia equity indices
    "^HSI", "^N225", "^KS11", "000001.SS",
    # CBOE yield indices
    "^FVX", "^TNX", "^TYX",
    # Volatility (established)
    "^VIX", "^SKEW",
    # US ETFs
    "HYG", "LQD", "JNK", "RSP", "IWF", "IWD", "TLT", "IEF", "SHY",
}

LEGACY_TREASURY_SYMBOLS: Set[str] = {
    "US2YT=X", "US3YT=X", "US5YT=X", "US7YT=X",
    "US10YT=X", "US20YT=X", "US30YT=X",
}

EXCLUDED_REPORT_ONLY_SYMBOLS: Set[str] = {
    "USnYT_MKT",
    "HK50", "KOR200c1", "CIHc1",
    "GC=F", "SI=F", "HG=F", "CL=F", "BZ=F", "NG=F", "NIY=F",
    "ES=F", "NQ=F", "YM=F", "RTY=F",
    "^MOVE", "^KS200",
}

# ---------------------------------------------------------------------------
# Tolerances
# ---------------------------------------------------------------------------

SOURCE_TOLERANCE: float = 1e-4          # relative
DERIVED_TOLERANCE: float = 1e-4         # relative (change, amplitude)
PCT_CHG_TOLERANCE: float = 1e-3         # relative
TREASURY_VALUE_TOLERANCE: float = 5e-5  # absolute (existing production constant)
STORED_PRECISION: float = 1e-4          # NUMERIC(20,4)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Verdict(str, Enum):
    MATCH = "MATCH"
    ROUNDING_ONLY = "ROUNDING_ONLY"
    CORRECTION_REQUIRED = "CORRECTION_REQUIRED"
    SOURCE_DEFINITION_MISMATCH = "SOURCE_DEFINITION_MISMATCH"
    CONFIRMATION_UNAVAILABLE = "CONFIRMATION_UNAVAILABLE"
    LEGACY_SOURCE_DEFINITION_MIXED = "LEGACY_SOURCE_DEFINITION_MIXED"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    STALE_PLAN = "STALE_PLAN"
    DEPENDENCY_RECONCILE_REQUIRED = "DEPENDENCY_RECONCILE_REQUIRED"
    SOURCE_DEFINITION_UNRESOLVED = "SOURCE_DEFINITION_UNRESOLVED"


class Reason(str, Enum):
    SOURCE_VALUE_REVISED = "SOURCE_VALUE_REVISED"
    DERIVED_FIELD_INCONSISTENT = "DERIVED_FIELD_INCONSISTENT"
    OFFICIAL_VALUE_REVISED = "OFFICIAL_VALUE_REVISED"
    STALE_PLAN = "STALE_PLAN"
    DEPENDENCY_RECONCILE_REQUIRED = "DEPENDENCY_RECONCILE_REQUIRED"
    PRIOR_SESSION_UNCONFIRMED = "PRIOR_SESSION_UNCONFIRMED"
    CALENDAR_UNAVAILABLE = "CALENDAR_UNAVAILABLE"
    SOURCE_DEFINITION_MISMATCH = "SOURCE_DEFINITION_MISMATCH"
    CONFIRMATION_UNAVAILABLE = "CONFIRMATION_UNAVAILABLE"
    LEGACY_SOURCE_DEFINITION_MIXED = "LEGACY_SOURCE_DEFINITION_MIXED"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    SOURCE_DEFINITION_UNRESOLVED = "SOURCE_DEFINITION_UNRESOLVED"
    D2_PRE_EXISTING_INCONSISTENCY = "D2_PRE_EXISTING_INCONSISTENCY"


class AuditEventType(str, Enum):
    REPAIR = "REPAIR"
    DETECTION = "DETECTION"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


def round_4(x: Optional[float]) -> Optional[float]:
    """Production rounding: Python round(float(x), 4) — banker's rounding."""
    if x is None:
        return None
    return round(float(x), 4)


def _within_tolerance(
    stored: Optional[float],
    confirmed: Optional[float],
    rel_tol: float,
    abs_tol: Optional[float] = None,
) -> bool:
    if stored is None and confirmed is None:
        return True
    if stored is None or confirmed is None:
        return False
    diff = abs(float(stored) - float(confirmed))
    if abs_tol is not None and diff <= abs_tol:
        return True
    return diff <= rel_tol * max(abs(float(stored)), abs(float(confirmed)), 1.0)


def _within_stored_precision(
    stored: Optional[float], confirmed: Optional[float]
) -> bool:
    if stored is None and confirmed is None:
        return True
    if stored is None or confirmed is None:
        return False
    return abs(float(stored) - float(confirmed)) <= STORED_PRECISION


SOURCE_FIELDS: Tuple[str, ...] = (
    "open", "high", "low", "close", "adj_close", "volume",
)
DERIVED_FIELDS: Tuple[str, ...] = (
    "prev_close", "change", "pct_chg", "amplitude",
)


@dataclass
class Row:
    """A stored or confirmed macro row (subset of public.macro columns)."""
    symbol: str
    date: dt.date
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    adj_close: Optional[float] = None
    volume: Optional[float] = None
    prev_close: Optional[float] = None
    change: Optional[float] = None
    pct_chg: Optional[float] = None
    amplitude: Optional[float] = None
    source: Optional[str] = None
    asset_type: Optional[str] = None

    def field_value(self, name: str) -> Optional[float]:
        return getattr(self, name, None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "date": self.date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "adj_close": self.adj_close,
            "volume": self.volume,
            "prev_close": self.prev_close,
            "change": self.change,
            "pct_chg": self.pct_chg,
            "amplitude": self.amplitude,
            "source": self.source,
            "asset_type": self.asset_type,
        }


@dataclass
class CorrectionPlan:
    """Advisory plan built outside the transaction."""
    symbol: str
    session_date: dt.date
    verdict: Verdict
    reason: Reason
    changed_source_fields: List[str] = field(default_factory=list)
    changed_derived_fields: List[str] = field(default_factory=list)
    new_row: Optional[Row] = None
    new_d1_row: Optional[Row] = None
    expected_old_values: Dict[str, Optional[float]] = field(default_factory=dict)
    expected_d1_old_values: Dict[str, Optional[float]] = field(default_factory=dict)
    d2_pre_existing_inconsistency: bool = False
    d2_detail: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconResult:
    """Outcome of decide() + build_correction_plan()."""
    symbol: str
    session_date: dt.date
    verdict: Verdict
    reason: Reason
    plan: Optional[CorrectionPlan] = None
    d2_pre_existing_inconsistency: bool = False
    d2_detail: Optional[str] = None


# ---------------------------------------------------------------------------
# Pure engine
# ---------------------------------------------------------------------------


def is_auto_correctable(symbol: str) -> bool:
    return symbol in AUTO_CORRECT_SYMBOLS


def is_detect_only(symbol: str) -> bool:
    return symbol in DETECT_ONLY_SYMBOLS


def is_legacy_treasury(symbol: str) -> bool:
    return symbol in LEGACY_TREASURY_SYMBOLS


def is_excluded_report_only(symbol: str) -> bool:
    return symbol in EXCLUDED_REPORT_ONLY_SYMBOLS


def _recompute_derived(
    row: Row, confirmed_prior_close: float
) -> Dict[str, Optional[float]]:
    """Recompute derived fields from source + confirmed prior close."""
    close = row.close
    if close is None:
        return {"prev_close": None, "change": None, "pct_chg": None, "amplitude": None}
    prev = round_4(confirmed_prior_close)
    change = round_4(close - prev)
    pct = round_4(change / prev * 100) if prev != 0 else None
    amp = (
        round_4((row.high - row.low) / prev * 100)
        if row.high is not None and row.low is not None and prev != 0
        else None
    )
    return {"prev_close": prev, "change": change, "pct_chg": pct, "amplitude": amp}


def _cmt_derived(
    close: float, confirmed_prior_close: float
) -> Dict[str, Optional[float]]:
    """CMT synthetic row: open=high=low=adj_close=close, amplitude=0."""
    prev = round_4(confirmed_prior_close)
    change = round_4(close - prev)
    pct = round_4(change / prev * 100) if prev != 0 else None
    return {"prev_close": prev, "change": change, "pct_chg": pct, "amplitude": 0.0}


def _compare_source_fields(
    stored: Row, confirmed: Row, is_cmt: bool
) -> Tuple[bool, bool, List[str]]:
    """Return (mismatch, has_differences, changed_fields).

    mismatch = any difference > tolerance (material)
    has_differences = any difference at all (even within stored precision)
    """
    mismatch = False
    has_differences = False
    changed: List[str] = []
    for f in SOURCE_FIELDS:
        s = stored.field_value(f)
        c = confirmed.field_value(f)
        if is_cmt and f in ("open", "high", "low", "adj_close"):
            # CMT mirrors close; skip separate comparison
            continue
        if f == "volume":
            # Index volume is noisy; excluded from auto-repair
            continue
        if s is None and c is None:
            continue
        if s is None or c is None:
            mismatch = True
            has_differences = True
            changed.append(f)
            continue
        if not _within_tolerance(s, c, SOURCE_TOLERANCE):
            mismatch = True
            has_differences = True
            changed.append(f)
        elif not _within_stored_precision(s, c):
            has_differences = True
    return mismatch, has_differences, changed


def _compare_derived_fields(
    stored: Row,
    recomputed: Dict[str, Optional[float]],
) -> Tuple[bool, List[str]]:
    """Return (mismatch, changed_fields)."""
    mismatch = False
    changed: List[str] = []
    for f in DERIVED_FIELDS:
        s = stored.field_value(f)
        c = recomputed.get(f)
        if s is None and c is None:
            continue
        if s is None or c is None:
            mismatch = True
            changed.append(f)
            continue
        tol = PCT_CHG_TOLERANCE if f == "pct_chg" else DERIVED_TOLERANCE
        if not _within_tolerance(s, c, tol):
            mismatch = True
            changed.append(f)
    return mismatch, changed


def decide(
    symbol: str,
    session_date: dt.date,
    stored: Optional[Row],
    confirmed: Optional[Row],
    confirmed_prior: Optional[Row],
    previous_valid_session: Optional[dt.date],
    is_cmt: bool = False,
) -> ReconResult:
    """Pure verdict engine. No I/O."""

    # 0. Eligibility
    if not is_auto_correctable(symbol):
        if is_legacy_treasury(symbol):
            return ReconResult(symbol, session_date,
                               Verdict.LEGACY_SOURCE_DEFINITION_MIXED,
                               Reason.LEGACY_SOURCE_DEFINITION_MIXED)
        if is_excluded_report_only(symbol) or is_detect_only(symbol):
            return ReconResult(symbol, session_date,
                               Verdict.SOURCE_DEFINITION_UNRESOLVED,
                               Reason.SOURCE_DEFINITION_UNRESOLVED)
        return ReconResult(symbol, session_date,
                           Verdict.SOURCE_DEFINITION_UNRESOLVED,
                           Reason.SOURCE_DEFINITION_UNRESOLVED)

    # 1. Confirmation available?
    if confirmed is None:
        return ReconResult(symbol, session_date,
                           Verdict.CONFIRMATION_UNAVAILABLE,
                           Reason.CONFIRMATION_UNAVAILABLE)

    # 2. Source definition match?
    if stored is not None and confirmed.source is not None:
        if stored.source is not None and stored.source != confirmed.source:
            return ReconResult(symbol, session_date,
                               Verdict.SOURCE_DEFINITION_MISMATCH,
                               Reason.SOURCE_DEFINITION_MISMATCH)

    # 3-4. Compare fields
    source_mismatch, source_has_diff, changed_source = _compare_source_fields(
        stored, confirmed, is_cmt
    ) if stored else (True, True, list(SOURCE_FIELDS))

    # Recompute derived from confirmed source + confirmed prior
    if confirmed_prior is not None and confirmed.close is not None:
        if is_cmt:
            recomputed = _cmt_derived(confirmed.close, confirmed_prior.close)
        else:
            recomputed = _recompute_derived(confirmed, confirmed_prior.close)
    else:
        recomputed = {f: None for f in DERIVED_FIELDS}

    derived_mismatch, changed_derived = _compare_derived_fields(
        stored, recomputed
    ) if stored else (False, [])

    # 5. Decide
    if source_mismatch:
        verdict = Verdict.CORRECTION_REQUIRED
        reason = Reason.SOURCE_VALUE_REVISED
    elif derived_mismatch:
        verdict = Verdict.CORRECTION_REQUIRED
        reason = Reason.DERIVED_FIELD_INCONSISTENT
    elif source_has_diff:
        verdict = Verdict.ROUNDING_ONLY
        reason = Reason.SOURCE_VALUE_REVISED
    else:
        verdict = Verdict.MATCH
        reason = Reason.SOURCE_VALUE_REVISED

    # 6. Prior session
    if previous_valid_session is None:
        if verdict == Verdict.CORRECTION_REQUIRED:
            return ReconResult(symbol, session_date,
                               Verdict.MANUAL_REVIEW_REQUIRED,
                               Reason.PRIOR_SESSION_UNCONFIRMED)

    return ReconResult(symbol, session_date, verdict, reason,
                       d2_pre_existing_inconsistency=False)


def build_correction_plan(
    symbol: str,
    session_date: dt.date,
    stored: Row,
    confirmed: Row,
    confirmed_prior: Row,
    stored_d1: Optional[Row],
    previous_valid_session_d1: Optional[dt.date],
    stored_d2: Optional[Row],
    is_cmt: bool = False,
) -> CorrectionPlan:
    """Build an advisory CorrectionPlan. Pure — no I/O."""

    # Source fields from confirmed
    new_source: Dict[str, Optional[float]] = {}
    for f in SOURCE_FIELDS:
        if is_cmt and f in ("open", "high", "low", "adj_close"):
            new_source[f] = round_4(confirmed.close)
        elif f == "volume":
            new_source[f] = None  # excluded from auto-repair
        else:
            new_source[f] = round_4(confirmed.field_value(f))

    changed_source = [
        f for f in SOURCE_FIELDS
        if f not in ("volume",)
        and not (is_cmt and f in ("open", "high", "low", "adj_close"))
        and not _within_tolerance(stored.field_value(f), new_source[f], SOURCE_TOLERANCE)
    ]

    # Derived fields
    if is_cmt:
        recomputed = _cmt_derived(confirmed.close, confirmed_prior.close)
    else:
        recomputed = _recompute_derived(confirmed, confirmed_prior.close)

    changed_derived = [
        f for f in DERIVED_FIELDS
        if not _within_tolerance(
            stored.field_value(f), recomputed.get(f),
            PCT_CHG_TOLERANCE if f == "pct_chg" else DERIVED_TOLERANCE,
        )
    ]

    # Build new D row
    new_d = Row(
        symbol=symbol,
        date=session_date,
        open=new_source.get("open", stored.open),
        high=new_source.get("high", stored.high),
        low=new_source.get("low", stored.low),
        close=new_source.get("close", stored.close),
        adj_close=new_source.get("adj_close", stored.adj_close),
        volume=stored.volume,  # never auto-repaired
        prev_close=recomputed.get("prev_close"),
        change=recomputed.get("change"),
        pct_chg=recomputed.get("pct_chg"),
        amplitude=recomputed.get("amplitude"),
        source=confirmed.source or stored.source,
        asset_type=stored.asset_type,
    )

    # Expected old values for optimistic predicates (round_4 for NUMERIC(20,4) precision)
    expected_old: Dict[str, Optional[float]] = {}
    for f in SOURCE_FIELDS + DERIVED_FIELDS:
        expected_old[f] = round_4(stored.field_value(f))

    # D+1 expected old values (from stored_d1, not from plan)
    expected_d1_old: Dict[str, Optional[float]] = {}
    if stored_d1 is not None:
        for f in ["prev_close", "change", "pct_chg", "amplitude"]:
            expected_d1_old[f] = round_4(stored_d1.field_value(f))

    # D+1 deterministic derived correction
    new_d1: Optional[Row] = None
    if (
        stored_d1 is not None
        and previous_valid_session_d1 is not None
        and previous_valid_session_d1 == session_date
        and new_d.close is not None
    ):
        d1_prev = round_4(new_d.close)
        d1_change = round_4(stored_d1.close - d1_prev) if stored_d1.close is not None else None
        d1_pct = (
            round_4(d1_change / d1_prev * 100)
            if d1_change is not None and d1_prev != 0
            else None
        )
        d1_amp = (
            round_4((stored_d1.high - stored_d1.low) / d1_prev * 100)
            if stored_d1.high is not None and stored_d1.low is not None and d1_prev != 0
            else None
        )
        new_d1 = Row(
            symbol=symbol,
            date=stored_d1.date,
            open=stored_d1.open,
            high=stored_d1.high,
            low=stored_d1.low,
            close=stored_d1.close,  # NEVER changed
            adj_close=stored_d1.adj_close,
            volume=stored_d1.volume,
            prev_close=d1_prev,
            change=d1_change,
            pct_chg=d1_pct,
            amplitude=d1_amp,
            source=stored_d1.source,
            asset_type=stored_d1.asset_type,
        )

    # D+2 pre-existing inconsistency (never blocks, never written)
    d2_pre_existing = False
    d2_detail = None
    if stored_d2 is not None and stored_d1 is not None:
        if stored_d2.prev_close is not None and stored_d1.close is not None:
            if not _within_tolerance(
                stored_d2.prev_close, stored_d1.close, DERIVED_TOLERANCE
            ):
                d2_pre_existing = True
                d2_detail = (
                    f"D+2 ({stored_d2.date}) prev_close={stored_d2.prev_close} "
                    f"!= D+1 ({stored_d1.date}) close={stored_d1.close}"
                )

    return CorrectionPlan(
        symbol=symbol,
        session_date=session_date,
        verdict=Verdict.CORRECTION_REQUIRED,
        reason=Reason.SOURCE_VALUE_REVISED,
        changed_source_fields=changed_source,
        changed_derived_fields=changed_derived,
        new_row=new_d,
        new_d1_row=new_d1,
        expected_old_values=expected_old,
        expected_d1_old_values=expected_d1_old,
        d2_pre_existing_inconsistency=d2_pre_existing,
        d2_detail=d2_detail,
        evidence={
            "confirmed_source": confirmed.source,
            "confirmed_date": confirmed.date.isoformat(),
            "confirmed_prior_date": confirmed_prior.date.isoformat(),
            "confirmed_prior_close": confirmed_prior.close,
        },
    )


def check_prior_dependency(
    symbol: str,
    session_date: dt.date,
    stored_prior: Optional[Row],
    confirmed_prior: Optional[Row],
    previous_valid_session: Optional[dt.date],
) -> Tuple[bool, Optional[Reason]]:
    """Check if the prior session is confirmed same-definition.

    Returns (ok, reason_if_not).
    """
    if previous_valid_session is None:
        return False, Reason.PRIOR_SESSION_UNCONFIRMED
    if confirmed_prior is None:
        return False, Reason.CONFIRMATION_UNAVAILABLE
    if stored_prior is None:
        return False, Reason.MANUAL_REVIEW_REQUIRED
    if not _within_tolerance(
        stored_prior.close, confirmed_prior.close, SOURCE_TOLERANCE
    ):
        return False, Reason.DEPENDENCY_RECONCILE_REQUIRED
    return True, None