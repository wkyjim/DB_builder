from datetime import date

from db_builder.positioning_flow_signals import (
    build_cot_signals,
    build_finra_signals,
    enrich_etf_flow_dashboard_rows,
    filter_meaningful_short_pressure_rows,
    short_pressure_implication,
)


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


def test_short_pressure_filter_keeps_market_implication_universe():
    rows = [
        {
            "asset_id": "EBND",
            "source": "FINRA short-sale volume",
            "signal_value": 0.87,
            "z_score": 4.0,
        },
        {
            "asset_id": "SPY",
            "source": "FINRA short-sale volume",
            "signal_value": 0.52,
            "z_score": 1.1,
            "pct_chg": -0.2,
        },
        {
            "asset_id": "NVDA",
            "source": "FINRA short-sale volume",
            "signal_value": 0.58,
            "z_score": 2.8,
            "pct_chg": -3.0,
        },
        {
            "asset_id": "XLK",
            "source": "FINRA short-sale volume",
            "signal_value": 0.61,
            "z_score": 2.1,
            "pct_chg": 0.6,
        },
    ]

    filtered = filter_meaningful_short_pressure_rows(rows)
    tickers = [row["asset_id"] for row in filtered]

    assert "EBND" not in tickers
    assert tickers[0] == "NVDA"
    assert set(tickers) == {"SPY", "NVDA", "XLK"}
    assert filtered[0]["asset_group"] == "High Beta Chips / Mag 7"
    assert "elevated short-sale pressure" in filtered[0]["market_implication"]


def test_short_pressure_implication_handles_index_context_without_elevated_z():
    implication = short_pressure_implication(
        {"asset_id": "QQQ", "signal_value": 0.55, "z_score": 0.5, "pct_chg": 1.0}
    )

    assert "Broad Index / Big Tech" in implication
    assert "not statistically elevated" in implication


def test_short_pressure_filter_includes_high_beta_chip_names():
    filtered = filter_meaningful_short_pressure_rows(
        [
            {"asset_id": "MU", "signal_value": 0.45, "z_score": 1.4, "pct_chg": -1.2},
            {"asset_id": "NVDA", "signal_value": 0.48, "z_score": 1.1, "pct_chg": 0.8},
            {"asset_id": "SPCX", "signal_value": 0.42, "z_score": 0.8, "pct_chg": -0.3},
            {"asset_id": "RANDOM", "signal_value": 0.9, "z_score": 5.0, "pct_chg": -5.0},
        ]
    )

    by_ticker = {row["asset_id"]: row for row in filtered}

    assert set(by_ticker) == {"MU", "NVDA", "SPCX"}
    assert by_ticker["MU"]["asset_group"] == "High Beta Chips / Memory"
    assert by_ticker["NVDA"]["asset_group"] == "High Beta Chips / Mag 7"
    assert by_ticker["SPCX"]["asset_group"] == "High Beta / Space"


def test_etf_flow_dashboard_rows_add_display_names_and_simple_comments():
    rows = enrich_etf_flow_dashboard_rows(
        [
            {"asset_id": "SPY", "signal_value": -1000, "z_score": 5000, "source": "ETF daily data"},
            {"asset_id": "XLK", "signal_value": 2000, "z_score": -3000, "source": "ETF daily data"},
            {"asset_id": "QQQ", "signal_value": 0, "z_score": None, "source": "ETF daily data"},
            {"asset_id": "TLT", "signal_value": 3000, "z_score": 4000, "source": "ETF daily data"},
            {"asset_id": "IBIT", "signal_value": -3000, "z_score": -4000, "source": "ETF daily data"},
            {
                "asset_id": "IJH",
                "signal_value": 0,
                "z_score": 4000,
                "flow_method": "shares_delta_zero_no_creation_redemption",
                "source": "ETF daily data",
            },
            {"asset_id": "SMH", "signal_value": 0, "z_score": None, "source": "ETF daily data"},
            {"asset_id": "SOXX", "signal_value": 0, "z_score": None, "source": "ETF daily data"},
        ]
    )

    by_ticker = {row["asset_id"]: row for row in rows}

    assert "QQQ" not in by_ticker
    assert "SMH" in by_ticker
    assert "SOXX" in by_ticker
    assert by_ticker["SPY"]["display_name"] == "SPY - Broad Equity"
    assert by_ticker["SPY"]["flow_bucket"] == "Broad Market ETF Flows"
    assert by_ticker["SPY"]["flow_comment"] == "1D outflow; 5D inflow."
    assert by_ticker["IJH"]["flow_comment"] == "No 1D shares-outstanding change reported; 5D inflow."
    assert by_ticker["TLT"]["display_name"] == "TLT - Long Duration Treasury"
    assert by_ticker["TLT"]["flow_bucket"] == "Fixed Income / Macro ETF Flows"
    assert by_ticker["IBIT"]["display_name"] == "IBIT - Bitcoin"
    assert by_ticker["IBIT"]["flow_bucket"] == "Fixed Income / Macro ETF Flows"
    assert by_ticker["XLK"]["display_name"] == "XLK - Technology"
    assert by_ticker["XLK"]["flow_bucket"] == "Sector / Thematic ETF Flows"
    assert by_ticker["SMH"]["flow_comment"] == "Issuer-backed snapshot saved; flow history is still building."
