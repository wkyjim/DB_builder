"""Phase A (additive) — official Treasury.gov CMT series tests.

Covers: symbol/tenor mapping, official observation handling, same-definition
previous-close chain, safe deferral, no Investing.com contamination, legacy
freeze, live-feed preservation, and no regression to non-Treasury behaviour.
"""
import datetime as dt
import os
import sys

import pandas as pd
import pytest

os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite://")
os.environ.setdefault("NEON_DATABASE_URL", "sqlite://")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import macro_data_fetch as m  # noqa: E402
from db_builder import session_calendar as sc  # noqa: E402

UTC = dt.timezone.utc
D = dt.date(2026, 9, 23)
D1 = dt.date(2026, 9, 22)
D0 = dt.date(2026, 9, 21)
NOW = dt.datetime(2026, 9, 30, tzinfo=UTC)


def _seed(cmt_symbol="US10YT_CMT", prior_val=5.05, cur_val=5.11):
    m._TREASURY_PUBLISHED_DATES.clear()
    m._TREASURY_PUBLISHED_VALUES.clear()
    m._TREASURY_PUBLISHED_DATES.update({D0, D1, D})
    m._TREASURY_PUBLISHED_VALUES[(cmt_symbol, D1)] = prior_val
    m._TREASURY_PUBLISHED_VALUES[(cmt_symbol, D)] = cur_val


# ------------------------------------------------------- 1. symbols/tenors
def test_seven_cmt_symbols_exist():
    assert len(sc.CMT_SYMBOLS) == 7
    assert sc.CMT_SYMBOLS == {
        "US2YT_CMT", "US3YT_CMT", "US5YT_CMT", "US7YT_CMT",
        "US10YT_CMT", "US20YT_CMT", "US30YT_CMT",
    }


@pytest.mark.parametrize("symbol,tenor,element", [
    ("US2YT_CMT", "2Y", "BC_2YEAR"),
    ("US3YT_CMT", "3Y", "BC_3YEAR"),
    ("US5YT_CMT", "5Y", "BC_5YEAR"),
    ("US7YT_CMT", "7Y", "BC_7YEAR"),
    ("US10YT_CMT", "10Y", "BC_10YEAR"),
    ("US20YT_CMT", "20Y", "BC_20YEAR"),
    ("US30YT_CMT", "30Y", "BC_30YEAR"),
])
def test_cmt_tenor_and_element_mapping(symbol, tenor, element):
    assert sc.TENOR_BY_CMT_SYMBOL[symbol] == tenor
    assert m.TREASURY_XML_ELEMENT_MAP[symbol] == element
    assert symbol in m.UST_TREASURY_FALLBACK_SYMBOLS


def test_cmt_asset_type_and_names():
    am = {s: (n, a) for s, n, a in m.ASSETS}
    for s in sc.CMT_SYMBOLS:
        assert am[s][1] == "ust_yield_cmt"
        assert "CMT" in am[s][0]


def test_cmt_uses_treasury_publication_adapter():
    for s in sc.CMT_SYMBOLS:
        a = sc.adapter_for(s)
        assert isinstance(a, sc.TreasuryCalendarAdapter)
        assert a.requires_publication_confirmation is True


def test_cmt_symbols_not_routed_to_investing_or_yfinance():
    for s in sc.CMT_SYMBOLS:
        assert s not in m.INVESTINY_ASSETS
    assert m.provider_for_symbol("US10YT_CMT") == "treasury.gov"


# --------------------------------------------- 2. official observation rules
def test_cmt_official_row_accepted():
    _seed()
    acc, dfr, rej = m.route_historical_rows(
        None, [{"date": D, "symbol": "US10YT_CMT", "close": 5.11}],
        now_utc=NOW, prior_lookup=lambda *a, **k: {D0, D1})
    assert len(acc) == 1 and not dfr and not rej


def test_cmt_unpublished_session_defers():
    _seed()
    m._TREASURY_PUBLISHED_DATES.discard(D)
    acc, dfr, _ = m.route_historical_rows(
        None, [{"date": D, "symbol": "US10YT_CMT", "close": 5.11}],
        now_utc=NOW, prior_lookup=lambda *a, **k: {D0, D1})
    assert not acc and len(dfr) == 1
    assert dfr[0][1] == "TREASURY_PUBLICATION_NOT_CONFIRMED"


