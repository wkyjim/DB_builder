from datetime import date, datetime, timezone

from db_builder.etf_flows import (
    ETF_FLOW_UNIVERSE,
    ETF_ISSUER_REGISTRY,
    ISHARES_ADDITIONAL_FLOW_ETFS,
    ISHARES_TOP_30_FLOW_ETFS,
    latest_potential_etf_flow_session,
    plan_etf_flow_missing_fetch,
    _snapshot_from_info,
    build_etf_flow_signals,
    flow_interpretation,
    parse_firsttrust_price_history,
    parse_blackrock_historical_rows,
    parse_spdr_navhist_workbook,
    parse_vaneck_fund_page,
    parse_ssga_fund_page,
    parse_vaneck_history_workbook,
)


def test_latest_potential_etf_flow_session_waits_until_5pm_new_york():
    assert latest_potential_etf_flow_session(
        datetime(2026, 7, 20, 20, 59, tzinfo=timezone.utc)
    ) == date(2026, 7, 17)

    assert latest_potential_etf_flow_session(
        datetime(2026, 7, 20, 21, 0, tzinfo=timezone.utc)
    ) == date(2026, 7, 20)


def test_plan_etf_flow_missing_fetch_only_selects_stale_tickers(monkeypatch):
    import db_builder.etf_flows as etf_flows

    def fake_latest_dates(engine, *, tickers):
        return {
            "SPY": date(2026, 7, 17),
            "IVV": date(2026, 7, 16),
        }

    monkeypatch.setattr(etf_flows, "fetch_latest_etf_flow_dates", fake_latest_dates)

    plan = plan_etf_flow_missing_fetch(
        object(),
        tickers=["SPY", "IVV", "SMH"],
        target_date=date(2026, 7, 17),
    )

    assert plan["tickers"] == ["IVV", "SMH"]
    assert plan["skipped_tickers"] == ["SPY"]
    assert plan["start_date"] == date(2026, 7, 17)


def test_plan_etf_flow_missing_fetch_skips_metadata_only_sources(monkeypatch):
    import db_builder.etf_flows as etf_flows

    monkeypatch.setattr(etf_flows, "fetch_latest_etf_flow_dates", lambda engine, *, tickers: {})

    plan = plan_etf_flow_missing_fetch(
        object(),
        tickers=["QQQ", "RSP", "IVV"],
        target_date=date(2026, 7, 17),
    )

    assert plan["tickers"] == ["IVV"]
    assert plan["unsupported_tickers"] == ["QQQ", "RSP"]


def test_snapshot_from_info_maps_free_yfinance_metadata():
    row = _snapshot_from_info(
        "SPY",
        {
            "totalAssets": 100_000_000,
            "sharesOutstanding": 1_000_000,
            "navPrice": 100,
            "regularMarketPrice": 101,
        },
        snapshot_date=date(2026, 7, 10),
    )

    assert row["snapshot_date"] == date(2026, 7, 10)
    assert row["date"] == date(2026, 7, 10)
    assert row["etf_ticker"] == "SPY"
    assert row["category"] == "Broad Equity"
    assert row["issuer"] == "State Street / SPDR"
    assert row["aum"] == 100_000_000
    assert row["nav"] == 100
    assert row["total_assets"] == 100_000_000
    assert row["shares_outstanding"] == 1_000_000
    assert row["flow_method"] == "snapshot_only"
    assert row["source"] == "yfinance metadata fallback"


def test_flow_interpretation_labels_proxy_not_official_flow():
    text = flow_interpretation(
        {
            "etf_ticker": "QQQ",
            "category": "Growth / Nasdaq",
            "net_fund_flow_1d": 250_000_000,
            "flow_method": "shares_delta_x_today_nav",
        }
    )

    assert "inflow estimate" in text
    assert "shares outstanding change x today's NAV" in text


