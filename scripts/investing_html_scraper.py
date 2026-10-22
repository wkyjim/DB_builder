"""Investing.com HTML scraper adapter for historical data extraction.

HTML-ONLY. Investing.com market data must be fetched by scraping the historical-data
HTML pages. The JSON API at api.investing.com is NOT used - it is blocked and is
explicitly out of scope for this project.

Access notes (verified 2026-10-21):
  * plain httpx / requests  -> HTTP 403 (Cloudflare WAF)
  * curl_cffi impersonate="chrome"      -> HTTP 403
  * curl_cffi impersonate="safari17_0"  -> HTTP 200  <-- the working fingerprint
  * some instruments only exist on a regional host (au/uk), and some use the
    /indices/ slug while the older /index/ slug now 404s. INVESTING_URLS below is
    the verified map - every entry has been confirmed to return HTTP 200.
"""
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from curl_cffi import requests as _http

sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")

# The only TLS fingerprint that currently gets past Investing.com's WAF.
IMPERSONATE = "safari17_0"

# Verified working HTML page per symbol (all confirmed HTTP 200).
INVESTING_URLS = {
    # --- US Treasury yields ---
    "US2YT=X": "https://www.investing.com/rates-bonds/u.s.-2-year-bond-yield-historical-data",
    "US3YT=X": "https://www.investing.com/rates-bonds/u.s.-3-year-bond-yield-historical-data",
    "US5YT=X": "https://www.investing.com/rates-bonds/u.s.-5-year-bond-yield-historical-data",
    "US7YT=X": "https://www.investing.com/rates-bonds/u.s.-7-year-bond-yield-historical-data",
    "US10YT=X": "https://www.investing.com/rates-bonds/u.s.-10-year-bond-yield-historical-data",
    "US20YT=X": "https://www.investing.com/rates-bonds/us-20-year-bond-yield-historical-data",
    "US30YT=X": "https://www.investing.com/rates-bonds/u.s.-30-year-bond-yield-historical-data",
    # --- commodities ---
    "BZ=F": "https://www.investing.com/commodities/brent-oil-historical-data",
    "CL=F": "https://www.investing.com/commodities/crude-oil-historical-data",
    "GC=F": "https://www.investing.com/commodities/gold-historical-data",
    "SI=F": "https://www.investing.com/commodities/silver-historical-data",
    "HG=F": "https://www.investing.com/commodities/copper-historical-data",
    "NG=F": "https://www.investing.com/commodities/natural-gas-historical-data",
    # --- US index futures ---
    "ES=F": "https://www.investing.com/indices/us-spx-500-futures-historical-data",
    "NQ=F": "https://www.investing.com/indices/nq-100-futures-historical-data",
    "YM=F": "https://uk.investing.com/indices/us-30-futures-historical-data",
    "RTY=F": "https://www.investing.com/indices/smallcap-2000-futures-historical-data",
    "NIY=F": "https://www.investing.com/indices/japan-225-futures-historical-data",
    # --- Asian futures ---
    "HK50": "https://uk.investing.com/indices/hong-kong-40-futures-historical-data",
    "KOR200c1": "https://au.investing.com/indices/korea-200-futures-historical-data",
    "CIHc1": "https://au.investing.com/indices/sse-50-futures-historical-data",
}

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

# Date formats seen on the historical-data pages.
_DMY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_MDY = re.compile(r"^([A-Za-z]{3,9})\s+(\d{1,2}),\s*(\d{4})$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_date(date_str: str) -> Optional[str]:
    """Parse a page date cell to ISO (YYYY-MM-DD).

    Handles the three layouts Investing.com emits:
      "Oct 19, 2026"   (US-style, most pages)
      "19/10/2026"      (DD/MM/YYYY, some regional pages)
      "2026-10-19"      (already ISO)
    """
    if not date_str:
        return None
    s = date_str.strip()
    m = _ISO.match(s)
    if m:
        return s
    m = _MDY.match(s)
    if m:
        mon = _MONTHS.get(m.group(1)[:3].title())
        if not mon:
            return None
        return "{:04d}-{:02d}-{:02d}".format(int(m.group(3)), mon, int(m.group(2)))
    m = _DMY.match(s)
    if m:
        return "{:04d}-{:02d}-{:02d}".format(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def parse_price(price_str: str) -> Optional[float]:
    """Parse a price cell to float. Returns None for empty / '---' cells."""
    if price_str is None:
        return None
    s = str(price_str).strip().replace(",", "").replace("%", "")
    if not s or s in {"-", "--", "---", "N/A"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_investing_html(symbol: str, timeout: int = 45) -> Optional[str]:
    """Fetch the historical-data HTML page for a symbol.

    HTML only. Raises nothing - returns None on any failure so callers can
    apply their own retry / breaker policy.
    """
    url = INVESTING_URLS.get(symbol)
    if not url:
        return None
    try:
        resp = _http.get(url, headers=HEADERS, impersonate=IMPERSONATE,
                         timeout=timeout, allow_redirects=True)
        if resp.status_code == 200 and len(resp.content) > 100_000:
            return resp.text
        return None
    except Exception:
        return None


def extract_historical_data(html: str) -> List[Dict[str, Any]]:
    """Extract historical OHLC rows from the historical-data table."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_cells = rows[0].find_all(["td", "th"])
        header_texts = [c.get_text(strip=True) for c in header_cells]
        if not ("Date" in header_texts and "Price" in header_texts and "Open" in header_texts):
            continue

        data: List[Dict[str, Any]] = []
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            texts = [c.get_text(strip=True) for c in cells]
            if len(texts) < 5:
                continue
            date = parse_date(texts[0])
            price = parse_price(texts[1])
            if not date or price is None:
                continue
            data.append({
                "date": date,
                "price": price,
                "open": parse_price(texts[2]),
                "high": parse_price(texts[3]),
                "low": parse_price(texts[4]),
            })
        if data:
            return data
    return []


def fetch_historical_data(symbol: str, timeout: int = 45) -> List[Dict[str, Any]]:
    """Fetch + parse historical data for one symbol."""
    html = fetch_investing_html(symbol, timeout=timeout)
    return extract_historical_data(html) if html else []


if __name__ == "__main__":
    print("=" * 88)
    print("INVESTING.COM HTML SCRAPER - self test")
    print("=" * 88)
    print("  fingerprint: {}".format(IMPERSONATE))
    print("  symbols in map: {}".format(len(INVESTING_URLS)))
    print()

    ok = 0
    fail = []
    for sym in INVESTING_URLS:
        recs = fetch_historical_data(sym)
        if recs:
            ok += 1
            print("  {:<12} {:>3} rows   {} .. {}".format(
                sym, len(recs), recs[-1]["date"], recs[0]["date"]))
        else:
            fail.append(sym)
            print("  {:<12} FAILED".format(sym))

    print()
    print("  OK: {}/{}   failed: {}".format(ok, len(INVESTING_URLS), fail))
