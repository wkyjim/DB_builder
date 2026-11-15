#!/usr/bin/env python3
"""P2O: evidence-aware AI interpretation and deterministic output assembly."""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))
from db_builder.research_validation import validate_narrative

PROMPT="""You are AEGIS. Use the supplied verified values, canonical periods and derived metrics. You may interpret and cite them, but Python will render the final financial facts. Do not invent estimates, targets, probabilities or unverified guidance. Compare positive and negative candidates; explain selection and rejection, earnings sustainability, industry drivers and downside risks. Any number of ideas, including zero.
Return JSON: {"selected":[{"ticker":"","stance":"BUY|NEUTRAL|AVOID|SELL","evidence_ids":[],"thesis":"","assumptions":[{"driver":"","failure_condition":""}],"catalysts":[],"risks":[],"alternatives_considered":[]}],"rejected":[{"ticker":"","reason":""}]}.
"""

def validate(data, packet):
    flags=[]; catalog={e['id']:e for c in packet['candidates'] for e in c['evidence_catalog']}
    allowed={c['ticker'] for c in packet['candidates']}
    selected=[x for x in (data or {}).get('selected',[]) if isinstance(x,dict)]
    rejected=[x for x in (data or {}).get('rejected',[]) if isinstance(x,dict)]
    for x in selected+rejected:
        if x.get('ticker') not in allowed: flags.append('outside_candidate:'+str(x.get('ticker')))
    def forbidden(obj):
        if isinstance(obj,dict):
            for k,v in obj.items():
                if str(k).lower() in {'target','price_target','probability','probabilities'}: return True
                if forbidden(v): return True
        elif isinstance(obj,list): return any(forbidden(v) for v in obj)
        return False
    if forbidden(data): flags.append('forbidden_target_or_probability')
    rendered={}
    for x in selected:
        t=x.get('ticker'); ids=x.get('evidence_ids') or []
        if not ids: flags.append(f'empty_evidence:{t}')
        ev=[]
        for eid in ids:
            e=catalog.get(eid)
            if not e: flags.append(f'unknown_evidence_id:{t}:{eid}')
            elif e['ticker']!=t: flags.append(f'cross_ticker:{t}:{eid}')
            else: ev.append(e)
        text=' '.join(str(x.get(k,'')) for k in ('thesis','catalysts','risks','alternatives_considered'))
        text += ' '.join(str(a.get('driver',''))+' '+str(a.get('failure_condition','')) for a in (x.get('assumptions') or []))
        flags += validate_narrative(text,ev,t)
        for a in x.get('assumptions') or []:
            if not a.get('driver') or not a.get('failure_condition'): flags.append(f'incomplete_assumption:{t}')
        if not x.get('alternatives_considered'): flags.append(f'no_alternatives:{t}')
        rendered[t]=[{'evidence_id':eid,'rendered':next((c['rendered_evidence'].get(eid) for c in packet['candidates'] if c['ticker']==t),None)} for eid in ids if eid in catalog]
    return {'selected':[x.get('ticker') for x in selected],'rejected':[x.get('ticker') for x in rejected],
            'rendered_facts':rendered,'flags':sorted(set(flags)),'qc_pass':bool(not flags)}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--evidence',required=True); ap.add_argument('--runs',type=int,default=3); a=ap.parse_args()
    packet=json.load(open(a.evidence,encoding='utf-8'))
    compact={'as_of':packet['as_of'],'candidates':[{'ticker':c['ticker'],'sector':c.get('sector'),
        'evidence_catalog':c['evidence_catalog']} for c in packet['candidates']],
        'deterministic_disclosures':packet.get('deterministic_disclosures',[])}
    results=[]
    for n in range(1,a.runs+1):
        t0=time.monotonic(); err=None; data=None; usage={}
        try:
            r=requests.post('http://127.0.0.1:8645/v1/chat/completions',headers={'Authorization':'Bearer local'},
                json={'model':'upstage/solar-mini4:free','temperature':0.0,'max_tokens':3500,
                      'response_format':{'type':'json_object'},
                      'messages':[{'role':'system','content':PROMPT},
                                  {'role':'user','content':json.dumps(compact,default=str)}]},timeout=300)
            body=r.json(); usage=body.get('usage') or {}
            data=json.loads(body['choices'][0]['message'].get('content') or '')
        except Exception as exc: err=repr(exc)
        v=validate(data,packet) if data is not None else {'selected':[],'rejected':[],'rendered_facts':{},'flags':['no_data'],'qc_pass':False}
        result={'run':n,'runtime_s':round(time.monotonic()-t0,2),'usage':usage,'selected':v['selected'],
                'rejected':v['rejected'],'rendered_facts':v['rendered_facts'],'flags':v['flags'],
                'qc_pass':v['qc_pass'],'deterministic_disclosures':packet['deterministic_disclosures'],
                'error':err,'raw':data}
        results.append(result); print(json.dumps({k:x for k,x in result.items() if k!='raw'},default=str))
    print(json.dumps({'as_of':packet['as_of'],'runs':results},indent=2,default=str))

if __name__=='__main__': main()