def test_flow_interpretation_explains_zero_creation_redemption():
    text = flow_interpretation(
        {
            "etf_ticker": "IJH",
            "category": "Mid Caps",
            "net_fund_flow_1d": 0,
            "flow_method": "shares_delta_zero_no_creation_redemption",
        }
    )

    assert "flat flow estimate" in text
    assert "shares outstanding were unchanged" in text


def test_issuer_registry_maps_core_etfs():
    assert ETF_ISSUER_REGISTRY["XLK"]["issuer"] == "State Street / SPDR"
    assert ETF_ISSUER_REGISTRY["QQQ"]["issuer"] == "Invesco"
    assert ETF_ISSUER_REGISTRY["HYG"]["issuer"] == "BlackRock / iShares"
    assert ETF_ISSUER_REGISTRY["SMH"]["issuer"] == "VanEck"


def test_etf_flow_universe_covers_requested_market_segments():
    expected = {
        "SPY",
        "QQQ",
        "DIA",
        "IWM",
        "ACWI",
        "EFA",
        "EEM",
        "GLD",
        "XLK",
        "XLF",
        "XLV",
        "XLE",
        "XLC",
        "SMH",
        "CIBR",
        "XAR",
        "NLR",
        "GRID",
        "HYG",
        "LQD",
        "TLT",
    }

    assert expected.issubset(set(ETF_FLOW_UNIVERSE))


def test_blackrock_example_link_is_registered_for_ivv():
    registry = ETF_ISSUER_REGISTRY["IVV"]

    assert registry["issuer"] == "BlackRock / iShares"
    assert registry["adapter"] == "blackrock_fund_download"
    assert "portfolioId=239726" in registry["source_url"]


def test_soxx_uses_blackrock_issuer_download():
    registry = ETF_ISSUER_REGISTRY["SOXX"]

    assert registry["issuer"] == "BlackRock / iShares"
    assert registry["adapter"] == "blackrock_fund_download"
    assert "portfolioId=239705" in registry["source_url"]
    assert "appSubType=ISHARES" in registry["source_url"]
    assert "targetSite=us-ishares" in registry["source_url"]


def test_top_30_ishares_funds_use_blackrock_downloads():
    assert len(ISHARES_TOP_30_FLOW_ETFS) == 30

    required_segments = {
        "IVV",  # broad S&P 500
        "IWM",  # small caps
        "IEFA",  # developed MSCI
        "IEMG",  # emerging MSCI
        "AGG",  # aggregate bonds
        "GOVT",  # U.S. Treasuries
        "TLT",  # duration/rates
        "IBIT",  # bitcoin
        "IAU",  # gold
        "SOXX",  # equity sector / semiconductors
    }
    assert required_segments.issubset(ISHARES_TOP_30_FLOW_ETFS)

    for ticker, portfolio_id in ISHARES_TOP_30_FLOW_ETFS.items():
        registry = ETF_ISSUER_REGISTRY[ticker]
        assert registry["issuer"] == "BlackRock / iShares"
        assert registry["adapter"] == "blackrock_fund_download"
        assert f"portfolioId={portfolio_id}" in registry["source_url"]
        assert "appSubType=ISHARES" in registry["source_url"]
        assert "targetSite=us-ishares" in registry["source_url"]


def test_additional_ishares_sector_and_commodity_funds_are_registered():
    expected = {"SLV", "SHY", "ACWI", "HYG", "IYW", "IYF", "IYH", "IYE", "IYR", "ITA"}

    assert set(ISHARES_ADDITIONAL_FLOW_ETFS) == expected
    for ticker, portfolio_id in ISHARES_ADDITIONAL_FLOW_ETFS.items():
        registry = ETF_ISSUER_REGISTRY[ticker]
        assert registry["adapter"] == "blackrock_fund_download"
        assert f"portfolioId={portfolio_id}" in registry["source_url"]


