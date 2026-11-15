#!/usr/bin/env python3
"""P2N: build canonical, evidence-only catalog for research AI."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))
from db_builder.sec_periods import resolve_period, render_fact

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True); ap.add_argument('--out',required=True); a=ap.parse_args()
    packet=json.load(open(a.input,encoding='utf-8'))
    for c in packet['candidates']:
        catalog=[]; ambiguous=[]; render={}
        for e in c.get('sec_actuals',[]):
            p=resolve_period(e)
            if p['status']!='verified':
                ambiguous.append({'id':e['id'],'ticker':c['ticker'],'metric':e.get('metric'),
                                  'period':e.get('period'),'reason':p['reason']})
                continue
            catalog.append({'id':e['id'],'ticker':c['ticker'],'metric':e.get('metric'),
                            'canonical_period':p['label'],'kind':'SEC_ACTUAL'})
            render[e['id']]=render_fact(e)
        for e in c.get('derived',[]):
            catalog.append({'id':e['id'],'ticker':c['ticker'],'metric':e.get('metric'),
                            'canonical_period':'DERIVED','kind':'DERIVED'})
            render[e['id']]=f"{c['ticker']} {e.get('metric')} DERIVED {e.get('period')}: {round(float(e.get('value')),2)} {e.get('unit')}"
        for e in c.get('typed_guidance',[]):
            catalog.append({'id':e['id'],'ticker':c['ticker'],'metric':e.get('metric'),
                            'canonical_period':e.get('period'),'kind':'GUIDANCE_TYPED'})
            render[e['id']]=f"{c['ticker']} guidance {e.get('metric')} {e.get('period')}: {e.get('low')}-{e.get('high')} {e.get('unit')} ({e.get('gaap_basis')}, {e.get('split_basis')})"
        c['evidence_catalog']=catalog
        c['ambiguous_evidence']=ambiguous
        c['rendered_evidence']=render
    packet['schema']='aegis.p2n.evidence.v1'
    packet['deterministic_disclosures']=packet.get('deterministic_disclosures',[])+[
        'AI receives canonical evidence IDs and qualitative catalog labels only; Python renders values.',
        'Ambiguous fiscal-period evidence is excluded from the catalog.',
    ]
    json.dump(packet,open(a.out,'w',encoding='utf-8'),indent=2,default=str)
    print(json.dumps({'candidates':len(packet['candidates']),
        'catalog':sum(len(c['evidence_catalog']) for c in packet['candidates']),
        'ambiguous':sum(len(c['ambiguous_evidence']) for c in packet['candidates'])}))

if __name__=='__main__': main()
