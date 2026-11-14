#!/usr/bin/env python3
"""Read-only normalized SEC fundamental evidence for controlled research.

Company-reported actuals only. No vendor consensus, no AI assumptions, no
target generation. This module is intentionally not called by the production
hourly pipeline.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from sqlalchemy import text
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from db_builder.config import local_engine


def load(tickers: list[str] | None = None, as_of: str | None = None) -> dict:
    with local_engine().connect() as c:
        if as_of is None:
            as_of = str(c.execute(text("select max(date) from public.us_equities")).scalar())
        where = "and m.ticker = ANY(:tickers)" if tickers else ""
        params = {"d": as_of}
        if tickers:
            params["tickers"] = [t.upper() for t in tickers]
        rows = c.execute(text(f"""
            select m.ticker,m.metric,m.period_type,m.period_end,m.value,m.unit,
                   m.formula,m.fiscal_year,m.fiscal_period,m.source_accession,m.filed,
                   m.available_at,m.source_url,m.share_basis_status,m.split_status
            from fundamentals.sec_metrics m
            where m.available_at <= :d {where}
            order by m.ticker,m.period_end desc,m.metric
        """), params).fetchall()
        out = {}
        for r in rows:
            d = out.setdefault(r[0], {"ticker": r[0], "as_of": as_of,
                                      "company_reported": {}, "vendor_estimate": {},
                                      "ai_assumed": {}})
            key = r[1]
            if key not in d["company_reported"]:
                d["company_reported"][key] = {
                    "value": float(r[4]) if r[4] is not None else None,
                    "unit": r[5], "period_type": r[2], "period_end": str(r[3]),
                    "formula": r[6], "fiscal_year": r[7], "fiscal_period": r[8],
                    "source_accession": r[9], "filed": str(r[10]) if r[10] else None,
                    "available_at": str(r[11]) if r[11] else None,
                    "source_url": r[12],
                    "share_basis_status": r[13], "split_status": r[14],
                }
        return out


if __name__ == "__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--tickers",default=None); ap.add_argument("--as-of",default=None)
    a=ap.parse_args()
    t=[x.strip() for x in a.tickers.split(",")] if a.tickers else None
    print(json.dumps(load(t,a.as_of),indent=2,default=str))
