#!/usr/bin/env python3
"""P2Q: prebuilt candidate/peer comparison packets with short immutable keys."""
from __future__ import annotations
import argparse, json

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True); ap.add_argument('--out',required=True); a=ap.parse_args()
    x=json.load(open(a.input,encoding='utf-8'))
    for c in x['candidates']:
        actual={e['id']:e for e in c.get('sec_actuals',[])}
        keys=[]
        # Keep a compact, auditable packet: latest SEC metrics + Python-derived evidence + typed guidance.
        priority=['revenue','net_income','operating_income','operating_cash_flow','diluted_eps','capex']
        for metric in priority:
            rows=[e for e in c.get('sec_actuals',[]) if e.get('metric')==metric and e.get('period_type')=='Q']
            rows=sorted(rows,key=lambda e:e.get('period'),reverse=True)[:1]
            for e in rows:
                keys.append({'key':f'K_{c["ticker"]}_{len(keys)+1:02d}','metric':metric,
                             'period':e.get('period'),'canonical_period':e.get('metric_fiscal_period'),
                             'value':e.get('value'),'unit':e.get('unit'),'source':e.get('source'),'kind':'SEC_ACTUAL',
                             'accession':e.get('accession'),'available_at':e.get('available_at')})
        for e in c.get('derived',[])[:8]:
            keys.append({'key':f'K_{c["ticker"]}_{len(keys)+1:02d}','metric':e.get('metric'),
                         'period':e.get('period'),'canonical_period':'DERIVED','value':e.get('value'),
                         'unit':e.get('unit'),'source':'Python derived','kind':'DERIVED','accession':e.get('input_ids'),
                         'available_at':None})
        for e in c.get('typed_guidance',[]):
            keys.append({'key':f'K_{c["ticker"]}_{len(keys)+1:02d}','metric':e.get('metric'),
                         'period':e.get('period'),'canonical_period':e.get('period'),'kind':'MANAGEMENT_GUIDANCE_TYPED',
                         'value':{'low':e.get('low'),'high':e.get('high')},'low':e.get('low'),'high':e.get('high'),'unit':e.get('unit'),
                         'source':'SEC 8-K typing','accession':e.get('source_passage_id'),
                         'gaap_basis':e.get('gaap_basis'),'split_basis':e.get('split_basis')})
        c['packet']={'ticker':c['ticker'],'sector':c.get('sector'),'evidence':keys}
        c['key_map']={k['key']:k for k in keys}
    groups={}
    for c in x['candidates']: groups.setdefault(str(c.get('sector') or 'Unknown'),[]).append(c['ticker'])
    x['peer_groups']=groups
    x['schema']='aegis.p2q.packets.v1'
    x['deterministic_disclosures']=x.get('deterministic_disclosures',[])+[
        'AI may reference only supplied short keys; Python owns all facts and rendering.',
        'Each Stage A selection must receive a valid, no-peer or insufficient-evidence disposition.',
        'Stage B is not run when Stage A fails.',
    ]
    json.dump(x,open(a.out,'w',encoding='utf-8'),indent=2,default=str)
    print(json.dumps({'candidates':len(x['candidates']),'keys':sum(len(c['packet']['evidence']) for c in x['candidates']),'peer_groups':groups}))

if __name__=='__main__': main()
