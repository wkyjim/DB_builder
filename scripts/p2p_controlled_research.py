#!/usr/bin/env python3
"""P2P staged comparative research with deterministic fact assembly."""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))
from db_builder.research_validation import validate_narrative
from db_builder.comparison_validation import build_peer_groups, validate_comparisons

STAGE_A="""You are AEGIS Stage A. Select promising or concerning candidates from the evidence catalog. Use only canonical evidence IDs. Write a concise thesis and explicit assumptions with drivers and failure conditions. Do not provide targets, probabilities, or unsupported estimates. Any number of ideas, including zero.
Return JSON: {"selected":[{"ticker":"","stance":"BUY|NEUTRAL|AVOID|SELL","evidence_ids":[],"thesis":"","assumptions":[{"driver":"","failure_condition":""}]}]}.
"""
STAGE_B="""You are AEGIS Stage B. Compare each selected company with relevant peers from the same sector. Do not compare unrelated businesses. For each comparison provide dimension, rationale, and candidate-specific evidence IDs for both companies. Explain relative growth, earnings sustainability, catalysts, risks and why the alternative is rejected or preferred. Do not provide targets or probabilities.
Return JSON: {"comparisons":[{"ticker":"","peer":"","dimension":"","thesis":"","evidence_ids":[]}]}.
"""

def catalog(packet):
    return {e['id']:e for c in packet['candidates'] for e in c['evidence_catalog']}

def call(prompt,payload):
    r=requests.post('http://127.0.0.1:8645/v1/chat/completions',headers={'Authorization':'Bearer local'},
        json={'model':'upstage/solar-mini4:free','temperature':0.0,'max_tokens':3000,
              'response_format':{'type':'json_object'},
              'messages':[{'role':'system','content':prompt},{'role':'user','content':json.dumps(payload,default=str)}]},timeout=240)
    body=r.json(); return json.loads(body['choices'][0]['message'].get('content') or ''), body.get('usage') or {}

def validate_a(data, packet):
    flags=[]; ev=catalog(packet); allowed={c['ticker'] for c in packet['candidates']}
    sel=[x for x in (data or {}).get('selected',[]) if isinstance(x,dict)]
    for x in sel:
        t=x.get('ticker')
        if t not in allowed: flags.append(f'outside_candidate:{t}')
        ids=x.get('evidence_ids') or []
        if not ids: flags.append(f'empty_evidence:{t}')
        selected_ev=[]
        for eid in ids:
            e=ev.get(eid)
            if not e: flags.append(f'unknown_evidence_id:{t}:{eid}')
            elif e['ticker']!=t: flags.append(f'cross_ticker:{t}:{eid}')
            else: selected_ev.append(e)
        text=str(x.get('thesis',''))
        flags += validate_narrative(text,selected_ev,t)
        for a in x.get('assumptions') or []:
            if not a.get('driver') or not a.get('failure_condition'): flags.append(f'incomplete_assumption:{t}')
    return sel, sorted(set(flags))

def validate_b(data, packet, selected):
    flags=[]; ev=catalog(packet); comps=data.get('comparisons') if isinstance(data,dict) else []
    comps=[x for x in (comps or []) if isinstance(x,dict)]
    flags += validate_comparisons(selected,comps,packet['candidates'])
    for x in comps:
        t,p=x.get('ticker'),x.get('peer'); ids=x.get('evidence_ids') or []
        evs=[]
        for eid in ids:
            e=ev.get(eid)
            if not e: flags.append(f'unknown_evidence_id:{t}:{eid}'); continue
            if e['ticker'] not in {t,p}: flags.append(f'comparison_wrong_ticker:{t}:{eid}'); continue
            evs.append(e)
        flags += validate_narrative(str(x.get('thesis','')),evs,t)
    return comps, sorted(set(flags))

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--evidence',required=True); ap.add_argument('--runs',type=int,default=3); a=ap.parse_args()
    packet=json.load(open(a.evidence,encoding='utf-8'))
    compact={'as_of':packet['as_of'],'candidates':[{'ticker':c['ticker'],'sector':c.get('sector'),
        'evidence_catalog':c['evidence_catalog']} for c in packet['candidates']],
        'deterministic_disclosures':packet.get('deterministic_disclosures',[])}
    results=[]
    for n in range(1,a.runs+1):
        t0=time.monotonic(); usage={}; errors={}; data_a=None; data_b=None
        try: data_a,ua=call(STAGE_A,compact); usage['stage_a']=ua
        except Exception as exc: errors['stage_a']=repr(exc)
        sel,flags_a=validate_a(data_a,packet)
        try:
            stageb={'selected':sel,'peer_groups':build_peer_groups(packet['candidates']),
                    'candidates':compact['candidates']}
            data_b,ub=call(STAGE_B,stageb); usage['stage_b']=ub
        except Exception as exc: errors['stage_b']=repr(exc)
        comps,flags_b=validate_b(data_b,packet,sel)
        results.append({'run':n,'runtime_s':round(time.monotonic()-t0,2),'usage':usage,
            'selected':[x['ticker'] for x in sel],'comparisons':comps,'flags':sorted(set(flags_a+flags_b)),
            'qc_pass':bool(not errors and not flags_a and not flags_b),'errors':errors,
            'rendered':{x['ticker']:x.get('evidence_ids') for x in sel},'disclosures':packet.get('deterministic_disclosures',[])})
        print(json.dumps(results[-1],default=str))
    print(json.dumps({'as_of':packet['as_of'],'runs':results},indent=2,default=str))

if __name__=='__main__': main()
