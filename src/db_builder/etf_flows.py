"""ETF daily data and net fund flow estimates from free metadata.

The canonical local table is public.etf_daily_data. It stores ETF-level NAV,
AUM, shares outstanding, and estimated net fund flow. The preferred flow
formula is:

    (shares outstanding today - shares outstanding yesterday) * NAV today

When shares outstanding is unavailable but AUM exists, the module can fall
back to AUM change and labels the method as price-contaminated.
"""

from __future__ import annotations

import html
import io
import math
import re
import time
from datetime import date, datetime, timezone

import pandas as pd
import requests
from bs4 import BeautifulSoup
from sqlalchemy import text

from db_builder.flow_sources import record_flow_source_health


ISSUER_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "Accept": "application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
    "Referer": "https://www.ishares.com/",
}


ETF_FLOW_UNIVERSE = {
    "SPY": "Broad Equity",
    "QQQ": "Growth / Nasdaq",
    "DIA": "Dow Industrials",
    "RSP": "Equal-Weight Equity",
    "IWM": "Small Caps",
    "IVV": "Broad Equity",
    "ITOT": "Total U.S. Equity",
    "IJH": "Mid Caps",
    "IJR": "Small Caps",
    "IWB": "Large / Mid Caps",
    "IVW": "S&P 500 Growth",
    "IVE": "S&P 500 Value",
    "IEFA": "Developed Markets ex-US",
    "IEMG": "Emerging Markets",
    "IXUS": "International Equity",
    "IWR": "Mid Caps",
    "ACWI": "Global Equity",
    "EFA": "Developed Markets ex-US",
    "EEM": "Emerging Markets",
    "VEA": "Developed Markets ex-US",
    "VWO": "Emerging Markets",
    "GLD": "Gold",
    "IAU": "Gold",
    "SLV": "Silver",
    "IBIT": "Bitcoin",
    "XLK": "Technology",
    "SMH": "Semiconductors",
    "SOXX": "Semiconductors",
    "CIBR": "Cybersecurity",
    "XAR": "Defense",
    "NLR": "Nuclear",
    "GRID": "Grid Infrastructure",
    "XLV": "Healthcare",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLU": "Utilities",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLI": "Industrials",
    "XLB": "Materials",
    "XLC": "Communication Services",
    "XLRE": "Real Estate",
    "IYF": "Financials",
    "IYW": "Technology",
    "IYH": "Healthcare",
    "IYE": "Energy",
    "IYR": "Real Estate",
    "ITA": "Aerospace & Defense",
    "AGG": "Core Bonds",
    "SGOV": "Treasury Bills",
    "GOVT": "U.S. Treasuries",
    "IUSB": "Core Bonds",
    "MUB": "Municipal Bonds",
    "MBB": "Mortgage Bonds",
    "QUAL": "Quality Factor",
    "DGRO": "Dividend Growth",
    "DYNF": "Equity Factor Rotation",
    "HYG": "High Yield Credit",
    "LQD": "Investment Grade Credit",
    "JNK": "High Yield Credit",
    "TLT": "Long Duration Treasury",
    "IEF": "Intermediate Treasury",
    "SHY": "Short Treasury",
    "IWF": "Growth",
    "IWD": "Value",
}

def _ssga_navhist_url(ticker: str) -> str:
    return f"https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/navhist-us-en-{ticker.lower()}.xlsx"


def _blackrock_ishares_fund_download_url(portfolio_id: str) -> str:
    return (
        "https://www.blackrock.com/varnish-api/blk-one01-product-data/"
        "product-data/api/v1/get-fund-document"
        "?appType=PRODUCT_PAGE"
        "&appSubType=ISHARES"
        "&targetSite=us-ishares"
        "&locale=en_US"
        f"&portfolioId={portfolio_id}"
        "&component=fundDownload"
        "&userType=individual"
    )


def _blackrock_ishares_entry(portfolio_id: str) -> dict:
    return {
        "issuer": "BlackRock / iShares",
        "adapter": "blackrock_fund_download",
        "source_url": _blackrock_ishares_fund_download_url(portfolio_id),
    }


ISHARES_TOP_30_FLOW_ETFS = {
    # Top 30 iShares ETFs from the live screener by total net assets.
    "IVV": "239726",
    "IEFA": "244049",
    "IEMG": "244050",
    "AGG": "239458",
    "IWF": "239706",
    "IJH": "239763",
    "IJR": "239774",
    "SGOV": "314116",
    "ITOT": "239724",
    "IWM": "239710",
    "IWD": "239708",
    "EFA": "239623",
    "IVW": "239725",
    "IAU": "239561",
    "IXUS": "244048",
    "IWR": "239718",
    "IWB": "239707",
    "IVE": "239728",
    "SOXX": "239705",
    "IEF": "239456",
    "IBIT": "333011",
    "QUAL": "256101",
    "MUB": "239766",
    "GOVT": "239468",
    "IUSB": "264615",
    "TLT": "239454",
    "DGRO": "264623",
    "MBB": "239465",
    "DYNF": "307283",
    "LQD": "239566",
}


