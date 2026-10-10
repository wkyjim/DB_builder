"""Phase A.2B -- retry / backoff policy (pure: no DB, no network, no I/O).

Three orthogonal layers, top to bottom:

    provider availability   -> may we attempt a fetch at all?
    deferred retry state    -> is THIS item retry-eligible now?
    historical gate         -> may data be written?   (UNCHANGED, elsewhere)

This module only ever SUPPRESSES fetches. It never promotes a row toward a
historical write. The frozen Phase A / A.2A eligibility gate remains the sole
authority for writability; backoff must never make a row eligible to write.

Invariant (enforced by construction -- this module returns only booleans and
state names, never write decisions):

    no layer may bypass the layer below it.
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------
# Retry classes (the state vocabulary)
# ---------------------------------------------------------------------
RETRY_ELIGIBLE = "RETRY_ELIGIBLE"
BACKOFF_ACTIVE = "BACKOFF_ACTIVE"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
SOURCE_DEFINITION_UNRESOLVED = "SOURCE_DEFINITION_UNRESOLVED"
PROVENANCE_UNVERIFIED = "PROVENANCE_UNVERIFIED"
EXPECTED_FROZEN = "EXPECTED_FROZEN"
RETRY_PATH_DEFECT = "RETRY_PATH_DEFECT"
MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"

ALL_RETRY_CLASSES: Tuple[str, ...] = (
    RETRY_ELIGIBLE,
    BACKOFF_ACTIVE,
    PROVIDER_UNAVAILABLE,
    SOURCE_DEFINITION_UNRESOLVED,
    PROVENANCE_UNVERIFIED,
    EXPECTED_FROZEN,
    RETRY_PATH_DEFECT,
    MANUAL_REVIEW_REQUIRED,
)

#: Deterministic precedence. Lower rank == STRONGER blocker. When a row has
#: more than one condition, the strongest (lowest rank) durable state wins, so
#: an incidental fetch error can never overwrite a stronger structural blocker.
#:
#:   1 EXPECTED_FROZEN            intentional permanent freeze
#:   2 SOURCE_DEFINITION/PROVENANCE   needs a config/source decision
#:   3 RETRY_PATH_DEFECT          our code cannot fetch; fixable
#:   4 MANUAL_REVIEW_REQUIRED     human decision
#:   5 PROVIDER_UNAVAILABLE       provider-level outage
#:   6 BACKOFF_ACTIVE             transient
#:   7 RETRY_ELIGIBLE
PRECEDENCE_RANK: Dict[str, int] = {
    EXPECTED_FROZEN: 1,
    SOURCE_DEFINITION_UNRESOLVED: 2,
    PROVENANCE_UNVERIFIED: 2,
    RETRY_PATH_DEFECT: 3,
    MANUAL_REVIEW_REQUIRED: 4,
    PROVIDER_UNAVAILABLE: 5,
    BACKOFF_ACTIVE: 6,
    RETRY_ELIGIBLE: 7,
}

#: Classes that suppress a dependent row in a P -> D chain.
BLOCKING_CLASSES = frozenset({
    BACKOFF_ACTIVE,
    PROVIDER_UNAVAILABLE,
    SOURCE_DEFINITION_UNRESOLVED,
    PROVENANCE_UNVERIFIED,
    EXPECTED_FROZEN,
    RETRY_PATH_DEFECT,
    MANUAL_REVIEW_REQUIRED,
})

#: Classes released only by a *condition* change (config change, code fix,
#: manual reset, provider recovery, or a later approved low-frequency review) --
#: never by elapsed time alone.
CONDITION_TRIGGERED_CLASSES = frozenset({
    PROVIDER_UNAVAILABLE,
    SOURCE_DEFINITION_UNRESOLVED,
    PROVENANCE_UNVERIFIED,
    EXPECTED_FROZEN,
    RETRY_PATH_DEFECT,
    MANUAL_REVIEW_REQUIRED,
})

#: Mirrors db_builder.deferred_history.RESOLVED without importing it (avoids a
#: circular import and keeps this module dependency-free).
STATUS_RESOLVED = "RESOLVED"


def rank(cls: Optional[str]) -> int:
    """Precedence rank (lower == stronger). Unknown/None == weakest."""
    return PRECEDENCE_RANK.get(cls, 99)


def resolve_retry_class(existing: Optional[str], new: Optional[str]) -> Optional[str]:
    """Return the STRONGER of two retry classes.

    This is the rule that keeps ``PROVENANCE_UNVERIFIED`` durable when an
    incidental yfinance ``ValueError`` also occurs: fixing the ValueError would
    still not make the historical row writable, so the stronger structural
    blocker must not be overwritten by the weaker incidental one.
    """
    if existing is None:
        return new
    if new is None:
        return existing
    return existing if rank(existing) <= rank(new) else new


# ---------------------------------------------------------------------
# Providers and granularity
# ---------------------------------------------------------------------
PROVIDER_INVESTING = "investing"
PROVIDER_YFINANCE = "yfinance"
PROVIDER_TREASURY = "treasury"
PROVIDER_UNKNOWN = "unknown"

#: Endpoint/data families within a provider. A single provider-wide key would
#: be too coarse: Investing.com continuous-futures and its other endpoint
#: families can behave independently, so suppressing a working family because
#: an unrelated one returned 403 would be wrong. Provider state is therefore
#: keyed by (provider, data_class).
DATA_CLASS_FUTURES = "futures"
DATA_CLASS_ASIA_INDEX = "asia_index"
DATA_CLASS_UST = "ust"
DATA_CLASS_CASH_INDEX = "cash_index"
DATA_CLASS_FX = "fx"
DATA_CLASS_CRYPTO = "crypto"
DATA_CLASS_ETF = "etf"
DATA_CLASS_UNKNOWN = "unknown"


def provider_for_symbol(symbol: str) -> str:
    """Best-effort provider attribution for a symbol (no I/O)."""
    s = symbol or ""
    if s in ("BZ=F", "CL=F") or s in ("HK50", "KOR200c1", "CIHc1"):
        return PROVIDER_INVESTING
    if s.endswith("_CMT") or (s.startswith("US") and s.endswith("YT=X")):
        return PROVIDER_TREASURY
    return PROVIDER_YFINANCE


def data_class_for_symbol(symbol: str) -> str:
    """Data/endpoint family used as the second key of provider state."""
    s = symbol or ""
    if s.endswith("=F"):
        return DATA_CLASS_FUTURES
    if s in ("HK50", "KOR200c1", "CIHc1"):
        return DATA_CLASS_ASIA_INDEX
    if s.endswith("_CMT") or (s.startswith("US") and s.endswith("YT=X")):
        return DATA_CLASS_UST
    if s.endswith("=X"):
        return DATA_CLASS_FX
    if s.endswith("-USD"):
        return DATA_CLASS_CRYPTO
    if s.startswith("^"):
        return DATA_CLASS_CASH_INDEX
    return DATA_CLASS_UNKNOWN


def provider_key(symbol: str) -> Tuple[str, str]:
    """(provider, data_class) state key for a symbol."""
    return (provider_for_symbol(symbol), data_class_for_symbol(symbol))


# ---------------------------------------------------------------------
# Provider fault taxonomy (HTTP semantics)
# ---------------------------------------------------------------------
#: 403/429 are access/rate decisions, materially different from a timeout or a
#: 5xx. They must NOT be retried forever on a fixed 24h window.
HTTP_ACCESS_MARKERS: Tuple[str, ...] = ("403", "429", "401")
HTTP_TRANSIENT_MARKERS: Tuple[str, ...] = (
    "500", "502", "503", "504", "Connection", "SSL", "DNS",
    "timeout", "Timeout",
)

#: Consecutive access-denial failures after which a provider family escalates
#: from PROVIDER_UNAVAILABLE to MANUAL_REVIEW_REQUIRED. Historical source
#: definition is never changed by this escalation.
PROVIDER_ACCESS_ESCALATION = 3


def is_access_denial(error_text: str) -> bool:
    """403/429/401 -- rate-limit or access denial, not a transient glitch."""
    s = str(error_text)
    return any(m in s for m in HTTP_ACCESS_MARKERS)


def is_transient_provider_error(error_text: str) -> bool:
    s = str(error_text)
    return any(m in s for m in HTTP_TRANSIENT_MARKERS)


def is_provider_error(error_text: str) -> bool:
    """Whether an error string indicates a provider-wide fault (either kind)."""
    return is_access_denial(error_text) or is_transient_provider_error(error_text)


# ---------------------------------------------------------------------
# Reason classification
# ---------------------------------------------------------------------
TRANSIENT_REASONS = frozenset({
    "CLOSED_UNCONFIRMED",
    "PREVIOUS_SESSION_NOT_CONFIRMED",
    "PREVIOUS_SESSION_OFFICIAL_NOT_CONFIRMED",
    "TREASURY_PUBLICATION_NOT_CONFIRMED",
    "RECOVERY_DATA_STILL_MISSING",
    "RECOVERY_PRIOR_STILL_UNCONFIRMED",
    "CMT_VALUE_NOT_OFFICIAL",
    "CMT_PREVIOUS_VALUE_NOT_OFFICIAL",
})

STRUCTURAL_REASONS: Dict[str, str] = {
    "PRIOR_PROVENANCE_UNVERIFIED": PROVENANCE_UNVERIFIED,
    "PRIOR_BEFORE_TRUST_START": PROVENANCE_UNVERIFIED,
    "PREVIOUS_VALUE_NOT_CONFIRMED": PROVENANCE_UNVERIFIED,
    "SOURCE_DEFINITION_UNRESOLVED": SOURCE_DEFINITION_UNRESOLVED,
}

FROZEN_REASONS = frozenset({"LEGACY_EOD_DEFINITION_UNRESOLVED"})

MANUAL_REASONS = frozenset({
    "WEEKEND_SESSION_REVIEW_REQUIRED",
    "PRIOR_SESSION_LOOKUP_FAILED",
    "PRIOR_VALUE_LOOKUP_FAILED",
})

#: Exception types from fetch_symbol_rows that are genuinely transient.
TRANSIENT_EXCEPTION_TYPES = frozenset({
    "Timeout", "TimeoutError", "ConnectionError", "ConnectionResetError",
    "ConnectionRefusedError", "HTTPError", "URLError", "SSLError",
    "socket.timeout", "OSError",
})

#: Real recovery attempts after which a deterministic fetch error parks.
DETERMINISTIC_ERROR_CEILING = 3

#: Real recovery attempts after which an unchanged structural state escalates.
STRUCTURAL_ESCALATION_CYCLES = 72

#: Exact known, now-repaired recovery-path defects. A row parked under one of
#: these identifiers becomes eligible for ONE bounded re-evaluation after the
#: fixed handler deploys; unrelated future RETRY_PATH_DEFECT states stay parked.
KNOWN_FIXED_DEFECTS = frozenset({
    "RECOVERY_ROUTING_V1_OIL_FALLTHROUGH",
})


def known_fixed_defect(error_class: Optional[str]) -> bool:
    """True if ``error_class`` names a defect this release repairs."""
    return (error_class or "") in KNOWN_FIXED_DEFECTS


def defect_identifier(error_text: str) -> Optional[str]:
    """Return the known-defect identifier for an exception message, else None.

    The v1 routing defect surfaced as the oil-only fallback rejecting a
    non-oil symbol. Matching the exact signature keeps unrelated future
    RETRY_PATH_DEFECT states out of the known-fixed release.
    """
    s = str(error_text or "")
    if "yfinance fallback is not enabled for" in s:
        return "RECOVERY_ROUTING_V1_OIL_FALLTHROUGH"
    return None


def classify_reason(reason: str, exception_type: Optional[str] = None) -> str:
    """Map a production defer reason to a retry class.

    Note: this returns the class implied by the reason ALONE. When a row
    already carries a stronger structural class, callers must combine the two
    with :func:`resolve_retry_class` so the stronger blocker wins.
    """
    r = (reason or "").strip()
    if r.startswith("RECOVERY_FETCH_ERROR:"):
        exc = exception_type or r.split(":", 1)[1]
        if exc in TRANSIENT_EXCEPTION_TYPES:
            return BACKOFF_ACTIVE
        # A deterministic fetch failure in the recovery path is our own
        # routing/parse defect -- fixable code, not a human decision.
        return RETRY_PATH_DEFECT
    if r.startswith("GATE_ERROR:"):
        return MANUAL_REVIEW_REQUIRED
    if r.startswith("CALENDAR"):
        return MANUAL_REVIEW_REQUIRED
    if r.startswith("PROVIDER_UNAVAILABLE"):
        return PROVIDER_UNAVAILABLE
    if r in FROZEN_REASONS:
        return EXPECTED_FROZEN
    if r in MANUAL_REASONS:
        return MANUAL_REVIEW_REQUIRED
    if r in STRUCTURAL_REASONS:
        return STRUCTURAL_REASONS[r]
    if r in TRANSIENT_REASONS:
        return BACKOFF_ACTIVE
    return MANUAL_REVIEW_REQUIRED


def is_known_reason(reason: str) -> bool:
    r = (reason or "").strip()
    if r.startswith(("RECOVERY_FETCH_ERROR:", "GATE_ERROR:", "CALENDAR",
                     "PROVIDER_UNAVAILABLE")):
        return True
    return (r in FROZEN_REASONS or r in MANUAL_REASONS
            or r in STRUCTURAL_REASONS or r in TRANSIENT_REASONS)


# ---------------------------------------------------------------------
# Backoff schedule
# ---------------------------------------------------------------------
BACKOFF_SCHEDULE: Tuple[Tuple[int, int], ...] = (
    (2, 1 * 3600),
    (5, 3 * 3600),
    (10, 6 * 3600),
    (20, 12 * 3600),
)
BACKOFF_CAP_SECONDS = 24 * 3600


def backoff_seconds(attempt_count: int) -> int:
    """Deterministic staged backoff for a transient failure.

    attempts 1-2 -> 1h; 3-5 -> 3h; 6-10 -> 6h; 11-20 -> 12h; 21+ -> 24h (cap).
    """
    n = max(1, int(attempt_count or 0))
    for threshold, seconds in BACKOFF_SCHEDULE:
        if n <= threshold:
            return seconds
    return BACKOFF_CAP_SECONDS


def next_retry_at(now: dt.datetime, attempt_count: int) -> dt.datetime:
    """Persisted absolute retry time (survives process restart)."""
    return now + dt.timedelta(seconds=backoff_seconds(attempt_count))


def provider_backoff_seconds(consecutive_failures: int,
                             *, access_denial: bool = False) -> int:
    """Provider-family backoff. Access denials escalate faster than glitches."""
    n = max(1, int(consecutive_failures or 0))
    base = backoff_seconds(n)
    if access_denial:
        # A persistent 403 must not be retried on a fixed 24h window forever;
        # widen the window so the escalation to MANUAL_REVIEW is reached.
        base = min(base * 2, 7 * 24 * 3600)
    return base


# ---------------------------------------------------------------------
# Eligibility decision
# ---------------------------------------------------------------------
def effective_class(row) -> str:
    """Stored retry class, with legacy NULL treated as RETRY_ELIGIBLE."""
    return getattr(row, "retry_class", None) or RETRY_ELIGIBLE


def _time_gate_open(row, now: dt.datetime) -> bool:
    nra = getattr(row, "next_retry_at", None)
    if nra is None:
        return True
    if nra.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    elif nra.tzinfo is not None and now.tzinfo is None:
        nra = nra.replace(tzinfo=None)
    return nra <= now


def is_retry_eligible(row, now: dt.datetime, *,
                      provider_available: Optional[bool] = None,
                      anchor_override=None) -> bool:
    """Whether this single row may be *fetched* this cycle.

    This says nothing about writability -- the historical gate decides that
    independently and may still defer or reject a successfully fetched row.

    ``anchor_override`` is a callable(row) -> bool supplied ONLY by
    Phase C.2 deferred recovery. It returns True only when:
      - effective_class(row) == PROVENANCE_UNVERIFIED
      - exact (symbol, source_definition) exists in SOURCE_DEFINITION_TRUST_START
      - D >= trust_start
      - expected P exists and P >= trust_start
      - stored P qualifies as anchor-derived provenance

    Default behavior (anchor_override=None) is unchanged:
    PROVENANCE_UNVERIFIED remains non-retryable.
    """
    if getattr(row, "status", None) == STATUS_RESOLVED:
        return False
    if provider_available is False:
        return False
    cls = effective_class(row)
    # A RETRY_PATH_DEFECT row parked under an exact KNOWN-FIXED defect is
    # released for bounded re-evaluation by the fixed handler. Unrelated
    # defect states stay parked (never globally released).
    if cls == RETRY_PATH_DEFECT:
        if known_fixed_defect(getattr(row, "last_error_class", None)):
            return True
        return False
    if cls in CONDITION_TRIGGERED_CLASSES:
        # Phase C.2: narrow-scope anchor override — ONLY for PROVENANCE_UNVERIFIED
        if cls == PROVENANCE_UNVERIFIED and anchor_override is not None \
                and anchor_override(row):
            return True  # Immediately due — no time gate
        return False
    if cls == RETRY_ELIGIBLE:
        return True
    if cls == BACKOFF_ACTIVE:
        return _time_gate_open(row, now)
    return False


def blocked_prerequisites(rows: Sequence, now: Optional[dt.datetime] = None,
                          *, anchor_override=None
                          ) -> Set[Tuple[str, dt.date]]:
    """(symbol, session_date) keys of rows that are blocking this cycle.

    A row blocks its dependents when it cannot resolve this cycle:

      * structural / frozen / manual / provider / defect classes block
        unconditionally;
      * a BACKOFF_ACTIVE row blocks only while its backoff has not elapsed.

    Phase C.2: if ``anchor_override`` is supplied and returns True for a
    CONDITION_TRIGGERED row, that row does NOT block dependents (it is
    runtime-eligible).
    """
    out: Set[Tuple[str, dt.date]] = set()
    for r in rows:
        if getattr(r, "status", None) == STATUS_RESOLVED:
            continue
        cls = effective_class(r)
        if cls in CONDITION_TRIGGERED_CLASSES:
            # Phase C.2: if anchor_override says this row is runtime-eligible,
            # it does NOT block dependents
            if anchor_override is not None and anchor_override(r):
                continue
            out.add((getattr(r, "symbol", None), getattr(r, "session_date", None)))
        elif cls == BACKOFF_ACTIVE:
            if now is None or not _time_gate_open(r, now):
                out.add((getattr(r, "symbol", None),
                         getattr(r, "session_date", None)))
    return out


def has_blocked_prerequisite(symbol: str, session_date: dt.date,
                             blocked: Iterable[Tuple[str, dt.date]]) -> bool:
    """True if an *earlier* same-symbol row is blocked (P blocks D)."""
    for sym, d in blocked:
        if sym == symbol and d is not None and session_date is not None \
                and d < session_date:
            return True
    return False


def filter_eligible(rows: Sequence, now: dt.datetime, *,
                    provider_available: Optional[bool] = None,
                    unavailable_keys: Optional[Set[Tuple[str, str]]] = None,
                    anchor_override=None) -> List:
    """Rows eligible for a recovery attempt this cycle, dependency-ordered.

    Applies, in order:
      1. provider gate   -- the row's (provider, data_class) must be available
      2. class gate      -- structural / frozen / manual / defect never eligible
      3. time gate       -- next_retry_at must have elapsed
      4. dependency gate -- D suppressed while an earlier same-symbol row
                            is blocked (never hammer D when P cannot resolve)

    Phase C.2: ``anchor_override`` is forwarded to ``blocked_prerequisites``
    and ``is_retry_eligible`` so that anchor-covered PU rows can be
    runtime-eligible without changing persisted state.
    """
    blocked = blocked_prerequisites(rows, now, anchor_override=anchor_override)
    out = []
    for r in rows:
        sym = getattr(r, "symbol", None)
        if unavailable_keys is not None:
            if provider_key(sym) in unavailable_keys:
                continue
        if not is_retry_eligible(r, now,
                                 provider_available=provider_available,
                                 anchor_override=anchor_override):
            continue
        if has_blocked_prerequisite(sym, getattr(r, "session_date", None),
                                    blocked):
            continue
        out.append(r)
    out.sort(key=lambda r: (getattr(r, "session_date", None),
                            getattr(r, "symbol", "")))
    return out


def classify_bucket(row, now: dt.datetime, *,
                    blocked: Optional[Set[Tuple[str, dt.date]]] = None,
                    unavailable_keys: Optional[Set[Tuple[str, str]]] = None
                    ) -> str:
    """Mutually-exclusive bucket for one row (for reporting/simulation).

    Buckets (exactly one per row):
      RESOLVED / FROZEN / STRUCTURAL / MANUAL / PROVIDER_UNAVAILABLE /
      RETRY_PATH_DEFECT / PREREQUISITE_BLOCKED / TIME_BACKOFF / ELIGIBLE
    """
    if getattr(row, "status", None) == STATUS_RESOLVED:
        return "RESOLVED"
    cls = effective_class(row)
    if cls == EXPECTED_FROZEN:
        return "FROZEN"
    if cls in (PROVENANCE_UNVERIFIED, SOURCE_DEFINITION_UNRESOLVED):
        return "STRUCTURAL"
    if cls == RETRY_PATH_DEFECT:
        return "RETRY_PATH_DEFECT"
    if cls == MANUAL_REVIEW_REQUIRED:
        return "MANUAL"
    if unavailable_keys is not None and \
            provider_key(getattr(row, "symbol", None)) in unavailable_keys:
        return "PROVIDER_UNAVAILABLE"
    if cls == PROVIDER_UNAVAILABLE:
        return "PROVIDER_UNAVAILABLE"
    if cls == BACKOFF_ACTIVE:
        if not _time_gate_open(row, now):
            return "TIME_BACKOFF"
    if blocked and has_blocked_prerequisite(
            getattr(row, "symbol", None), getattr(row, "session_date", None),
            blocked):
        return "PREREQUISITE_BLOCKED"
    return "ELIGIBLE"


# ---------------------------------------------------------------------
# Manual / condition-triggered release
# ---------------------------------------------------------------------
def release_on_condition_change(row, *, now: dt.datetime,
                                condition_changed: bool = False) -> bool:
    """Return True if a structural/defect/manual row should become retryable.

    Only an explicit condition change (source-definition / provenance config
    change, code fix, provider recovery, or manual reset) releases these rows.
    Elapsed time alone never does.
    """
    cls = effective_class(row)
    if cls not in CONDITION_TRIGGERED_CLASSES:
        return False
    return bool(condition_changed)


def manual_reset_allowed(row) -> bool:
    """Structural, defect and manual rows may be reset; EXPECTED_FROZEN may
    only be released by an explicit unfreeze decision."""
    return effective_class(row) in (
        SOURCE_DEFINITION_UNRESOLVED,
        PROVENANCE_UNVERIFIED,
        MANUAL_REVIEW_REQUIRED,
        PROVIDER_UNAVAILABLE,
        RETRY_PATH_DEFECT,
    )
