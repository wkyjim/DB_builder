#!/usr/bin/env python3
"""P2K typed, evidence-bound AI research and deterministic claim validation."""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path
import requests
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
# P2M: AI does not calculate; Python-precomputed derived evidence is cited.

PROMPT="""You are AEGIS, an institutional fundamental researcher. Use only the supplied evidence. Return typed claims. Never invent guidance, FY2027/FY2028 consensus, revisions, probabilities, targets or share adjustments.
Types:
- sourced_fact: cite one evidence_id and copy ticker, metric, period, unit, value exactly from that record.
- assumption: provide driver and failure_condition; do not use an evidence_id as if the assumption were a fact.
- inference: provide rationale; label it as inference and do not introduce unsourced numbers.
Use Python-precomputed DERIVED evidence IDs directly; never calculate a metric yourself.
Every selected company must have at least one claim. Compare positive and negative candidates; include AVOID/SELL when supported, any number of ideas including zero. Do not force labels.
Return JSON: {"selected":[{"ticker":"","stance":"BUY|NEUTRAL|AVOID|SELL","thesis":"","alternatives_considered":[],"claims":[{"claim_type":"sourced_fact","evidence_id":"","ticker":"","metric":"","period":"","unit":"","value":0,"statement":""},{"claim_type":"assumption","driver":"","failure_condition":"","statement":""},{"claim_type":"inference","rationale":"","statement":""}]}],"rejected":[{"ticker":"","reason":""}]}.
"""

def flat(packet):
    out={}
    for c in packet['candidates']:
        for group in ('sec_actuals','management_guidance','typed_guidance','derived','vendor_estimates','external_industry','corporate_actions'):
            for e in c.get(group,[]): out[e['id']]=e
    return out

def close(a,b,tol=0.02):
    try: return abs(float(a)-float(b)) <= max(tol,abs(float(b))*0.01)
    except Exception: return False

