#!/usr/bin/env python3
"""P2H controlled AI research repeatability with deterministic disclosures."""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
import requests
ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src')); sys.path.insert(0,str(ROOT/'scripts'))

PROMPT="""You are AEGIS, an institutional fundamental researcher. Use only the supplied SEC actuals, vendor estimates and explicit limitations. Never invent management guidance, FY2027/FY2028 consensus, revisions, probabilities, price targets or share adjustments.
The evidence packet contains deterministic warnings. Copy every global warning and every per-company warning relevant to a selected or rejected company into "warnings". Do not replace missing data with assumptions.
Compare candidates and select at most five for research; include AVOID or SELL when the evidence supports it; do not force BUY ideas. For each selected company give: stance, thesis, historical drivers and earnings quality, 2027-2028 demand/outlook, guidance vs independent assumptions (say unavailable if absent), valuation evidence (say unavailable if absent), catalysts, risks, failure scenario, why preferable to named alternatives. Do not provide numeric targets or probabilities.
Return JSON: {"selected":[{"ticker":"","stance":"BUY|NEUTRAL|AVOID|SELL","thesis":"","drivers":[],"outlook_2027_2028":"","guidance_vs_assumptions":"","valuation_evidence":"","catalysts":[],"risks":[],"failure_scenario":"","why_preferred":"","alternatives_considered":[]}],"rejected":[{"ticker":"","reason":""}],"warnings":[]}.
"""

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--evidence',default=r'C:\tmp\p2h_evidence.json'); ap.add_argument('--runs',type=int,default=3); a=ap.parse_args()
    packet=json.load(open(a.evidence,encoding='utf-8'))
    global_w=packet.get('deterministic_warnings',[])
    evidence=[{'ticker':x['ticker'],'company_reported':x.get('company_reported'),'vendor_estimate':x.get('vendor_estimate'),
               'vendor_source':x.get('vendor_source'),'management_guidance':x.get('management_guidance'),
               'corporate_actions':x.get('corporate_actions'),'warnings':x.get('warnings')} for x in packet['candidates']]
    results=[]
    for n in range(1,a.runs+1):
        t0=time.monotonic(); err=None; data=None; usage={}
        payload={'as_of':packet.get('as_of'),'candidates':evidence,'deterministic_warnings':global_w}
        try:
            r=requests.post('http://127.0.0.1:8645/v1/chat/completions',headers={'Authorization':'Bearer local'},
                json={'model':'upstage/solar-mini4:free','temperature':0.0,'max_tokens':3500,
                      'response_format':{'type':'json_object'},
                      'messages':[{'role':'system','content':PROMPT},
                                  {'role':'user','content':json.dumps(payload,default=str)}]},timeout=240)
            body=r.json(); usage=body.get('usage') or {}
            data=json.loads(body['choices'][0]['message'].get('content') or '')
        except Exception as exc: err=repr(exc)
        sel=data.get('selected',[]) if isinstance(data,dict) else []
        rej=data.get('rejected',[]) if isinstance(data,dict) else []
        warn=data.get('warnings',[]) if isinstance(data,dict) else []
        selected_t={x.get('ticker') for x in sel if isinstance(x,dict)}
        rejected_t={x.get('ticker') for x in rej if isinstance(x,dict)}
        relevant={x['ticker']:x.get('warnings',[]) for x in packet['candidates']}
        required=global_w + [w for t in selected_t|rejected_t for w in relevant.get(t,[])]
        missing=[w for w in required if w not in warn]
        # Deterministic warnings are authoritative and are appended outside AI prose.
        def has_forbidden_keys(obj):
            if isinstance(obj, dict):
                return any(str(k).lower() in ('target','price_target','probability','probabilities')
                           or has_forbidden_keys(v) for k,v in obj.items())
            if isinstance(obj,list): return any(has_forbidden_keys(v) for v in obj)
            return False
        warnings_complete=all(w in warn for w in global_w)
        qc=(err is None and len(sel)<=5 and warnings_complete and not has_forbidden_keys(data))
        results.append({'run':n,'runtime_s':round(time.monotonic()-t0,2),'usage':usage,
                        'selected':[{'ticker':x.get('ticker'),'stance':x.get('stance')} for x in sel if isinstance(x,dict)],
                        'rejected':[x.get('ticker') for x in rej if isinstance(x,dict)], 'warnings_count':len(warn),
                        'missing_deterministic_warnings':len(missing),'qc_pass':qc,'error':err})
        print(json.dumps(results[-1],default=str))
    print(json.dumps({'as_of':packet.get('as_of'),'candidate_count':len(evidence),'runs':results,'global_warnings':global_w},indent=2))

if __name__=='__main__': main()
