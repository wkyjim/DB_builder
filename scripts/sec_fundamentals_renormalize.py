#!/usr/bin/env python3
"""Rebuild normalized SEC metrics from preserved raw facts (no network)."""
from __future__ import annotations
import argparse, sys
from pathlib import Path
from sqlalchemy import text
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src')); sys.path.insert(0,str(ROOT/'scripts'))
from db_builder.config import local_engine
from sec_fundamentals_ingest import normalize, METRICS, ensure_schema
TAG_TO_METRIC={tag:m for m,tags in METRICS.items() for tag in tags}

ap=argparse.ArgumentParser(); ap.add_argument('--tickers',default=None); a=ap.parse_args()
engine=local_engine()
ensure_schema(engine)
with engine.connect() as c:
    as_of=str(c.execute(text("select max(date) from public.us_equities")).scalar())
    tickers=[x.strip().upper() for x in a.tickers.split(',')] if a.tickers else [r[0] for r in c.execute(text("select distinct ticker from fundamentals.sec_facts order by ticker")).fetchall()]
for t in tickers:
    with engine.connect() as c:
        rows=c.execute(text("""select cik,ticker,taxonomy,tag,unit,value,start_date,end_date,fy,fp,form,
            accession,filed,accepted_at,frame,period_type,source_url,retrieved_at,run_id,is_restatement,
            available_at,availability_basis from fundamentals.sec_facts where ticker=:t and quarantined is not true"""),{'t':t}).fetchall()
    if not rows:
        print('SKIP',t); continue
    cik=rows[0][0]
    raw=[]
    for r in rows:
        d=dict(r._mapping)
        d['metric']=TAG_TO_METRIC.get(d.get('tag'))
        for k in ('start_date','end_date','filed'):
            if d.get(k) is not None: d[k]=str(d[k])
        raw.append(d)
    metrics=normalize(t,cik,raw,as_of=as_of)
    with engine.begin() as c:
        c.execute(text("delete from fundamentals.sec_metrics where ticker=:t"),{'t':t})
        for m in metrics:
            c.execute(text("""insert into fundamentals.sec_metrics
                (cik,ticker,metric,period_end,period_type,value,unit,formula,
                 fiscal_year,fiscal_period,fiscal_period_original,metric_period_type,
                 start_date,end_date,derivation_method,source_accession,filed,available_at,
                 source_url,is_derived,retrieved_at,share_basis_status,split_status)
                values (:cik,:ticker,:metric,:period_end,:period_type,:value,:unit,:formula,
                 :fiscal_year,:fiscal_period,:fiscal_period_original,:metric_period_type,
                 :start_date,:end_date,:derivation_method,:source_accession,:filed,:available_at,
                 :source_url,:is_derived,:retrieved_at,:share_basis_status,:split_status)"""),m)
        c.execute(text("update fundamentals.sec_coverage set metric_count=:n,retrieved_at=now() where ticker=:t"),{'n':len(metrics),'t':t})
    print('OK',t,'metrics',len(metrics))
