from db_builder.comparison_validation import validate_comparisons, build_peer_groups

C = [
    {"ticker": "MU", "sector": "Information Technology"},
    {"ticker": "SNDK", "sector": "Information Technology"},
    {"ticker": "APP", "sector": "Communication Services"},
]


def test_meaningful_same_sector_comparison():
    assert validate_comparisons([{"ticker": "MU"}], [{
        "ticker": "MU", "peer": "SNDK", "dimension": "growth durability",
        "evidence_ids": ["SEC:MU:revenue:Q:2026-09-03", "SEC:SNDK:revenue:Q:2026-10-02"],
        "thesis": "MU has a more diversified memory mix than SNDK, while SNDK carries higher segment concentration and a pending split basis."
    }], C) == []


def test_unrelated_or_generic_comparison_is_rejected():
    flags = validate_comparisons([{"ticker": "MU"}], [{
        "ticker": "MU", "peer": "APP", "dimension": "generic",
        "evidence_ids": [], "thesis": "APP is a better business."
    }], C)
    assert "unrelated_peer:MU:APP" in flags
    assert any(x.startswith("comparison_without_evidence") for x in flags)


def test_missing_comparison_for_peer_group():
    assert "missing_comparison:MU" in validate_comparisons([{"ticker": "MU"}], [], C)


def test_peer_groups_are_dynamic():
    assert build_peer_groups(C)["Information Technology"] == ["MU", "SNDK"]
