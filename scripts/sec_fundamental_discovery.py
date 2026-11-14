#!/usr/bin/env python3
"""Read-only independent fundamental discovery buckets from normalized SEC data.

No composite scores, no AI consensus, no production activation.
"""
from __future__ import annotations
import argparse, json, sys
from collections import defaultdict
from pathlib import Path
from sqlalchemy import text
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'src'))
from db_builder.config import local_engine


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--as-of',default=None); a=ap.parse_args()
    with local_engine().connect() as c:
        as_of=a.as_of or str(c.execute(text("select max(date) from public.us_equities")).scalar())
        rows=c.execute(text("""
          select m.ticker,m.metric,m.value,m.period_end,m.source_accession,m.available_at,
                 coalesce(s.sector,(select x.sector from public.security_classification x
                   where x.ticker=m.ticker order by x.is_active desc limit 1)) sector
          from fundamentals.sec_metrics m
          left join public.security_classification s on s.ticker=m.ticker and s.is_active=true
          where m.period_type='Y' and m.available_at<=:d
        """),{'d':as_of}).fetchall()
    by=defaultdict(dict)
    for t,metric,val,period,acc,avail,sector in rows:
        by[t].setdefault('ticker',t); by[t]['sector']=sector
        if metric=='revenue': by[t]['revenue']=float(val) if val is not None else None; by[t]['period']=str(period); by[t]['accession']=acc
        elif metric=='revenue_growth_yoy': by[t]['revenue_growth']=float(val) if val is not None else None
        elif metric=='operating_margin': by[t]['operating_margin']=float(val) if val is not None else None
        elif metric=='net_margin': by[t]['net_margin']=float(val) if val is not None else None
        elif metric=='free_cash_flow': by[t]['fcf']=float(val) if val is not None else None
        elif metric=='diluted_eps': by[t]['eps']=float(val) if val is not None else None
        elif metric=='diluted_shares': by[t]['shares']=float(val) if val is not None else None
    universe=[v for v in by.values() if v.get('revenue') is not None and v.get('revenue_growth') is not None]
    def top(key, n, pred=lambda x: True, rev=True):
        vals=[x for x in universe if x.get(key) is not None and pred(x)]
        vals.sort(key=lambda x:x[key],reverse=rev)
        return vals[:n]
    growth=top('revenue_growth',15)
    # Operating margins are not comparable for banks/insurers; use net margin there.
    def margin_key(x):
        if x.get('sector') in ('Financials','Real Estate'):
            return x.get('net_margin')
        return x.get('operating_margin')
    for x in universe: x['margin']=margin_key(x)
    margin=top('margin',15,lambda x:x.get('sector') not in ('Financials','Real Estate'))
    for x in universe:
        x['fcf_margin']=(x['fcf']/x['revenue']*100) if x.get('fcf') is not None and x.get('revenue') else None
    cash=top('fcf_margin',15,lambda x:x.get('sector') not in ('Financials','Real Estate'))
    negative=top('revenue_growth',10,lambda x:x.get('revenue_growth')<0 and x.get('net_margin') is not None and x['net_margin']<0,rev=False)
    def compact(x):
        return {'ticker':x['ticker'],'sector':x.get('sector'),'period_end':x.get('period'),
                'revenue':x.get('revenue'),'revenue_growth':x.get('revenue_growth'),
                'operating_margin':x.get('operating_margin'),'net_margin':x.get('net_margin'),
                'fcf_margin':x.get('fcf_margin'),'source_accession':x.get('accession'),
                'missing_flags':[k for k in ('revenue','revenue_growth','operating_margin','net_margin','fcf','eps','shares') if x.get(k) is None]}
    out={'as_of':as_of,'universe':len(by),'eligible':len(universe),
         'buckets':{'growth':[compact(x) for x in growth],
                    'margin':[compact(x) for x in margin],
                    'cash_flow':[compact(x) for x in cash],
                    'negative':[compact(x) for x in negative],
                    'valuation':{'available':False,
                                 'reason':'share-basis and split consistency not verified; no SEC historical P/E or per-share growth'}},
         'excluded':{'missing_revenue_or_growth':len(by)-len(universe)}}
    print(json.dumps(out,indent=2,default=str))


if __name__=='__main__': main()