def validate(data, packet):
    flags=[]; ev=flat(packet); allowed={c['ticker'] for c in packet['candidates']}
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
    total=valid=sourced=calculated=assumptions=inferences=0
    for x in selected:
        t=x.get('ticker'); claims=x.get('claims') or []
        if not claims: flags.append(f'empty_claim_set:{t}')
        for c in claims:
            total+=1; typ=c.get('claim_type'); ok=True
            if typ=='sourced_fact':
                sourced+=1; eid=c.get('evidence_id'); e=ev.get(eid)
                if not e: flags.append(f'unknown_evidence_id:{t}:{eid}'); ok=False
                else:
                    if e.get('ticker')!=t and e.get('kind')!='EXTERNAL_MARKET':
                        flags.append(f'cross_ticker:{t}:{eid}'); ok=False
                    if e.get('kind')=='MANAGEMENT_GUIDANCE_TYPED':
                        if c.get('metric') not in (None,'') and c.get('metric')!=e.get('metric'):
                            flags.append(f'metric_mismatch:{t}:{eid}'); ok=False
                    for fld in ('metric','period','unit'):
                        expected=e.get(fld) or e.get('period_end') or ''
                        if fld=='unit' and e.get('kind')=='MANAGEMENT_GUIDANCE' and str(c.get(fld)).lower() in ('text',''):
                            continue
                        if fld=='value' and e.get('kind')=='CORPORATE_ACTION' and '8' in str(c.get('value')) and '8-for-1' in str(e.get('value')):
                            continue
                        if c.get(fld) not in (None,'') and str(c.get(fld))!=str(expected):
                            flags.append(f'{fld}_mismatch:{t}:{eid}'); ok=False
                    if e.get('kind')!='MANAGEMENT_GUIDANCE_TYPED' and c.get('value') is not None and e.get('value') is not None and not close(c.get('value'),e.get('value')):
                        flags.append(f'value_mismatch:{t}:{eid}'); ok=False
                    qm=re.search(r'\bQ([1-4])\b',str(c.get('statement','')),re.I)
                    mfp=str(e.get('metric_fiscal_period') or '').upper()
                    if qm and mfp in ('Q1','Q2','Q3','Q4') and qm.group(1).upper()!=mfp:
                        flags.append(f'fiscal_quarter_mismatch:{t}:{eid}'); ok=False
                    st=str(c.get('statement',''))
                    qm=re.search(r'\bQ([1-4])\b',st,re.I)
                    fp=str(e.get('metric_period_type') or e.get('fiscal_period') or '').upper()
                    if qm and fp in ('Q1','Q2','Q3','Q4') and qm.group(1).upper()!=fp:
                        flags.append(f'fiscal_quarter_mismatch:{t}:{eid}'); ok=False
                    ym=re.search(r'\bFY\s*(\d{4})\b',st,re.I)
                    if ym and e.get('fiscal_year') and str(ym.group(1))!=str(e.get('fiscal_year')):
                        flags.append(f'fiscal_year_mismatch:{t}:{eid}'); ok=False
            elif typ=='calculated':
                flags.append(f'ai_calculation_not_allowed:{t}'); ok=False
            elif typ=='assumption':
                assumptions+=1
                if not c.get('driver') or not c.get('failure_condition'): flags.append(f'incomplete_assumption:{t}'); ok=False
                if c.get('evidence_id') or c.get('evidence_ids'): flags.append(f'assumption_cites_fact:{t}'); ok=False
            elif typ=='inference':
                inferences+=1
                if not c.get('rationale'): flags.append(f'inference_without_rationale:{t}'); ok=False
            else:
                flags.append(f'unknown_claim_type:{t}:{typ}'); ok=False
            if ok: valid+=1
    return {'selected':[x.get('ticker') for x in selected],'rejected':[x.get('ticker') for x in rejected],
            'claims_total':total,'claims_valid':valid,'sourced_facts':sourced,'calculated':calculated,
            'assumptions':assumptions,'inferences':inferences,'flags':sorted(set(flags)),
            'qc_pass':bool(not flags),'selected_with_claims':sum(1 for x in selected if x.get('claims'))}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--evidence',required=True); ap.add_argument('--runs',type=int,default=3); a=ap.parse_args()
    packet=json.load(open(a.evidence,encoding='utf-8'))
    results=[]
    for n in range(1,a.runs+1):
        t0=time.monotonic(); err=None; data=None; usage={}
        try:
            r=requests.post('http://127.0.0.1:8645/v1/chat/completions',headers={'Authorization':'Bearer local'},
                json={'model':'upstage/solar-mini4:free','temperature':0.0,'max_tokens':4000,
                      'response_format':{'type':'json_object'},
                      'messages':[{'role':'system','content':PROMPT},
                                  {'role':'user','content':json.dumps(packet,default=str)}]},timeout=300)
            body=r.json(); usage=body.get('usage') or {}
            data=json.loads(body['choices'][0]['message'].get('content') or '')
        except Exception as exc: err=repr(exc)
        v=validate(data,packet) if data is not None else {'claims_total':0,'claims_valid':0,'flags':['no_data'],'qc_pass':False}
        result={'run':n,'runtime_s':round(time.monotonic()-t0,2),'usage':usage,
                'selected':v.get('selected',[]),'rejected':v.get('rejected',[]),
                'claims_total':v.get('claims_total'),'claims_valid':v.get('claims_valid'),
                'sourced_facts':v.get('sourced_facts'),'calculated':v.get('calculated'),
                'assumptions':v.get('assumptions'),'inferences':v.get('inferences'),
                'flags':v.get('flags',[]),'qc_pass':v.get('qc_pass',False),'error':err,'raw':data,
                'deterministic_disclosures':packet.get('deterministic_disclosures',[])}
        results.append(result)
        print(json.dumps({k:x for k,x in result.items() if k!='raw'},default=str))
    print(json.dumps({'as_of':packet.get('as_of'),'candidate_count':len(packet['candidates']),'runs':results},indent=2,default=str))

if __name__=='__main__': main()
