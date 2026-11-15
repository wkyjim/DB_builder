#!/usr/bin/env python3
"""Targeted per-share and corporate-action QC for P2H research candidates."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))
from db_builder.config import local_engine
from sqlalchemy import text

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--tickers',required=True); a=ap.parse_args()
    tickers=[x.strip().upper() for x in a.tickers.split(',') if x.strip()]
    with local_engine().connect() as c:
        rows=c.execute(text("""select ticker,metric,period_end,value,source_accession
          from fundamentals.sec_metrics where period_type='Y' and ticker=any(:t)
          and metric in ('diluted_eps','diluted_shares','common_net_income','net_income')
          order by ticker,period_end"""),{'t':tickers}).fetchall()
        splits=c.execute(text("""select ticker,split_date,ratio from public.stock_split_events
          where ticker=any(:t) order by ticker,split_date"""),{'t':tickers}).fetchall()
    by={}
    for t,m,p,v,a in rows:
        by.setdefault(t,{}).setdefault(str(p),{})[m]={'value':float(v) if v is not None else None,
                                                     'accession':a}
    sp={}
    for t,d,r in splits: sp.setdefault(t,[]).append({'date':str(d),'ratio':r})
    out=[]
    for t in tickers:
        checks=[]
        for p,m in by.get(t,{}).items():
            eps=m.get('diluted_eps',{}).get('value'); sh=m.get('diluted_shares',{}).get('value')
            inc=m.get('common_net_income',{}).get('value')
            if inc is None: inc=m.get('net_income',{}).get('value')
            if eps is not None and sh is not None and inc:
                calc=eps*sh; diff=abs(calc-inc)/abs(inc)*100 if inc else None
                checks.append({'period':p,'eps':eps,'diluted_shares':sh,
                               'income_basis':'common' if 'common_net_income' in m else 'net',
                               'income':inc,'eps_times_shares':round(calc,2),
                               'difference_pct':round(diff,3) if diff is not None else None,
                               'accessions':{k:v['accession'] for k,v in m.items() if isinstance(v,dict)}})
        out.append({'ticker':t,'identity_checks':checks,'split_events':sp.get(t,[]),
                    'split_history_status':'recorded_events_only' if sp.get(t) else 'no_event_in_local_table',
                    'split_history_complete':False,
                    'share_basis_status':'unverified',
                    'per_share_growth_eligible':False,
                    'historical_pe_eligible':False,
                    'reason':'split history and all corporate actions are not independently complete; identity checks do not authorize per-share growth'})
    print(json.dumps({'tickers':tickers,'results':out},indent=2))

if __name__=='__main__': main()
