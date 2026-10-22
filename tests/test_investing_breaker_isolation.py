"""Tests for per-symbol Investing.com circuit breaker isolation.

Validates that one symbol's 403 does not block unrelated Investing.com symbols.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch


def _load_module():
    """Load macro_data_fetch.py as a module."""
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "macro_data_fetch.py"
    spec = importlib.util.spec_from_file_location("macro_data_fetch", script_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["macro_data_fetch"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_bz_f_403_opens_bz_f_breaker_only():
    """BZ=F 403 opens BZ=F breaker only."""
    mod = _load_module()
    mod.reset_investiny_provider_state()
    assert mod.is_investiny_symbol_available("BZ=F"), "BZ=F should start available"
    mod.record_investiny_symbol_failure("BZ=F", "HTTP 403")
    assert not mod.is_investiny_symbol_available("BZ=F"), "BZ=F should be blocked"


def test_us2ytx_fetches_after_bz_f_breaker():
    """After BZ=F breaker opens, US2YT=X still fetches."""
    mod = _load_module()
    mod.reset_investiny_provider_state()
    mod.record_investiny_symbol_failure("BZ=F", "HTTP 403")
    assert mod.is_investiny_symbol_available("US2YT=X"), "US2YT=X should still be available"


def test_us30ytx_fetches_after_bz_f_breaker():
    """After BZ=F breaker opens, US30YT=X still fetches."""
    mod = _load_module()
    mod.reset_investiny_provider_state()
    mod.record_investiny_symbol_failure("BZ=F", "HTTP 403")
    assert mod.is_investiny_symbol_available("US30YT=X"), "US30YT=X should still be available"


def test_hk50_fetches_after_bz_f_breaker():
    """After BZ=F breaker opens, HK50 still fetches."""
    mod = _load_module()
    mod.reset_investiny_provider_state()
    mod.record_investiny_symbol_failure("BZ=F", "HTTP 403")
    assert mod.is_investiny_symbol_available("HK50"), "HK50 should still be available"


def test_kor200c1_fetches_after_bz_f_breaker():
    """After BZ=F breaker opens, KOR200c1 still fetches."""
    mod = _load_module()
    mod.reset_investiny_provider_state()
    mod.record_investiny_symbol_failure("BZ=F", "HTTP 403")
    assert mod.is_investiny_symbol_available("KOR200c1"), "KOR200c1 should still be available"


def test_cihc1_fetches_after_bz_f_breaker():
    """After BZ=F breaker opens, CIHc1 still fetches."""
    mod = _load_module()
    mod.reset_investiny_provider_state()
    mod.record_investiny_symbol_failure("BZ=F", "HTTP 403")
    assert mod.is_investiny_symbol_available("CIHc1"), "CIHc1 should still be available"


def test_us10ytx_403_does_not_block_other_usnytx():
    """US10YT=X 403 does not block other USnYT=X maturities."""
    mod = _load_module()
    mod.reset_investiny_provider_state()
    mod.record_investiny_symbol_failure("US10YT=X", "HTTP 403")
    for sym in ["US2YT=X", "US3YT=X", "US5YT=X", "US7YT=X", "US20YT=X", "US30YT=X"]:
        assert mod.is_investiny_symbol_available(sym), f"{sym} should still be available"


def test_breaker_cooldown_works():
    """One symbol breaker cooldown works - symbol stays blocked."""
    mod = _load_module()
    mod.reset_investiny_provider_state()
    mod.record_investiny_symbol_failure("BZ=F", "HTTP 403")
    assert not mod.is_investiny_symbol_available("BZ=F")
    mod.record_investiny_symbol_failure("BZ=F", "HTTP 403")
    assert not mod.is_investiny_symbol_available("BZ=F"), "BZ=F should stay blocked"


def test_breaker_reset_after_new_cycle():
    """Breaker reset works after new cycle."""
    mod = _load_module()
    mod.reset_investiny_provider_state()
    mod.record_investiny_symbol_failure("BZ=F", "HTTP 403")
    assert not mod.is_investiny_symbol_available("BZ=F")
    mod.reset_investiny_provider_state()
    assert mod.is_investiny_symbol_available("BZ=F"), "BZ=F should be available after reset"


def test_global_investing_breaker_no_longer_exists():
    """Global Investing breaker no longer exists."""
    mod = _load_module()
    assert not hasattr(mod, "_provider_breaker_open"), "Global _provider_breaker_open should not exist"
    assert not hasattr(mod, "is_investiny_provider_available"), "Global is_investiny_provider_available should not exist"
    assert not hasattr(mod, "record_investiny_provider_failure"), "Global record_investiny_provider_failure should not exist"
