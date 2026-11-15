#!/usr/bin/env python3
"""Controlled, read-only P2G AI research repeatability test.

Uses normalized SEC actuals only. No vendor consensus is invented; no targets or
probabilities are requested. Results are written to /tmp and never published.
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src')); sys.path.insert(0,str(ROOT/'scripts'))
from db_builder.config import local_engine
from sqlalchemy import text

PROMPT = """You are AEGIS, an institutional fundamental researcher. Use only the supplied company-reported SEC evidence. Do not invent consensus, guidance, targets, probabilities, P/E, EPS growth or corporate actions. If consensus or FY2028 data is missing, say so.
Compare the candidate companies and select at most 5 for qualitative research. For each selected company explain historical fundamental drivers, earnings quality, 2027-2028 industry demand and company outlook, management guidance vs independent assumptions (if absent, say unavailable), valuation evidence, catalysts, risks, failure scenarios, and why it is preferable to named rejected alternatives. Do not force a price target or scenario probability.
Return one JSON object: {"selected":[{"ticker":"...","stance":"BUY|NEUTRAL|AVOID|SELL","thesis":"...","drivers":["..."],"outlook_2027_2028":"...","guidance_vs_assumptions":"...","valuation_evidence":"...","catalysts":["..."],"risks":["..."],"failure_scenario":"...","why_preferred":"...","alternatives_considered":["..."]}],"rejected":[{"ticker":"...","reason":"..."}],"missing_data":["..."]}.
"""


def load_candidates(as_of):
    with local_engine().connect() as c:
        rows=c.execute(text("""
          select ticker,metric,value,period_end,period_type,source_accession,available_at,unit
          from fundamentals.sec_metrics where period_type='Y' and available_at<=:d
        """),{'d':as_of}).fetchall()
    by={}
    for t,m,v,p,pt,acc,av,u in rows:
        x=by.setdefault(t,{'ticker':t,'company_reported':{},'vendor_estimate':{},'ai_assumed':{}})
        if m not in x['company_reported']:
            x['company_reported'][m]={'value':float(v) if v is not None else None,'unit':u,
                                      'period_end':str(p),'source_accession':acc,
                                      'available_at':str(av)}
    def metric(x,k):
        z=x['company_reported'].get(k) or {}
        return z.get('value')
    universe=[x for x in by.values() if metric(x,'revenue') is not None]
    growth=sorted([x for x in universe if metric(x,'revenue_growth_yoy') is not None],
                  key=lambda x: metric(x,'revenue_growth_yoy'),reverse=True)[:8]
    margin=sorted([x for x in universe if metric(x,'operating_margin') is not None and metric(x,'operating_margin')<=100],
                  key=lambda x: metric(x,'operating_margin'),reverse=True)[:8]
    cash=sorted([x for x in universe if metric(x,'free_cash_flow') is not None and metric(x,'revenue')],
                key=lambda x: (metric(x,'free_cash_flow')/metric(x,'revenue')),reverse=True)[:8]
    neg=sorted([x for x in universe if metric(x,'revenue_growth_yoy') is not None and metric(x,'net_margin') is not None and metric(x,'net_margin')<0],
               key=lambda x: metric(x,'revenue_growth_yoy'))[:5]
    seen={}; out=[]
    for x in growth+margin+cash+neg:
        if x['ticker'] not in seen:
            seen[x['ticker']]=1; out.append(x)
    return out[:12]


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--runs',type=int,default=3); ap.add_argument('--as-of',default=None)
    a=ap.parse_args()
    with local_engine().connect() as c:
        as_of=a.as_of or str(c.execute(text("select max(date) from public.us_equities")).scalar())
    evidence=load_candidates(as_of)
    results=[]
    for run in range(1,a.runs+1):
        payload={'as_of':as_of,'candidates':evidence,
                 'explicit_limitations':['FY2028 consensus unavailable','vendor consensus not supplied in this controlled packet','share-basis unverified','no price targets permitted']}
        t0=time.monotonic(); err=None; data=None; usage={}
        try:
            r=requests.post('http://127.0.0.1:8645/v1/chat/completions',
                headers={'Authorization':'Bearer local'},
                json={'model':'upstage/solar-mini4:free','temperature':0.0,
                      'max_tokens':3500,'response_format':{'type':'json_object'},
                      'messages':[{'role':'system','content':PROMPT},
                                  {'role':'user','content':json.dumps(payload,default=str)}]},timeout=240)
            usage=r.json().get('usage') or {}
            content=r.json()['choices'][0]['message'].get('content') or ''
            data=json.loads(content)
        except Exception as exc:
            err=repr(exc)
        results.append({'run':run,'runtime_s':round(time.monotonic()-t0,2),'usage':usage,
                        'selected':[x.get('ticker') for x in (data or {}).get('selected',[])],
                        'rejected':[x.get('ticker') for x in (data or {}).get('rejected',[])],
                        'company_keys':sorted((data or {}).get('selected',[{}])[0].keys()) if (data or {}).get('selected') else [],
                        'missing_data':(data or {}).get('missing_data',[]),'error':err})
        print(json.dumps(results[-1],default=str))
    print(json.dumps({'as_of':as_of,'candidates':[x['ticker'] for x in evidence],'runs':results},indent=2,default=str))


if __name__=='__main__': main()
