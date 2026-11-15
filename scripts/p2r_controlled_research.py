#!/usr/bin/env python3
"""P2R: shadow comparative research with direct peers, alternatives and shuffled order."""
from __future__ import annotations
import argparse, json, random, sys, time
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))
from db_builder.research_validation import validate_narrative

A="""You are AEGIS Stage A. Select candidates using only supplied evidence keys. Cite at least one key per selection. Write qualitative thesis and explicit assumptions. Do not invent targets, probabilities or unsupported facts. Any number including zero is allowed.
Return JSON: {"selected":[{"ticker":"","stance":"BUY|NEUTRAL|AVOID|SELL","evidence_keys":[],"thesis":"","assumptions":[{"driver":"","failure_condition":""}]}]}.
"""
B="""You are AEGIS Stage B. For every selected ticker give exactly one disposition: direct_peer (same GICS industry), alternative_opportunity (same sector, different industry), no_peer, or insufficient_evidence. For direct or alternative comparisons, name the peer and cite supplied keys for both companies; explain business-model differences, growth, earnings sustainability, catalysts, risks and preference/rejection. Do not force a peer or fabricate evidence.
Return JSON: {"dispositions":[{"ticker":"","status":"direct_peer|alternative_opportunity|no_peer|insufficient_evidence","peer":"","dimension":"","thesis":"","evidence_keys":[]}]}.
"""

def keymap(packet):
    return {k['key']:(c['ticker'],k) for c in packet['candidates'] for k in c['packet']['evidence']}

def call(prompt,payload):
    r=requests.post('http://127.0.0.1:8645/v1/chat/completions',headers={'Authorization':'Bearer local'},
      json={'model':'upstage/solar-mini4:free','temperature':0.0,'max_tokens':3000,'response_format':{'type':'json_object'},
            'messages':[{'role':'system','content':prompt},{'role':'user','content':json.dumps(payload,default=str)}]},timeout=240)
    body=r.json(); return json.loads(body['choices'][0]['message'].get('content') or ''), body.get('usage') or {}

def validate_a(data, packet):
    flags=[]; km=keymap(packet); selected=[x for x in (data or {}).get('selected',[]) if isinstance(x,dict)]
    valid_ticks={c['ticker'] for c in packet['candidates']}
    for x in selected:
        t=x.get('ticker')
        if t not in valid_ticks: flags.append(f'outside_candidate:{t}')
        keys=x.get('evidence_keys') or []
        if not keys: flags.append(f'empty_evidence:{t}')
        ev=[]
        for k in keys:
            if k not in km: flags.append(f'unknown_key:{t}:{k}'); continue
            if km[k][0]!=t: flags.append(f'cross_ticker_key:{t}:{k}'); continue
            ev.append(km[k][1])
        flags += validate_narrative(str(x.get('thesis','')),ev,t)
        for a in x.get('assumptions') or []:
            if not isinstance(a,dict) or not a.get('driver') or not a.get('failure_condition'):
                flags.append(f'incomplete_assumption:{t}')
    return selected, sorted(set(flags))

def validate_b(data, packet, selected):
    flags=[]; km=keymap(packet); disp=[x for x in (data or {}).get('dispositions',[]) if isinstance(x,dict)]
    by={x.get('ticker'):x for x in disp}
    cmap={c['ticker']:c for c in packet['candidates']}
    for x in selected:
        t=x.get('ticker'); d=by.get(t)
        if not d: flags.append(f'missing_disposition:{t}'); continue
        status=str(d.get('status','')).lower()
        if status not in {'direct_peer','alternative_opportunity','no_peer','insufficient_evidence'}:
            flags.append(f'invalid_disposition:{t}:{status}'); continue
        peer=d.get('peer')
        if status=='direct_peer':
            if peer not in (cmap.get(t,{}).get('direct_peers') or []): flags.append(f'not_direct_peer:{t}:{peer}')
        elif status=='alternative_opportunity':
            if peer not in (cmap.get(t,{}).get('alternative_peers') or []): flags.append(f'not_alternative_peer:{t}:{peer}')
        elif status=='no_peer':
            if cmap.get(t,{}).get('direct_peers') or cmap.get(t,{}).get('alternative_peers'):
                flags.append(f'no_peer_false:{t}')
        elif status=='insufficient_evidence' and not d.get('thesis'):
            flags.append(f'insufficient_without_reason:{t}')
        if status in {'direct_peer','alternative_opportunity'}:
            keys=d.get('evidence_keys') or []
            if not keys: flags.append(f'comparison_empty_evidence:{t}')
            ev=[]
            for k in keys:
                if k not in km: flags.append(f'unknown_key:{t}:{k}'); continue
                owner,fact=km[k]
                if owner not in {t,peer}: flags.append(f'comparison_wrong_owner:{t}:{k}'); continue
                ev.append(fact)
            flags += validate_narrative(str(d.get('thesis','')),ev,t)
            if len(str(d.get('thesis','')).strip())<80: flags.append(f'generic_comparison:{t}')
            if not d.get('dimension'): flags.append(f'missing_dimension:{t}')
    return disp, sorted(set(flags))

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--evidence',required=True); ap.add_argument('--runs',type=int,default=5); a=ap.parse_args()
    packet=json.load(open(a.evidence,encoding='utf-8'))
    results=[]
    for n in range(1,a.runs+1):
        rng=random.Random(1000+n); order=[c.copy() for c in packet['candidates']]; rng.shuffle(order)
        for c in order: rng.shuffle(c['packet']['evidence'])
        compact={'as_of':packet['as_of'],'candidates':[{'ticker':c['ticker'],'sector':c.get('sector'),
            'industry':c.get('industry'),'evidence':c['packet']['evidence']} for c in order],
            'peer_groups':{'direct':{c['ticker']:c.get('direct_peers',[]) for c in packet['candidates']},
                           'alternative':{c['ticker']:c.get('alternative_peers',[]) for c in packet['candidates']}}}
        t0=time.monotonic(); usage={}; errors={}; da=None; db=None
        try:
            da,ua=call(A,compact); usage['stage_a']=ua
        except Exception as exc: errors['stage_a']=repr(exc)
        if da is None:
            results.append({'run':n,'order':[c['ticker'] for c in order],'selected':[],'dispositions':[],'flags':['stage_a_provider_failure'],'qc_pass':False,'errors':errors,'usage':usage}); print(json.dumps(results[-1])); continue
        sel,fa=validate_a(da,packet)
        try:
            db,ub=call(B,{'as_of':packet['as_of'],'selected':sel,
                'direct_peers':compact['peer_groups']['direct'],'alternative_peers':compact['peer_groups']['alternative'],
                'candidates':compact['candidates']}); usage['stage_b']=ub
        except Exception as exc: errors['stage_b']=repr(exc); db=None
        disp,fb=validate_b(db,packet,sel) if db is not None else ([],['stage_b_provider_failure'])
        results.append({'run':n,'order':[c['ticker'] for c in order],'selected':[x['ticker'] for x in sel],
            'dispositions':disp,'flags':sorted(set(fa+fb)),'qc_pass':not errors and not fa and not fb,
            'errors':errors,'usage':usage,'raw_a':da,'raw_b':db})
        print(json.dumps({k:v for k,v in results[-1].items() if k not in ('raw_a','raw_b')},default=str))
    print(json.dumps({'as_of':packet['as_of'],'runs':results},indent=2,default=str))

if __name__=='__main__': main()
