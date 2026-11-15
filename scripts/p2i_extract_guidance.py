#!/usr/bin/env python3
"""P2I: extract official SEC 8-K earnings-release guidance passages."""
from __future__ import annotations
import json, re, sys, time
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from sqlalchemy import text as sql_text

ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))
from db_builder.config import local_engine
from sqlalchemy import text as sql_text
UA={'User-Agent':'AEGIS research contact@example.com'}
TICKERS=['MU','EXE','SNDK','LITE','CPT','SMCI','APP','RDDT','TPL','REG','VRSN','CME']
KEY=re.compile(r'\b(guidance|outlook|expects?|forecast|anticipates?|revenue|margin|capex|capital expenditure|earnings per share|EPS|volume)\b',re.I)
NUM=re.compile(r'\d')


def get(url, **kw):
    r=requests.get(url,headers=UA,timeout=35,**kw); r.raise_for_status(); return r

def main():
    with local_engine().connect() as c:
        rows=c.execute(sql_text("select ticker,cik from fundamentals.sec_coverage where ticker=any(:t)"),{'t':TICKERS}).fetchall()
    ciks={t:c for t,c in rows}
    out={}
    for t in TICKERS:
        cik=ciks.get(t)
        rec={'ticker':t,'cik':cik,'guidance':[],'filings_checked':0,'errors':[]}
        if not cik:
            rec['errors'].append('missing CIK'); out[t]=rec; continue
        try:
            sub=get(f'https://data.sec.gov/submissions/CIK{cik}.json').json()
            recent=sub.get('filings',{}).get('recent',{})
            forms=recent.get('form',[]); accs=recent.get('accessionNumber',[])
            dates=recent.get('filingDate',[])
            picked=[]
            for form,acc,date in zip(forms,accs,dates):
                if form in ('8-K','8-K/A') and len(picked)<4:
                    picked.append((form,acc,date))
            for form,acc,date in picked:
                rec['filings_checked']+=1
                nodash=acc.replace('-','')
                try:
                    idx=get(f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}/index.json').json()
                    files=[x.get('name') for x in idx.get('directory',{}).get('item',[])]
                    exhibits=[f for f in files if re.search(r'(99[.-]?1|ex99|ex-99)',str(f),re.I)]
                    if not exhibits: continue
                    for f in exhibits[:2]:
                        url=f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}/{f}'
                        html=get(url).text
                        soup=BeautifulSoup(html,'html.parser')
                        for tag in soup(['script','style','noscript']): tag.decompose()
                        text=' '.join(soup.get_text(' ').split())
                        sentences=re.split(r'(?<=[.!?])\s+',text)
                        found=[]
                        for s in sentences:
                            if 25<len(s)<650 and KEY.search(s) and NUM.search(s):
                                found.append(s.strip())
                        for s in found[:3]:
                            rec['guidance'].append({'form':form,'filing_date':date,'accession':acc,
                                'source_url':url,'passage':s})
                        time.sleep(0.12)
                except Exception as e:
                    rec['errors'].append(f'{acc}: {type(e).__name__}: {e}')
        except Exception as e:
            rec['errors'].append(f'submissions: {type(e).__name__}: {e}')
        out[t]=rec
        print(t,'filings',rec['filings_checked'],'passages',len(rec['guidance']),'errors',len(rec['errors']))
    print(json.dumps({'generated_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                      'results':out},indent=2,default=str))

if __name__=='__main__': main()
