from __future__ import annotations

from db_builder.market_dispersion import compute_broad_market_dispersion, compute_sector_constituent_dispersion


def test_broad_market_dispersion_detects_sector_range_and_size_style_spread():
    rows = [
        {"ticker": "XLK", "return_20d": 10, "return_60d": 20},
        {"ticker": "XLE", "return_20d": -5, "return_60d": -10},
        {"ticker": "XLV", "return_20d": 2, "return_60d": 3},
        {"ticker": "RSP", "return_20d": 4},
        {"ticker": "SPY", "return_20d": 1},
        {"ticker": "IWM", "return_20d": -2},
        {"ticker": "IWF", "return_20d": 6},
        {"ticker": "IWD", "return_20d": 2},
        {"ticker": "QQQ", "return_20d": 5},
    ]

    result = compute_broad_market_dispersion(rows)

    assert result["sector_20d"]["range"] == 15
    assert result["sector_20d"]["leader"] == "XLK"
    assert result["sector_20d"]["laggard"] == "XLE"
    assert result["size_style"][0]["signal"] == "broader participation"


def test_sector_constituent_dispersion_requires_minimum_constituents():
    rows = [
        {
            "ticker": f"T{i}",
            "sector": "Information Technology",
            "return_20d": i,
            "close": 100 + i,
            "ma_50": 90,
            "ma_200": 80,
        }
        for i in range(10)
    ]
    rows.append(
        {
            "ticker": "E1",
            "sector": "Energy",
            "return_20d": 1,
            "close": 100,
            "ma_50": 90,
            "ma_200": 80,
        }
    )

    result = compute_sector_constituent_dispersion(rows, min_count=10)
    by_sector = {row["sector"]: row for row in result}

    assert by_sector["Information Technology"]["status"] == "ok"
    assert by_sector["Information Technology"]["constituent_count"] == 10
    assert by_sector["Information Technology"]["breadth_50d_pct"] == 100
    assert by_sector["Energy"]["status"] == "insufficient mapped constituents"
