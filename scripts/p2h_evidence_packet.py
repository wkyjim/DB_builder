#!/usr/bin/env python3
"""Build a read-only P2H evidence packet for dynamically selected SEC candidates."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'data/fundamentals/sp500_latest.json').exists():
    ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))
from db_builder.config import local_engine
from sqlalchemy import text


def candidates(c, as_of):
    rows=c.execute(text("""
      select ticker,metric,value,period_end,source_accession,available_at
      from fundamentals.sec_metrics where period_type='Y' and available_at<=:d
    """),{'d':as_of}).fetchall()
    by={}
    for t,m,v,p,a,av in rows:
        x=by.setdefault(t,{'ticker':t,'actuals':{}})
        x['actuals'].setdefault(m,{'value':float(v) if v is not None else None,
                                   'period_end':str(p),'accession':a,'available_at':str(av)})
    def v(x,k): return (x['actuals'].get(k) or {}).get('value')
    u=[x for x in by.values() if v(x,'revenue') is not None]
    growth=sorted([x for x in u if v(x,'revenue_growth_yoy') is not None],key=lambda x:v(x,'revenue_growth_yoy'),reverse=True)[:8]
    margin=sorted([x for x in u if v(x,'operating_margin') is not None and v(x,'operating_margin')<=100],key=lambda x:v(x,'operating_margin'),reverse=True)[:8]
    cash=sorted([x for x in u if v(x,'free_cash_flow') is not None and v(x,'revenue')],key=lambda x:v(x,'free_cash_flow')/v(x,'revenue'),reverse=True)[:8]
    neg=sorted([x for x in u if v(x,'revenue_growth_yoy') is not None and v(x,'net_margin') is not None and v(x,'net_margin')<0],key=lambda x:v(x,'revenue_growth_yoy'))[:5]
    out=[]; seen=set()
    for x in growth+margin+cash+neg:
        if x['ticker'] not in seen: seen.add(x['ticker']); out.append(x)
    return out[:12]


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--as-of',default=None); a=ap.parse_args()
    with local_engine().connect() as c:
        as_of=a.as_of or str(c.execute(text("select max(date) from public.us_equities")).scalar())
        cand=candidates(c,as_of)
        tick=[x['ticker'] for x in cand]
        splits=c.execute(text("""select ticker,split_date,ratio from public.stock_split_events
          where ticker = any(:t) order by ticker,split_date"""),{'t':tick}).fetchall()
        split_by={}
        for t,d,r in splits: split_by.setdefault(t,[]).append({'date':str(d),'ratio':r})
        coverage=c.execute(text("""select ticker,cik,status from fundamentals.sec_coverage
          where ticker = any(:t)"""),{'t':tick}).fetchall()
        cik_by={t:cik for t,cik,_ in coverage}
        sector_rows=c.execute(text("""select distinct on (ticker) ticker,sector
          from public.security_classification where ticker=any(:t)
          order by ticker,is_active desc"""),{'t':tick}).fetchall()
        sector_by={t:s for t,s in sector_rows}
        filing_rows=c.execute(text("""select ticker,form,accession,max(filed)
          from fundamentals.sec_facts where ticker=any(:t)
          group by ticker,form,accession order by ticker,max(filed) desc"""),{'t':tick}).fetchall()
        filings_by={}
        for t,form,acc,filed in filing_rows:
            filings_by.setdefault(t,[]).append({'form':form,'accession':acc,'filed':str(filed)})
    cache=json.load(open(ROOT/'data/fundamentals/sp500_latest.json'))
    vendor={r['ticker']:r for r in cache['rows'] if r['ticker'] in set(tick)}
    out=[]
    for x in cand:
        t=x['ticker']; v=vendor.get(t,{}); ve=v.get('vendor_estimate') or {}
        sb=[]
        for metric in ('diluted_eps','diluted_shares','common_net_income','net_income'):
            if x['actuals'].get(metric): sb.append(metric)
        # Per-period identity is checked in the dedicated share-basis QC script.
        warnings=[]
        if not ve.get('forward_eps'): warnings.append(f'{t}: vendor forward EPS unavailable')
        if ve.get('forward_eps') and not v.get('fiscal_period'): warnings.append(f'{t}: vendor fiscal period unverified')
        if not ve.get('consensus_available'): warnings.append(f'{t}: vendor consensus unavailable')
        if not ve.get('revisions_available'): warnings.append(f'{t}: earnings revisions unavailable')
        if not split_by.get(t): warnings.append(f'{t}: no recorded split event; full corporate-action history unresolved')
        warnings.append(f'{t}: share-basis and per-share valuation incomplete')
        out.append({
            'ticker':t,'cik':cik_by.get(t),'sector':sector_by.get(t),
            'company_reported':x['actuals'],
            'vendor_estimate':ve,
            'vendor_source':{'provider':'Yahoo Finance via fundamental cache','source_ids':v.get('source_ids',[]),
                             'fiscal_period':v.get('fiscal_period'),'fiscal_year_mapping':v.get('fiscal_year_mapping','unverified')} if ve else {},
            'management_guidance':{'available':False,'reason':'no verified guidance extraction in controlled evidence packet'},
            'recent_filings':filings_by.get(t,[])[:6],
            'earnings_disclosures':{'available':bool(filings_by.get(t)),'basis':'SEC filing metadata only; management guidance text not extracted'},
            'corporate_actions':split_by.get(t,[]),
            'warnings':warnings,
        })
    packet={'as_of':as_of,'universe':len(cand),'candidates':out,
            'deterministic_warnings':[
              'FY2028 consensus and revisions are unavailable; do not treat vendor FY+1 as FY2027/FY2028.',
              'Vendor estimates are not used for targets unless fiscal period and share basis are verified.',
              'Share-basis and split history are incomplete for per-share valuation.',
              'No management guidance was extracted; absence is recorded, not replaced by AI assumptions.',
              'No scenario probabilities are supported by this packet.',
            ]}
    print(json.dumps(packet,indent=2,default=str))


if __name__=='__main__': main()
