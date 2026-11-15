#!/usr/bin/env python3
"""P2U: strict enum relative-preference unit research."""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))
from db_builder.research_validation import validate_narrative

P="""You are AEGIS. Produce one structured investment judgment for the candidate versus the peer. Python owns the candidate and peer identities. Use only supplied evidence keys. Return relative_preference as exactly one of: candidate, peer, neither, insufficient_evidence. Provide evidence_keys, thesis, drivers, catalysts, risks, failure_conditions and rejection_reason. Relative preference does not imply BUY. Do not provide targets, probabilities or unsupported facts; Python renders facts.
Return JSON: {"relative_preference":"candidate|peer|neither|insufficient_evidence","evidence_keys":[],"thesis":"","drivers":[],"catalysts":[],"risks":[],"failure_conditions":[],"rejection_reason":""}.
"""

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--packet',required=True); ap.add_argument('--candidate',default='MU'); ap.add_argument('--peer',default='SNDK'); ap.add_argument('--runs',type=int,default=5); a=ap.parse_args()
    p=json.load(open(a.packet,encoding='utf-8')); biz=json.load(open(ROOT/'data/p2t_business_descriptions.json'))
    cmap={c['ticker']:c for c in p['candidates']}; candidate,peer=a.candidate,a.peer
    for t in (candidate,peer):
        c=cmap[t]; c['packet']['evidence'].append({'key':f'BIZ_{t}','metric':'business_description','period':'10-K',
            'canonical_period':'10-K','value':biz[t]['passage'],'unit':'text','source':biz[t]['source'],'accession':biz[t]['url']})
    ev=[{'key':k['key'],'ticker':t,'metric':k['metric'],'period':k['period'],'canonical_period':k['canonical_period'],
         'value':k['value'],'unit':k['unit'],'source':k['source']} for t in (candidate,peer) for k in cmap[t]['packet']['evidence']]
    payload={'candidate':candidate,'peer':peer,'evidence':ev,'business_descriptions':{t:biz[t] for t in (candidate,peer)}}
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
            enum=data.get('relative_preference')
            if enum not in {'candidate','peer','neither','insufficient_evidence'}: flags.append(f'invalid_relative_preference:{enum}')
            text=' '.join(str(data.get(k,'')) for k in ('thesis','rejection_reason'))
            for k in data.get('evidence_keys') or []:
                if k not in keys: flags.append(f'unknown_key:{k}')
            flags += validate_narrative(text,[keys[k] for k in (data.get('evidence_keys') or []) if k in keys],candidate)
            if candidate=='MU' and re.search(r'foundry',text,re.I):
                if not any('foundry' in str(keys[k].get('value','')).lower() for k in (data.get('evidence_keys') or []) if k in keys):
                    flags.append('unsupported_business_claim:MU:foundry')
            if not data.get('drivers') or not data.get('catalysts') or not data.get('risks') or not data.get('failure_conditions'): flags.append('incomplete_judgment')
        else: flags.append('no_data')
        preferred_ticker = candidate if isinstance(data,dict) and data.get('relative_preference')=='candidate' else (peer if isinstance(data,dict) and data.get('relative_preference')=='peer' else None)
        result={'run':n,'candidate':candidate,'peer':peer,'runtime_s':round(time.monotonic()-t0,2),'usage':usage,
                'relative_preference':data.get('relative_preference') if isinstance(data,dict) else None,
                'preferred_ticker':preferred_ticker,'data':data,'flags':flags,'qc_pass':not flags and err is None,'error':err}
        results.append(result); print(json.dumps({k:v for k,v in result.items() if k!='data'},default=str))
    print(json.dumps({'unit':f'{candidate}_vs_{peer}','runs':results},indent=2,default=str))

if __name__=='__main__': main()
