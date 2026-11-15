from db_builder.research_validation import validate_narrative


def _ev(**kw):
    base = {"id": "SEC:MU:revenue:Q:2026-09-03", "ticker": "MU", "metric": "revenue",
            "canonical_period": "Q4", "kind": "SEC_ACTUAL", "value": 54_229_000_000,
            "unit": "USD"}
    base.update(kw)
    return base


def test_verified_q4_value_is_accepted():
    assert validate_narrative("Q4 revenue was 54.229 billion USD.", [_ev()], "MU") == []


def test_incorrect_quarter_is_rejected():
    flags = validate_narrative("Q3 revenue was 54.229 billion USD.", [_ev()], "MU")
    assert "unsupported_period:MU:Q3" in flags


def test_unsupported_number_is_rejected():
    flags = validate_narrative("Q4 revenue was 99.999 billion USD.", [_ev()], "MU")
    assert any(x.startswith("unsupported_number:MU:") for x in flags)


def test_price_target_is_rejected():
    flags = validate_narrative("Q4 revenue was 54.229 billion USD; price target 900.", [_ev()], "MU")
    assert "unsupported_target_or_probability" in flags
