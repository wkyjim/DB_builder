from hourly_ai_macro_run import _etf_price_change


def test_implausible_etf_change_is_retained_but_not_used():
    result = _etf_price_change(141.7)
    assert result["raw_pct_chg"] == 141.7
    assert result["pct_chg_used"] is False
    assert "implausible" in result["pct_chg_warning"]


def test_valid_extreme_is_retained_and_used():
    result = _etf_price_change(20.0)
    assert result["raw_pct_chg"] == 20.0
    assert result["pct_chg_used"] is True
    assert result["pct_chg_warning"] == "ok"