ISHARES_ADDITIONAL_FLOW_ETFS = {
    # Additional iShares sector/commodity/rates funds requested for report
    # coverage where they are not in the top-30 asset list.
    "SLV": "239855",
    "SHY": "239452",
    "ACWI": "239600",
    "HYG": "239565",
    "IYW": "239522",
    "IYF": "239508",
    "IYH": "239511",
    "IYE": "239507",
    "IYR": "239520",
    "ITA": "239502",
}


ISHARES_FLOW_ETF_PORTFOLIO_IDS = {
    **ISHARES_TOP_30_FLOW_ETFS,
    **ISHARES_ADDITIONAL_FLOW_ETFS,
}


ETF_ISSUER_REGISTRY = {
    "SPY": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/navhist-us-en-spy.xlsx"},
    "QQQ": {"issuer": "Invesco", "adapter": "yfinance_metadata", "source_url": "https://www.invesco.com/qqq-etf/en/home.html"},
    "DIA": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("DIA")},
    "RSP": {"issuer": "Invesco", "adapter": "yfinance_metadata", "source_url": "https://www.invesco.com/us/financial-products/etfs/product-detail?audienceType=Investor&ticker=RSP"},
    "VEA": {"issuer": "Vanguard", "adapter": "yfinance_metadata", "source_url": "https://investor.vanguard.com/investment-products/etfs/profile/vea"},
    "VWO": {"issuer": "Vanguard", "adapter": "yfinance_metadata", "source_url": "https://investor.vanguard.com/investment-products/etfs/profile/vwo"},
    "GLD": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("GLD")},
    "XLK": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("XLK")},
    "XLF": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("XLF")},
    "XLV": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("XLV")},
    "XLE": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("XLE")},
    "XLI": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("XLI")},
    "XLY": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("XLY")},
    "XLP": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("XLP")},
    "XLU": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("XLU")},
    "XLB": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("XLB")},
    "XLC": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("XLC")},
    "XLRE": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("XLRE")},
    "SMH": {"issuer": "VanEck", "adapter": "vaneck_page", "source_url": "https://www.vaneck.com/us/en/investments/semiconductor-etf-smh/", "history_url": "https://www.vaneck.com/us/en/investments/semiconductor-etf-smh/downloads/fundhistoprices/"},
    "CIBR": {"issuer": "First Trust", "adapter": "firsttrust_price_history", "source_url": "https://www.ftportfolios.com/Retail/Etf/EtfPriceHistory.aspx?Ticker=CIBR"},
    "XAR": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("XAR")},
    "NLR": {"issuer": "VanEck", "adapter": "yfinance_metadata", "source_url": "https://www.vaneck.com/us/en/investments/uranium-nuclear-energy-etf-nlr/"},
    "GRID": {"issuer": "First Trust", "adapter": "firsttrust_price_history", "source_url": "https://www.ftportfolios.com/Retail/Etf/EtfPriceHistory.aspx?Ticker=GRID"},
    "JNK": {"issuer": "State Street / SPDR", "adapter": "ssga_navhist", "source_url": _ssga_navhist_url("JNK")},
    "SHY": {"issuer": "BlackRock / iShares", "adapter": "yfinance_metadata", "source_url": "https://www.ishares.com/us/products/239452/ishares-1-3-year-treasury-bond-etf"},
    **{ticker: _blackrock_ishares_entry(portfolio_id) for ticker, portfolio_id in ISHARES_FLOW_ETF_PORTFOLIO_IDS.items()},
}


def _row_template(
    *,
    obs_date: date,
    ticker: str,
    nav: float | None,
    aum: float | None,
    shares: float | None,
    market_price: float | None,
    source: str,
    source_url: str | None,
) -> dict:
    ticker = ticker.upper()
    registry = ETF_ISSUER_REGISTRY.get(ticker, {})
    return {
        "date": obs_date,
        "snapshot_date": obs_date,
        "etf_ticker": ticker,
        "category": ETF_FLOW_UNIVERSE.get(ticker, "ETF"),
        "issuer": registry.get("issuer"),
        "nav": nav,
        "nav_price": nav,
        "market_price": market_price,
        "aum": aum,
        "total_assets": aum,
        "shares_outstanding": shares,
        "shares_outstanding_change_1d": None,
        "shares_outstanding_change_5d": None,
        "net_fund_flow_1d": None,
        "net_fund_flow_5d": None,
        "estimated_flow_1d": None,
        "estimated_flow_5d": None,
        "shares_change_1d": None,
        "shares_change_5d": None,
        "aum_change_1d": None,
        "aum_change_5d": None,
        "flow_method": "snapshot_only",
        "source": source,
        "source_url": source_url,
    }


