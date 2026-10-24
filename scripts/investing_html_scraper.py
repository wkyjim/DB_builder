"""Investing.com historical-data adapter.

Two access paths, both public and both HTML-origin:

  1. The historical-data page itself, scraped as HTML
     (`fetch_investing_html` / `extract_historical_data`).
  2. The date-range endpoint those pages use to render their own table
     (`fetch_instrument_range`). The page only ever shows the most recent
     ~20 rows, so an explicit `start-date` / `end-date` is the only way to reach
     older history - the pages expose no URL parameter for it.

The `investiny` package and its Python JSON API client are NOT used - it is
blocked (HTTP 403) and explicitly out of scope for this project.

Access notes (verified 2026-10-21, re-verified 2026-10-25):
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

# Instrument ids read out of the historical-data pages (confirmed 2026-10-25).
# Used as a fallback when the id cannot be read out of the page HTML itself.
INVESTING_INSTRUMENT_IDS = {
    "BZ=F": "8833",
    "CIHc1": "1115062",
    "CL=F": "8849",
    "ES=F": "8839",
    "GC=F": "8830",
    "HG=F": "8831",
    "HK50": "8984",
    "KOR200c1": "8987",
    "NG=F": "8862",
    "NIY=F": "8859",
    "NQ=F": "8874",
    "RTY=F": "8864",
    "SI=F": "8836",
    "US10YT=X": "23705",
    "US20YT=X": "1161827",
    "US2YT=X": "23701",
    "US30YT=X": "23706",
    "US3YT=X": "23702",
    "US5YT=X": "23703",
    "US7YT=X": "23704",
    "YM=F": "8873",
}

# The historical-data pages load their table through this endpoint; it accepts
# start-date / end-date, which is how history older than the page's default
# ~20-row window is retrieved.
HISTORICAL_RANGE_URL = (
    "https://api.investing.com/api/financialdata/historical/{instrument_id}"
)

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_API_HEADERS = dict(HEADERS)
_API_HEADERS["Accept"] = "application/json"
_API_HEADERS["domain-id"] = "www"

# The page embeds its own instrument id in the Next.js payload.
_INSTRUMENT_ID_RE = re.compile(r'"instrumentId":"(\d+)"')

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
    s = str(date_str).strip()
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


def extract_instrument_id(html: str) -> Optional[str]:
    """Read the instrument id out of a historical-data page."""
    if not html:
        return None
    m = _INSTRUMENT_ID_RE.search(html)
    return m.group(1) if m else None


def parse_instrument_row(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert one date-range row into the scraper's record shape.

    The endpoint emits strings like "2,733.00" for prices and an ISO-ish
    timestamp for the date; both go through the same parsers as the HTML path.
    """
    if not isinstance(rec, dict):
        return None
    iso = rec.get("rowDateTimestamp")
    if not iso:
        return None
    d = parse_date(str(iso)[:10])
    if not d:
        return None
    price = parse_price(rec.get("last_close"))
    if price is None:
        return None
    return {
        "date": d,
        "price": price,
        "open": parse_price(rec.get("last_open")),
        "high": parse_price(rec.get("last_max")),
        "low": parse_price(rec.get("last_min")),
    }


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


def fetch_instrument_range(
    symbol: str,
    start_date: Any = None,
    end_date: Any = None,
    timeout: int = 45,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch an explicit date range through the page's own date-range endpoint.

    The historical-data page renders only its most recent ~20 rows and exposes
    no URL parameter for a wider window, so this endpoint is the only way to
    reach older history. Returns a date-ascending list of records, or None when
    the range cannot be served (unknown symbol, blocked response, unparsable
    payload) so callers can fall back to the default HTML page.
    """
    if not INVESTING_URLS.get(symbol):
        return None

    instrument_id = INVESTING_INSTRUMENT_IDS.get(symbol)
    if not instrument_id:
        page = fetch_investing_html(symbol, timeout=timeout)
        instrument_id = extract_instrument_id(page) if page else None
    if not instrument_id:
        return None

    params = {
        "start-date": getattr(start_date, "isoformat", lambda: "")(),
        "end-date": getattr(end_date, "isoformat", lambda: "")(),
        "time-frame": "Daily",
        "add-missing-rows": "false",
    }
    try:
        resp = _http.get(
            HISTORICAL_RANGE_URL.format(instrument_id=instrument_id),
            params=params, headers=_API_HEADERS, impersonate=IMPERSONATE,
            timeout=timeout, allow_redirects=True,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
    except Exception:
        return None

    data = (payload or {}).get("data") or []
    rows = [r for r in (parse_instrument_row(x) for x in data) if r]
    rows.sort(key=lambda r: r["date"])
    return rows or None


def fetch_historical_data(
    symbol: str,
    timeout: int = 45,
    start_date: Any = None,
    end_date: Any = None,
) -> List[Dict[str, Any]]:
    """Fetch + parse historical data for one symbol.

    With start_date/end_date the page's own date-range endpoint is used so
    history older than the page's default ~20-row window can be retrieved;
    without them the plain historical-data page is scraped. Both paths return
    the same record shape and both fall through to the HTML page when the
    range endpoint is unavailable.
    """
    if start_date is not None or end_date is not None:
        ranged = fetch_instrument_range(
            symbol, start_date=start_date, end_date=end_date, timeout=timeout
        )
        if ranged:
            return ranged

    html = fetch_investing_html(symbol, timeout=timeout)
    return extract_historical_data(html) if html else []


if __name__ == "__main__":
    print("=" * 88)
    print("INVESTING.COM HISTORICAL-DATA SCRAPER - self test")
    print("=" * 88)
    print("  fingerprint: {}".format(IMPERSONATE))
    print("  symbols in map: {}".format(len(INVESTING_URLS)))
    print("  instrument ids mapped: {}".format(len(INVESTING_INSTRUMENT_IDS)))
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