def test_corrected_ishares_product_ids_are_registered():
    assert "portfolioId=239708" in ETF_ISSUER_REGISTRY["IWD"]["source_url"]
    assert "portfolioId=239507" in ETF_ISSUER_REGISTRY["IYE"]["source_url"]
    assert "portfolioId=239508" in ETF_ISSUER_REGISTRY["IYF"]["source_url"]


def test_smh_uses_us_vaneck_page_and_us_history():
    registry = ETF_ISSUER_REGISTRY["SMH"]

    assert registry["issuer"] == "VanEck"
    assert registry["adapter"] == "vaneck_page"
    assert "vaneck.com/us/en/investments/semiconductor-etf-smh" in registry["source_url"]
    assert "vaneck.com/us/en/investments/semiconductor-etf-smh/downloads/fundhistoprices" in registry["history_url"]


def test_parse_ssga_fund_page_extracts_nav_aum_and_derived_shares():
    html = """
    {&#34;nav&#34;:{&#34;label&#34;:&#34;NAV&#34;,&#34;asOfDateSimple&#34;:&#34;Jul 09 2026&#34;,&#34;originalValue&#34;:&#34;185.339571&#34;},
    &#34;aum&#34;:{&#34;label&#34;:&#34;Assets Under Management&#34;,&#34;originalValue&#34;:&#34;120593378019.36&#34;},
    &#34;nav-date&#34;:{&#34;label&#34;:&#34;NAV Date&#34;,&#34;value&#34;:&#34;09-Jul-2026&#34;}}
    """
    row = parse_ssga_fund_page(html, "XLK", source_url="https://example.test/xlk")

    assert row["date"] == date(2026, 7, 9)
    assert row["issuer"] == "State Street / SPDR"
    assert row["source"] == "issuer: State Street / SPDR"
    assert row["nav"] == 185.339571
    assert row["aum"] == 120593378019.36
    assert round(row["shares_outstanding"]) == round(120593378019.36 / 185.339571)


def test_parse_firsttrust_price_history_extracts_rows():
    html = """
    <table id="ContentPlaceHolder1_PriceHistory_dgETFPrices">
      <tr><td>Date</td><td>Market Price</td><td>Net Asset Value</td><td>Bid/Ask</td><td>Volume</td><td>Net Assets</td></tr>
      <tr><td>7/9/2026</td><td>$94.26</td><td>$94.23</td><td>$94.28</td><td>2,550,723</td><td>$14,624,439,659</td></tr>
    </table>
    """
    rows = parse_firsttrust_price_history(html, "CIBR", source_url="https://example.test")

    assert rows[0]["date"] == date(2026, 7, 9)
    assert rows[0]["nav"] == 94.23
    assert rows[0]["aum"] == 14624439659
    assert round(rows[0]["shares_outstanding"]) == round(14624439659 / 94.23)


def test_parse_spdr_navhist_workbook_extracts_rows():
    import io
    import pandas as pd

    buf = io.BytesIO()
    df = pd.DataFrame(
        [
            ["Fund Name:", "State Street Test ETF", None, None],
            ["Ticker Symbol:", "SPY", None, None],
            [None, None, None, None],
            ["Date", "NAV", "Shares Outstanding", "Total Net Assets"],
            ["09-Jul-2026", 751.69, 1041932116, 783212630071.01],
        ]
    )
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, header=False, sheet_name="navhist")
    rows = parse_spdr_navhist_workbook(buf.getvalue(), "SPY", source_url="https://example.test")

    assert rows[0]["date"] == date(2026, 7, 9)
    assert rows[0]["nav"] == 751.69
    assert rows[0]["shares_outstanding"] == 1041932116