def setup_etf_flow_schema(engine) -> None:
    sql = text(
        """
        CREATE TABLE IF NOT EXISTS public.etf_daily_data (
            date date NOT NULL,
            etf_ticker text NOT NULL,
            category text,
            issuer text,
            nav numeric,
            market_price numeric,
            aum numeric,
            shares_outstanding numeric,
            shares_outstanding_change_1d numeric,
            shares_outstanding_change_5d numeric,
            net_fund_flow_1d numeric,
            net_fund_flow_5d numeric,
            flow_method text,
            source text NOT NULL DEFAULT 'yfinance metadata',
            source_url text,
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now(),
            PRIMARY KEY (date, etf_ticker, source)
        );

        CREATE TABLE IF NOT EXISTS public.etf_flow_snapshots (
            snapshot_date date NOT NULL,
            etf_ticker text NOT NULL,
            category text,
            total_assets numeric,
            shares_outstanding numeric,
            nav_price numeric,
            market_price numeric,
            estimated_flow_1d numeric,
            estimated_flow_5d numeric,
            shares_change_1d numeric,
            shares_change_5d numeric,
            aum_change_1d numeric,
            aum_change_5d numeric,
            flow_method text,
            source text NOT NULL DEFAULT 'yfinance metadata',
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now(),
            PRIMARY KEY (snapshot_date, etf_ticker, source)
        );

        ALTER TABLE public.etf_daily_data ADD COLUMN IF NOT EXISTS issuer text;
        ALTER TABLE public.etf_daily_data ADD COLUMN IF NOT EXISTS source_url text;
        """
    )
    with engine.begin() as conn:
        conn.execute(sql)


def _clean_float(value) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(numeric) or math.isinf(numeric):
        return None
    return numeric


