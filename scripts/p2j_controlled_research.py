#!/usr/bin/env python3
"""P2J evidence-ID-bound AI research test with claim-level validation."""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path
import requests

PROMPT="""You are AEGIS, an institutional fundamental researcher. Use only the supplied evidence. Every factual or numerical claim must cite the exact evidence IDs in "claims". Do not invent guidance, consensus, periods, units, targets, probabilities or share adjustments. Qualitative inference is allowed only when explicitly labeled "inference" and must not be presented as verified fact.
Return JSON: {"selected":[{"ticker":"","stance":"BUY|NEUTRAL|AVOID|SELL","thesis":"","drivers":[],"outlook_2027_2028":"","guidance_vs_assumptions":"","valuation_evidence":"","catalysts":[],"risks":[],"failure_scenario":"","why_preferred":"","alternatives_considered":[],"claims":[{"statement":"","evidence_ids":[],"claim_type":"sourced|calculated|assumption|inference"}]}],"rejected":[{"ticker":"","reason":""}]}. Any number of ideas, including zero, is allowed.
"""

def flatten_evidence(packet):
    out={}
    for c in packet['candidates']:
        for group in ('sec_actuals','management_guidance','vendor_estimates','external_industry'):
            for e in c.get(group,[]): out[e['id']]=e
    return out

def validate(data, packet):
    flags=[]; ev=flatten_evidence(packet); allowed={c['ticker'] for c in packet['candidates']}
    selected=[x for x in (data or {}).get('selected',[]) if isinstance(x,dict)]
    rejected=[x for x in (data or {}).get('rejected',[]) if isinstance(x,dict)]
    if len(selected)>5: flags.append('more_than_five_selected')
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
    total=valid=0
    for x in selected:
        t=x.get('ticker'); claims=x.get('claims') or []
        if not claims: flags.append(f'no_claims:{t}')
        for c in claims:
            total+=1; ids=c.get('evidence_ids') or []; ok=True
            if not ids: flags.append(f'claim_without_evidence:{t}'); ok=False
            for eid in ids:
                e=ev.get(eid)
                if not e: flags.append(f'unknown_evidence_id:{eid}'); ok=False; continue
                if e.get('ticker') != t: flags.append(f'cross_ticker_evidence:{t}:{eid}'); ok=False
            st=str(c.get('statement','')); low=st.lower()
            # Claims that mention guidance/consensus must cite the corresponding evidence type.
            if re.search(r'\bguidance|company expects|management expects\b',low) and not any(ev.get(i,{}).get('kind')=='MANAGEMENT_GUIDANCE' for i in ids):
                flags.append(f'guidance_claim_without_guidance_id:{t}'); ok=False
            if 'consensus' in low:
                flags.append(f'consensus_claim_without_verified_consensus:{t}'); ok=False
            if re.search(r'\bFY2027\b|\bFY2028\b|fiscal 2027|fiscal 2028',st,re.I):
                gids=[i for i in ids if ev.get(i,{}).get('kind')=='MANAGEMENT_GUIDANCE']
                if not gids: flags.append(f'period_claim_without_guidance:{t}'); ok=False
                if gids and not any('2027' in str(ev.get(i,{}).get('passage','')) or '2028' in str(ev.get(i,{}).get('passage','')) for i in gids):
                    flags.append(f'period_claim_not_supported:{t}'); ok=False
            # Numeric claim: every token must match the cited evidence or a derived split factor 8.
            nums=[float(n.replace(',','')) for n in re.findall(r'\\d+(?:[.,]\\d+)?',st)]
            for n in nums:
                if n in (2026,2027,2028): continue
                vals=[]
                for i in ids:
                    e=ev.get(i,{})
                    for k in ('value','return_20d_pct'):
                        if isinstance(e.get(k),(int,float)): vals.append(abs(float(e[k])))
                    for tok in re.findall(r'\\d+(?:[.,]\\d+)?',str(e.get('passage',''))):
                        vals.append(abs(float(tok.replace(',',''))))
                if not any(abs(n-v)<=max(0.01,abs(v)*0.01) for v in vals):
                    flags.append(f'unsupported_numeric:{t}:{n}'); ok=False
            if ok: valid+=1
    return {'selected':[x.get('ticker') for x in selected],'rejected':[x.get('ticker') for x in rejected],
            'claims_total':total,'claims_valid':valid,'flags':sorted(set(flags)),
            'qc_pass':bool(not flags)}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--evidence',required=True); ap.add_argument('--runs',type=int,default=3); a=ap.parse_args()
    packet=json.load(open(a.evidence,encoding='utf-8'))
    compact={'as_of':packet['as_of'],'candidates':packet['candidates'],'deterministic_disclosures':packet['deterministic_disclosures']}
    results=[]
    for n in range(1,a.runs+1):
        t0=time.monotonic(); err=None; data=None; usage={}
        try:
            r=requests.post('http://127.0.0.1:8645/v1/chat/completions',headers={'Authorization':'Bearer local'},
                json={'model':'upstage/solar-mini4:free','temperature':0.0,'max_tokens':3500,
                      'response_format':{'type':'json_object'},
                      'messages':[{'role':'system','content':PROMPT},
                                  {'role':'user','content':json.dumps(compact,default=str)}]},timeout=240)
            body=r.json(); usage=body.get('usage') or {}
            data=json.loads(body['choices'][0]['message'].get('content') or '')
        except Exception as exc: err=repr(exc)
        v=validate(data,packet) if data is not None else {'claims_total':0,'claims_valid':0,'flags':['no_data'],'qc_pass':False}
        results.append({'run':n,'runtime_s':round(time.monotonic()-t0,2),'usage':usage,
                        'selected':v.get('selected',[]),'rejected':v.get('rejected',[]),
                        'claims_total':v.get('claims_total'),'claims_valid':v.get('claims_valid'),
                        'flags':v.get('flags',[]),'qc_pass':v.get('qc_pass',False),
                        'deterministic_disclosures':packet['deterministic_disclosures'],'error':err,
                        'raw':data})
        print(json.dumps({k:x for k,x in results[-1].items() if k!='raw'},default=str))
    print(json.dumps({'as_of':packet['as_of'],'candidate_count':len(packet['candidates']),'runs':results},indent=2,default=str))

if __name__=='__main__': main()
