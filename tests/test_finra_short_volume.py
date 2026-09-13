from datetime import date, timedelta

import pytest

from db_builder.finra_short_volume import compute_finra_features, parse_finra_short_volume_text


def test_parse_finra_short_volume_text():
    text = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n20260702|AAPL|500|0|1000|Q\n"

    rows = parse_finra_short_volume_text(text)

    assert rows[0]["trade_date"].isoformat() == "2026-07-02"
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["short_volume_ratio"] == 0.5
    assert rows[0]["market"] == "Q"


def test_parse_finra_daily_preserves_short_exempt_separately():
    body = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n20260702|AAPL|500|25|1000|Q\n"
    row = parse_finra_short_volume_text(body, source_file="CNMSshvol20260702.txt")[0]

    assert row["short_volume"] == 500
    assert row["short_exempt_volume"] == 25
    assert row["source_file"] == "CNMSshvol20260702.txt"


def test_mixed_case_finra_symbol_does_not_collapse_into_common_ticker():
    body = (
        "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
        "20260702|BCpC|10|0|20|Q\n"
        "20260702|BCPC|100|0|200|Q\n"
        "2\n"
    )
    rows = parse_finra_short_volume_text(body)

    assert {row["ticker"] for row in rows} == {"BCpC", "BCPC"}
    assert next(row for row in rows if row["ticker"] == "BCpC")["normalized_ticker"] is None
    assert next(row for row in rows if row["ticker"] == "BCPC")["normalized_ticker"] == "BCPC"


def test_parse_finra_daily_rejects_malformed_file():
    with pytest.raises(ValueError, match="Missing FINRA columns"):
        parse_finra_short_volume_text("Date|Symbol|ShortVolume\n20260702|AAPL|500\n")


def test_compute_finra_features_flags_short_pressure():
    rows = []
    start = date(2026, 1, 1)
    for idx in range(65):
        rows.append(
            {
                "trade_date": start + timedelta(days=idx),
                "ticker": "AAPL",
                "short_volume": 100,
                "short_exempt_volume": 0,
                "total_volume": 1000,
                "short_volume_ratio": 0.1,
                "short_volume_z_60d": None,
                "short_pressure_flag": False,
                "short_covering_candidate": False,
                "bearish_pressure_flag": False,
                "market": "CNMS",
            }
        )
    rows[-1]["short_volume_ratio"] = 0.9

    featured = compute_finra_features(rows)

    assert featured[-1]["short_volume_z_60d"] is not None
    assert featured[-1]["short_pressure_flag"] is True
