import pandas as pd

from db_builder.cot_positions import compute_cot_features, normalize_cot_dataframe


def test_normalize_cot_dataframe_maps_tracked_market():
    df = pd.DataFrame(
        [
            {
                "Market_and_Exchange_Names": "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
                "CFTC_Contract_Market_Code": "13874A",
                "Report_Date_as_YYYY-MM-DD": "2026-06-30",
                "Open_Interest_All": 1000,
                "Asset_Mgr_Positions_Long_All": 600,
                "Asset_Mgr_Positions_Short_All": 250,
                "Asset_Mgr_Positions_Spread_All": 50,
            }
        ]
    )

    rows = normalize_cot_dataframe(df)

    assert rows[0]["asset_id"] == "SPX"
    assert rows[0]["contract_code"] == "13874A"
    assert rows[0]["asset_class"] == "equity_index"
    assert rows[0]["net_position"] == 350
    assert rows[0]["net_position_pct_oi"] == 0.35


def test_compute_cot_features_flags_crowded_long():
    rows = []
    start = pd.Timestamp("2025-01-01")
    for idx in range(40):
        rows.append(
            {
                "report_date": (start + pd.Timedelta(days=7 * idx)).date(),
                "market_name": "GOLD",
                "asset_class": "commodity",
                "contract_code": "GOLD",
                "long_contracts": 100 + idx,
                "short_contracts": 100,
                "spread_contracts": 0,
                "open_interest": 1000,
                "net_position": idx,
                "net_position_pct_oi": 0.01,
                "raw_payload": {},
            }
        )
    rows[-1]["net_position_pct_oi"] = 0.50

    featured = compute_cot_features(rows)

    assert featured[-1]["contract_code"] == "GOLD"
    assert featured[-1]["net_position_z_3y"] is not None
    assert featured[-1]["crowded_long"] is True
