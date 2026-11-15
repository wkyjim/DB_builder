from db_builder.sec_periods import resolve_period, render_fact


def test_mu_derived_q4_is_verified_as_q4():
    row = {"ticker": "MU", "metric": "revenue", "period_type": "Q",
           "period": "2026-09-03", "value": 54.229, "unit": "USD",
           "derivation_method": "fy_minus_9m", "fiscal_period_original": "FY",
           "metric_fiscal_period": "Q4"}
    p = resolve_period(row)
    assert p["status"] == "verified"
    assert p["label"] == "Q4"
    assert "Q4" in render_fact(row)


def test_reported_q1_is_verified():
    row = {"ticker": "SNDK", "metric": "revenue", "period_type": "Q",
           "period": "2026-10-02", "value": 11.031, "unit": "USD",
           "derivation_method": "reported_quarter", "fiscal_period_original": "Q1",
           "metric_fiscal_period": "Q1"}
    assert resolve_period(row)["label"] == "Q1"


def test_ambiguous_original_fy_reported_quarter_is_quarantined():
    row = {"ticker": "TEST", "metric": "revenue", "period_type": "Q",
           "period": "2026-09-30", "value": 1.0, "unit": "USD",
           "derivation_method": "reported_quarter", "fiscal_period_original": "FY",
           "metric_fiscal_period": "Q4"}
    p = resolve_period(row)
    assert p["status"] == "ambiguous"
    assert p["label"] is None
    assert "UNAVAILABLE" in render_fact(row)
