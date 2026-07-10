from datetime import date

from db_builder.positioning_flow_signals import build_cot_signals, build_finra_signals


def test_build_cot_signals_interprets_crowded_long():
    signals = build_cot_signals(
        [
            {
                "report_date": date(2026, 6, 30),
                "contract_code": "SPX",
                "asset_id": "SPX",
                "asset_class": "equity_index",
                "net_position_pct_oi": 0.25,
                "net_position_z_3y": 2.2,
                "percentile_3y": 0.98,
                "crowded_long": True,
                "crowded_short": False,
            }
        ]
    )

    assert signals[0]["asset_id"] == "SPX"
    assert signals[0]["source"] == "CFTC COT"
    assert "Crowded long" in signals[0]["interpretation"]


def test_build_finra_signals_uses_short_sale_volume_language():
    signals = build_finra_signals(
        [
            {
                "trade_date": date(2026, 7, 2),
                "ticker": "NVDA",
                "short_volume_ratio": 0.55,
                "short_volume_z_60d": 2.5,
                "short_pressure_flag": True,
                "short_covering_candidate": False,
                "bearish_pressure_flag": True,
            }
        ]
    )

    assert signals[0]["asset_id"] == "NVDA"
    assert signals[0]["source"] == "FINRA short-sale volume"
    assert "bearish pressure" in signals[0]["interpretation"].lower()
