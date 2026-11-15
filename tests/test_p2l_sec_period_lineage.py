from datetime import date

from scripts.sec_fundamentals_ingest import normalize


def _row(metric, tag, period_type, start, end, value, accession, fp="FY"):
    return {
        "metric": metric, "tag": tag, "period_type": period_type,
        "start_date": start, "end_date": end, "value": value, "unit": "USD",
        "fy": 2026, "fp": fp, "accession": accession, "filed": "2026-11-01",
        "available_at": date(2026, 11, 1), "source_url": "https://www.sec.gov/",
        "retrieved_at": None,
    }


def test_normalized_quarter_keeps_original_fiscal_label_and_period_dates():
    raw = [
        _row("revenue", "Revenues", "Y", "2025-09-28", "2026-09-26", 145.0, "acc-y", "FY"),
        _row("revenue", "Revenues", "Q", "2026-03-29", "2026-06-27", 34.0, "acc-q", "Q2"),
    ]
    rows = normalize("TEST", "0000000001", raw, as_of="2026-11-13")
    q = next(x for x in rows if x["metric"] == "revenue" and x["period_type"] == "Q")
    assert q["metric_period_type"] == "Q"
    assert q["fiscal_period_original"] == "Q2"
    assert str(q["start_date"]) == "2026-03-29"
    assert str(q["end_date"]) == "2026-06-27"
    assert q["derivation_method"] == "reported_quarter"


def test_annual_lineage_is_distinct_from_metric_period_type():
    raw = [_row("revenue", "Revenues", "Y", "2025-09-28", "2026-09-26", 145.0, "acc-y", "FY")]
    rows = normalize("TEST", "0000000001", raw, as_of="2026-11-13")
    y = next(x for x in rows if x["metric"] == "revenue" and x["period_type"] == "Y")
    assert y["metric_period_type"] == "Y"
    assert y["fiscal_period_original"] == "FY"
    assert str(y["end_date"]) == "2026-09-26"


def test_fy_minus_nine_months_normalizes_to_q4_but_keeps_original_fy():
    raw = [
        _row("revenue", "Revenues", "Y", "2025-09-28", "2026-09-26", 145.0, "acc-y", "FY"),
        _row("revenue", "Revenues", "YTD", "2025-09-28", "2026-06-27", 116.0, "acc-9m", "FY"),
    ]
    rows = normalize("TEST", "0000000001", raw, as_of="2026-11-13")
    q4 = next(x for x in rows if x["metric"] == "revenue" and x["period_type"] == "Q")
    assert q4["fiscal_period_original"] == "FY"
    assert q4["metric_period_type"] == "Q"
    assert q4["metric_fiscal_period"] == "Q4"
    assert q4["derivation_method"] == "fy_minus_9m"
