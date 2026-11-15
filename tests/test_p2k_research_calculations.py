from db_builder.research_calculations import (
    evaluate_calculation, growth_pct, margin_pct, parse_fiscal_label, pct_to_bps,
    usd_billions_to_millions,
)


def test_mu_q3_growth_is_345_8_not_300():
    assert round(growth_pct(41.46, 9.30), 1) == 345.8
    assert round(growth_pct(41.46, 9.30), 0) != 300


def test_rounding_vendor_and_actual_values():
    assert round(207.76048, 2) == 207.76
    assert round(74.33, 2) == 74.33


def test_fiscal_year_string_and_percentage_units():
    assert parse_fiscal_label("FY2027") == "FY2027"
    assert parse_fiscal_label("Q3 FY2026") == "Q3 FY2026"
    with __import__("pytest").raises(ValueError):
        parse_fiscal_label("027")
    assert pct_to_bps(3.75) == 375.0
    assert evaluate_calculation("growth_pct(41.46, 9.30)", {}) == growth_pct(41.46, 9.30)
    assert round(evaluate_calculation("max(0.1, 0.2)", {}), 2) == 0.20


def test_unit_conversion_and_margin():
    assert usd_billions_to_millions(12.2) == 12200.0
    assert round(margin_pct(8.78, 11.031), 1) == 79.6


def test_formula_rejects_unknown_inputs_and_attributes():
    import pytest
    with pytest.raises(Exception):
        evaluate_calculation("(a - b) / c * 100", {"a": 1})
    with pytest.raises(Exception):
        evaluate_calculation("__import__('os')", {})
