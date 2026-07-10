from datetime import date, timedelta

from db_builder.finra_short_volume import compute_finra_features, parse_finra_short_volume_text


def test_parse_finra_short_volume_text():
    text = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n20260702|AAPL|500|0|1000|Q\n"

    rows = parse_finra_short_volume_text(text)

    assert rows[0]["trade_date"].isoformat() == "2026-07-02"
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["short_volume_ratio"] == 0.5


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

