#!/usr/bin/env python3
"""P2R: add industry-level direct peers and alternative-opportunity peers."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))
from db_builder.config import local_engine
from sqlalchemy import text

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True); ap.add_argument('--out',required=True); a=ap.parse_args()
    x=json.load(open(a.input,encoding='utf-8')); tick=[c['ticker'] for c in x['candidates']]
    with local_engine().connect() as c:
        rows=c.execute(text("""select distinct on (ticker) ticker,industry from public.security_classification
          where ticker=any(:t) order by ticker,is_active desc"""),{'t':tick}).fetchall()
    ind={t:i for t,i in rows}
    by_ind={}; by_sector={}
    for c in x['candidates']:
        c['industry']=ind.get(c['ticker'])
        by_ind.setdefault(c['industry'],[]).append(c['ticker'])
        by_sector.setdefault(c.get('sector'),[]).append(c['ticker'])
    for c in x['candidates']:
        c['direct_peers']=[t for t in by_ind.get(c['industry'],[]) if t!=c['ticker']]
        c['alternative_peers']=[t for t in by_sector.get(c.get('sector'),[])
                                 if t!=c['ticker'] and ind.get(t)!=c['industry']]
    x['industry_groups']=by_ind; x['sector_groups']=by_sector; x['schema']='aegis.p2r.evidence.v1'
    x['deterministic_disclosures']=x.get('deterministic_disclosures',[])+[
        'Direct peers share GICS industry; different-industry names are alternative opportunities, not direct comparables.',
        'Candidate and evidence ordering is shuffled in controlled tests to measure selection bias.']
    json.dump(x,open(a.out,'w',encoding='utf-8'),indent=2,default=str)
    print(json.dumps({'candidates':len(x['candidates']),'industry_groups':by_ind,'sector_groups':by_sector}))

if __name__=='__main__': main()
