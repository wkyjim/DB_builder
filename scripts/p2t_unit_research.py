#!/usr/bin/env python3
"""P2T: one verified candidate-vs-peer research unit, Python-rendered facts."""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))
from db_builder.research_validation import validate_narrative

P="""You are AEGIS. Produce one structured investment judgment for the selected candidate versus its eligible direct peer. Use only supplied evidence keys. Return the preferred ticker, preference (prefer/avoid/neutral), qualitative thesis, growth drivers, catalysts, risks, failure conditions, rejection reason for the other company, and evidence keys. Do not write numeric targets, probabilities or unsupported facts; Python renders facts.
Return JSON: {"candidate":"","peer":"","preferred":"","preference":"prefer|avoid|neutral","evidence_keys":[],"thesis":"","drivers":[],"catalysts":[],"risks":[],"failure_conditions":[],"rejection_reason":""}.
"""

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--packet',required=True); ap.add_argument('--runs',type=int,default=3); a=ap.parse_args()
    p=json.load(open(a.packet,encoding='utf-8'))
    # Build a single direct unit: MU vs SNDK, with official business descriptions.
    biz=json.load(open(ROOT/'data/p2t_business_descriptions.json'))
    cands={c['ticker']:c for c in p['candidates']}
    for t in ('MU','SNDK'):
        c=cands[t]
        c['packet']['evidence'].append({'key':f'BIZ_{t}','metric':'business_description','period':'10-K',
            'canonical_period':'10-K','value':biz[t]['passage'],'unit':'text','source':biz[t]['source'],
            'accession':biz[t]['url']})
    ev=[{'key':k['key'],'ticker':t,'metric':k['metric'],'period':k['period'],'canonical_period':k['canonical_period'],
         'value':k['value'],'unit':k['unit'],'source':k['source']} for t in ('MU','SNDK') for k in cands[t]['packet']['evidence']]
    payload={'candidate':'MU','peer':'SNDK','evidence':ev,'business_descriptions':biz}
    results=[]
    for n in range(1,a.runs+1):
        t0=time.monotonic(); err=None; data=None; usage={}
        try:
            r=requests.post('http://127.0.0.1:8645/v1/chat/completions',headers={'Authorization':'Bearer local'},
              json={'model':'upstage/solar-mini4:free','temperature':0.0,'max_tokens':2500,'response_format':{'type':'json_object'},
                    'messages':[{'role':'system','content':P},{'role':'user','content':json.dumps(payload,default=str)}]},timeout=240)
            body=r.json(); usage=body.get('usage') or {}; data=json.loads(body['choices'][0]['message'].get('content') or '')
        except Exception as exc: err=repr(exc)
        flags=[]; keys={e['key']:e for e in ev}; text=''
        if isinstance(data,dict):
            text=' '.join(str(data.get(k,'')) for k in ('thesis','rejection_reason'))
            for k in data.get('evidence_keys') or []:
                if k not in keys: flags.append(f'unknown_key:{k}')
            if data.get('candidate') not in ('MU','SNDK'): flags.append('invalid_candidate')
            if data.get('peer') not in ('MU','SNDK') or data.get('peer')==data.get('candidate'): flags.append('invalid_peer')
            if data.get('preferred') not in (data.get('candidate'), data.get('peer')): flags.append('invalid_preferred_ticker')
            flags += validate_narrative(text,[keys[k] for k in (data.get('evidence_keys') or []) if k in keys],data.get('candidate',''))
            # Business claim check: MU 10-K has no foundry claim.
            if data.get('candidate')=='MU' and re.search(r'foundry',text,re.I):
                if not any('foundry' in str(keys[k].get('value','')).lower() for k in (data.get('evidence_keys') or []) if k in keys):
                    flags.append('unsupported_business_claim:MU:foundry')
            if not data.get('drivers') or not data.get('catalysts') or not data.get('risks') or not data.get('failure_conditions'): flags.append('incomplete_judgment')
        else: flags.append('no_data')
        rendered={k:keys[k] for k in (data or {}).get('evidence_keys',[]) if k in keys} if isinstance(data,dict) else {}
        result={'run':n,'runtime_s':round(time.monotonic()-t0,2),'usage':usage,'data':data,'rendered':rendered,'flags':flags,'qc_pass':not flags and err is None,'error':err}
        results.append(result); print(json.dumps({k:v for k,v in result.items() if k!='data'},default=str))
    print(json.dumps({'unit':'MU_vs_SNDK','runs':results},indent=2,default=str))

if __name__=='__main__': main()
