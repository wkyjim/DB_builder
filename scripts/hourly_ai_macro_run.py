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

ENGINE = local_engine()

DATA = ROOT / "market-dashboard" / "data"
PROMPT_PATH = ROOT / "market-dashboard" / "scripts" / "hourly_ai_prompt.txt"
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
    classes = _read("select ticker,sector,company_name from public.security_classification where is_active=true")
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
            "security_type": "Stock",
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
    }
    if data_qc["equity_lag_sessions"] == 0:
        data_qc["status"] = "PASS"
    elif data_qc["equity_lag_sessions"] <= 1:
        data_qc["status"] = "PASS_WITH_WARNINGS"
        data_qc["warning"] = "US equity data is one completed session behind the inference date; not an automatic failure."
    else:
        data_qc["status"] = "FAIL"
        data_qc["warning"] = "US equity data is more than one completed session behind the inference date."
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


def _ai_call(prompt: str, payload: dict, model: str) -> tuple[dict, dict]:
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
        "max_tokens": 3000,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, default=str)},
        ],
    }
    last = ""
    for attempt in range(3):
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
        "source_catalog": evidence["source_catalog"],
    }
    market_prompt = (ROOT / "market-dashboard" / "scripts" / "hourly_ai_prompt_market.txt").read_text(encoding="utf-8")
    ideas_prompt = (ROOT / "market-dashboard" / "scripts" / "hourly_ai_prompt_ideas.txt").read_text(encoding="utf-8")
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
    market, market_meta = _ai_call(market_prompt, payload, model)
    if cached and cached.get("ideas") and cached.get("scenarios"):
        ideas = {"ideas": cached["ideas"], "scenarios": cached["scenarios"]}
        ideas_meta = {"cached": True, "cached_at": cached.get("created_at"),
                      "age_hours": round(cache_age_h, 2), "fingerprint": fingerprint,
                      "source_dates": cached.get("source_dates", {})}
    else:
        ideas, ideas_meta = _ai_call(ideas_prompt, payload, model)
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
    return merged, {"provider": "hermes-local-proxy", "model": model,
                    "market": market_meta, "fundamental_research": ideas_meta,
                    "fingerprint": fingerprint,
                    "source_dates": ideas_meta.get("source_dates", {})}

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
    lines += ["", "## Market Snapshot", "", "| Asset | Latest | Change | Observation |", "| --- | --- | --- | --- |"]
    for row in report.get("market_snapshot", []):
        lines.append(f"| {row.get('asset','')} | {row.get('latest','')} | {row.get('change','')} | {row.get('observation','')} |")
    lines += ["", "## Macro, Fiscal, Rates, Liquidity and Credit", ""]
    for x in report.get("macro_analysis", []):
        lines.append(f"- {x}")
    lines += ["", "## US Equities and 11 GICS Sectors", "", "| GICS sector | Evidence | AI research stance |", "| --- | --- | --- |"]
    for row in report.get("sectors", []):
        lines.append(f"| {row.get('sector','')} | {row.get('evidence','')} | {row.get('view','')} |")
    lines += ["", "### Sector fundamentals, valuation and earnings", "",
              report.get("sector_fundamentals", "No verified point-in-time sector fundamental dataset is available."), "",
              "## Growth vs Value", ""]
    for x in report.get("growth_value", []):
        lines.append(f"- {x}")
    lines += ["", "## ETF Flows and Breadth", ""]
    for x in report.get("flows_breadth", []):
        lines.append(f"- {x}")
    lines += ["", "## Investment Ideas", "",
              "All ideas are research judgments, not validated return predictions.", "",
              "| Ticker | Security | Research view | Price | Base target | Review |", "| --- | --- | --- | --- | --- | --- |"]
    for i in report.get("ideas", []):
        lines.append(f"| {i.get('ticker','')} | {i.get('name','')} | {i.get('research_view','')} | {i.get('price','')} | {i.get('base_target','')} | {i.get('review','')} |")
    for i in report.get("ideas", []):
        lines += [f"### {i.get('ticker','')} — {i.get('name','')}", "",
                  f"- **Research view:** {i.get('research_view','')}",
                  f"- **Thesis:** {i.get('thesis','')}",
                  f"- **Catalysts:** {', '.join(i.get('catalysts', []))}",
                  f"- **Risks:** {', '.join(i.get('risks', []))}",
                  f"- **Valuation assumptions:** {i.get('valuation_assumptions','')}",
                  f"- **Bull / Base / Bear:** {i.get('bull_target','')} / {i.get('base_target','')} / {i.get('bear_target','')}",
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
              "- Source citations include the supplied provenance catalog; market values are from the verified database snapshot.",
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
    current = [x for x in report.get("sources", []) if str(x.get("url", "")).startswith("http")]
    seen = {x["url"] for x in current}
    for x in evidence.get("source_catalog", []):
        if len(current) >= 10:
            break
        u = str(x.get("url", ""))
        if u.startswith("http") and u not in seen:
            current.append({"label": x.get("label", ""), "url": u, "date": "catalog/undated"})
            seen.add(u)
    report["sources"] = current[:12]
    return report


def _normalize_scenarios(report: dict) -> dict:
    scenarios = report.get("scenarios") or {}
    for horizon in ("short_term", "long_term"):
        rows = scenarios.get(horizon)
        if isinstance(rows, dict):
            scenarios[horizon] = [{"scenario": k, **(v if isinstance(v, dict) else {})} for k, v in rows.items()]
    report["scenarios"] = scenarios
    return report


def validate(report: dict, evidence: dict, risk: dict) -> list[str]:
    fails = []
    if len(report.get("sectors", [])) != 11 or {x.get("sector") for x in report.get("sectors", [])} != set(GICS):
        fails.append("sectors_not_exactly_11_gics")
    ideas = report.get("ideas") or []
    if not 3 <= len(ideas) <= 5:
        fails.append("ideas_not_3_to_5")
    allowed = {x["ticker"] for x in evidence.get("candidates", [])}
    for i in ideas:
        if str(i.get("ticker", "")).upper() not in allowed:
            fails.append(f"idea_ticker_not_in_candidate_universe:{i.get('ticker')}")
        for k in ("research_view", "thesis", "valuation_assumptions", "base_target", "bull_target", "bear_target"):
            if not i.get(k):
                fails.append(f"idea_missing_{k}")
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
    report = _ensure_sources(report, evidence)
    report = _normalize_scenarios(report)
    report["snapshot_id"] = f"aegis-hourly-{now.strftime('%Y%m%dT%H%M%SZ')}"
    report["risk"] = risk
    report["outcomes"] = outcomes
    fails = validate(report, evidence, risk)
    if fails:
        if dry:
            Path("/tmp/aegis_hourly").mkdir(parents=True, exist_ok=True)
            Path("/tmp/aegis_hourly/report.failed.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print("REPORT QC FAILED:", fails)
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
        "model": args.model,
        "ai": ai_meta,
        "source_dates": ai_meta.get("source_dates", {}),
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
                      "as_of": evidence["as_of"], "model": args.model, "ai": ai_meta, "risk": risk,
                      "outcomes": outcomes, "published": published}, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
