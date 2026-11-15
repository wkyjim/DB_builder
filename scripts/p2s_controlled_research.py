#!/usr/bin/env python3
"""P2S: per-candidate Stage B comparative research."""
from __future__ import annotations
import argparse, json, random, sys, time
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))
from db_builder.research_validation import validate_narrative

A="""You are AEGIS Stage A. Select candidates using only supplied evidence keys. Cite at least one key. Write qualitative thesis and assumptions with drivers/failure conditions. Do not invent targets, probabilities or unsupported facts. Any number, including zero, is allowed.
Return JSON: {"selected":[{"ticker":"","stance":"BUY|NEUTRAL|AVOID|SELL","evidence_keys":[],"thesis":"","assumptions":[{"driver":"","failure_condition":""}]}]}.
"""
B="""You are AEGIS Stage B. You are given one selected company, its assigned comparison class and eligible peers with immutable evidence keys. Choose one eligible peer and explain relative business model, growth, earnings quality, catalysts, risks and why the alternative is preferred or rejected. Cite supplied keys for both companies. Do not invent facts, targets or probabilities.
Return JSON: {"peer":"","dimension":"","thesis":"","evidence_keys":[]}.
"""

def keymap(packet):
    return {k['key']:(c['ticker'],k) for c in packet['candidates'] for k in c['packet']['evidence']}

def call(prompt,payload):
    r=requests.post('http://127.0.0.1:8645/v1/chat/completions',headers={'Authorization':'Bearer local'},
      json={'model':'upstage/solar-mini4:free','temperature':0.0,'max_tokens':2500,'response_format':{'type':'json_object'},
            'messages':[{'role':'system','content':prompt},{'role':'user','content':json.dumps(payload,default=str)}]},timeout=240)
    body=r.json(); return json.loads(body['choices'][0]['message'].get('content') or ''), body.get('usage') or {}

def packet_for(packet,t):
    return next(c for c in packet['candidates'] if c['ticker']==t)

def validate_a(data,packet):
    flags=[]; km=keymap(packet); allowed={c['ticker'] for c in packet['candidates']}
    sel=[x for x in (data or {}).get('selected',[]) if isinstance(x,dict)]
    for x in sel:
        t=x.get('ticker')
        if t not in allowed: flags.append(f'outside_candidate:{t}')
        keys=x.get('evidence_keys') or []
        if not keys: flags.append(f'empty_evidence:{t}')
        ev=[]
        for k in keys:
            if k not in km: flags.append(f'unknown_key:{t}:{k}'); continue
            if km[k][0]!=t: flags.append(f'cross_ticker_key:{t}:{k}'); continue
            ev.append(km[k][1])
        flags += validate_narrative(str(x.get('thesis','')),ev,t)
        for a in x.get('assumptions') or []:
            if not isinstance(a,dict) or not a.get('driver') or not a.get('failure_condition'): flags.append(f'incomplete_assumption:{t}')
    return sel,sorted(set(flags))

def validate_disposition(t, disp, packet, assigned, eligible):
    flags=[]; km=keymap(packet); ev=[]
    peer=disp.get('peer')
    if peer not in eligible: flags.append(f'peer_not_eligible:{t}:{peer}')
    if peer==t: flags.append(f'self_comparison:{t}')
    for k in disp.get('evidence_keys') or []:
        if k not in km: flags.append(f'unknown_key:{t}:{k}'); continue
        owner,fact=km[k]
        if owner not in ({t}|set(eligible)): flags.append(f'comparison_wrong_owner:{t}:{k}'); continue
        ev.append(fact)
    if not ev: flags.append(f'comparison_without_evidence:{t}')
    flags += validate_narrative(str(disp.get('thesis','')),ev,t)
    if len(str(disp.get('thesis','')).strip())<80: flags.append(f'generic_comparison:{t}')
    if not disp.get('dimension'): flags.append(f'missing_dimension:{t}')
    return flags

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--evidence',required=True); ap.add_argument('--runs',type=int,default=5); ap.add_argument('--retries',type=int,default=1); a=ap.parse_args()
    packet=json.load(open(a.evidence,encoding='utf-8'))
    results=[]
    for n in range(1,a.runs+1):
        rng=random.Random(2000+n); order=[c.copy() for c in packet['candidates']]; rng.shuffle(order)
        for c in order: rng.shuffle(c['packet']['evidence'])
        compact={'as_of':packet['as_of'],'candidates':[{'ticker':c['ticker'],'sector':c.get('sector'),'industry':c.get('industry'),
            'business_model':c.get('business_profile',{}).get('business_model'),'evidence':c['packet']['evidence']} for c in order],
            'direct_peers':{c['ticker']:c.get('direct_peers',[]) for c in packet['candidates']},
            'alternative_peers':{c['ticker']:c.get('alternative_peers',[]) for c in packet['candidates']}}
        t0=time.monotonic(); usage={}; errors={}; da=None
        try: da,ua=call(A,compact); usage['stage_a']=ua
        except Exception as exc: errors['stage_a']=repr(exc)
        if da is None:
            results.append({'run':n,'selected':[],'dispositions':[],'flags':['stage_a_provider_failure'],'qc_pass':False,'errors':errors,'usage':usage}); print(json.dumps(results[-1])); continue
        sel,flags_a=validate_a(da,packet); dispositions=[]; flags_b=[]; raw_b={}
        for x in sel:
            t=x['ticker']; c=packet_for(packet,t)
            direct=c.get('direct_peers',[]); alt=c.get('alternative_peers',[])
            assigned='direct_peer' if direct else ('alternative_opportunity' if alt else 'no_peer')
            eligible=direct if direct else alt
            if not eligible:
                dispositions.append({'ticker':t,'status':'no_peer','peer':None,'dimension':'business model',
                    'thesis':'No verified business-model peer exists in the current candidate set.','evidence_keys':[]})
                continue
            db=None; unit_err=None
            for attempt in range(a.retries+1):
                try:
                    db,ub=call(B,{'ticker':t,'assigned_status':assigned,'eligible_peers':[{'ticker':p,'business_profile':packet_for(packet,p).get('business_profile'),'evidence':packet_for(packet,p)['packet']['evidence']} for p in eligible],
                        'selected_evidence':c['packet']['evidence']}); usage.setdefault('stage_b',{})[t]=ub; raw_b[t]=db; unit_err=None; break
                except Exception as exc: unit_err=repr(exc)
            if db is None:
                dispositions.append({'ticker':t,'status':'failed','peer':None,'dimension':None,'thesis':'','evidence_keys':[],'error':unit_err})
                flags_b.append(f'stage_b_failed:{t}')
                continue
            disp={'ticker':t,'status':assigned,'peer':db.get('peer'),'dimension':db.get('dimension'),
                  'thesis':db.get('thesis'),'evidence_keys':db.get('evidence_keys',[])}
            dispositions.append(disp); flags_b += validate_disposition(t,db,packet,assigned,eligible)
        results.append({'run':n,'runtime_s':round(time.monotonic()-t0,2),'order':[c['ticker'] for c in order],'selected':[x['ticker'] for x in sel],
            'dispositions':dispositions,'flags':sorted(set(flags_a+flags_b)),'qc_pass':not errors and not flags_a and not flags_b,
            'errors':errors,'usage':usage,'raw_a':da,'raw_b':raw_b})
        print(json.dumps({k:v for k,v in results[-1].items() if k!='raw_a'},default=str))
    print(json.dumps({'as_of':packet['as_of'],'runs':results},indent=2,default=str))

if __name__=='__main__': main()
