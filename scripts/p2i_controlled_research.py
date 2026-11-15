#!/usr/bin/env python3
"""P2I controlled AI comparison with deterministic disclosures and factual QC."""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path
import requests

PROMPT="""You are AEGIS, an institutional fundamental researcher. Use only supplied SEC actuals, SEC guidance passages, vendor estimates and explicit limitations. Never invent management guidance, FY2027/FY2028 consensus, revisions, probabilities, price targets or share adjustments. The final report's missing-data warnings are generated deterministically and are not your responsibility to reproduce.
Compare candidates and return any number of qualified ideas, including zero. Include AVOID or SELL when the evidence supports it. Do not force BUY or five ideas. For each selected company provide stance, thesis, drivers, 2027-2028 qualitative outlook, guidance vs assumptions (state unavailable if no passage), valuation evidence (state unavailable if absent), catalysts, risks, failure scenario, why preferred and alternatives considered. Do not give numeric targets, probabilities or unsupported factual claims.
Return JSON: {"selected":[{"ticker":"","stance":"BUY|NEUTRAL|AVOID|SELL","thesis":"","drivers":[],"outlook_2027_2028":"","guidance_vs_assumptions":"","valuation_evidence":"","catalysts":[],"risks":[],"failure_scenario":"","why_preferred":"","alternatives_considered":[]}],"rejected":[{"ticker":"","reason":""}]}.
"""

def numset(obj):
    out=set()
    def walk(x):
        if isinstance(x,dict):
            for v in x.values(): walk(v)
        elif isinstance(x,list):
            for v in x: walk(v)
        elif isinstance(x,(int,float)):
            out.add(abs(float(x)))
        elif isinstance(x,str):
            for tok in re.findall(r'\d+(?:[.,]\d+)?',x):
                try: out.add(abs(float(tok.replace(',',''))))
                except ValueError: pass
    walk(obj); return out

def validate(data, packet):
    flags=[]; allowed_ticks={c['ticker'] for c in packet['candidates']}
    selected=[x for x in (data or {}).get('selected',[]) if isinstance(x,dict)]
    rejected=[x for x in (data or {}).get('rejected',[]) if isinstance(x,dict)]
    if len(selected)>5: flags.append('more_than_five_selected')
    for x in selected+rejected:
        if x.get('ticker') not in allowed_ticks: flags.append('outside_candidate:'+str(x.get('ticker')))
    # Structured valuation/probability keys are not allowed without a verified valuation input.
    def forbidden(obj):
        if isinstance(obj,dict):
            for k,v in obj.items():
                if str(k).lower() in {'target','price_target','probability','probabilities'}: return True
                if forbidden(v): return True
        elif isinstance(obj,list): return any(forbidden(v) for v in obj)
        return False
    if forbidden(data): flags.append('forbidden_target_or_probability')
    # Only numbers present in the controlled evidence are admissible.
    allowed=numset(packet); allowed |= {2026,2027,2028,2029}
    for x in selected:
        t=x.get('ticker'); txt=' '.join(str(x.get(k,'')) for k in ('thesis','outlook_2027_2028','guidance_vs_assumptions','valuation_evidence','failure_scenario','why_preferred'))
        for tok in re.findall(r'(?<![A-Za-z])\d+(?:[.,]\d+)?',txt):
            try: v=abs(float(tok.replace(',','')))
            except ValueError: continue
            if v not in allowed and v not in {2026,2027,2028,2029}: flags.append(f'unsupported_numeric:{t}:{tok}')
        low=txt.lower()
        meta=next((c for c in packet['candidates'] if c['ticker']==t),{})
        has_g=bool(meta.get('management_guidance',{}).get('available'))
        if re.search(r'\bguidance|company expects|management expects|outlook\b',low) and not has_g:
            flags.append(f'guidance_without_evidence:{t}')
        if 'consensus' in low and not meta.get('vendor_estimate',{}).get('consensus_available'):
            flags.append(f'consensus_without_evidence:{t}')
        if re.search(r'\bFY2027\b|\bFY2028\b|fiscal 2027|fiscal 2028',txt,re.I) and not has_g:
            flags.append(f'period_without_guidance:{t}')
    return {'selected':[x.get('ticker') for x in selected],'rejected':[x.get('ticker') for x in rejected],
            'flags':sorted(set(flags)),'qc_pass':not flags}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--evidence',required=True); ap.add_argument('--runs',type=int,default=3); a=ap.parse_args()
    packet=json.load(open(a.evidence,encoding='utf-8'))
    evidence=[{'ticker':c['ticker'],'sector':c.get('sector'),'company_reported':c.get('company_reported'),
               'vendor_estimate':c.get('vendor_estimate'),'vendor_source':c.get('vendor_source'),
               'management_guidance':c.get('management_guidance'),'recent_filings':c.get('recent_filings'),
               'corporate_actions':c.get('corporate_actions'),'warnings':c.get('warnings')} for c in packet['candidates']]
    results=[]
    for n in range(1,a.runs+1):
        t0=time.monotonic(); err=None; data=None; usage={}
        try:
            r=requests.post('http://127.0.0.1:8645/v1/chat/completions',headers={'Authorization':'Bearer local'},
                json={'model':'upstage/solar-mini4:free','temperature':0.0,'max_tokens':3500,
                      'response_format':{'type':'json_object'},
                      'messages':[{'role':'system','content':PROMPT},
                                  {'role':'user','content':json.dumps({'as_of':packet['as_of'],'candidates':evidence},default=str)}]},timeout=240)
            body=r.json(); usage=body.get('usage') or {}
            data=json.loads(body['choices'][0]['message'].get('content') or '')
        except Exception as exc: err=repr(exc)
        v=validate(data,packet) if data is not None else {'flags':['no_data'],'qc_pass':False}
        # Deterministic disclosures are assembled here, never delegated to AI.
        results.append({'run':n,'runtime_s':round(time.monotonic()-t0,2),'usage':usage,
                        'selected':v['selected'],'rejected':v['rejected'],'flags':v['flags'],'qc_pass':v['qc_pass'],
                        'deterministic_disclosures':packet.get('deterministic_warnings',[]),'error':err})
        print(json.dumps(results[-1],default=str))
    print(json.dumps({'as_of':packet['as_of'],'candidate_count':len(evidence),'runs':results},indent=2))

if __name__=='__main__': main()
