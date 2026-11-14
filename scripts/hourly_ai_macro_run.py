#!/usr/bin/env python3
"""Hourly AI-first macro run.

Dry-run by default.  Production publication requires --publish.

Data QC -> frozen EOD risk -> AI research -> report QC -> Markdown/JSON.
The AI must select ideas from the dynamic candidate universe; no ticker or
conclusion is hardcoded here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import sys
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd
import requests
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from db_builder.config import local_engine  # noqa: E402
from session_calendar import last_completed_session, next_sessions  # noqa: E402
from source_retrieval import retrieve as retrieve_external_sources  # noqa: E402

ENGINE = local_engine()

DATA = ROOT / "market-dashboard" / "data"
PROMPT_PATH = ROOT / "market-dashboard" / "scripts" / "hourly_ai_prompt.txt"
FALLBACK_MODELS = ["meituan/longcat-2.5:free", "stepfun/step-3.7-flash:free"]
REGISTRY = ROOT / "data" / "phase5e_model_registry.json"
GICS = [
    "Health Care", "Consumer Staples", "Consumer Discretionary", "Real Estate",
    "Financials", "Utilities", "Materials", "Industrials",
    "Communication Services", "Information Technology", "Energy",
]
SECTOR_ETF = {
    "Health Care": "XLV", "Consumer Staples": "XLP", "Consumer Discretionary": "XLY",
    "Real Estate": "XLRE", "Financials": "XLF", "Utilities": "XLU",
    "Materials": "XLB", "Industrials": "XLI", "Communication Services": "XLC",
    "Information Technology": "XLK", "Energy": "XLE",
}
SOURCE_CATALOG = [
    {"label": "Internal market snapshot", "url": "https://wkyjim.github.io/market-dashboard/data/latest-report.md", "note": "Published AEGIS snapshot"},
    {"label": "Market API", "url": "https://api.138.2.69.165.sslip.io/openapi.json", "note": "Authoritative market API specification"},
    {"label": "Federal Reserve FOMC calendar", "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", "note": "Policy dates"},
    {"label": "US Treasury refunding", "url": "https://home.treasury.gov/news/press-releases/sb0678/", "note": "Fiscal supply"},
    {"label": "BLS CPI calendar", "url": "https://www.bls.gov/schedule/news_release/cpi.htm", "note": "Inflation release dates"},
    {"label": "Reuters markets", "url": "https://www.reuters.com/markets/", "note": "Market context"},
    {"label": "SEC EDGAR", "url": "https://www.sec.gov/edgar/search/", "note": "Company filings"},
    {"label": "Yahoo Finance", "url": "https://finance.yahoo.com/", "note": "Consensus and valuation context"},
    {"label": "CompaniesMarketCap", "url": "https://companiesmarketcap.com/", "note": "Valuation history"},
    {"label": "Federal Reserve press releases", "url": "https://www.federalreserve.gov/newsevents/pressreleases.htm", "note": "Policy statements"},
    {"label": "BEA releases", "url": "https://www.bea.gov/news/schedule", "note": "Economic release schedule"},
]


def _sha(text_: str) -> str:
    return hashlib.sha256(text_.encode("utf-8")).hexdigest()


def _query(sql: str, params: dict | None = None) -> list:
    with ENGINE.connect() as c:
        return c.execute(text(sql), params or {}).fetchall()


def _read(sql: str, params: dict | None = None) -> pd.DataFrame:
    with ENGINE.connect() as c:
        return pd.read_sql(text(sql), c, params=params or {})


def load_evidence(as_of: date) -> dict:
    latest_equity = _query("select max(date) from public.us_equities")[0][0]
    latest_macro = _query("select max(date) from public.macro")[0][0]
    prov_rows = _query("""
        select coalesce(data_source,'legacy_massive') as source,
               coalesce(is_provisional,false) as provisional, count(*) as n
        from public.us_equities where date=:d group by 1,2
    """, {"d": latest_equity})
    equity_provenance = {
        "provisional": int(sum(r[2] for r in prov_rows if r[1])),
        "canonical": int(sum(r[2] for r in prov_rows if not r[1])),
        "sources": {str(r[0]): int(r[2]) for r in prov_rows},
    }
    breadth = _read("select * from public.sp500_breadth_daily order by date desc limit 1")
    breadth_row = breadth.iloc[0].to_dict() if len(breadth) else {}
    sector_tickers = tuple(SECTOR_ETF.values())
    sector_prices = _read(
        "select ticker,date,close from public.us_equities where ticker in :tickers and date<=:d order by ticker,date",
        {"tickers": sector_tickers, "d": latest_equity},
    )
    sectors = []
    for name, etf in SECTOR_ETF.items():
        df = sector_prices[sector_prices.ticker == etf].sort_values("date")
        ret20 = float(df.close.iloc[-1] / df.close.iloc[-21] - 1) if len(df) >= 21 else None
        sectors.append({"sector": name, "etf": etf, "return_20d": ret20})
    flows = _read(
        "select date,ticker,flow_zscore_20d,flow_zscore_60d,flow_state,price_state,flow_structure "
        "from public.etf_flow_signal_daily order by date desc limit 40"
    )
    flow_date = flows.date.max() if len(flows) else None
    flow_rows = flows[flows.date == flow_date].head(25).to_dict("records") if len(flows) else []
    macro = _read(
        "select date,symbol,close,pct_chg from public.macro where date=:d order by symbol",
        {"d": latest_macro},
    )
    macro_rows = macro.to_dict("records")
    econ_ids = ("CPIAUCSL", "CPILFESL", "UNRATE", "PAYEMS", "GDPC1", "PCEPILFE",
                "PCEPI", "DFF", "BAMLH0A0HYM2", "BAMLC0A0CM", "ICSA", "CCSA")
    econ_rows = []
    for sid in econ_ids:
        row = _query(
            "select date,series_id,series_name,value,unit,category "
            "from public.economic_indicators where series_id=:s order by date desc limit 1",
            {"s": sid},
        )
        if row:
            r = row[0]
            econ_rows.append({"date": str(r[0]), "series_id": r[1], "name": r[2],
                              "value": float(r[3]), "unit": r[4], "category": r[5]})
    snap = _read(
        "select date,ticker,close,pct_chg,volume from public.us_equities where date=:d order by volume desc limit 50",
        {"d": latest_equity},
    )
    classes = _read("select ticker,sector,company_name,cik from public.security_classification where is_active=true")
    cmap = {str(r.ticker).upper(): r for r in classes.itertuples()}
    candidates = []
    for r in snap.itertuples():
        t = str(r.ticker).upper()
        if len(t) > 5 or t.startswith("^") or t.endswith("=X") or t.endswith("=F"):
            continue
        c = cmap.get(t)
        if c is None:
            continue
        candidates.append({
            "ticker": t, "name": str(c.company_name or t), "sector": str(c.sector or ""),
            "security_type": "Stock", "cik": str(c.cik or ""),
            "close": float(r.close) if pd.notna(r.close) else None,
            "pct_chg": float(r.pct_chg) if pd.notna(r.pct_chg) else None,
            "volume": float(r.volume) if pd.notna(r.volume) else None,
            "date": str(r.date.date()) if hasattr(r.date, "date") else str(r.date),
        })
    etf_rows = _read(
        "select ticker, fund_name, primary_segment from public.etf_master "
        "where is_active=true and flow_eligible=true order by ticker limit 80"
    )
    etf_tickers = [str(x).upper() for x in etf_rows.ticker] + list(SECTOR_ETF.values())
    price_rows = _read(
        "select ticker,date,close,pct_chg,volume from public.us_equities "
        "where date=:d and ticker in :tickers",
        {"d": latest_equity, "tickers": tuple(sorted(set(etf_tickers)))},
    )
    pmap = {str(r.ticker).upper(): r for r in price_rows.itertuples()}
    existing = {x["ticker"] for x in candidates}
    for name, etf in SECTOR_ETF.items():
        r = pmap.get(etf)
        if r is not None and etf not in existing:
            candidates.append({"ticker": etf, "name": name + " sector ETF", "sector": name,
                               "security_type": "ETF", "close": float(r.close) if pd.notna(r.close) else None,
                               "pct_chg": float(r.pct_chg) if pd.notna(r.pct_chg) else None,
                               "volume": float(r.volume) if pd.notna(r.volume) else None,
                               "date": str(r.date)})
            existing.add(etf)
    for e in etf_rows.itertuples():
        et = str(e.ticker).upper()
        r = pmap.get(et)
        if r is not None and et not in existing:
            candidates.append({"ticker": et, "name": str(e.fund_name or et),
                               "sector": str(e.primary_segment or ""), "security_type": "ETF",
                               "close": float(r.close) if pd.notna(r.close) else None,
                               "pct_chg": float(r.pct_chg) if pd.notna(r.pct_chg) else None,
                               "volume": float(r.volume) if pd.notna(r.volume) else None,
                               "date": str(r.date)})
            existing.add(et)
    def lag(start, end):
        if start is None or end is None:
            return None
        d = start.date() if hasattr(start, "date") else start
        e = end.date() if hasattr(end, "date") else end
        if d >= e:
            return 0
        n = 0
        for x in next_sessions(d, 10):
            n += 1
            if x >= e:
                break
        return n
    data_qc = {
        "equity_session": str(latest_equity),
        "equity_lag_sessions": lag(latest_equity, as_of),
        "macro_date": str(latest_macro),
        "breadth_date": str(breadth_row.get("date")),
        "breadth_lag_sessions": lag(breadth_row.get("date"), as_of),
        "flows_date": str(flow_date),
        "flows_lag_sessions": lag(flow_date, as_of),
        "equity_provenance": equity_provenance,
    }
    if data_qc["equity_lag_sessions"] == 0:
        data_qc["status"] = "PASS"
    elif data_qc["equity_lag_sessions"] <= 1:
        data_qc["status"] = "PASS_WITH_WARNINGS"
        data_qc["warning"] = "US equity data is one completed session behind the inference date; not an automatic failure."
    else:
        data_qc["status"] = "FAIL"
        data_qc["warning"] = "US equity data is more than one completed session behind the inference date."
    if equity_provenance["provisional"] and not equity_provenance["canonical"]:
        data_qc["status"] = "PASS_WITH_WARNINGS"
        data_qc["warning"] = "Equity prices for the session are provisional yfinance data; Massive has not yet replaced them."
    elif equity_provenance["provisional"] and equity_provenance["canonical"]:
        data_qc["status"] = "PASS_WITH_WARNINGS"
        data_qc["warning"] = "Equity session contains a mix of provisional yfinance and canonical Massive rows."
    if data_qc["status"] in ("PASS", "PASS_WITH_WARNINGS") and any(
            (data_qc.get(k) or 0) > 0 for k in ("breadth_lag_sessions", "flows_lag_sessions")):
        data_qc["status"] = "PASS_WITH_WARNINGS"
        data_qc["warning_detail"] = "Breadth or ETF-flow input lags the equity session; each is timestamped in the report."
    return {
        "as_of": str(as_of),
        "latest_equity_date": str(latest_equity.date()) if hasattr(latest_equity, "date") else str(latest_equity),
        "data_qc": data_qc,
        "latest_macro_date": str(latest_macro.date()) if hasattr(latest_macro, "date") else str(latest_macro),
        "breadth": {k: (v.item() if hasattr(v, "item") else v) for k, v in breadth_row.items() if k != "updated_at"},
        "sectors": sectors,
        "flows_date": str(flow_date.date()) if hasattr(flow_date, "date") else str(flow_date),
        "flows": flow_rows,
        "macro": macro_rows,
        "economic": econ_rows,
        "candidates": candidates[:80],
        "source_catalog": SOURCE_CATALOG,
    }

def risk_state(as_of: date, publish: bool) -> dict:
    # The shared EOD workflow is the only writer of official forecasts.
    if publish:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "phase5h_daily_shadow.py")],
            cwd=str(ROOT), check=False, capture_output=True, text=True,
        )
    rows = _query(
        "select forecast_id,as_of_date,target_start,target_end,status,forecast_vol_5d,"
        "spike_probability,hist20_forecast,ewma_forecast from aegis_shadow.forecasts "
        "order by as_of_date desc limit 1"
    )
    if not rows:
        return {"status": "UNAVAILABLE", "reason": "no frozen forecast"}
    r = rows[0]
    hourly = {"as_of": str(r[1]), "source": "frozen_eod_forecast", "experimental": False,
              "eligible_for_formal": True, "forecast_vol_5d": float(r[5])}
    return {
        "forecast_id": r[0], "as_of": str(r[1]), "target_start": str(r[2]), "target_end": str(r[3]),
        "status": r[4], "forecast_vol_5d": float(r[5]), "spike_probability": float(r[6]),
        "historical_baseline": float(r[7]), "ewma_baseline": float(r[8]),
        "threshold": 0.184956, "directional_signal": False,
        "experimental_hourly": False, "hourly_estimate": hourly,
        "note": "Frozen daily-close model; official only after completed US close.",
    }


def outcomes_state() -> dict:
    rows = _query(
        "select count(*), avg(o.abs_error), sqrt(avg(o.squared_error)), "
        "avg(case when o.spike_outcome then 1 else 0 end), "
        "avg(power((case when o.spike_outcome then 1 else 0 end) - f.spike_probability, 2)) "
        "from aegis_shadow.outcomes o join aegis_shadow.forecasts f using(forecast_id)"
    )[0]
    return {"count": int(rows[0]), "mae": float(rows[1]) if rows[1] is not None else None,
            "rmse": float(rows[2]) if rows[2] is not None else None,
            "spike_rate": float(rows[3]) if rows[3] is not None else None,
            "brier": float(rows[4]) if rows[4] is not None else None}


def _ensure_proxy() -> None:
    try:
        requests.get("http://127.0.0.1:8645/v1/models", headers={"Authorization": "Bearer local"}, timeout=3)
        return
    except Exception:
        pass
    exe = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "bin" / "hermes.cmd"
    if exe.exists():
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000
        subprocess.Popen([str(exe), "proxy", "start"], **kwargs)
    for _ in range(20):
        try:
            requests.get("http://127.0.0.1:8645/v1/models", headers={"Authorization": "Bearer local"}, timeout=3)
            return
        except Exception:
            time.sleep(2)
    raise RuntimeError("Hermes AI proxy unavailable")


def _ai_call(prompt: str, payload: dict, model: str, max_tokens: int = 3000, attempts: int = 2) -> tuple[dict, dict]:
    _ensure_proxy()
    started = time.monotonic()
    meta = {
        "provider": "hermes-local-proxy",
        "model": model,
        "prompt_chars": len(prompt),
        "payload_chars": len(json.dumps(payload, default=str)),
        "attempts": 0,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "elapsed_s": None,
        "response_sha256": None,
    }
    body = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, default=str)},
        ],
    }
    last = ""
    for attempt in range(attempts):
        meta["attempts"] = attempt + 1
        try:
            r = requests.post(
                "http://127.0.0.1:8645/v1/chat/completions",
                headers={"Authorization": "Bearer local"},
                json=body, timeout=240,
            )
        except Exception as exc:
            last = repr(exc)
            time.sleep(10 + attempt * 10)
            continue
        last = r.text
        if r.status_code == 200:
            response = r.json()
            usage = response.get("usage") or {}
            meta.update({
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            })
            message = response.get("choices", [{}])[0].get("message", {})
            content = message.get("content")
            if not content:
                raise RuntimeError("AI returned empty content")
            content = content.strip()
            if content.startswith("```"):
                content = content.strip("`")
                if content.startswith("json"):
                    content = content[4:].lstrip()
            meta["elapsed_s"] = round(time.monotonic() - started, 2)
            meta["response_sha256"] = _sha(content)
            return json.loads(content), meta
        if r.status_code not in (429, 500, 502, 503, 504):
            break
        time.sleep(10 + attempt * 10)
    raise RuntimeError(f"AI request failed: {last[:500]}")


def build_ticker_evidence(evidence: dict, ext: dict) -> dict:
    """Build a source-bound valuation object for every candidate.

    AI may select from these objects and supply assumptions, but it cannot
    write EPS, forward P/E, holdings, weights or source IDs back into them.
    """
    sources = [s for s in (ext.get("sources") or []) if s.get("status") == "ok"]
    out = {}
    for c in evidence.get("candidates", []) or []:
        ticker = str(c.get("ticker", "")).upper()
        ss = [s for s in sources if str(s.get("ticker") or "").upper() == ticker]
        filing = next((s for s in ss if s.get("kind") == "filing" and
                       (s.get("facts") or {}).get("diluted_eps") is not None), None)
        val = next((s for s in ss if s.get("kind") in ("stock", "etf") and
                    (s.get("facts") or {})), None)
        ff = (filing or {}).get("facts") or {}
        vf = (val or {}).get("facts") or {}
        price = _num(c.get("close"))
        valuation_date = c.get("date")
        source_ids = [s["id"] for s in ss]
        if c.get("security_type") == "Stock":
            forward_eps = _num(vf.get("forward_eps"))
            forward_pe = _num(vf.get("forward_pe"))
            # Explicitly do not substitute reported historical diluted EPS
            # for forward EPS. A missing forward EPS remains null.
            if forward_eps is None and forward_pe not in (None, 0) and price is not None:
                forward_eps = price / forward_pe
            out[ticker] = {
                "ticker": ticker, "security_type": "Stock",
                "fiscal_period": ff.get("fiscal_end"),
                "diluted_eps_reported": _num(ff.get("diluted_eps")),
                "diluted_eps_definition": ff.get("definition") or "reported diluted EPS",
                "forward_eps": forward_eps,
                "forward_eps_basis": ("source.forwardEps" if _num(vf.get("forward_eps")) is not None
                                      else ("derived: price / source.forwardPE"
                                            if forward_eps is not None else None)),
                "forward_pe": forward_pe, "price": price,
                "valuation_date": valuation_date,
                "source_as_of": (ff.get("filed") or valuation_date),
                "source_ids": source_ids,
                "filing_source_id": filing.get("id") if filing else None,
                "valuation_source_id": val.get("id") if val else None,
                "fresh": True if source_ids else False,
            }
        elif c.get("security_type") == "ETF":
            holdings = [h for h in (vf.get("holdings") or []) if h.get("weight_pct") is not None]
            coverage = sum(float(h["weight_pct"]) for h in holdings) if holdings else None
            out[ticker] = {
                "ticker": ticker, "security_type": "ETF",
                "holdings": holdings,
                "holdings_coverage_pct": coverage,
                "holdings_as_of": vf.get("holdings_as_of") or valuation_date,
                "price": price, "valuation_date": valuation_date,
                "portfolio_valuation": (
                    f"Top {len(holdings)} holdings; numeric weight coverage {coverage:.2f}%"
                    if holdings and coverage is not None else None),
                "source_ids": source_ids,
                "valuation_source_id": val.get("id") if val else None,
                "fresh": bool(holdings) and coverage is not None,
            }
    return out


def apply_evidence_bindings(report: dict, evidence: dict) -> dict:
    """Overwrite AI valuation values with source-bound evidence and compute targets.

    This runs before validation. AI-provided EPS/P/E/targets are discarded;
    only its qualitative research and numeric assumption inputs survive.
    """
    te = evidence.get("ticker_evidence") or {}
    for idea in report.get("ideas", []) or []:
        ticker = str(idea.get("ticker", "")).upper()
        ev = te.get(ticker)
        if not ev:
            continue
        idea["price"] = ev.get("price")
        idea["valuation_date"] = ev.get("valuation_date")
        idea["source_ids"] = [x for x in (idea.get("source_ids") or [])
                              if x in (ev.get("source_ids") or [])]
        inputs = idea.get("assumption_inputs") or {}
        if ev["security_type"] == "Stock":
            feps, fpe = ev.get("forward_eps"), ev.get("forward_pe")
            idea["eps"] = feps
            idea["forward_pe"] = fpe
            idea["diluted_eps_reported"] = ev.get("diluted_eps_reported")
            idea["diluted_eps_definition"] = ev.get("diluted_eps_definition")
            idea["fiscal_period"] = ev.get("fiscal_period")
            idea["forward_eps_basis"] = ev.get("forward_eps_basis")
            vals = [("bull_target", inputs.get("bull_multiple")),
                    ("base_target", inputs.get("base_multiple")),
                    ("bear_target", inputs.get("bear_multiple"))]
            for key, multiple in vals:
                m = _num(multiple)
                idea[key] = round(feps * m, 2) if feps is not None and m and m > 0 else None
            if feps is not None and _num(inputs.get("base_multiple")):
                idea["valuation_assumptions"] = (
                    f"Forward EPS {feps:.4f} ({ev.get('forward_eps_basis')}) x "
                    f"AI multiple assumption {_num(inputs.get('base_multiple')):.2f}; "
                    f"source IDs {', '.join(idea.get('source_ids', []))}")
            else:
                idea["valuation_assumptions"] = "Unavailable: verified forward EPS or assumption missing."
        else:
            idea.pop("eps", None)
            idea.pop("forward_pe", None)
            holdings = ev.get("holdings") or []
            idea["top_holdings"] = [f"{h['ticker']} {h['weight_pct']:.2f}%" for h in holdings]
            idea["portfolio_valuation"] = ev.get("portfolio_valuation")
            idea["holdings_coverage_pct"] = ev.get("holdings_coverage_pct")
            idea["holdings_as_of"] = ev.get("holdings_as_of")
            vals = [("bull_target", inputs.get("bull_return_pct")),
                    ("base_target", inputs.get("base_return_pct")),
                    ("bear_target", inputs.get("bear_return_pct"))]
            for key, pct in vals:
                r = _num(pct)
                idea[key] = round(ev["price"] * (1 + r / 100.0), 2) if ev.get("price") is not None and r is not None else None
            idea["valuation_assumptions"] = (
                f"Holdings/portfolio valuation; {len(holdings)} weighted holdings; "
                f"coverage {ev.get('holdings_coverage_pct')}%; source IDs "
                f"{', '.join(idea.get('source_ids', []))}")
    for horizon in ("short_term", "long_term"):
        for row in (report.get("scenarios") or {}).get(horizon, []) or []:
            r = _num(row.get("return_pct"))
            if r is not None:
                row["range"] = f"{r:+.2f}%"
    return report


def _research_fingerprint(evidence: dict, risk: dict) -> str:
    return _sha(json.dumps({
        "as_of": evidence.get("as_of"),
        "latest_equity_date": evidence.get("latest_equity_date"),
        "latest_macro_date": evidence.get("latest_macro_date"),
        "sectors": evidence.get("sectors"),
        "flows_date": evidence.get("flows_date"),
        "breadth": evidence.get("breadth"),
        "macro": evidence.get("macro"),
        "economic": evidence.get("economic"),
        "candidates": evidence.get("candidates"),
        "risk": risk,
        "ticker_evidence": evidence.get("ticker_evidence"),
        "external_source_evidence": [
            {k: s.get(k) for k in ("id", "url", "published_at", "status", "claims")}
            for s in (evidence.get("external_sources") or {}).get("sources", [])
        ],
    }, sort_keys=True, default=str))


def call_ai(evidence: dict, risk: dict, model: str) -> tuple[dict, dict]:
    payload = {
        "as_of": evidence["as_of"],
        "latest_equity_date": evidence["latest_equity_date"],
        "latest_macro_date": evidence["latest_macro_date"],
        "data_qc": evidence.get("data_qc", {}),
        "breadth": evidence["breadth"],
        "sectors": evidence["sectors"],
        "flows": evidence["flows"],
        "macro": evidence["macro"],
        "economic": evidence.get("economic", []),
        "candidates": evidence["candidates"],
        "risk": risk,
        "external_sources": (evidence.get("external_sources") or {}).get("sources", []),
        "source_retrieval": (evidence.get("external_sources") or {}).get("counts", {}),
    }
    market_prompt = (ROOT / "market-dashboard" / "scripts" / "hourly_ai_prompt_market.txt").read_text(encoding="utf-8")
    ideas_prompt = (ROOT / "market-dashboard" / "scripts" / "hourly_ai_prompt_ideas.txt").read_text(encoding="utf-8")
    all_external = (evidence.get("external_sources") or {}).get("sources", [])
    sector_etfs = set(SECTOR_ETF.values())
    def compact(s):
        return {"id": s.get("id"), "kind": s.get("kind"), "ticker": s.get("ticker"),
                "published_at": s.get("published_at"), "status": s.get("status"),
                "claims": [str(c)[:240] for c in (s.get("claims") or [])[:1]]}
    market_external = [compact(s) for s in all_external if s.get("kind") == "macro" or (s.get("kind") == "etf" and s.get("ticker") in sector_etfs)]
    idea_tickers = {c.get("ticker") for c in evidence.get("candidates", [])}
    ideas_external = [compact(s) for s in all_external if s.get("ticker") in idea_tickers or s.get("kind") == "macro"]
    market_payload = {k: payload[k] for k in (
        "as_of", "latest_equity_date", "latest_macro_date", "data_qc", "breadth",
        "sectors", "flows", "macro", "economic", "risk", "source_retrieval")}
    market_payload["macro"] = payload["macro"][:3]
    market_payload["economic"] = payload["economic"][:3]
    market_payload["flows"] = payload["flows"][:3]
    market_payload["external_sources"] = market_external[:20]
    ideas_payload = {k: payload[k] for k in (
        "as_of", "latest_equity_date", "latest_macro_date", "data_qc", "candidates",
        "risk", "source_retrieval")}
    ideas_payload["candidates"] = payload["candidates"][:8]
    ideas_payload["external_sources"] = ideas_external[:16]
    fingerprint = _research_fingerprint(evidence, risk)
    cache_path = ROOT / "data" / "hourly_research_cache.json"
    cached = None
    cache_age_h = None
    if cache_path.exists():
        try:
            c = json.loads(cache_path.read_text(encoding="utf-8"))
            if c.get("fingerprint") == fingerprint:
                created = datetime.fromisoformat(c.get("created_at", ""))
                cache_age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600
                if 0 <= cache_age_h <= 24:
                    cached = c
        except Exception:
            cached = None
    last_exc = None
    for candidate_model in [model] + [m for m in FALLBACK_MODELS if m != model]:
        try:
            market, market_meta = _ai_call(market_prompt, market_payload, candidate_model, max_tokens=3000)
            if cached and cached.get("ideas") and cached.get("scenarios"):
                ideas = {"ideas": cached["ideas"], "scenarios": cached["scenarios"]}
                ideas_meta = {"cached": True, "cached_at": cached.get("created_at"),
                              "age_hours": round(cache_age_h, 2), "fingerprint": fingerprint,
                              "source_dates": cached.get("source_dates", {})}
            else:
                ideas, ideas_meta = _ai_call(ideas_prompt, ideas_payload, candidate_model, max_tokens=3000)
                ideas_meta = {**ideas_meta, "cached": False, "fingerprint": fingerprint,
                              "source_dates": {"as_of": evidence.get("as_of"),
                                               "latest_equity_date": evidence.get("latest_equity_date"),
                                               "latest_macro_date": evidence.get("latest_macro_date"),
                                               "flows_date": evidence.get("flows_date"),
                                               "breadth_date": evidence.get("data_qc", {}).get("breadth_date")}}
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "fingerprint": fingerprint,
                    "source_dates": ideas_meta["source_dates"],
                    "ideas": ideas.get("ideas", []),
                    "scenarios": ideas.get("scenarios", {}),
                }, indent=2), encoding="utf-8")
            merged = dict(market)
            for key in ("ideas", "scenarios"):
                if key in ideas:
                    merged[key] = ideas[key]
            return merged, {"provider": "hermes-local-proxy", "model": candidate_model,
                            "market": market_meta, "fundamental_research": ideas_meta,
                            "fingerprint": fingerprint,
                            "source_dates": ideas_meta.get("source_dates", {})}
        except Exception as exc:
            last_exc = exc
            print(f"MODEL FAILED — {candidate_model}: {exc}")
            continue
    raise RuntimeError(f"all AI models failed: {last_exc}")

def render(report: dict, evidence: dict, risk: dict, outcomes: dict, generated: str, ai_meta: dict | None = None) -> str:
    r = risk
    lines = [
        "# AEGIS AI-First Market Intelligence", "",
        f"Generated at: {generated}",
        f"Data cutoff: {evidence['latest_equity_date']} US cash close",
        f"Snapshot ID: {report.get('snapshot_id', 'hourly')}", "",
        "## Executive Summary", "",
        f"- **AI market view: {report.get('market_view', 'research judgment only')}**",
    ]
    for x in report.get("executive_summary", []):
        lines.append(f"- {x}")
    dq = evidence.get("data_qc", {})
    lines += ["", "## Data QC", "",
              f"- Status: **{dq.get('status', 'UNKNOWN')}**",
              f"- Equity session: {dq.get('equity_lag_sessions')} completed session(s) behind inference date",
              f"- Equity provenance: {dq.get('equity_provenance', {}).get('provisional', 0)} provisional / "
              f"{dq.get('equity_provenance', {}).get('canonical', 0)} canonical",
              f"- Breadth lag: {dq.get('breadth_lag_sessions')} session(s); ETF-flow lag: {dq.get('flows_lag_sessions')} session(s)",
              f"- Note: {dq.get('warning') or dq.get('warning_detail') or 'No material lag.'}",
              f"- External sources: {(evidence.get('external_sources') or {}).get('counts', {}).get('ok', 0)} ok / "
              f"{(evidence.get('external_sources') or {}).get('counts', {}).get('unavailable', 0)} unavailable; "
              f"retrieved {(evidence.get('external_sources') or {}).get('retrieved_at', 'unknown')}", ""]
    lines += ["## Market Snapshot", "", "| Asset | Latest | Change | Observation |", "| --- | --- | --- | --- |"]
    for row in report.get("market_snapshot", []):
        lines.append(f"| {row.get('asset','')} | {row.get('latest','')} | {row.get('change','')} | {row.get('observation','')} |")
    lines += ["", "## Macro, Fiscal, Rates, Liquidity and Credit", ""]
    for x in report.get("macro_analysis", []):
        lines.append(f"- {x}")
    lines += ["", "## US Equities and 11 GICS Sectors", "",
              f"Macro source IDs: {', '.join(report.get('macro_source_ids', []))}",
              "", "| GICS sector | Evidence | AI research stance | Source IDs |",
              "| --- | --- | --- | --- |"]
    for row in report.get("sectors", []):
        lines.append(f"| {row.get('sector','')} | {row.get('evidence','')} | {row.get('view','')} | {', '.join(row.get('source_ids', []))} |")
    lines += ["", "### Sector fundamentals, valuation and earnings", "",
              report.get("sector_fundamentals", "No verified point-in-time sector fundamental dataset is available."), "",
              "## Growth vs Value", ""]
    for x in report.get("growth_value", []):
        lines.append(f"- {x}")
    lines += ["", "## ETF Flows and Breadth", ""]
    for x in report.get("flows_breadth", []):
        lines.append(f"- {x}")
    stock_ideas = [i for i in report.get("ideas", []) if i.get("security_type") == "Stock"]
    etf_ideas = [i for i in report.get("ideas", []) if i.get("security_type") == "ETF"]
    lines += ["", "## Stock Ideas", "",
              "Single-stock research uses reported EPS, forward P/E and company fundamentals. All ideas are research judgments, not validated return predictions.", ""]
    if stock_ideas:
        lines += ["| Ticker | Security | Research view | Price | EPS | Forward P/E | Base target | Review |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for i in stock_ideas:
            lines.append(f"| {i.get('ticker','')} | {i.get('name','')} | {i.get('research_view','')} | {i.get('price','')} | {i.get('eps','')} | {i.get('forward_pe','')} | {i.get('base_target','')} | {i.get('review','')} |")
    else:
        lines.append("No qualifying stock ideas this run.")
    lines += ["", "## ETF Ideas", "",
              "ETF research uses holdings, portfolio valuation and exposure. Single-stock EPS × P/E valuation is not applied to ETFs.", ""]
    if etf_ideas:
        lines += ["| Ticker | Fund | Research view | Price | Top holdings | Portfolio valuation | Sector exposure | Review |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for i in etf_ideas:
            th = i.get("top_holdings", "")
            if isinstance(th, (list, tuple)):
                th = ", ".join(str(x) for x in th)
            lines.append(f"| {i.get('ticker','')} | {i.get('name','')} | {i.get('research_view','')} | {i.get('price','')} | {th} | {i.get('portfolio_valuation','')} | {i.get('sector_exposure','')} | {i.get('review','')} |")
    else:
        lines.append("No qualifying ETF ideas this run.")
    for i in report.get("ideas", []):
        lines += [f"### {i.get('ticker','')} — {i.get('name','')}", "",
                  f"- **Type:** {i.get('security_type','')}",
                  f"- **Research view:** {i.get('research_view','')}",
                  f"- **Thesis:** {i.get('thesis','')}",
                  f"- **Source IDs:** {', '.join(i.get('source_ids', []))}",
                  f"- **Catalysts:** {', '.join(i.get('catalysts', []))}",
                  f"- **Risks:** {', '.join(i.get('risks', []))}",
                  f"- **Valuation method:** {i.get('valuation_method','')}",
                  f"- **Valuation assumptions:** {i.get('valuation_assumptions','')}"]
        if i.get("security_type") == "Stock":
            lines += [f"- **EPS:** {i.get('eps','')}",
                      f"- **Forward P/E:** {i.get('forward_pe','')}",
                      f"- **Company fundamentals:** {i.get('company_fundamentals','')}"]
        else:
            th = i.get("top_holdings", "")
            if isinstance(th, (list, tuple)):
                th = ", ".join(str(x) for x in th)
            lines += [f"- **Top holdings:** {th}",
                      f"- **Portfolio valuation:** {i.get('portfolio_valuation','')}",
                      f"- **Sector exposure:** {i.get('sector_exposure','')}",
                      f"- **Underlying drivers:** {i.get('underlying_drivers','')}"]
        lines += [f"- **Bull / Base / Bear:** {i.get('bull_target','')} / {i.get('base_target','')} / {i.get('bear_target','')}",
                  f"- **Scenario probabilities:** {i.get('bull_prob','')} / {i.get('base_prob','')} / {i.get('bear_prob','')}",
                  ""]
    lines += ["## Quant Risk Layer", "", "| Field | Value |", "| --- | --- |",
              "| Volatility model | vol5_ols_v1 |", "| Spike model | spike_logit_v1 |",
              f"| As-of date | {r.get('as_of','')} |", f"| Target window | {r.get('target_start','')} to {r.get('target_end','')} |",
              f"| Forecast 5D annualized volatility | {float(r.get('forecast_vol_5d',0))*100:.2f}% |",
              f"| Historical-vol baseline | {float(r.get('historical_baseline',0))*100:.2f}% |",
              f"| EWMA baseline | {float(r.get('ewma_baseline',0))*100:.2f}% |",
              f"| Spike probability | {float(r.get('spike_probability',0))*100:.2f}% |",
              f"| Spike threshold | {float(r.get('threshold',0))*100:.2f}% |",
              f"| Status | {r.get('status','')} |", f"| Forecast ID | {r.get('forecast_id','')} |",
              "", "Risk-only; no directional interpretation or allocation implication.", "",
              "## Scenarios", ""]
    for horizon in ("short_term", "long_term"):
        lines += [f"### {horizon.replace('_',' ').title()}", "", "| Scenario | Probability | S&P 500 range | Assumptions |", "| --- | --- | --- | --- |"]
        for x in (report.get("scenarios") or {}).get(horizon, []):
            if isinstance(x, dict):
                lines.append(f"| {x.get('scenario','')} | {x.get('probability','')}% | {x.get('range','')} | {x.get('assumptions','')} |")
            else:
                lines.append(f"| {x} | | | |")
        lines.append("")
    lines += ["## Positioning", ""]
    for x in report.get("positioning", []):
        lines.append(f"- {x}")
    lines += ["", "## Catalysts", "", "| Date | Event | Why it matters |", "| --- | --- | --- |"]
    for x in report.get("catalysts", []):
        lines.append(f"| {x.get('date','')} | {x.get('event','')} | {x.get('why','')} |")
    lines += ["", "## Key Risks", ""]
    for x in report.get("risks", []):
        lines.append(f"- {x}")
    lines += ["", "## External Source Ledger", "",
              "| ID | Type | Ticker | Published | Status | URL | Claims |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    ext = (evidence.get("external_sources") or {}).get("sources", [])
    for s in ext[:40]:
        claims = "; ".join(s.get("claims", []))[:500]
        lines.append(f"| {s.get('id','')} | {s.get('kind','')} | {s.get('ticker','') or 'macro'} | {s.get('published_at') or (s.get('retrieved_at') or '')[:10]} | {s.get('status','')} | {s.get('url','')} | {claims} |")
    lines += ["", "## Sources", ""]
    for s in report.get("sources", []):
        lines.append(f"- {s.get('label','')}: {s.get('url','')} ({s.get('date','')})")
    meta = ai_meta or {}
    mm = meta.get("market", {}) if isinstance(meta, dict) else {}
    fr = meta.get("fundamental_research", {}) if isinstance(meta, dict) else {}
    lines += ["", "## AI Provenance", "",
              f"- Provider / model: {meta.get('provider', 'unknown')} / {meta.get('model', 'unknown')}",
              f"- Market research: attempts={mm.get('attempts')} tokens={mm.get('total_tokens')} "
              f"(prompt={mm.get('prompt_tokens')}, completion={mm.get('completion_tokens')}) "
              f"runtime={mm.get('elapsed_s')}s",
              f"- Fundamental research: {'carried forward' if fr.get('cached') else 'generated this run'}"
              + (f" from {fr.get('cached_at')} (age {fr.get('age_hours')}h)" if fr.get('cached') else
                 f" at {fr.get('generated_at', meta.get('generated_at', 'this run'))}"),
              f"- Evidence source dates: {meta.get('source_dates', {})}",
              f"- Evidence fingerprint: {meta.get('fingerprint', '')}",
              "- Source citations are retrieved external records in the ledger above; market values remain from the verified database snapshot.",
              ""]
    lines += ["", "## Disclosures", "",
              "- This is research and market intelligence, not investment advice.",
              "- AI conclusions are research judgments, not validated return predictions.",
              "- The frozen risk layer is risk-only and is not used for directional views or allocations.",
              "- Legacy scores are not active inputs or outputs.", ""]
    return "\n".join(lines)


def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, str):
        return (obj.replace("overweight", "favored")
                   .replace("Overweight", "Favored")
                   .replace("underweight", "less favored")
                   .replace("Underweight", "Less favored")
                   .replace("rule-based", "legacy")
                   .replace("Rule-Based", "Legacy")
                   .replace("CIO", "Legacy")
                   .replace("cio", "legacy"))
    return obj


def _ensure_sources(report, evidence):
    ext = (evidence.get("external_sources") or {}).get("sources", [])
    current = []
    seen = set()
    for s in ext:
        if s.get("status") != "ok":
            continue
        u = str(s.get("url", ""))
        if not u.startswith("http") or u in seen:
            continue
        current.append({
            "id": s.get("id"),
            "label": f"{s.get('kind')} {s.get('ticker') or 'macro'}",
            "url": u,
            "date": s.get("published_at") or (s.get("retrieved_at") or "")[:10],
            "status": s.get("status"),
        })
        seen.add(u)
    report["sources"] = current[:20]
    return report


def _normalize_ideas(report: dict) -> dict:
    if len(report.get("ideas", []) or []) > 5:
        report["ideas"] = report["ideas"][:5]
    for i in report.get("ideas", []) or []:
        if i.get("security_type") == "ETF":
            i.pop("eps", None)
            i.pop("forward_pe", None)
            th = i.get("top_holdings")
            if isinstance(th, str):
                i["top_holdings"] = [x.strip() for x in th.split(",") if x.strip()]
    return report


def _normalize_scenarios(report: dict) -> dict:
    scenarios = report.get("scenarios") or {}
    for horizon in ("short_term", "long_term"):
        rows = scenarios.get(horizon)
        if isinstance(rows, dict):
            scenarios[horizon] = [{"scenario": k, **(v if isinstance(v, dict) else {})} for k, v in rows.items()]
    report["scenarios"] = scenarios
    return report


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def validate(report: dict, evidence: dict, risk: dict) -> list[str]:
    fails = []
    ext = (evidence.get("external_sources") or {}).get("sources", [])
    ok_sources = {s.get("id"): s for s in ext if s.get("status") == "ok"}
    if len(ok_sources) < 10:
        fails.append("external_sources_below_10_ok")
    if len({s.get("url", "").split("/")[2] for s in ok_sources.values() if "/" in s.get("url", "")}) < 4:
        fails.append("external_sources_below_4_domains")
    sectors = report.get("sectors", [])
    if len(sectors) != 11 or {x.get("sector") for x in sectors} != set(GICS):
        fails.append("sectors_not_exactly_11_gics")
    for sec in sectors:
        ids = sec.get("source_ids") or []
        if not ids or any(x not in ok_sources for x in ids):
            fails.append(f"sector_source_ids_invalid:{sec.get('sector')}")
    macro_ids = report.get("macro_source_ids") or []
    if len(macro_ids) < 3 or any(x not in ok_sources for x in macro_ids):
        fails.append("macro_source_ids_invalid")
    ideas = report.get("ideas") or []
    if not 3 <= len(ideas) <= 5:
        fails.append("ideas_not_3_to_5")
    allowed = {x["ticker"] for x in evidence.get("candidates", [])}
    ticker_evidence = evidence.get("ticker_evidence") or {}
    valuation_signatures: dict[tuple, str] = {}
    source_sets: dict[tuple, str] = {}
    narratives: dict[str, str] = {}
    for i in ideas:
        ticker = str(i.get("ticker", "")).upper()
        if ticker not in allowed:
            fails.append(f"idea_ticker_not_in_candidate_universe:{ticker}")
        ids = i.get("source_ids") or []
        if not ids or any(x not in ok_sources for x in ids):
            fails.append(f"idea_source_ids_invalid:{ticker}")
            continue
        if any(ok_sources[x].get("ticker") not in (ticker, None) for x in ids):
            fails.append(f"idea_source_ticker_mismatch:{ticker}")
        ev = ticker_evidence.get(ticker)
        if not ev:
            fails.append(f"ticker_evidence_missing:{ticker}")
        elif any(x not in ev.get("source_ids", []) for x in ids):
            fails.append(f"idea_source_not_bound:{ticker}")
        if ids:
            source_key = tuple(sorted(str(x) for x in ids))
            if source_key in source_sets and source_sets[source_key] != ticker:
                fails.append(f"cross_ticker_source_id_reuse:{source_sets[source_key]}:{ticker}")
            source_sets.setdefault(source_key, ticker)
        thesis = str(i.get("thesis") or "").strip().lower()
        if thesis:
            if thesis in narratives and narratives[thesis] != ticker:
                fails.append(f"cross_ticker_narrative_reuse:{narratives[thesis]}:{ticker}")
            narratives.setdefault(thesis, ticker)
        stype = i.get("security_type")
        if stype == "Stock":
            eps = _num(i.get("eps"))
            pe = _num(i.get("forward_pe"))
            if eps is None:
                fails.append(f"stock_eps_not_numeric:{ticker}")
            if pe is None:
                fails.append(f"stock_forward_pe_not_numeric:{ticker}")
            if eps is not None and pe is not None:
                sig = (round(eps, 6), round(pe, 6))
                if sig in valuation_signatures and valuation_signatures[sig] != ticker:
                    fails.append(f"cross_ticker_valuation_reuse:{valuation_signatures[sig]}:{ticker}")
                valuation_signatures.setdefault(sig, ticker)
            if ev and ev.get("forward_eps") is None:
                fails.append(f"stock_forward_eps_unavailable:{ticker}")
            if ev and ev.get("forward_pe") is None:
                fails.append(f"stock_forward_pe_unavailable:{ticker}")
            if ev and _num(i.get("eps")) != _num(ev.get("forward_eps")):
                fails.append(f"stock_eps_not_evidence_bound:{ticker}")
            if ev and _num(i.get("forward_pe")) != _num(ev.get("forward_pe")):
                fails.append(f"stock_pe_not_evidence_bound:{ticker}")
            assumptions = i.get("assumption_inputs") or {}
            for key in ("base_multiple", "bull_multiple", "bear_multiple"):
                if _num(assumptions.get(key)) is None or _num(assumptions.get(key)) <= 0:
                    fails.append(f"stock_assumption_not_numeric:{ticker}:{key}")
            if not any(ok_sources[x].get("kind") == "filing" for x in ids):
                fails.append(f"stock_missing_filing_source:{ticker}")
            if not any(ok_sources[x].get("kind") in ("stock", "news") for x in ids):
                fails.append(f"stock_missing_market_or_news_source:{ticker}")
            claim_text = " ".join(" ".join(ok_sources[x].get("claims", [])) for x in ids).lower()
            if "diluted eps" not in claim_text:
                fails.append(f"stock_eps_not_source_supported:{ticker}")
            if "forward p/e" not in claim_text:
                fails.append(f"stock_forward_pe_not_source_supported:{ticker}")
        elif stype == "ETF":
            if "eps" in i or "forward_pe" in i:
                fails.append(f"etf_has_stock_valuation_fields:{ticker}")
            for f in ("top_holdings", "portfolio_valuation", "sector_exposure", "underlying_drivers"):
                if not i.get(f):
                    fails.append(f"etf_missing_{f}:{ticker}")
            holdings = i.get("top_holdings")
            if isinstance(holdings, str):
                holdings = [x.strip() for x in holdings.split(",") if x.strip()]
            if not isinstance(holdings, list) or len(holdings) < 3:
                fails.append(f"etf_holdings_incomplete:{ticker}")
            pv = str(i.get("portfolio_valuation") or "")
            if not re.search(r"\d+(?:\.\d+)?\s*%|weight", pv, flags=re.I):
                fails.append(f"etf_holdings_weights_not_numeric:{ticker}")
            if ev:
                if len(ev.get("holdings") or []) < 3:
                    fails.append(f"etf_evidence_holdings_incomplete:{ticker}")
                if not ev.get("holdings_coverage_pct") or float(ev["holdings_coverage_pct"]) <= 0:
                    fails.append(f"etf_evidence_coverage_missing:{ticker}")
            assumptions = i.get("assumption_inputs") or {}
            for key in ("base_return_pct", "bull_return_pct", "bear_return_pct"):
                if _num(assumptions.get(key)) is None:
                    fails.append(f"etf_assumption_not_numeric:{ticker}:{key}")
            if not any(ok_sources[x].get("kind") in ("etf", "news") for x in ids):
                fails.append(f"etf_missing_etf_or_news_source:{ticker}")
            claim_text = " ".join(" ".join(ok_sources[x].get("claims", [])) for x in ids).lower()
            if "top holdings" not in claim_text and "p/e" not in claim_text:
                fails.append(f"etf_valuation_not_source_supported:{ticker}")
        else:
            fails.append(f"idea_security_type_invalid:{ticker}")
        for k in ("research_view", "valuation_assumptions"):
            if not i.get(k):
                fails.append(f"idea_missing_{k}:{ticker}")
        if stype == "Stock":
            if not i.get("thesis"):
                fails.append(f"stock_missing_thesis:{ticker}")
            for k in ("base_target", "bull_target", "bear_target"):
                if not i.get(k):
                    fails.append(f"stock_missing_{k}:{ticker}")
    for horizon in ("short_term", "long_term"):
        rows = (report.get("scenarios") or {}).get(horizon) or []
        if len(rows) != 3 or any(not isinstance(x, dict) for x in rows):
            fails.append(f"scenarios_{horizon}_malformed")
        else:
            try:
                if abs(sum(float(x.get("probability", 0)) for x in rows) - 100) > 0.5:
                    fails.append(f"scenarios_{horizon}_probabilities_not_100")
            except (TypeError, ValueError):
                fails.append(f"scenarios_{horizon}_bad_probability")
            for x in rows:
                range_text = str(x.get("range") or "").strip()
                numeric_range = bool(
                    re.search(r"\d+(?:\.\d+)?\s*%", range_text)
                    or re.search(r"\d.*(?:-|–|—|\bto\b).*\d", range_text, flags=re.I)
                )
                if not numeric_range:
                    fails.append(f"scenarios_{horizon}_range_not_numeric")
                if _num(x.get("return_pct")) is None:
                    fails.append(f"scenarios_{horizon}_return_pct_missing")
    urls = [x.get("url", "") for x in report.get("sources", []) if str(x.get("url", "")).startswith("http")]
    domains = {u.split("/")[2].lower() for u in urls if "/" in u}
    if len(urls) < 10 or len(domains) < 4:
        fails.append("sources_below_10_urls_or_4_domains")
    text_ = json.dumps(report).lower()
    for term in ("cio", "bullish_score", "market_regime_v2", "forward signal", "sector score", "overweight", "underweight", "rule-based"):
        if term in text_:
            fails.append(f"legacy_term:{term}")
    if not risk.get("forecast_id"):
        fails.append("missing_risk_forecast")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--model", default="upstage/solar-mini4:free")
    ap.add_argument("--out", default="/tmp/aegis_hourly")
    args = ap.parse_args()
    if args.publish and args.dry_run:
        raise SystemExit("choose --dry-run or --publish")
    dry = not args.publish
    now = datetime.now(timezone.utc)
    as_of = last_completed_session(now)
    evidence = load_evidence(as_of)
    if evidence.get("data_qc", {}).get("status") == "FAIL":
        print("DATA QC FAILED — previous report retained:", evidence["data_qc"])
        return 1
    try:
        ext = retrieve_external_sources(evidence, evidence.get("candidates", []), max_stocks=8, max_etfs=24)
    except Exception as exc:
        print("EXTERNAL SOURCE RETRIEVAL FAILED — previous report retained:", repr(exc))
        return 5
    evidence["external_sources"] = ext
    evidence["ticker_evidence"] = build_ticker_evidence(evidence, ext)
    ok_ids = {s["id"] for s in ext.get("sources", []) if s.get("status") == "ok"}
    eligible = []
    for c in evidence.get("candidates", []):
        ids = [s["id"] for s in ext.get("sources", []) if s.get("ticker") == c["ticker"] and s.get("status") == "ok"]
        if ids:
            c = dict(c)
            c["source_ids"] = ids
            eligible.append(c)
    evidence["candidates"] = eligible
    if not eligible:
        print("EXTERNAL SOURCE RETRIEVAL FAILED — no eligible candidates; previous report retained")
        return 5
    risk = risk_state(as_of, publish=not dry)
    alerts = []
    dq = evidence.get("data_qc", {})
    if dq.get("status") not in ("PASS",):
        alerts.append({"type": "data_qc", "detail": dq})
    if (risk.get("hourly_estimate") or {}).get("source") != "frozen_eod_forecast":
        risk["experimental_hourly"] = True
        risk["hourly_estimate"] = {**(risk.get("hourly_estimate") or {}),
                                    "experimental": True, "eligible_for_formal": False}
        risk["note"] = "Intraday inputs are experimental; not eligible for formal performance."
    outcomes = outcomes_state()
    if risk.get("status") == "UNAVAILABLE":
        print("RISK UNAVAILABLE — report not published")
        return 2
    ai_meta = {}
    try:
        report, ai_meta = call_ai(evidence, risk, args.model)
    except Exception as exc:
        print(f"AI FAILED — retaining previous report: {exc}")
        return 3
    ai_meta["generated_at"] = now.isoformat()
    report["ai_meta"] = ai_meta
    report = _sanitize(report)
    allowed_tickers = {c.get("ticker") for c in evidence.get("candidates", [])}
    kept_ideas = []
    dropped_ideas = []
    for idea in report.get("ideas", []) or []:
        if str(idea.get("ticker", "")).upper() in allowed_tickers:
            kept_ideas.append(idea)
        else:
            dropped_ideas.append(idea.get("ticker"))
    if dropped_ideas:
        print("DROPPED_UNSOURCED_IDEAS:", dropped_ideas)
    report["ideas"] = kept_ideas
    report = _ensure_sources(report, evidence)
    report = _normalize_ideas(report)
    report = _normalize_scenarios(report)
    report = apply_evidence_bindings(report, evidence)
    report["snapshot_id"] = f"aegis-hourly-{now.strftime('%Y%m%dT%H%M%SZ')}"
    report["risk"] = risk
    report["outcomes"] = outcomes
    fails = validate(report, evidence, risk)
    if fails:
        failed_dir = ROOT / "data" / "ai_failed"
        failed_dir.mkdir(parents=True, exist_ok=True)
        failed_path = failed_dir / f"report_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
        failed_path.write_text(json.dumps({
            "fails": fails, "report": report,
            "evidence_fingerprint": _research_fingerprint(evidence, risk),
            "model": (ai_meta or {}).get("model"), "created_at": now.isoformat(),
        }, indent=2, default=str), encoding="utf-8")
        if dry:
            Path("/tmp/aegis_hourly").mkdir(parents=True, exist_ok=True)
            Path("/tmp/aegis_hourly/report.failed.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"REPORT QC FAILED: {fails}; artifact={failed_path}")
        return 4
    generated = now.astimezone(ZoneInfo("Asia/Hong_Kong")).strftime("%d %B %Y, %H:%M %Z")
    risk_sha = _sha(json.dumps(risk, indent=2, default=str))
    content_sha = _sha(json.dumps({
        "report": {k: v for k, v in report.items() if k not in ("snapshot_id", "ai_meta")},
        "evidence_sha": _sha(json.dumps(evidence, sort_keys=True, default=str)),
        "risk": risk,
    }, sort_keys=True, default=str))
    md = render(report, evidence, risk, outcomes, generated, ai_meta)
    report_sha = _sha(md)
    outdir = Path(args.out) if dry else DATA
    outdir.mkdir(parents=True, exist_ok=True)
    existing_snap_path = DATA / "report_snapshot.json"
    existing_content = None
    if existing_snap_path.exists():
        try:
            existing_content = json.loads(existing_snap_path.read_text(encoding="utf-8")).get("content_sha256")
        except Exception:
            existing_content = None
    snapshot = {
        "schema": "aegis-hourly-snapshot-v1",
        "snapshot_id": report["snapshot_id"],
        "generated_at_utc": now.isoformat(),
        "as_of": evidence["as_of"],
        "latest_equity_date": evidence["latest_equity_date"],
        "latest_macro_date": evidence["latest_macro_date"],
        "data_qc": evidence.get("data_qc", {}),
        "model": ai_meta.get("model", args.model),
        "ai": ai_meta,
        "source_dates": ai_meta.get("source_dates", {}),
        "external_sources": (evidence.get("external_sources") or {}).get("sources", []),
        "source_retrieval": (evidence.get("external_sources") or {}).get("counts", {}),
        "evidence_sha256": _sha(json.dumps(evidence, sort_keys=True, default=str)),
        "report_sha256": report_sha,
        "risk_layer_sha256": risk_sha,
        "content_sha256": content_sha,
        "risk": risk,
        "outcomes": outcomes,
        "alerts": alerts,
        "qc": "PASS",
    }
    if not dry and existing_content == content_sha:
        print(json.dumps({"status": "duplicate", "content_sha256": content_sha, "as_of": evidence["as_of"]}, indent=2))
        return 0
    (outdir / "latest-report.md").write_text(md, encoding="utf-8")
    (outdir / ("snapshot.json" if dry else "report_snapshot.json")).write_text(
        json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    (outdir / ("risk.json" if dry else "risk_layer.json")).write_text(
        json.dumps(risk, indent=2, default=str), encoding="utf-8")
    published = False
    if not dry:
        repo = ROOT / "market-dashboard"
        files = ["data/latest-report.md", "data/report_snapshot.json", "data/risk_layer.json"]
        subprocess.run(["git", "-C", str(repo), "add", *files], check=False)
        diff = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"], check=False)
        if diff.returncode != 0:
            commit = subprocess.run(["git", "-C", str(repo), "commit", "-m", "report: hourly AI-first macro snapshot"], capture_output=True, text=True)
            if commit.returncode == 0:
                push = subprocess.run(["git", "-C", str(repo), "push", "origin", "main"], capture_output=True, text=True)
                published = push.returncode == 0
                if not published:
                    print("PUBLISH WARNING: git push failed; files remain committed locally")
            else:
                print("PUBLISH WARNING: git commit failed")
    print(json.dumps({"dry_run": dry, "output": str(outdir), "report_sha256": report_sha,
                      "as_of": evidence["as_of"], "model": ai_meta.get("model", args.model), "ai": ai_meta, "risk": risk,
                      "outcomes": outcomes, "published": published}, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