def test_parse_vaneck_history_workbook_extracts_nav_only_rows():
    import io
    import pandas as pd

    buf = io.BytesIO()
    pd.DataFrame(
        [
            ["VanEck Semiconductor ETF - SMH", None, None, None, None],
            ["Date", "NAV", "Change", "% Change", "AUM"],
            ["07/09/2026", 607.7219, 14.46, 2.44, "73,711,722,293.66"],
        ]
    ).to_excel(buf, index=False, header=False)
    rows = parse_vaneck_history_workbook(buf.getvalue(), "SMH", source_url="https://example.test")

    assert rows[0]["date"] == date(2026, 7, 9)
    assert rows[0]["nav"] == 607.7219
    assert rows[0]["aum"] == 73711722293.66
    assert round(rows[0]["shares_outstanding"]) == round(73711722293.66 / 607.7219)


def test_parse_vaneck_fund_page_extracts_current_nav_aum_and_derived_shares():
    html = """
    <ul>
      <li>
        <div class="text-util-md item-title ignore-widow">NAV</div>
        <div class="item-value"><span class="dollar-sign">$</span>611.25</div>
        <span class="as-of-date">as of July 10, 2026</span>
      </li>
      <li>
        <div class="text-util-md item-title ignore-widow">Total Net Assets</div>
        <div class="item-value"><span class="dollar-sign">$</span>73.62B</div>
        <span class="as-of-date">as of July 10, 2026</span>
      </li>
    </ul>
    """
    row = parse_vaneck_fund_page(html, "SMH", source_url="https://example.test/smh")

    assert row["date"] == date(2026, 7, 10)
    assert row["source"] == "issuer: VanEck fund page"
    assert row["nav"] == 611.25
    assert row["aum"] == 73_620_000_000
    assert round(row["shares_outstanding"]) == round(73_620_000_000 / 611.25)


def test_parse_blackrock_historical_rows_extracts_nav_and_shares():
    xml = """
    <ss:Workbook xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
      <ss:Worksheet ss:Name="Historical">
        <ss:Table>
          <ss:Row>
            <ss:Cell><ss:Data ss:Type="String">As Of</ss:Data></ss:Cell>
            <ss:Cell><ss:Data ss:Type="String">NAV per Share</ss:Data></ss:Cell>
            <ss:Cell><ss:Data ss:Type="String">Ex-Dividends</ss:Data></ss:Cell>
            <ss:Cell><ss:Data ss:Type="String">Shares Outstanding</ss:Data></ss:Cell>
          </ss:Row>
          <ss:Row>
            <ss:Cell><ss:Data ss:Type="String">Jul 10, 2026</ss:Data></ss:Cell>
            <ss:Cell><ss:Data ss:Type="Number">581.209555</ss:Data></ss:Cell>
            <ss:Cell><ss:Data ss:Type="String">--</ss:Data></ss:Cell>
            <ss:Cell><ss:Data ss:Type="Number">81900000</ss:Data></ss:Cell>
          </ss:Row>
        </ss:Table>
      </ss:Worksheet>
    </ss:Workbook>
    """
    rows = parse_blackrock_historical_rows(xml, "SOXX", source_url="https://example.test")

    assert rows[0]["date"] == date(2026, 7, 10)
    assert rows[0]["source"] == "issuer: BlackRock historical"
    assert rows[0]["nav"] == 581.209555
    assert rows[0]["shares_outstanding"] == 81900000
    assert round(rows[0]["aum"]) == round(581.209555 * 81900000)


def test_build_etf_flow_signals_outputs_positioning_signal_rows():
    signals = build_etf_flow_signals(
        [
            {
                "snapshot_date": date(2026, 7, 10),
                "etf_ticker": "SMH",
                "category": "Semiconductors",
                "net_fund_flow_1d": -50_000_000,
                "net_fund_flow_5d": -100_000_000,
                "flow_method": "shares_delta_x_today_nav",
            }
        ]
    )

    assert signals[0]["asset_id"] == "SMH"
    assert signals[0]["source"] == "ETF daily data"
    assert signals[0]["flow_method"] == "shares_delta_x_today_nav"
    assert signals[0]["signal_name"] == "ETF daily net fund flow"
    assert signals[0]["z_score"] == -100_000_000
    assert "outflow estimate" in signals[0]["interpretation"]
