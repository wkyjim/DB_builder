#!/usr/bin/env python3
"""P2M: add Python-precomputed derived evidence and typed guidance."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'src').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')
sys.path.insert(0,str(ROOT/'src'))
from db_builder.research_calculations import calculate
from db_builder.config import local_engine
from sqlalchemy import text


def sec_map(c):
    return {e['id']:e for e in c.get('sec_actuals',[])}


def pick(metrics, metric, period_type):
    rows=[e for e in metrics if e.get('metric')==metric and e.get('period_type')==period_type]
    return sorted(rows,key=lambda e:e.get('period'),reverse=True)


def add_derived(t, d, derived):
    ids={i['id']:i for i in d['sec_actuals']}
    # Inputs are explicit evidence IDs; the calculation is Python-owned.
    def calc(name, kind, input_ids, unit):
        vals=[]
        for eid in input_ids:
            if eid not in ids: return
            vals.append(float(ids[eid]['value']))
        try:
            value=calculate(kind, dict(zip(('current','prior') if kind=='growth_pct' else ('numerator','denominator'), vals)))
        except Exception:
            return
        derived.append({'id':f'DERIVED:{t}:{name}:{ids[input_ids[0]]["period"]}',
                        'ticker':t,'kind':'DERIVED_CALCULATION','metric':name,
                        'period':ids[input_ids[0]]['period'],'unit':unit,'value':value,
                        'calculation_type':kind,'input_ids':input_ids,'source':'Python precomputed',
                        'lineage':[{'id':eid,'metric':ids[eid]['metric'],'period':ids[eid]['period']}
                                   for eid in input_ids]})
    annual=d['sec_actuals']
    for metric in ('revenue','net_income','operating_cash_flow'):
        q=pick(annual,metric,'Q')
        if len(q)>=5:
            cur, prior=q[0], q[4]
            calc(f'{metric}_growth_yoy','growth_pct',[cur['id'],prior['id']],'%')
    rev=pick(annual,'revenue','Y'); net=pick(annual,'net_income','Y'); op=pick(annual,'operating_income','Y')
    if rev and net:
        calc('net_margin','margin_pct',[net[0]['id'],rev[0]['id']],'%')
    if rev and op:
        calc('operating_margin','margin_pct',[op[0]['id'],rev[0]['id']],'%')


def typed_guidance(t, guidance):
    out=[]
    for i,x in enumerate(guidance,1):
        p=x.get('passage',''); text=p.lower(); gid=f'{x["id"]}:{i}'
        if t=='SNDK' and 'second quarter 2027 revenue' in text and 'pre-split' in text:
            out += [
              {'id':f'GUIDT:{gid}:revenue','ticker':t,'kind':'MANAGEMENT_GUIDANCE_TYPED','metric':'revenue',
               'low':12.2,'high':12.6,'unit':'USD billions','period':'Q2 FY2027','gaap_basis':'GAAP',
               'split_basis':'pre-split','source_passage_id':x['id'],'source':x['source_url']},
              {'id':f'GUIDT:{gid}:eps','ticker':t,'kind':'MANAGEMENT_GUIDANCE_TYPED','metric':'diluted_eps',
               'low':55.0,'high':57.0,'unit':'USD/shares','period':'Q2 FY2027','gaap_basis':'non-GAAP',
               'split_basis':'pre-split','source_passage_id':x['id'],'source':x['source_url']},
            ]
        elif t=='EXE' and 'full year 2026' in text and 'capital investment guidance' in text:
            out.append({'id':f'GUIDT:{gid}:capex','ticker':t,'kind':'MANAGEMENT_GUIDANCE_TYPED','metric':'capex',
                        'low':3.0,'high':3.0,'unit':'USD billions','period':'FY2026','gaap_basis':'company guidance',
                        'split_basis':'not applicable','source_passage_id':x['id'],'source':x['source_url']})
    return out


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True); ap.add_argument('--out',required=True); a=ap.parse_args()
    packet=json.load(open(a.input,encoding='utf-8'))
    tickers=[c['ticker'] for c in packet['candidates']]
    with local_engine().connect() as conn:
        dbrows=conn.execute(text("""select ticker,metric,value,period_end,period_type,unit,formula,
          fiscal_period_original,metric_period_type,metric_fiscal_period,start_date,end_date,derivation_method,source_accession,available_at
          from fundamentals.sec_metrics where ticker=any(:t) and pit_eligible is not false"""),{'t':tickers}).fetchall()
    extra={}
    for t,m,v,p,pt,u,f,fpo,mpt,mfp,st,en,dm,acc,av in dbrows:
        extra.setdefault(t,[]).append({'id':f'SEC:{t}:{m}:{pt}:{p}','ticker':t,'kind':'SEC_ACTUAL','metric':m,
            'period':str(p),'period_type':pt,'value':float(v) if v is not None else None,'unit':u,
            'formula':f,'fiscal_period_original':fpo,'metric_period_type':mpt,'metric_fiscal_period':mfp,'start_date':str(st),'end_date':str(en),'derivation_method':dm,'source':'SEC Companyfacts',
            'accession':acc,'available_at':str(av)})
    for c in packet['candidates']:
        existing={e['id'] for e in c.get('sec_actuals',[])}
        extra_by_id={e['id']:e for e in extra.get(c['ticker'],[])}
        for e in c.get('sec_actuals',[]):
            if e['id'] in extra_by_id:
                e.update({k:extra_by_id[e['id']].get(k) for k in ('fiscal_year','fiscal_period','fiscal_period_original','metric_period_type','metric_fiscal_period','start_date','end_date','derivation_method') if extra_by_id[e['id']].get(k) is not None})
        c['sec_actuals'] += [e for e in extra.get(c['ticker'],[]) if e['id'] not in existing]
        derived=[]; add_derived(c['ticker'],c,derived)
        # Compact the AI packet: derived IDs carry lineage; do not send 300+ raw facts.
        c['sec_actuals']=c['sec_actuals'][:18]
        c['derived']=derived
        c['typed_guidance']=typed_guidance(c['ticker'],c.get('management_guidance',[]))
        # Preserve ambiguous guidance as qualitative only.
        c['guidance_qualitative']=[g for g in c.get('management_guidance',[]) if not any(
            q.get('source_passage_id')==g.get('id') for q in c['typed_guidance'])]
    packet['schema']='aegis.p2m.evidence.v1'
    packet['deterministic_disclosures']=packet.get('deterministic_disclosures',[])+[
        'Derived metrics are Python-precomputed evidence IDs; AI does not calculate them.',
        'Typed guidance separates GAAP/non-GAAP and pre/post-split bases; ambiguous guidance remains qualitative.',
    ]
    json.dump(packet,open(a.out,'w',encoding='utf-8'),indent=2,default=str)
    print(json.dumps({'candidates':len(packet['candidates']),'derived':sum(len(c['derived']) for c in packet['candidates']),'typed_guidance':sum(len(c['typed_guidance']) for c in packet['candidates'])}))

if __name__=='__main__': main()
