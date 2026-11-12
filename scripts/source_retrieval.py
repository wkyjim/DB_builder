"""External source retrieval for AEGIS hourly research.

Retrieves authoritative macro, filing, valuation and news evidence before the
AI research call.  Every record carries a URL, publication/observation date,
retrieval timestamp, status and the claims it supports.  Catalog-only links are
never treated as verified evidence.
"""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote_plus

import requests
import yfinance as yf

UA = "AEGIS research contact@example.com"
HEADERS = {"User-Agent": UA, "Accept": "application/json,text/csv,text/html,*/*"}
TIMEOUT = 15
MACRO_SERIES = [
    ("DFF", "Effective federal funds rate"),
    ("BAMLH0A0HYM2", "US high-yield OAS"),
    ("BAMLC0A0CM", "US investment-grade OAS"),
    ("CPIAUCSL", "US CPI"),
    ("UNRATE", "US unemployment rate"),
    ("PAYEMS", "US nonfarm payrolls"),
    ("T10Y2Y", "10Y-2Y Treasury spread"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get(url: str, *, timeout: int = TIMEOUT) -> tuple[int | None, str, str | None]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        return r.status_code, r.text, None
    except Exception as exc:
        return None, "", repr(exc)


def _source(sources: list[dict], *, kind: str, ticker: str | None, url: str,
            published_at: str | None, status: str, claims: list[str],
            http_status: int | None = None, error: str | None = None) -> dict:
    rec = {
        "id": f"S{len(sources) + 1:03d}",
        "kind": kind,
        "ticker": ticker,
        "url": url,
        "published_at": published_at,
        "retrieved_at": _now(),
        "status": status,
        "http_status": http_status,
        "claims": [c for c in claims if c],
        "error": error,
    }
    sources.append(rec)
    return rec


def _fred(series: str, label: str, sources: list[dict]) -> None:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd=2026-01-01"
    code, text, err = _get(url)
    if code != 200 or not text:
        _source(sources, kind="macro", ticker=None, url=url, published_at=None,
                status="unavailable", claims=[], http_status=code, error=err or f"HTTP {code}")
        return
    rows = [ln.split(",") for ln in text.splitlines() if ln and not ln.startswith("observation_date")]
    rows = [r for r in rows if len(r) >= 2 and r[1] not in ("", ".")]
    if not rows:
        _source(sources, kind="macro", ticker=None, url=url, published_at=None,
                status="unavailable", claims=[], http_status=code, error="no observations")
        return
    d, v = rows[-1][0], rows[-1][1]
    _source(sources, kind="macro", ticker=None, url=url, published_at=d,
            status="ok", claims=[f"{label} ({series}) latest observation {d}: {v}"], http_status=code)


def _sec(cik: str | None, ticker: str, sources: list[dict]) -> None:
    if not cik:
        _source(sources, kind="filing", ticker=ticker, url="https://www.sec.gov/edgar/search/",
                published_at=None, status="unavailable", claims=[], error="CIK unavailable")
        return
    c = str(cik).strip().zfill(10)
    sub_url = f"https://data.sec.gov/submissions/CIK{c}.json"
    code, text, err = _get(sub_url)
    if code != 200 or not text:
        _source(sources, kind="filing", ticker=ticker, url=sub_url, published_at=None,
                status="unavailable", claims=[], http_status=code, error=err or f"HTTP {code}")
    else:
        try:
            data = json.loads(text)
            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            accs = recent.get("accessionNumber", [])
            docs = recent.get("primaryDocument", [])
            claims = []
            latest_date = None
            for form, dt, acc, doc in zip(forms, dates, accs, docs):
                if form in ("10-Q", "10-K", "8-K"):
                    latest_date = latest_date or dt
                    if len(claims) < 3:
                        claims.append(f"{form} filed {dt} (accession {acc})")
            url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={c}&type=10-Q&dateb=&owner=include&count=10"
            _source(sources, kind="filing", ticker=ticker, url=url, published_at=latest_date,
                    status="ok" if claims else "unavailable", claims=claims, http_status=code,
                    error=None if claims else "no recent 10-Q/10-K/8-K")
        except Exception as exc:
            _source(sources, kind="filing", ticker=ticker, url=sub_url, published_at=None,
                    status="unavailable", claims=[], http_status=code, error=repr(exc))
    facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{c}.json"
    code, text, err = _get(facts_url)
    if code != 200 or not text:
        _source(sources, kind="filing", ticker=ticker, url=facts_url, published_at=None,
                status="unavailable", claims=[], http_status=code, error=err or f"HTTP {code}")
        return
    try:
        data = json.loads(text)
        facts = data.get("facts", {}).get("us-gaap", {})
        eps = facts.get("EarningsPerShareDiluted", {}).get("units", {}).get("USD/shares", [])
        eps = [x for x in eps if x.get("val") is not None and x.get("end")]
        if eps:
            latest = max(eps, key=lambda x: (x.get("end", ""), x.get("filed", "")))
            _source(sources, kind="filing", ticker=ticker, url=facts_url,
                    published_at=latest.get("filed") or latest.get("end"),
                    status="ok", claims=[f"Diluted EPS {latest.get('val')} for period ending {latest.get('end')} (filed {latest.get('filed')})"],
                    http_status=code)
        else:
            _source(sources, kind="filing", ticker=ticker, url=facts_url, published_at=None,
                    status="unavailable", claims=[], http_status=code, error="no diluted EPS fact")
    except Exception as exc:
        _source(sources, kind="filing", ticker=ticker, url=facts_url, published_at=None,
                status="unavailable", claims=[], http_status=code, error=repr(exc))


def _yf(ticker: str, kind: str, sources: list[dict]) -> None:
    url = f"https://finance.yahoo.com/quote/{ticker}"
    try:
        tk = yf.Ticker(ticker)
        info = tk.info or {}
        claims = []
        for key, label in (("trailingPE", "Trailing P/E"), ("forwardPE", "Forward P/E"),
                           ("marketCap", "Market cap"), ("sector", "Sector"),
                           ("category", "Category"), ("totalAssets", "Total assets")):
            if info.get(key) not in (None, ""):
                claims.append(f"{label}: {info[key]}")
        if kind == "etf":
            try:
                fd = tk.funds_data
                th = getattr(fd, "top_holdings", None)
                if th is not None and len(th):
                    names = []
                    for idx, row in th.head(5).iterrows():
                        names.append(f"{idx} {row.get('Holding Percent', '')}")
                    claims.append("Top holdings: " + "; ".join(names))
            except Exception:
                pass
        _source(sources, kind=kind, ticker=ticker, url=url,
                published_at=None, status="ok" if claims else "unavailable",
                claims=claims, error=None if claims else "no valuation fields")
    except Exception as exc:
        _source(sources, kind=kind, ticker=ticker, url=url, published_at=None,
                status="unavailable", claims=[], error=repr(exc))


def _news(ticker: str, name: str, sources: list[dict]) -> None:
    q = quote_plus(f"{ticker} {name} stock")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    code, text, err = _get(url)
    if code != 200 or not text:
        _source(sources, kind="news", ticker=ticker, url=url, published_at=None,
                status="unavailable", claims=[], http_status=code, error=err or f"HTTP {code}")
        return
    try:
        root = ET.fromstring(text)
        items = root.findall(".//item")[:3]
        claims = []
        latest = None
        for it in items:
            title = (it.findtext("title") or "").strip()
            pub = (it.findtext("pubDate") or "").strip()
            link = (it.findtext("link") or "").strip()
            if title:
                claims.append(f"{title} ({pub})")
                latest = latest or pub
        _source(sources, kind="news", ticker=ticker, url=url, published_at=latest,
                status="ok" if claims else "unavailable", claims=claims, http_status=code,
                error=None if claims else "no news items")
    except Exception as exc:
        _source(sources, kind="news", ticker=ticker, url=url, published_at=None,
                status="unavailable", claims=[], http_status=code, error=repr(exc))


def retrieve(evidence: dict, candidates: list[dict], *, max_stocks: int = 8, max_etfs: int = 8) -> dict:
    sources: list[dict] = []
    for series, label in MACRO_SERIES:
        _fred(series, label, sources)
    stocks = [c for c in candidates if c.get("security_type") == "Stock"][:max_stocks]
    etfs = [c for c in candidates if c.get("security_type") == "ETF"][:max_etfs]
    jobs = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for c in stocks:
            jobs.append(ex.submit(_sec, c.get("cik"), c["ticker"], sources))
            jobs.append(ex.submit(_yf, c["ticker"], "stock", sources))
            jobs.append(ex.submit(_news, c["ticker"], c.get("name", ""), sources))
        for c in etfs:
            jobs.append(ex.submit(_yf, c["ticker"], "etf", sources))
            jobs.append(ex.submit(_news, c["ticker"], c.get("name", ""), sources))
        for f in as_completed(jobs):
            try:
                f.result()
            except Exception:
                pass
    by_ticker: dict[str, list[str]] = {}
    for s in sources:
        if s.get("ticker"):
            by_ticker.setdefault(s["ticker"], []).append(s["id"])
    return {
        "retrieved_at": _now(),
        "sources": sources,
        "by_ticker": by_ticker,
        "counts": {
            "total": len(sources),
            "ok": sum(1 for s in sources if s["status"] == "ok"),
            "unavailable": sum(1 for s in sources if s["status"] != "ok"),
            "tickers": len(by_ticker),
        },
    }
