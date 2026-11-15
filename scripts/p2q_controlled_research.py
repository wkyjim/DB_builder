#!/usr/bin/env python3
"""P2Q staged evidence-bound comparative research."""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))
from db_builder.research_validation import validate_narrative

A="""You are AEGIS Stage A. Select candidates using only packet keys. Every selected ticker must cite at least one supplied key. Write a qualitative thesis and explicit assumptions. Do not calculate facts, invent estimates, targets or probabilities. Any number including zero is allowed.
Return JSON: {"selected":[{"ticker":"","stance":"BUY|NEUTRAL|AVOID|SELL","evidence_keys":[],"thesis":"","assumptions":[{"driver":"","failure_condition":""}]}]}.
"""
B="""You are AEGIS Stage B. For every selected ticker provide exactly one disposition: valid comparison, no economically comparable peer, or insufficient evidence. For a valid comparison, name a same-sector peer and cite only supplied keys for both companies; explain relative growth, earnings sustainability, catalysts, risks and rejection/preference. Do not force a peer or invent evidence.
Return JSON: {"dispositions":[{"ticker":"","status":"valid|no_peer|insufficient_evidence","peer":"","dimension":"","thesis":"","evidence_keys":[]}]}.
"""

def keymap(packet):
    return {k['key']:(c['ticker'],k) for c in packet['candidates'] for k in c['packet']['evidence']}

def call(prompt,payload):
    r=requests.post('http://127.0.0.1:8645/v1/chat/completions',headers={'Authorization':'Bearer local'},
      json={'model':'upstage/solar-mini4:free','temperature':0.0,'max_tokens':3000,'response_format':{'type':'json_object'},
            'messages':[{'role':'system','content':prompt},{'role':'user','content':json.dumps(payload,default=str)}]},timeout=240)
    body=r.json(); return json.loads(body['choices'][0]['message'].get('content') or ''), body.get('usage') or {}

def facts_for(keys, packet):
    km=keymap(packet); out=[]
    for k in keys:
        if k in km: out.append(km[k][1])
    return out

def validate_a(data, packet):
    flags=[]; km=keymap(packet); selected=[x for x in (data or {}).get('selected',[]) if isinstance(x,dict)]
    for x in selected:
        t=x.get('ticker')
        if not any(c['ticker']==t for c in packet['candidates']): flags.append(f'outside_candidate:{t}')
        keys=x.get('evidence_keys') or []
        if not keys: flags.append(f'empty_evidence:{t}')
        ev=[]
        for k in keys:
            if k not in km: flags.append(f'unknown_key:{t}:{k}'); continue
            if km[k][0]!=t: flags.append(f'cross_ticker_key:{t}:{k}'); continue
            ev.append(km[k][1])
        flags += validate_narrative(str(x.get('thesis','')),ev,t)
        for a in x.get('assumptions') or []:
            if not a.get('driver') or not a.get('failure_condition'): flags.append(f'incomplete_assumption:{t}')
    return selected, sorted(set(flags))

def validate_b(data, packet, selected):
    flags=[]; km=keymap(packet); sector={c['ticker']:c.get('sector') for c in packet['candidates']}
    peers={s:[t for t in ts] for s,ts in packet['peer_groups'].items()}
    disp=[x for x in (data or {}).get('dispositions',[]) if isinstance(x,dict)]
    by={x.get('ticker'):x for x in disp}
    for x in selected:
        t=x.get('ticker'); d=by.get(t)
        if not d: flags.append(f'missing_disposition:{t}'); continue
        status=str(d.get('status','')).lower()
        if status not in {'valid','no_peer','insufficient_evidence'}:
            flags.append(f'invalid_disposition:{t}:{status}'); continue
        available=peers.get(sector.get(t),[])
        if status=='valid':
            p=d.get('peer')
            if not p or p==t: flags.append(f'invalid_peer:{t}:{p}')
            elif sector.get(p)!=sector.get(t): flags.append(f'unrelated_peer:{t}:{p}')
            keys=d.get('evidence_keys') or []
            if not keys: flags.append(f'comparison_empty_evidence:{t}')
            ev=[]
            for k in keys:
                if k not in km: flags.append(f'unknown_key:{t}:{k}'); continue
                owner,fact=km[k]
                if owner not in {t,p}: flags.append(f'comparison_wrong_owner:{t}:{k}'); continue
                ev.append(fact)
            flags += validate_narrative(str(d.get('thesis','')),ev,t)
            if len(str(d.get('thesis','')).strip())<80: flags.append(f'generic_comparison:{t}')
            for dim in ('dimension',):
                if not d.get(dim): flags.append(f'missing_dimension:{t}')
        elif status=='no_peer':
            if available: flags.append(f'no_peer_false:{t}')
        elif status=='insufficient_evidence':
            if not d.get('thesis'): flags.append(f'insufficient_without_reason:{t}')
    return disp, sorted(set(flags))

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--evidence',required=True); ap.add_argument('--runs',type=int,default=5); ap.add_argument('--retries',type=int,default=1); a=ap.parse_args()
    packet=json.load(open(a.evidence,encoding='utf-8'))
    compact={'as_of':packet['as_of'],'candidates':[{'ticker':c['ticker'],'sector':c.get('sector'),'evidence':c['packet']['evidence']} for c in packet['candidates']],
            'peer_groups':packet['peer_groups'],'deterministic_disclosures':packet['deterministic_disclosures']}
    results=[]
    for n in range(1,a.runs+1):
        t0=time.monotonic(); usage={}; errors={}; da=None; db=None
        for attempt in range(a.retries+1):
            try:
                da,ua=call(A,compact); usage['stage_a']=ua; errors.pop('stage_a',None); break
            except Exception as exc: errors['stage_a']=repr(exc); usage['stage_a']={}
        if da is None:
            results.append({'run':n,'runtime_s':round(time.monotonic()-t0,2),'selected':[],'dispositions':[],
                'flags':['stage_a_provider_failure'],'qc_pass':False,'errors':errors,'usage':usage,
                'note':'stage_b_not_run'})
            print(json.dumps(results[-1])); continue
        sel,fa=validate_a(da,packet)
        try:
            db,ub=call(B,{'as_of':packet['as_of'],'selected':sel,'peer_groups':packet['peer_groups'],
                'candidates':compact['candidates']}); usage['stage_b']=ub
        except Exception as exc: errors['stage_b']=repr(exc); db=None
        disp,fb=validate_b(db,packet,sel) if db is not None else ([],['stage_b_provider_failure'])
        results.append({'run':n,'runtime_s':round(time.monotonic()-t0,2),'selected':[x['ticker'] for x in sel],
            'dispositions':disp,'flags':sorted(set(fa+fb)),'qc_pass':not errors and not fa and not fb,
            'errors':errors,'usage':usage,'raw_a':da,'raw_b':db})
        print(json.dumps(results[-1],default=str))
    print(json.dumps({'as_of':packet['as_of'],'runs':results},indent=2,default=str))

if __name__=='__main__': main()
