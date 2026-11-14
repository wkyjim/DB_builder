#!/usr/bin/env python3
"""Build a lightweight, point-in-time S&P 500 fundamental evidence layer.

This is not a scoring model. It records only source-backed fields and explicit
missing-data flags. The full universe is sourced from the local equity store;
optional external source snapshots enrich shortlist names with Yahoo/SEC facts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from db_builder.config import local_engine  # noqa: E402
from sqlalchemy import text  # noqa: E402

FIELDS = ["revenue_growth", "eps_growth", "gross_margin", "operating_margin",
          "profit_margin", "forward_eps", "forward_pe", "fiscal_period",
          "eps_period", "fy1_growth", "fy1_revenue_growth", "fy2_status",
          "consensus_available", "revisions_available"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build(as_of: date, external_snapshot: Path | None) -> dict:
    source_fingerprint = None
    with local_engine().connect() as c:
        latest = c.execute(text("select max(date) from public.us_equities")).scalar()
        members = c.execute(text("""
            select distinct ticker from public.sp500_membership_history
            where start_date <= :d and (end_date is null or end_date > :d)
            order by ticker
        """), {"d": as_of}).fetchall()
        prices = c.execute(text("""
            select distinct on (ticker) ticker,date,close,volume,pe_ttm,mkt_cap,retrieved_at
            from public.us_equities
            where date <= :d and ticker = ANY(:tickers)
            order by ticker,date desc
        """), {"d": as_of, "tickers": [r[0] for r in members]}).fetchall()
        pe_rows = c.execute(text("""
            select distinct on (ticker) ticker,date,pe_ttm
            from public.us_equities
            where date >= (cast(:d as date) - interval '30 days') and date <= :d
              and pe_ttm is not null and ticker = ANY(:tickers)
            order by ticker,date desc
        """), {"d": as_of, "tickers": [r[0] for r in members]}).fetchall()
        source_fingerprint = hashlib.sha256(json.dumps({
            "latest": str(latest),
            "members": [r[0] for r in members],
            "retrieved": [str(r[-1]) for r in prices],
        }, default=str, sort_keys=True).encode()).hexdigest()
    price_map = {r[0]: r for r in prices}
    pe_map = {r[0]: r for r in pe_rows}
    external = {}
    if external_snapshot and external_snapshot.exists():
        snap = json.loads(external_snapshot.read_text(encoding="utf-8"))
        for s in snap.get("external_sources") or []:
            t = str(s.get("ticker") or "").upper()
            if not t or s.get("status") != "ok":
                continue
            if s.get("kind") not in ("stock", "etf"):
                continue
            external[t] = {"facts": s.get("facts") or {}, "source_id": s.get("id")}
    rows = []
    for (ticker,) in members:
        p = price_map.get(ticker)
        pe = pe_map.get(ticker)
        facts = (external.get(ticker) or {}).get("facts") or {}
        row = {
            "ticker": ticker,
            "as_of": str(as_of),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "price": _num(p[2]) if p else None,
            "volume": _num(p[3]) if p else None,
            "pe_ttm": _num(p[4]) if p and p[4] is not None else (_num(pe[2]) if pe else None),
            "pe_as_of": str(pe[1]) if pe else (str(p[1]) if p else None),
            "market_cap": _num(p[5]) if p else None,
            "source_ids": ["DB.us_equities:" + str(p[1])] if p else [],
            **{k: None for k in FIELDS},
        }
        if facts:
            row.update({
                "revenue_growth": _num(facts.get("revenue_growth")),
                "eps_growth": _num(facts.get("earnings_growth")),
                "gross_margin": _num(facts.get("gross_margin")),
                "operating_margin": _num(facts.get("operating_margin")),
                "profit_margin": _num(facts.get("profit_margin")),
                "forward_eps": _num(facts.get("forward_eps")),
                "forward_pe": _num(facts.get("forward_pe")),
                "fiscal_period": facts.get("fiscal_period"),
                "eps_period": facts.get("eps_period"),
                "fy1_growth": _num(facts.get("fy1_growth")),
                "fy1_revenue_growth": _num(facts.get("fy1_revenue_growth")),
                "fy2_status": facts.get("fy2_status") or "not available from retrieved source",
                "consensus_available": bool(facts.get("consensus_available")),
                "revisions_available": bool(facts.get("revisions_available")),
            })
            if (external.get(ticker) or {}).get("source_id"):
                row["source_ids"].append(external[ticker]["source_id"])
        row["missing_flags"] = [f for f in FIELDS if row.get(f) is None]
        rows.append(row)
    return {
        "schema": "aegis.fundamental_universe.v1",
        "as_of": str(as_of),
        "source_fingerprint": source_fingerprint,
        "coverage": {
            "universe": len(rows),
            "with_price": sum(1 for r in rows if r["price"] is not None),
            "with_pe": sum(1 for r in rows if r["pe_ttm"] is not None),
            "with_revenue_growth": sum(1 for r in rows if r["revenue_growth"] is not None),
            "with_forward_eps": sum(1 for r in rows if r["forward_eps"] is not None),
            "with_consensus": sum(1 for r in rows if r["consensus_available"]),
        },
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--external-snapshot", default=None)
    ap.add_argument("--out", default=str(ROOT / "data" / "fundamentals" / "sp500_latest.json"))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    with local_engine().connect() as c:
        as_of = date.fromisoformat(args.as_of) if args.as_of else c.execute(text("select max(date) from public.us_equities")).scalar()
    out = Path(args.out)
    # Refresh only when the source fingerprint changes, unless forced.
    if out.exists() and not args.force:
        try:
            old = json.loads(out.read_text(encoding="utf-8"))
            old_fp = old.get("source_fingerprint")
        except Exception:
            old_fp = None
    else:
        old_fp = None
    payload = build(as_of, Path(args.external_snapshot) if args.external_snapshot else None)
    if old_fp == payload["source_fingerprint"] and not args.force:
        print(f"SKIP unchanged: {out} ({payload['source_fingerprint'][:12]})")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"WROTE {out}")
    print(json.dumps(payload["coverage"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
