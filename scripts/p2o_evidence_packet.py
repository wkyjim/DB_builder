#!/usr/bin/env python3
"""P2O: add verified values to canonical evidence for AI interpretation."""
from __future__ import annotations
import argparse, json

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True); ap.add_argument('--out',required=True); a=ap.parse_args()
    x=json.load(open(a.input,encoding='utf-8'))
    for c in x['candidates']:
        actual={e['id']:e for e in c.get('sec_actuals',[])}
        derived={e['id']:e for e in c.get('derived',[])}
        guided={e['id']:e for e in c.get('typed_guidance',[])}
        for e in c['evidence_catalog']:
            src=actual.get(e['id']) or derived.get(e['id']) or guided.get(e['id'])
            if not src: continue
            e.update({'value':src.get('value'),'low':src.get('low'),'high':src.get('high'),
                      'unit':src.get('unit'),'period':src.get('period') or src.get('period_end'),
                      'gaap_basis':src.get('gaap_basis'),'split_basis':src.get('split_basis')})
    x['schema']='aegis.p2o.evidence.v1'
    x['deterministic_disclosures']=x.get('deterministic_disclosures',[])+[
        'AI may interpret verified values but Python renders final financial facts.']
    json.dump(x,open(a.out,'w',encoding='utf-8'),indent=2,default=str)
    print(json.dumps({'candidates':len(x['candidates']),'catalog':sum(len(c['evidence_catalog']) for c in x['candidates'])}))

if __name__=='__main__': main()