def _clean_money_value(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("$", "").replace(",", "").replace(" ", "")
    multiplier = 1.0
    if text.upper().endswith("B"):
        multiplier = 1_000_000_000.0
        text = text[:-1]
    elif text.upper().endswith("M"):
        multiplier = 1_000_000.0
        text = text[:-1]
    elif text.upper().endswith("K"):
        multiplier = 1_000.0
        text = text[:-1]
    numeric = _clean_float(text)
    return numeric * multiplier if numeric is not None else None


def _snapshot_from_info(ticker: str, info: dict, *, snapshot_date: date | None = None) -> dict:
    ticker = ticker.upper()
    obs_date = snapshot_date or datetime.now(timezone.utc).date()
    registry = ETF_ISSUER_REGISTRY.get(ticker, {})
    nav = _clean_float(info.get("navPrice"))
    aum = _clean_float(info.get("totalAssets"))
    shares = _clean_float(info.get("sharesOutstanding"))
    return _row_template(
        obs_date=obs_date,
        ticker=ticker,
        nav=nav,
        aum=aum,
        shares=shares,
        market_price=_clean_float(info.get("regularMarketPrice") or info.get("currentPrice")),
        source="yfinance metadata fallback",
        source_url=registry.get("source_url"),
    )


def _parse_ssga_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%b %d %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_ssga_fund_page(html_text: str, ticker: str, *, source_url: str) -> dict:
    ticker = ticker.upper()
    decoded = html.unescape(html_text)
    nav_match = re.search(r'"nav"\s*:\s*\{.*?"originalValue"\s*:\s*"([^"]+)"', decoded, re.S)
    aum_match = re.search(r'"aum"\s*:\s*\{.*?"originalValue"\s*:\s*"([^"]+)"', decoded, re.S)
    date_match = re.search(r'"nav-date"\s*:\s*\{.*?"value"\s*:\s*"([^"]+)"', decoded, re.S)
    if not date_match:
        date_match = re.search(r'"nav"\s*:\s*\{.*?"asOfDateSimple"\s*:\s*"([^"]+)"', decoded, re.S)
    nav = _clean_float(nav_match.group(1) if nav_match else None)
    aum = _clean_float(aum_match.group(1) if aum_match else None)
    shares = aum / nav if aum is not None and nav not in (None, 0) else None
    obs_date = _parse_ssga_date(date_match.group(1) if date_match else None) or datetime.now(timezone.utc).date()
    return _row_template(
        obs_date=obs_date,
        ticker=ticker,
        nav=nav,
        aum=aum,
        shares=shares,
        market_price=None,
        source="issuer: State Street / SPDR",
        source_url=source_url,
    )


def _parse_date_any(value, *, day_first: bool = False) -> date | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    text_value = str(value).strip()
    slash_formats = ("%d/%m/%Y", "%m/%d/%Y", "%d/%m/%y") if day_first else ("%m/%d/%Y", "%d/%m/%Y", "%d/%m/%y")
    for fmt in ("%d-%b-%Y", *slash_formats, "%Y-%m-%d"):
        try:
            return datetime.strptime(text_value, fmt).date()
        except ValueError:
            continue
    parsed = pd.to_datetime(text_value, errors="coerce", dayfirst=day_first)
    if pd.isna(parsed):
        return None
    return parsed.date()


def parse_spdr_navhist_workbook(content: bytes, ticker: str, *, source_url: str, start_date: date | None = None) -> list[dict]:
    df = pd.read_excel(io.BytesIO(content), sheet_name=0, header=None)
    header_idx = None
    for idx, row in df.iterrows():
        values = [str(item).strip().lower() for item in row.tolist()]
        if "date" in values and "nav" in values:
            header_idx = idx
            break
    if header_idx is None:
        return []
    data = df.iloc[header_idx + 1 :].copy()
    headers = [str(item).strip().lower() for item in df.iloc[header_idx].tolist()]
    data.columns = headers
    rows = []
    for _, item in data.iterrows():
        obs_date = _parse_date_any(item.get("date"))
        if obs_date is None or (start_date and obs_date < start_date):
            continue
        nav = _clean_float(item.get("nav"))
        shares = _clean_float(item.get("shares outstanding"))
        aum = _clean_float(item.get("total net assets"))
        if nav is None and shares is None and aum is None:
            continue
        rows.append(
            _row_template(
                obs_date=obs_date,
                ticker=ticker,
                nav=nav,
                aum=aum,
                shares=shares,
                market_price=None,
                source="issuer: State Street / SPDR navhist",
                source_url=source_url,
            )
        )
    return rows


def parse_vaneck_history_workbook(content: bytes, ticker: str, *, source_url: str, start_date: date | None = None) -> list[dict]:
    raw = pd.read_excel(io.BytesIO(content), sheet_name=0, header=None)
    header_idx = None
    for idx, row in raw.iterrows():
        values = [str(item).strip().lower() for item in row.tolist()]
        if "date" in values and "nav" in values:
            header_idx = idx
            break
    if header_idx is None:
        return []
    df = raw.iloc[header_idx + 1 :].copy()
    df.columns = [str(item).strip() for item in raw.iloc[header_idx].tolist()]
    columns = {str(col).strip().lower(): col for col in df.columns}
    date_col = columns.get("date")
    nav_col = columns.get("nav")
    if date_col is None or nav_col is None:
        return []
    market_col = columns.get("last trade")
    aum_col = columns.get("aum")
    rows = []
    for _, item in df.iterrows():
        obs_date = _parse_date_any(item.get(date_col))
        if obs_date is None or (start_date and obs_date < start_date):
            continue
        nav = _clean_float(item.get(nav_col))
        if nav is None:
            continue
        market_price = _clean_float(str(item.get(market_col)).replace(",", "")) if market_col else None
        aum = _clean_float(str(item.get(aum_col)).replace(",", "")) if aum_col else None
        shares = aum / nav if aum is not None and nav not in (None, 0) else None
        rows.append(
            _row_template(
                obs_date=obs_date,
                ticker=ticker,
                nav=nav,
                aum=aum,
                shares=shares,
                market_price=market_price,
                source="issuer: VanEck history",
                source_url=source_url,
            )
        )
    return rows


def parse_vaneck_fund_page(html_text: str, ticker: str, *, source_url: str) -> dict | None:
    soup = BeautifulSoup(html_text, "html.parser")
    metrics = {}
    for item in soup.find_all("li"):
        title_node = item.find(class_=re.compile("item-title"))
        value_node = item.find(class_=re.compile("item-value"))
        date_node = item.find(class_=re.compile("as-of-date"))
        if not title_node or not value_node:
            continue
        title = title_node.get_text(" ", strip=True).lower()
        value = value_node.get_text(" ", strip=True)
        as_of = date_node.get_text(" ", strip=True).replace("as of", "").strip() if date_node else ""
        if title.startswith("nav"):
            metrics["nav"] = _clean_money_value(value)
            metrics["date"] = _parse_date_any(as_of)
        elif title == "total net assets":
            metrics["aum"] = _clean_money_value(value)
            metrics.setdefault("date", _parse_date_any(as_of))
    nav = metrics.get("nav")
    aum = metrics.get("aum")
    obs_date = metrics.get("date")
    if obs_date is None or nav is None:
        return None
    shares = aum / nav if aum is not None and nav not in (None, 0) else None
    return _row_template(
        obs_date=obs_date,
        ticker=ticker,
        nav=nav,
        aum=aum,
        shares=shares,
        market_price=None,
        source="issuer: VanEck fund page",
        source_url=source_url,
    )


def parse_firsttrust_price_history(html_text: str, ticker: str, *, source_url: str, start_date: date | None = None) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.find("table", id=re.compile("dgETFPrices"))
    if table is None:
        return []
    rows = []
    for tr in table.find_all("tr")[1:]:
        cells = [cell.get_text(" ", strip=True).replace("$", "").replace(",", "") for cell in tr.find_all(["td", "th"])]
        if len(cells) < 6:
            continue
        obs_date = _parse_date_any(cells[0])
        if obs_date is None or (start_date and obs_date < start_date):
            continue
        market_price = _clean_float(cells[1])
        nav = _clean_float(cells[2])
        aum = _clean_float(cells[5])
        shares = aum / nav if aum is not None and nav not in (None, 0) else None
        rows.append(
            _row_template(
                obs_date=obs_date,
                ticker=ticker,
                nav=nav,
                aum=aum,
                shares=shares,
                market_price=market_price,
                source="issuer: First Trust price history",
                source_url=source_url,
            )
        )
    return rows


def parse_blackrock_fund_download(xml_text: str, ticker: str, *, source_url: str) -> dict | None:
    rows = []
    for rowtxt in re.findall(r"<ss:Row[^>]*>(.*?)</ss:Row>", xml_text, flags=re.S):
        vals = []
        for celltxt in re.findall(r"<ss:Cell[^>]*>(.*?)</ss:Cell>", rowtxt, flags=re.S):
            match = re.search(r"<ss:Data[^>]*>(.*?)</ss:Data>", celltxt, flags=re.S)
            vals.append(html.unescape(re.sub(r"<.*?>", "", match.group(1)).strip()) if match else "")
        if vals:
            rows.append(vals)
    obs_date = None
    shares = None
    holdings_started = False
    market_value_idx = None
    total_market_value = 0.0
    for row in rows:
        if len(row) == 1 and obs_date is None:
            obs_date = _parse_date_any(row[0])
        if row and row[0] == "Shares Outstanding" and len(row) > 1:
            shares = _clean_float(row[1].replace(",", ""))
        if row and row[0] == "Ticker":
            holdings_started = True
            lowered = [value.lower() for value in row]
            market_value_idx = lowered.index("market value") if "market value" in lowered else None
            continue
        if holdings_started and market_value_idx is not None and len(row) > market_value_idx:
            value = _clean_float(str(row[market_value_idx]).replace(",", ""))
            if value is not None:
                total_market_value += value
    if obs_date is None or shares is None:
        return None
    aum = total_market_value if total_market_value > 0 else None
    nav = aum / shares if aum is not None and shares not in (None, 0) else None
    return _row_template(
        obs_date=obs_date,
        ticker=ticker,
        nav=nav,
        aum=aum,
        shares=shares,
        market_price=None,
        source="issuer: BlackRock fund download",
        source_url=source_url,
    )


def _spreadsheetml_rows(section_text: str) -> list[list[str]]:
    rows = []
    for rowtxt in re.findall(r"<ss:Row[^>]*>(.*?)</ss:Row>", section_text, flags=re.S):
        vals = []
        for celltxt in re.findall(r"<ss:Cell[^>]*>(.*?)</ss:Cell>", rowtxt, flags=re.S):
            match = re.search(r"<ss:Data[^>]*>(.*?)</ss:Data>", celltxt, flags=re.S)
            vals.append(html.unescape(re.sub(r"<.*?>", "", match.group(1)).strip()) if match else "")
        if vals:
            rows.append(vals)
    return rows


def parse_blackrock_historical_rows(xml_text: str, ticker: str, *, source_url: str, start_date: date | None = None) -> list[dict]:
    worksheet = re.search(
        r"<ss:Worksheet[^>]*ss:Name=\"Historical\"[^>]*>(.*?)</ss:Worksheet>",
        xml_text,
        flags=re.S,
    )
    if not worksheet:
        return []
    rows = _spreadsheetml_rows(worksheet.group(1))
    if not rows:
        return []
    header_idx = None
    for idx, row in enumerate(rows[:100]):
        normalized = [str(value).strip().lower().replace("-", " ") for value in row]
        row_text = " | ".join(normalized)
        if ("as of" in row_text or "date" in row_text) and "nav per share" in row_text and "shares outstanding" in row_text:
            header_idx = idx
            break
    if header_idx is None:
        return []
    header = [str(value).strip().lower().replace("-", " ") for value in rows[header_idx]]

    def index_for(*phrases: str) -> int | None:
        for phrase in phrases:
            for idx, value in enumerate(header):
                if phrase in value:
                    return idx
        return None

    date_idx = index_for("as of", "date")
    nav_idx = index_for("nav per share")
    shares_idx = index_for("shares outstanding", "shares out")
    if date_idx is None or nav_idx is None or shares_idx is None:
        return []

    parsed_rows = []
    for row in rows[header_idx + 1 :]:
        if len(row) <= max(date_idx, nav_idx, shares_idx):
            continue
        obs_date = _parse_date_any(row[date_idx])
        if obs_date is None or (start_date and obs_date < start_date):
            continue
        nav = _clean_float(str(row[nav_idx]).replace(",", ""))
        shares = _clean_float(str(row[shares_idx]).replace(",", ""))
        if nav is None or shares is None:
            continue
        parsed_rows.append(
            _row_template(
                obs_date=obs_date,
                ticker=ticker,
                nav=nav,
                aum=nav * shares,
                shares=shares,
                market_price=None,
                source="issuer: BlackRock historical",
                source_url=source_url,
            )
        )
    return parsed_rows


def fetch_issuer_etf_daily_row(ticker: str, *, timeout: int = 20) -> dict:
    ticker = ticker.upper()
    registry = ETF_ISSUER_REGISTRY.get(ticker, {})
    source_url = registry.get("source_url")
    if registry.get("adapter") == "ssga_navhist" and source_url:
        response = requests.get(source_url, headers=ISSUER_REQUEST_HEADERS, timeout=timeout)
        response.raise_for_status()
        rows = parse_spdr_navhist_workbook(response.content, ticker, source_url=source_url)
        if rows:
            return max(rows, key=lambda row: row["date"])
    if registry.get("adapter") == "ssga_page" and registry.get("source_url"):
        response = requests.get(registry["source_url"], headers=ISSUER_REQUEST_HEADERS, timeout=timeout)
        response.raise_for_status()
        row = parse_ssga_fund_page(response.text, ticker, source_url=registry["source_url"])
        if row.get("nav") is not None or row.get("aum") is not None:
            return row
    if registry.get("adapter") == "vaneck_page" and source_url:
        response = requests.get(source_url, headers=ISSUER_REQUEST_HEADERS, timeout=timeout)
        response.raise_for_status()
        row = parse_vaneck_fund_page(response.text, ticker, source_url=source_url)
        if row:
            return row
    if registry.get("adapter") == "blackrock_fund_download" and source_url:
        response = requests.get(source_url, headers=ISSUER_REQUEST_HEADERS, timeout=timeout)
        response.raise_for_status()
        row = parse_blackrock_fund_download(response.text, ticker, source_url=source_url)
        if row:
            return row
    raise RuntimeError(f"No issuer parser available for {ticker}")


def fetch_issuer_etf_history_rows(ticker: str, *, start_date: date | None = None, timeout: int = 30) -> list[dict]:
    ticker = ticker.upper()
    registry = ETF_ISSUER_REGISTRY.get(ticker, {})
    source_url = registry.get("source_url")
    history_url = registry.get("history_url")
    adapter = registry.get("adapter")
    if not source_url:
        return []
    request_url = history_url if adapter == "vaneck_page" and history_url else source_url
    response = requests.get(request_url, headers=ISSUER_REQUEST_HEADERS, timeout=timeout)
    response.raise_for_status()
    if adapter == "ssga_navhist":
        return parse_spdr_navhist_workbook(response.content, ticker, source_url=source_url, start_date=start_date)
    if adapter == "vaneck_history":
        return parse_vaneck_history_workbook(response.content, ticker, source_url=request_url, start_date=start_date)
    if adapter == "vaneck_page":
        rows = parse_vaneck_history_workbook(response.content, ticker, source_url=request_url, start_date=start_date)
        try:
            current_row = fetch_issuer_etf_daily_row(ticker, timeout=timeout)
        except Exception:
            current_row = None
        if current_row and (start_date is None or current_row["date"] >= start_date):
            rows = [row for row in rows if row["date"] != current_row["date"]]
            rows.append(current_row)
        return rows
    if adapter == "firsttrust_price_history":
        return parse_firsttrust_price_history(response.text, ticker, source_url=source_url, start_date=start_date)
    if adapter == "blackrock_fund_download":
        rows = parse_blackrock_historical_rows(response.text, ticker, source_url=source_url, start_date=start_date)
        row = parse_blackrock_fund_download(response.text, ticker, source_url=source_url)
        if row and (start_date is None or row["date"] >= start_date):
            rows = [history_row for history_row in rows if history_row["date"] != row["date"]]
            rows.append(row)
        return rows
    return []


def fetch_etf_flow_snapshots(
    tickers: list[str] | None = None,
    *,
    snapshot_date: date | None = None,
    start_date: date | None = None,
) -> list[dict]:
    import yfinance as yf

    rows = []
    for ticker in tickers or list(ETF_FLOW_UNIVERSE):
        if start_date is not None:
            try:
                history_rows = fetch_issuer_etf_history_rows(ticker, start_date=start_date)
            except Exception:
                history_rows = []
            if history_rows:
                rows.extend(history_rows)
                continue
        try:
            row = fetch_issuer_etf_daily_row(ticker)
            if snapshot_date is not None:
                row["date"] = snapshot_date
                row["snapshot_date"] = snapshot_date
        except Exception:
            info = yf.Ticker(ticker).get_info()
            row = _snapshot_from_info(ticker, info, snapshot_date=snapshot_date)
        if row["aum"] is None and row["shares_outstanding"] is None:
            continue
        rows.append(row)
    return rows


def upsert_etf_flow_snapshots(engine, rows: list[dict]) -> int:
    if not rows:
        return 0
    setup_etf_flow_schema(engine)
    daily_sql = text(
        """
        INSERT INTO public.etf_daily_data (
            date, etf_ticker, category, issuer, nav, market_price, aum, shares_outstanding,
            shares_outstanding_change_1d, shares_outstanding_change_5d,
            net_fund_flow_1d, net_fund_flow_5d, flow_method, source, source_url, created_at, updated_at
        )
        VALUES (
            :date, :etf_ticker, :category, :issuer, :nav, :market_price, :aum, :shares_outstanding,
            :shares_outstanding_change_1d, :shares_outstanding_change_5d,
            :net_fund_flow_1d, :net_fund_flow_5d, :flow_method, :source, :source_url, now(), now()
        )
        ON CONFLICT (date, etf_ticker, source)
        DO UPDATE SET
            category = EXCLUDED.category,
            issuer = EXCLUDED.issuer,
            nav = EXCLUDED.nav,
            market_price = EXCLUDED.market_price,
            aum = EXCLUDED.aum,
            shares_outstanding = EXCLUDED.shares_outstanding,
            shares_outstanding_change_1d = EXCLUDED.shares_outstanding_change_1d,
            shares_outstanding_change_5d = EXCLUDED.shares_outstanding_change_5d,
            net_fund_flow_1d = EXCLUDED.net_fund_flow_1d,
            net_fund_flow_5d = EXCLUDED.net_fund_flow_5d,
            flow_method = EXCLUDED.flow_method,
            source_url = EXCLUDED.source_url,
            updated_at = now();
        """
    )
    legacy_sql = text(
        """
        INSERT INTO public.etf_flow_snapshots (
            snapshot_date, etf_ticker, category, total_assets, shares_outstanding,
            nav_price, market_price, estimated_flow_1d, estimated_flow_5d,
            shares_change_1d, shares_change_5d, aum_change_1d, aum_change_5d,
            flow_method, source, created_at, updated_at
        )
        VALUES (
            :snapshot_date, :etf_ticker, :category, :total_assets, :shares_outstanding,
            :nav_price, :market_price, :estimated_flow_1d, :estimated_flow_5d,
            :shares_change_1d, :shares_change_5d, :aum_change_1d, :aum_change_5d,
            :flow_method, :source, now(), now()
        )
        ON CONFLICT (snapshot_date, etf_ticker, source)
        DO UPDATE SET
            category = EXCLUDED.category,
            total_assets = EXCLUDED.total_assets,
            shares_outstanding = EXCLUDED.shares_outstanding,
            nav_price = EXCLUDED.nav_price,
            market_price = EXCLUDED.market_price,
            estimated_flow_1d = EXCLUDED.estimated_flow_1d,
            estimated_flow_5d = EXCLUDED.estimated_flow_5d,
            shares_change_1d = EXCLUDED.shares_change_1d,
            shares_change_5d = EXCLUDED.shares_change_5d,
            aum_change_1d = EXCLUDED.aum_change_1d,
            aum_change_5d = EXCLUDED.aum_change_5d,
            flow_method = EXCLUDED.flow_method,
            updated_at = now();
        """
    )
    with engine.begin() as conn:
        conn.execute(daily_sql, rows)
        conn.execute(legacy_sql, rows)
    return len(rows)


def recompute_etf_flow_features(engine) -> int:
    setup_etf_flow_schema(engine)
    daily_sql = text(
        """
        WITH enriched AS (
            SELECT
                date,
                etf_ticker,
                source,
                aum,
                shares_outstanding,
                nav,
                LAG(aum, 1) OVER w AS prev_aum_1d,
                LAG(aum, 5) OVER w AS prev_aum_5d,
                LAG(shares_outstanding, 1) OVER w AS prev_shares_1d,
                LAG(shares_outstanding, 5) OVER w AS prev_shares_5d
            FROM public.etf_daily_data
            WINDOW w AS (PARTITION BY etf_ticker, source ORDER BY date)
        )
        UPDATE public.etf_daily_data d
        SET
            shares_outstanding_change_1d = CASE WHEN e.prev_shares_1d IS NULL OR e.shares_outstanding IS NULL THEN NULL ELSE e.shares_outstanding - e.prev_shares_1d END,
            shares_outstanding_change_5d = CASE WHEN e.prev_shares_5d IS NULL OR e.shares_outstanding IS NULL THEN NULL ELSE e.shares_outstanding - e.prev_shares_5d END,
            net_fund_flow_1d = CASE
                WHEN e.prev_shares_1d IS NOT NULL AND e.shares_outstanding IS NOT NULL AND COALESCE(e.nav, 0) > 0
                    THEN (e.shares_outstanding - e.prev_shares_1d) * e.nav
                WHEN e.prev_aum_1d IS NOT NULL AND e.aum IS NOT NULL
                    THEN e.aum - e.prev_aum_1d
                ELSE NULL
            END,
            net_fund_flow_5d = CASE
                WHEN e.prev_shares_5d IS NOT NULL AND e.shares_outstanding IS NOT NULL AND COALESCE(e.nav, 0) > 0
                    THEN (e.shares_outstanding - e.prev_shares_5d) * e.nav
                WHEN e.prev_aum_5d IS NOT NULL AND e.aum IS NOT NULL
                    THEN e.aum - e.prev_aum_5d
                ELSE NULL
            END,
            flow_method = CASE
                WHEN e.prev_shares_1d IS NOT NULL AND e.shares_outstanding IS NOT NULL AND COALESCE(e.nav, 0) > 0
                    THEN 'shares_delta_x_today_nav'
                WHEN e.prev_aum_1d IS NOT NULL AND e.aum IS NOT NULL
                    THEN 'aum_delta_proxy_price_contaminated'
                ELSE 'snapshot_only'
            END,
            updated_at = now()
        FROM enriched e
        WHERE d.date = e.date
          AND d.etf_ticker = e.etf_ticker
          AND d.source = e.source;
        """
    )
    legacy_sql = text(
        """
        UPDATE public.etf_flow_snapshots s
        SET
            estimated_flow_1d = d.net_fund_flow_1d,
            estimated_flow_5d = d.net_fund_flow_5d,
            shares_change_1d = d.shares_outstanding_change_1d,
            shares_change_5d = d.shares_outstanding_change_5d,
            flow_method = d.flow_method,
            updated_at = now()
        FROM public.etf_daily_data d
        WHERE s.snapshot_date = d.date
          AND s.etf_ticker = d.etf_ticker
          AND s.source = d.source;
        """
    )
    with engine.begin() as conn:
        daily_result = conn.execute(daily_sql)
        legacy_result = conn.execute(legacy_sql)
    return int(daily_result.rowcount or 0) + int(legacy_result.rowcount or 0)


def flow_interpretation(row: dict) -> str:
    ticker = row.get("etf_ticker") or row.get("asset_id") or "ETF"
    category = row.get("category") or "ETF"
    flow = _clean_float(row.get("net_fund_flow_1d") if "net_fund_flow_1d" in row else row.get("estimated_flow_1d"))
    method = row.get("flow_method") or "snapshot_only"
    if flow is None:
        return f"{ticker} ({category}): daily net flow history is still building."
    direction = "inflow estimate" if flow > 0 else "outflow estimate" if flow < 0 else "flat flow estimate"
    caveat = "daily net flow = shares outstanding change x today's NAV" if method == "shares_delta_x_today_nav" else "AUM-change estimate, price-contaminated"
    return f"{ticker} ({category}): {direction}; {caveat}."


def fetch_latest_etf_flow_rows(engine) -> list[dict]:
    sql = text(
        """
        WITH max_date AS (
            SELECT COALESCE(MAX(date), CURRENT_DATE) AS date
            FROM public.etf_daily_data
        )
        , ranked AS (
            SELECT
                date AS snapshot_date,
                etf_ticker,
                category,
                aum AS total_assets,
                shares_outstanding,
                nav AS nav_price,
                market_price,
                net_fund_flow_1d,
                net_fund_flow_5d,
                net_fund_flow_1d AS estimated_flow_1d,
                net_fund_flow_5d AS estimated_flow_5d,
                shares_outstanding_change_1d AS shares_change_1d,
                shares_outstanding_change_5d AS shares_change_5d,
                flow_method,
                source,
                ROW_NUMBER() OVER (
                    PARTITION BY etf_ticker
                    ORDER BY
                        CASE
                            WHEN source ILIKE '%navhist%' THEN 0
                            WHEN source ILIKE '%First Trust%' THEN 0
                            WHEN source ILIKE '%BlackRock%' THEN 1
                            WHEN source ILIKE '%VanEck%' THEN 2
                            WHEN source ILIKE '%yfinance%' THEN 4
                            ELSE 3
                        END,
                        CASE WHEN net_fund_flow_1d IS NULL THEN 1 ELSE 0 END,
                        date DESC,
                        ABS(COALESCE(net_fund_flow_1d, 0)) DESC
                ) AS rn
            FROM public.etf_daily_data
            WHERE date >= (SELECT date FROM max_date) - INTERVAL '14 days'
              AND source <> 'yfinance metadata'
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        ORDER BY
            CASE
                WHEN source ILIKE '%navhist%' THEN 0
                WHEN source ILIKE '%First Trust%' THEN 0
                WHEN source ILIKE '%BlackRock%' THEN 1
                WHEN source ILIKE '%VanEck%' THEN 2
                WHEN source ILIKE '%yfinance%' THEN 4
                ELSE 3
            END,
            CASE WHEN net_fund_flow_1d IS NULL THEN 1 ELSE 0 END,
            ABS(COALESCE(net_fund_flow_1d, 0)) DESC,
            total_assets DESC NULLS LAST
        """
    )
    try:
        return pd.read_sql(sql, engine).to_dict(orient="records")
    except Exception:
        return []


def build_etf_flow_signals(rows: list[dict]) -> list[dict]:
    signals = []
    for row in rows:
        if not row.get("snapshot_date") or not row.get("etf_ticker"):
            continue
        signals.append(
            {
                "signal_date": row["snapshot_date"],
                "asset_type": "etf",
                "asset_id": row["etf_ticker"],
                "signal_name": "ETF daily net fund flow",
                "signal_value": row.get("net_fund_flow_1d", row.get("estimated_flow_1d")),
                "z_score": row.get("net_fund_flow_5d", row.get("estimated_flow_5d")),
                "percentile": None,
                "interpretation": flow_interpretation(row),
                "source": "ETF daily data",
            }
        )
    return signals


def run_etf_flow_fetch(
    engine,
    *,
    tickers: list[str] | None = None,
    dry_run: bool = False,
    snapshot_date: date | None = None,
    start_date: date | None = None,
) -> dict:
    started = time.perf_counter()
    rows: list[dict] = []
    error = None
    try:
        rows = fetch_etf_flow_snapshots(tickers=tickers, snapshot_date=snapshot_date, start_date=start_date)
        upserted = 0 if dry_run else upsert_etf_flow_snapshots(engine, rows)
        recomputed = 0 if dry_run else recompute_etf_flow_features(engine)
        succeeded = True
    except Exception as exc:
        upserted = 0
        recomputed = 0
        succeeded = False
        error = str(exc)
    if not dry_run:
        record_flow_source_health(
            engine,
            source_name="ETF daily data",
            source_type="etf_daily_data",
            succeeded=succeeded,
            fetch_seconds=time.perf_counter() - started,
            latest_available_date=snapshot_date or datetime.now(timezone.utc).date() if succeeded else None,
            error=error,
        )
    if error:
        raise RuntimeError(error)
    return {"rows": len(rows), "upserted": upserted, "recomputed": recomputed, "sample": rows[:10]}
