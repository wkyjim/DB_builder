#!/usr/bin/env python3
"""Incremental SEC Companyfacts ingestion for S&P 500 fundamentals.

Additive schema only: fundamentals.sec_facts, sec_metrics, sec_coverage,
sec_ingest_runs. Company-reported facts remain separate from vendor estimates.
No AI and no report publication.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from db_builder.config import local_engine  # noqa: E402
from db_builder.trading_calendar import is_valid_nyse_session  # noqa: E402

UA = "AEGIS research contact@example.com"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
METRICS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "diluted_eps": ["EarningsPerShareDiluted"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "debt": ["LongTermDebtNoncurrent", "LongTermDebt", "DebtCurrent",
             "LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cik_map() -> dict[str, str]:
    r = requests.get(SEC_TICKERS, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    out = {}
    for row in r.json().values():
        out[str(row.get("ticker", "")).upper()] = str(row.get("cik_str", "")).zfill(10)
    return out


def _period_type(start, end) -> str:
    if not start:
        return "I"
    try:
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    except Exception:
        return "UNKNOWN"
    if 80 <= days <= 100:
        return "Q"
    if 350 <= days <= 380:
        return "Y"
    if 170 <= days <= 300:
        return "YTD"
    return "OTHER"


def _sec_get(cik: str, retries: int = 4) -> dict:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {r.status_code}")
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("SEC fetch exhausted")


def _acceptance_map(cik: str) -> dict[str, str]:
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    recent = (r.json().get("filings") or {}).get("recent") or {}
    accn = recent.get("accessionNumber") or []
    accepted = recent.get("acceptanceDateTime") or []
    return {str(a): str(t) for a, t in zip(accn, accepted) if a and t}


def _next_business_day(filed: str | None) -> str | None:
    if not filed:
        return None
    d = date.fromisoformat(filed) + timedelta(days=1)
    for _ in range(10):
        if is_valid_nyse_session(d):
            return d.isoformat()
        d += timedelta(days=1)
    return d.isoformat()
    return d.isoformat()


def ensure_schema(engine) -> None:
    ddl = [
        "create schema if not exists fundamentals",
        """create table if not exists fundamentals.sec_ingest_runs (
            run_id text primary key, started_at timestamptz not null,
            completed_at timestamptz, requested int, ok int, failed int,
            source text, git_commit text, notes text)""",
        """create table if not exists fundamentals.sec_facts (
            cik text not null, ticker text not null, taxonomy text not null,
            tag text not null, unit text not null, value numeric,
            start_date date, end_date date not null, fy int, fp text,
            form text, accession text not null, filed date, accepted_at timestamptz,
            frame text, period_type text, source_url text, retrieved_at timestamptz,
            run_id text, is_restatement boolean default false,
            primary key (cik, tag, unit, start_date, end_date, accession))""",
        """create table if not exists fundamentals.sec_metrics (
            cik text not null, ticker text not null, metric text not null,
            period_end date not null, period_type text not null, value numeric,
            unit text not null, formula text, fiscal_year int, fiscal_period text,
            source_accession text, filed date, available_at timestamptz,
            source_url text, is_derived boolean, retrieved_at timestamptz,
            share_basis_status text, split_status text,
            primary key (cik, metric, period_end, period_type, unit))""",
        """create table if not exists fundamentals.sec_coverage (
            ticker text primary key, cik text, status text, fact_count int,
            metric_count int, latest_filed date, error text, retrieved_at timestamptz)""",
        "alter table fundamentals.sec_facts add column if not exists fact_id bigserial",
        "alter table fundamentals.sec_facts drop constraint if exists sec_facts_pkey",
        "alter table fundamentals.sec_facts alter column start_date drop not null",
        "alter table fundamentals.sec_facts add column if not exists available_at date",
        "alter table fundamentals.sec_facts add column if not exists availability_basis text",
        "create unique index if not exists ux_sec_facts_period on fundamentals.sec_facts(cik,tag,unit,coalesce(start_date,date '0001-01-01'),end_date,accession)",
        "create index if not exists ix_sec_facts_ticker_end on fundamentals.sec_facts(ticker,end_date)",
        "create index if not exists ix_sec_metrics_ticker_metric on fundamentals.sec_metrics(ticker,metric,period_end)",
    ]
    with engine.begin() as c:
        for q in ddl:
            c.execute(text(q))


def parse_company(cik: str, ticker: str, run_id: str, source_url: str,
                  acceptance: dict[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    data = _sec_get(cik)
    acceptance = acceptance or {}
    facts = data.get("facts", {}).get("us-gaap", {})
    raw = []
    for metric, tags in METRICS.items():
        for tag in tags:
            block = facts.get(tag, {})
            for unit, observations in (block.get("units") or {}).items():
                if unit not in ("USD", "USD/shares", "shares") and metric not in ("diluted_eps", "diluted_shares"):
                    continue
                for x in observations:
                    if x.get("val") is None or not x.get("end") or not x.get("accn"):
                        continue
                    start = x.get("start")
                    raw.append({
                        "cik": cik, "ticker": ticker, "taxonomy": "us-gaap", "tag": tag,
                        "metric": metric, "unit": unit, "value": _num(x.get("val")),
                        "start_date": start, "end_date": x.get("end"), "fy": x.get("fy"),
                        "fp": x.get("fp"), "form": x.get("form"), "accession": x.get("accn"),
                        "filed": x.get("filed"),
                        "accepted_at": acceptance.get(str(x.get("accn"))) or None,
                        "available_at": (date.fromisoformat(acceptance[str(x.get("accn"))][:10])
                                         if str(x.get("accn")) in acceptance
                                         else date.fromisoformat(_next_business_day(x.get("filed")))
                                         if x.get("filed") else None),
                        "availability_basis": ("accepted" if str(x.get("accn")) in acceptance
                                               else "next_session_fallback"),
                        "frame": x.get("frame"),
                        "period_type": _period_type(start, x.get("end")),
                        "source_url": source_url, "retrieved_at": datetime.now(timezone.utc),
                        "run_id": run_id, "is_restatement": False,
                    })
    # Mark restatements: same metric/tag/unit/period with more than one accession.
    groups = defaultdict(set)
    for r in raw:
        groups[(r["metric"], r["tag"], r["unit"], r["start_date"], r["end_date"])].add(r["accession"])
    for r in raw:
        r["is_restatement"] = len(groups[(r["metric"], r["tag"], r["unit"],
                                          r["start_date"], r["end_date"])]) > 1
    return raw, data


def latest_observations(raw: list[dict]) -> dict[tuple, dict]:
    """Latest-filed observation per metric/unit/period, preserving all raw facts."""
    out = {}
    for r in raw:
        key = (r["metric"], r["tag"], r["unit"], r["start_date"], r["end_date"], r["period_type"])
        old = out.get(key)
        if old is None or (r.get("filed") or "") >= (old.get("filed") or ""):
            out[key] = r
    return out


def _tag_priority(metric: str, tag: str) -> int:
    return METRICS.get(metric, []).index(tag) if tag in METRICS.get(metric, []) else 999


def _best_annual(rows: list[dict], metric: str) -> dict | None:
    if not rows:
        return None
    latest_end = max(r["end_date"] for r in rows)
    candidates = [r for r in rows if r["end_date"] == latest_end]
    latest_first = sorted(candidates, key=lambda r: r.get("filed") or "", reverse=True)
    return sorted(latest_first, key=lambda r: _tag_priority(metric, r["tag"]))[0]


def normalize(ticker: str, cik: str, raw: list[dict], as_of: str | None = None) -> list[dict]:
    if as_of:
        raw = [r for r in raw if str(r.get("filed") or "") <= as_of]
    obs = latest_observations(raw)
    metrics = []
    def add(metric, period_end, ptype, value, unit, formula, row, derived=True):
        if value is None:
            return
        metrics.append({
            "cik": cik, "ticker": ticker, "metric": metric,
            "period_end": period_end, "period_type": ptype, "value": value,
            "unit": unit, "formula": formula, "fiscal_year": row.get("fy"),
            "fiscal_period": row.get("fp"), "source_accession": row.get("accession"),
            "filed": row.get("filed"),
            "available_at": (datetime.combine(row["available_at"], datetime.min.time(), tzinfo=timezone.utc)
                             if row.get("available_at") else None),
            "source_url": row.get("source_url"), "is_derived": derived,
            "retrieved_at": row.get("retrieved_at"),
            "share_basis_status": "unverified",
            "split_status": "unverified",
        })
    # Store latest annual observations directly.
    annual = {}
    for r in obs.values():
        if r["period_type"] == "Y":
            annual.setdefault(r["metric"], []).append(r)
    for metric, rows in annual.items():
        r = _best_annual(rows, metric)
        if r:
            add(metric, r["end_date"], "Y", r["value"], r["unit"], "reported_annual", r, derived=False)
            # YoY growth for revenue/EPS/net income, within the same selected tag.
            same_tag = [x for x in rows if x["tag"] == r["tag"]]
            prior = next((x for x in sorted(same_tag, key=lambda y: y["end_date"], reverse=True)
                          if x["end_date"] < r["end_date"]), None)
            if prior and prior["value"] not in (None, 0) and metric != "diluted_eps":
                add(metric + "_growth_yoy", r["end_date"], "Y",
                    (r["value"] / prior["value"] - 1) * 100, "percent", "latest_annual / prior_annual - 1", r)
    # Margins from latest annual revenue/operating/net.
    def latest_metric(metric):
        return _best_annual(annual.get(metric) or [], metric)
    rev, op, net = latest_metric("revenue"), latest_metric("operating_income"), latest_metric("net_income")
    if rev and op and rev["value"]:
        op_margin = op["value"] / rev["value"] * 100
        if abs(op_margin) <= 100:
            add("operating_margin", rev["end_date"], "Y", op_margin, "percent",
                "operating_income / revenue", rev)
    if rev and net and rev["value"]:
        net_margin = net["value"] / rev["value"] * 100
        if abs(net_margin) <= 100:
            add("net_margin", rev["end_date"], "Y", net_margin, "percent",
                "net_income / revenue", rev)
    ocf, capex = latest_metric("operating_cash_flow"), latest_metric("capex")
    if ocf and capex:
        add("free_cash_flow", ocf["end_date"], "Y", ocf["value"] - capex["value"], "USD",
            "operating_cash_flow - capex", ocf)
    # TTM revenue/net income from Q observations only; no mixing with YTD/annual.
    def ttm(metric):
        rows = [r for r in obs.values() if r["metric"] == metric and r["period_type"] == "Q"]
        rows.sort(key=lambda x: x["end_date"], reverse=True)
        if len(rows) < 4:
            return None
        first4 = rows[:4]
        # Require four distinct consecutive quarters, approximately 90 days apart.
        ends = [date.fromisoformat(r["end_date"]) for r in first4]
        if any(80 > (a - b).days or (a - b).days > 100 for a, b in zip(ends, ends[1:])):
            return None
        return sum(r["value"] for r in first4), first4[0]
    for metric in ("revenue", "net_income", "operating_cash_flow"):
        t = ttm(metric)
        if t:
            value, row = t
            add(metric + "_ttm", row["end_date"], "TTM", value, row["unit"], "sum_latest_four_quarters", row)
    return metrics


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--dedupe-issuer", action="store_true", default=True)
    ap.add_argument("--tickers", default=None)
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()
    run_id = args.run_id or "secfacts_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    engine = local_engine()
    ensure_schema(engine)
    with engine.connect() as c:
        latest = c.execute(text("select max(date) from public.us_equities")).scalar()
        rows = c.execute(text("""
            select distinct m.ticker, s.cik, s.company_name
            from public.sp500_membership_history m
            left join public.security_classification s
              on s.ticker=m.ticker and s.is_active=true
            where m.start_date<=:d and (m.end_date is null or m.end_date>:d)
            order by m.ticker
        """), {"d": latest}).fetchall()
    sec_map = _cik_map()
    universe = []
    missing = []
    seen = {}
    for ticker, cik, name in rows:
        t = str(ticker).upper()
        if not cik:
            alt = t.replace(".", "-")
            cik = sec_map.get(alt) or sec_map.get(t)
            if not cik:
                missing.append(t)
                continue
        c = str(cik).zfill(10)
        if args.dedupe_issuer and c in seen:
            continue
        seen[c] = t
        universe.append((t, c, name))
    if args.tickers:
        wanted = {x.strip().upper() for x in args.tickers.split(",")}
        universe = [x for x in universe if x[0] in wanted]
    universe = universe[args.offset:args.offset + args.limit]
    with engine.begin() as c:
        c.execute(text("""insert into fundamentals.sec_ingest_runs
            (run_id,started_at,requested,ok,failed,source,notes)
            values (:r, now(), :n, 0, 0, 'SEC companyfacts',
                    'missing CIK mappings: ' || :m)
            on conflict (run_id) do nothing"""), {"r": run_id, "n": len(universe), "m": ",".join(missing)})
    ok = failed = 0
    for ticker, cik, name in universe:
        source_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        try:
            raw, _ = parse_company(cik, ticker, run_id, source_url,
                                   acceptance=_acceptance_map(cik))
            metrics = normalize(ticker, cik, raw, as_of=str(latest))
            with engine.begin() as c:
                for r in raw:
                    c.execute(text("""insert into fundamentals.sec_facts
                        (cik,ticker,taxonomy,tag,unit,value,start_date,end_date,fy,fp,form,
                         accession,filed,accepted_at,frame,period_type,source_url,retrieved_at,
                         run_id,is_restatement,available_at,availability_basis)
                        values (:cik,:ticker,:taxonomy,:tag,:unit,:value,:start_date,:end_date,
                         :fy,:fp,:form,:accession,:filed,:accepted_at,:frame,:period_type,
                         :source_url,:retrieved_at,:run_id,:is_restatement,:available_at,:availability_basis)
                        on conflict (cik,tag,unit,coalesce(start_date,date '0001-01-01'),end_date,accession)
                        do update set accepted_at=coalesce(excluded.accepted_at,sec_facts.accepted_at),
                        available_at=coalesce(excluded.available_at,sec_facts.available_at),
                        availability_basis=coalesce(excluded.availability_basis,sec_facts.availability_basis),
                        retrieved_at=excluded.retrieved_at, run_id=excluded.run_id"""), r)
                for m in metrics:
                    c.execute(text("""insert into fundamentals.sec_metrics
                        (cik,ticker,metric,period_end,period_type,value,unit,formula,
                         fiscal_year,fiscal_period,source_accession,filed,available_at,
                         source_url,is_derived,retrieved_at,share_basis_status,split_status)
                        values (:cik,:ticker,:metric,:period_end,:period_type,:value,:unit,:formula,
                         :fiscal_year,:fiscal_period,:source_accession,:filed,:available_at,
                         :source_url,:is_derived,:retrieved_at,:share_basis_status,:split_status)
                        on conflict (cik,metric,period_end,period_type,unit) do update set
                         value=excluded.value, source_accession=excluded.source_accession,
                         filed=excluded.filed, available_at=excluded.available_at,
                         retrieved_at=excluded.retrieved_at"""), m)
                c.execute(text("""insert into fundamentals.sec_coverage
                    (ticker,cik,status,fact_count,metric_count,latest_filed,error,retrieved_at)
                    values (:t,:c,'ok',:f,:m,(select max(filed) from fundamentals.sec_facts where ticker=:t),
                    null,now())
                    on conflict (ticker) do update set cik=excluded.cik,status='ok',
                    fact_count=excluded.fact_count,metric_count=excluded.metric_count,
                    latest_filed=excluded.latest_filed,error=null,retrieved_at=now()"""),
                    {"t": ticker, "c": cik, "f": len(raw), "m": len(metrics)})
            ok += 1
            print(f"OK {ticker} cik={cik} facts={len(raw)} metrics={len(metrics)}")
        except Exception as exc:
            failed += 1
            with engine.begin() as c:
                c.execute(text("""insert into fundamentals.sec_coverage
                    (ticker,cik,status,fact_count,metric_count,latest_filed,error,retrieved_at)
                    values (:t,:c,'failed',0,0,null,:e,now())
                    on conflict (ticker) do update set status='failed',error=excluded.error,retrieved_at=now()"""),
                    {"t": ticker, "c": cik, "e": repr(exc)[:500]})
            print(f"FAIL {ticker} cik={cik}: {exc}")
        time.sleep(0.12)
    with engine.begin() as c:
        c.execute(text("""update fundamentals.sec_ingest_runs
            set completed_at=now(),ok=:o,failed=:f where run_id=:r"""),
            {"o": ok, "f": failed, "r": run_id})
    print(json.dumps({"run_id": run_id, "requested": len(universe), "ok": ok,
                      "failed": failed, "missing_cik": missing}, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
