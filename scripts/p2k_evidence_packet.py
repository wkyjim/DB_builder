#!/usr/bin/env python3
"""P2J evidence IDs: compact, grounded packet for AI claim validation."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))
from db_builder.config import local_engine
from sqlalchemy import text

SECTOR_ETF={'Information Technology':'XLK','Energy':'XLE','Real Estate':'XLRE','Communication Services':'XLC',
            'Financials':'XLF','Health Care':'XLV','Industrials':'XLI','Consumer Discretionary':'XLY',
            'Consumer Staples':'XLP','Materials':'XLB','Utilities':'XLU'}
POSITIVE=['MU','SNDK','APP','RDDT','SMCI','LITE']
NEGATIVE=['LYB','DOW','GIS','ECHO','IFF','ALB']
TICKERS=POSITIVE+NEGATIVE

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--as-of',default=None); a=ap.parse_args()
    with local_engine().connect() as c:
        as_of=a.as_of or str(c.execute(text("select max(date) from public.us_equities")).scalar())
        rows=c.execute(text("""select ticker,metric,value,period_end,period_type,unit,formula,fiscal_year,fiscal_period,fiscal_period_original,metric_period_type,
          source_accession,available_at,split_status,share_basis_status
          from fundamentals.sec_metrics where ticker=any(:t) and available_at<=:d
          and period_type in ('Y','Q') order by ticker,metric,period_end"""),{'t':TICKERS,'d':as_of}).fetchall()
        sectors=dict(c.execute(text("""select distinct on (ticker) ticker,sector
          from public.security_classification where ticker=any(:t) order by ticker,is_active desc"""),{'t':TICKERS}).fetchall())
        etfs=list(SECTOR_ETF.values())
        etfrows=c.execute(text("""select ticker,date,close from public.us_equities
          where ticker=any(:e) and date<=(:d) order by ticker,date"""),{'e':etfs,'d':as_of}).fetchall()
    # actuals compact: latest annual plus recent quarters, one row per metric/period
    by={}
    for t,m,v,p,pt,u,f,fy,fp,fpo,mpt,acc,av,ss,sb in rows:
        x=by.setdefault(t,{})
        key=f'{m}|{pt}|{p}'
        x.setdefault(key,{'id':f'SEC:{t}:{m}:{pt}:{p}','metric':m,'period_type':pt,'period_end':str(p),
                          'value':float(v) if v is not None else None,'unit':u,'formula':f,
                          'fiscal_year':fy,'fiscal_period':fp,'fiscal_period_original':fpo,'metric_period_type':mpt,
                          'source_accession':acc,'available_at':str(av),'split_status':ss,'share_basis_status':sb})
    cache=json.load(open(ROOT/'data/fundamentals/sp500_latest.json'))
    vendor={r['ticker']:r for r in cache['rows'] if r['ticker'] in TICKERS}
    guidance=json.load(open(ROOT/'data/p2i_guidance.json'))['results']
    # sector ETF 20-session returns
    etfret={}
    for e in etfs:
        vals=[(d,float(cl)) for t,d,cl in etfrows if t==e]
        if len(vals)>21:
            etfret[e]={'id':f'MKT:{e}:{as_of}:ret20','ticker':e,'as_of':as_of,
                       'return_20d_pct':round((vals[-1][1]/vals[-21][1]-1)*100,2)}
    out=[]
    for t in TICKERS:
        actuals=list(by.get(t,{}).values())
        actuals=sorted(actuals,key=lambda x:(x['period_type']!='Y',x['period_end']),reverse=True)[:18]
        sec=[{'id':x['id'],'ticker':t,'kind':'SEC_ACTUAL','metric':x['metric'],'period':x['period_end'],
              'period_type':x['period_type'],'value':x['value'],'unit':x['unit'],
              'formula':x['formula'],'fiscal_year':x['fiscal_year'],'fiscal_period':x['fiscal_period'],
              'fiscal_period_original':x.get('fiscal_period_original'),'metric_period_type':x.get('metric_period_type'),
              'source':'SEC Companyfacts','accession':x['source_accession'],
              'available_at':x['available_at']} for x in actuals]
        g=[]
        for i,x in enumerate(guidance.get(t,{}).get('guidance',[]),1):
            g.append({'id':f'GUID:{t}:{x["accession"]}:{i}','ticker':t,'kind':'MANAGEMENT_GUIDANCE',
                      'metric':'guidance','period':x['filing_date'],'unit':'text','value':None,
                      'form':x['form'],'filing_date':x['filing_date'],'accession':x['accession'],
                      'source_url':x['source_url'],'passage':x['passage']})
        ve=vendor.get(t,{}); v=[]
        for field in ('forward_eps','forward_pe','fy1_growth','fy1_revenue_growth'):
            if ve.get(field) is not None:
                unit={'forward_eps':'USD/shares','forward_pe':'x','fy1_growth':'%','fy1_revenue_growth':'%'}.get(field,'')
                v.append({'id':f'VEN:{t}:{field}:YahooCache:{ve.get("as_of")}','ticker':t,'kind':'VENDOR_ESTIMATE',
                          'metric':field,'period':'fiscal_period_unverified','unit':unit,
                          'field':field,'value':ve[field],'as_of':ve.get('as_of'),
                          'fiscal_period':ve.get('fiscal_period'),'fiscal_year_mapping':'unverified',
                          'consensus_available':bool(ve.get('vendor_estimate',{}).get('consensus_available')),
                          'revisions_available':bool(ve.get('vendor_estimate',{}).get('revisions_available'))})
        s=sectors.get(t); e=SECTOR_ETF.get(s); mkt=etfret.get(e) if e else None
        external=[]
        if mkt:
            mkt={'id':mkt['id'],'ticker':mkt['ticker'],'kind':'EXTERNAL_MARKET','metric':'return_20d',
                 'period':mkt['as_of'],'unit':'%','value':mkt['return_20d_pct'],'as_of':mkt['as_of']}
            external=[mkt]
        ca=[]
        if t=='SNDK':
            ca=[{'id':'CA:SNDK:2026-11-19:8for1','ticker':'SNDK','kind':'CORPORATE_ACTION',
                 'action':'8-for-1 forward stock split','record_date':'2026-11-09',
                 'distribution_date':'2026-11-19','first_post_split_trading':'2026-11-20',
                 'metric':'split_ratio','period':'2026-11-19','unit':'ratio','value':'8-for-1',
                 'status':'pending','basis':'pre-split guidance; post-split trading not yet effective',
                 'source_url':'https://www.sec.gov/Archives/edgar/data/2023554/000162828026068243/sndkq1-27ex991xpressrelease.htm'}]
        out.append({'ticker':t,'sector':s,'sec_actuals':sec,'management_guidance':g,
                    'vendor_estimates':v,'external_industry':external,'corporate_actions':ca,
                    'warnings':[
                        f'{t}: vendor fiscal period unverified' if v else f'{t}: vendor forward estimates unavailable',
                        f'{t}: management guidance unavailable' if not g else f'{t}: guidance extracted; verify GAAP/non-GAAP and split basis',
                        f'{t}: share basis and split history unresolved',
                    ]})
    packet={'as_of':as_of,'schema':'aegis.p2k.evidence.v1','candidates':out,
            'deterministic_disclosures':[
              'Vendor FY+1 is not FY2027 or FY2028 consensus; fiscal periods remain unverified.',
              'Management guidance is separate from analyst consensus and AI assumptions.',
              'Share-basis and corporate-action history is incomplete; per-share valuation is withheld.',
              'No probabilities or unsupported targets are permitted.',
            ]}
    print(json.dumps(packet,indent=2,default=str))

if __name__=='__main__': main()