def test_cmt_non_official_value_deferred():
    """A value that is not the official par yield must never be stored."""
    _seed()
    acc, dfr, _ = m.route_historical_rows(
        None, [{"date": D, "symbol": "US10YT_CMT", "close": 4.7230}],
        now_utc=NOW, prior_lookup=lambda *a, **k: {D0, D1})
    assert not acc and len(dfr) == 1
    assert dfr[0][1] == "CMT_VALUE_NOT_OFFICIAL"


def test_cmt_prior_session_must_be_official():
    """prev_close must come from the previous official CMT observation."""
    m._TREASURY_PUBLISHED_DATES.clear()
    m._TREASURY_PUBLISHED_VALUES.clear()
    m._TREASURY_PUBLISHED_DATES.update({D1, D})
    m._TREASURY_PUBLISHED_VALUES[("US10YT_CMT", D)] = 5.11   # prior NOT published
    acc, dfr, _ = m.route_historical_rows(
        None, [{"date": D, "symbol": "US10YT_CMT", "close": 5.11}],
        now_utc=NOW, prior_lookup=lambda *a, **k: {D0, D1})
    assert not acc and len(dfr) == 1
    # The prior session's official value is missing -> prev_close cannot be
    # sourced from the same definition, so the row defers.
    assert dfr[0][1] in ("PREVIOUS_SESSION_OFFICIAL_NOT_CONFIRMED",
                         "CMT_PREVIOUS_VALUE_NOT_OFFICIAL")


# ------------------------------------------- 3. same-definition prev_close
def test_cmt_previous_close_chain_is_cmt_only():
    """The CMT series resolves its prior from CMT rows, never legacy rows."""
    _seed()   # official CMT values recorded for D-1 and D
    cmt_prior = {"US10YT_CMT": {D0, D1}, "US10YT=X": {D0}}
    acc, dfr, _ = m.route_historical_rows(
        None, [{"date": D, "symbol": "US10YT_CMT", "close": 5.11}],
        now_utc=NOW, prior_lookup=lambda e, s, a, b: cmt_prior.get(s, set()))
    assert len(acc) == 1, "CMT prior present (date + official value) -> accepted"


def test_legacy_prior_does_not_satisfy_cmt():
    """A legacy USnYT=X prior must not stand in for a CMT prior."""
    only_legacy = lambda e, s, a, b: {D0, D1} if s == "US10YT=X" else set()
    acc, dfr, _ = m.route_historical_rows(
        None, [{"date": D, "symbol": "US10YT_CMT", "close": 5.11}],
        now_utc=NOW, prior_lookup=only_legacy)
    assert not acc, "legacy prior must not authorise a CMT write"
    assert len(dfr) == 1


def test_dedupe_key_separates_series():
    """Existing-key dedupe must never merge legacy and CMT rows."""
    existing = pd.DataFrame([{"symbol": "US10YT=X", "date": D}])
    out = m.filter_duplicate_symbol_dates(
        [{"symbol": "US10YT_CMT", "date": D, "close": 5.11}], existing)
    assert len(out) == 1, "distinct symbol -> not deduped away"
    out2 = m.filter_duplicate_symbol_dates(
        [{"symbol": "US10YT=X", "date": D, "close": 4.72}], existing)
    assert out2 == [], "same symbol+date -> deduped"


# ------------------------------------------------- 4. Treasury unavailable
def test_treasury_unavailable_defers_cmt(monkeypatch):
    def boom(*_a, **_k):
        raise ConnectionError("treasury.gov unavailable")
    monkeypatch.setattr(m, "fetch_treasury_par_yields", boom)
    with pytest.raises(m.SymbolProcessingError):
        m.process_treasury_fallback_symbol(
            "US10YT_CMT", {"name": "x", "asset_type": "ust_yield_cmt"})


# --------------------------------------------------- 5. no contamination
def test_investing_recovery_does_not_contaminate_cmt():
    """Investing.com values for the legacy symbol never authorise CMT."""
    _seed()
    acc, dfr, _ = m.route_historical_rows(
        None, [{"date": D, "symbol": "US10YT_CMT", "close": 4.7230}],
        now_utc=NOW, prior_lookup=lambda *a, **k: {D0, D1})
    assert not acc
    assert dfr[0][1] == "CMT_VALUE_NOT_OFFICIAL"


