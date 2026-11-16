#!/usr/bin/env python3
"""P2W: minimal unit with business-description attribution and no financial re-entry."""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))

P="""You are AEGIS. Python owns all financial facts. Do not write financial values, financial periods, guidance ranges or calculations in prose. Verified nonfinancial business descriptions may be quoted only when their business-description key is selected. Return qualitative thesis, drivers, catalysts, risks, failure conditions, rejection reason and evidence keys. `investment_stance` and `risk_status` describe the CANDIDATE ticker only. `relative_preference` is a separate candidate-versus-peer comparison. Do not infer BUY/SELL or give targets/probabilities.
Return JSON: {"relative_preference":"candidate|peer|neither|insufficient_evidence","investment_stance":"positive|negative|neutral|not_applicable","risk_status":"risk_supported|risk_not_supported|insufficient_evidence|not_applicable","evidence_keys":[],"thesis":"","drivers":[],"catalysts":[],"risks":[],"failure_conditions":[],"rejection_reason":""}.
"""

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--packet',required=True); ap.add_argument('--candidate',default='MU'); ap.add_argument('--peer',default='SNDK'); ap.add_argument('--negative',action='store_true'); ap.add_argument('--runs',type=int,default=5); a=ap.parse_args()
    p=json.load(open(a.packet,encoding='utf-8')); biz=json.load(open(ROOT/'data/p2t_business_descriptions.json'))
    cmap={c['ticker']:c for c in p['candidates']}; candidate,peer=a.candidate,a.peer
    for t in (candidate,peer):
        c=cmap[t]; c['packet']['evidence'].append({'key':f'BIZ_{t}','metric':'business_description','period':'10-K',
            'canonical_period':'10-K','kind':'BUSINESS_DESCRIPTION','value':biz[t]['passage'],'unit':'text','source':biz[t]['source'],'accession':biz[t]['url']})
    ev=[{'key':k['key'],'ticker':t,'metric':k['metric'],'period':k['period'],'canonical_period':k['canonical_period'],
         'kind':k.get('kind'),'value':k['value'],'unit':k['unit'],'source':k['source']} for t in (candidate,peer) for k in cmap[t]['packet']['evidence']]
    payload={'candidate':candidate,'peer':peer,'negative':a.negative,'evidence':ev,'business_descriptions':{t:biz[t] for t in (candidate,peer)}}
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
            if data.get('relative_preference') not in {'candidate','peer','neither','insufficient_evidence'}: flags.append(f'invalid_relative_preference:{data.get("relative_preference")}')
            if data.get('investment_stance') not in {'positive','negative','neutral','not_applicable'}: flags.append('invalid_investment_stance')
            rs=data.get('risk_status')
            if rs not in {'risk_supported','risk_not_supported','insufficient_evidence','not_applicable'}: flags.append('invalid_risk_status')
            if a.negative and rs=='not_applicable': flags.append('negative_unit_requires_risk_status')
            text=' '.join(str(data.get(k,'')) for k in ('thesis','rejection_reason'))
            selected=[keys[k] for k in (data.get('evidence_keys') or []) if k in keys]
            for k in data.get('evidence_keys') or []:
                if k not in keys: flags.append(f'unknown_key:{k}')
            biz_text=' '.join(str(e.get('value','')) for e in selected if e.get('kind')=='BUSINESS_DESCRIPTION')
            for tok in re.findall(r'\d+(?:[.,]\d+)?',text):
                if tok not in biz_text: flags.append(f'financial_fact_reentry:{candidate}:{tok}')
            low=text.lower()
            if re.search(r'memory|nand|dram|storage|chemical|materials|packag|petrochem',low) and not biz_text:
                flags.append('unbound_business_claim')
            if re.search(r'guidance|consensus|forward|expects|forecast',low) and not any(e.get('kind')=='MANAGEMENT_GUIDANCE_TYPED' for e in selected):
                flags.append('unsupported_forward_claim')
            if re.search(r'price target|probability|probabilities',low): flags.append('unsupported_target_or_probability')
            if not data.get('drivers') or not data.get('catalysts') or not data.get('risks') or not data.get('failure_conditions'): flags.append('incomplete_judgment')
        else: flags.append('no_data')
        preferred_ticker = candidate if isinstance(data,dict) and data.get('relative_preference')=='candidate' else (peer if isinstance(data,dict) and data.get('relative_preference')=='peer' else None)
        result={'run':n,'candidate':candidate,'peer':peer,'stance_ticker':candidate,'risk_ticker':candidate,
                'runtime_s':round(time.monotonic()-t0,2),'usage':usage,
                'relative_preference':data.get('relative_preference') if isinstance(data,dict) else None,
                'investment_stance':data.get('investment_stance') if isinstance(data,dict) else None,
                'risk_status':data.get('risk_status') if isinstance(data,dict) else None,
                'preferred_ticker':preferred_ticker,'data':data,'flags':flags,'qc_pass':not flags and err is None,'error':err}
        results.append(result); print(json.dumps({k:v for k,v in result.items() if k!='data'},default=str))
    print(json.dumps({'unit':f'{candidate}_vs_{peer}','runs':results},indent=2,default=str))

if __name__=='__main__': main()