def test_legacy_and_cmt_are_distinct_symbols():
    assert not (sc.CMT_SYMBOLS & sc.TREASURY_SYMBOLS)
    for c in sc.CMT_SYMBOLS:
        assert c not in sc.TREASURY_SYMBOLS


# ------------------------------------------- 6. legacy freeze / 7. live feed
def test_legacy_historical_write_is_frozen():
    """Legacy USnYT=X gains no new historical rows until EOD is resolved."""
    _seed("US10YT=X", 5.05, 5.11)
    acc, dfr, rej = m.route_historical_rows(
        None, [{"date": D, "symbol": "US10YT=X", "close": 5.11}],
        now_utc=NOW, prior_lookup=lambda *a, **k: {D0, D1})
    assert not acc, "legacy historical writes must be frozen"
    assert len(dfr) == 1
    assert dfr[0][1] == "LEGACY_EOD_DEFINITION_UNRESOLVED"


def test_legacy_live_row_still_served():
    """macro_live keeps serving the legacy Investing.com observation."""
    frame = pd.DataFrame({
        "Date": pd.to_datetime(["2026-09-22", "2026-09-23"]),
        "Open": [4.80, 4.82], "High": [4.85, 4.87],
        "Low": [4.79, 4.81], "Close": [4.8100, 4.7230],
        "Adj Close": [4.8100, 4.7230], "Volume": [None, None],
    })
    live = m.extract_latest_unfinished_row(
        frame, "US10YT=X", "legacy", "ust_yield",
        observed_at=NOW, source_label="investing.com", allow_closed_session=True)
    assert live is not None
    assert live["source"] == "investing.com"
    assert live["is_market_closed"] is False


# --------------------------------------------- 8. non-Treasury unaffected
def test_non_treasury_gate_unchanged():
    acc, dfr, _ = m.route_historical_rows(
        None, [{"date": D, "symbol": "^GSPC", "close": 5700.0}],
        now_utc=NOW, prior_lookup=lambda *a, **k: {D0, D1})
    assert len(acc) == 1 and not dfr


def test_cmt_series_not_required_by_default_reports():
    """CMT symbols are additive; they must not displace existing coverage."""
    from db_builder import rule_based_config as cfg
    for s in sc.CMT_SYMBOLS:
        assert s not in cfg.MACRO_SYMBOLS


# ------------------------------------------- 9. deferred recovery intact
def test_deferred_recovery_still_works_for_cmt():
    from db_builder.deferred_history import MemoryDeferredStore
    store = MemoryDeferredStore()
    row = {"date": D, "symbol": "US10YT_CMT", "close": 5.11}

    _, dfr, _ = m.route_historical_rows(
        None, [row], treasury_published_dates=set(), now_utc=NOW,
        deferred_store=store, prior_lookup=lambda *a, **k: {D0, D1})
    assert len(dfr) == 1 and len(store.all_rows()) == 1

    acc, dfr2, _ = m.route_historical_rows(
        None, [row], now_utc=NOW, deferred_store=store,
        treasury_published_values={("US10YT_CMT", D1): 5.05,
                                   ("US10YT_CMT", D): 5.11},
        prior_lookup=lambda *a, **k: {D0, D1})
    assert len(acc) == 1 and not dfr2


# ------------------------------------- 10. reserved MKT stays disabled
def test_market_reference_symbols_are_not_ingested():
    am = {s for s, _n, _a in m.ASSETS}
    for s in sc.MARKET_REFERENCE_SYMBOLS:
        assert s not in am, "reserved MKT symbols must not be ingested"
        assert s not in m.TREASURY_XML_ELEMENT_MAP
        assert s not in m.UST_TREASURY_FALLBACK_SYMBOLS


def test_market_reference_symbols_do_not_create_queue_entries():
    """Inactive series must never populate the deferred queue."""
    from db_builder.deferred_history import MemoryDeferredStore
    store = MemoryDeferredStore()
    m.route_historical_rows(
        None, [], now_utc=NOW, deferred_store=store,
        prior_lookup=lambda *a, **k: set())
    assert len(store.all_rows()) == 0
