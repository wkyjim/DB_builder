#!/usr/bin/env python3
"""P2S: business-profile peers from verified product/revenue-driver tags."""
from __future__ import annotations
import argparse, json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'data/p2s_business_profiles.json').exists(): ROOT=Path(r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder')

PROFILES = json.load(open(ROOT / 'data/p2s_business_profiles.json'))

def overlap(a, b):
    pa = set(PROFILES.get(a, {}).get('products', [])); pb = set(PROFILES.get(b, {}).get('products', []))
    return sorted(pa & pb)

def peer_sets(tickers, sectors):
    direct = {}; alt = {}
    for t in tickers:
        direct[t] = []
        for p in tickers:
            if p == t: continue
            if overlap(t, p): direct[t].append(p)
    for t in tickers:
        sector_t = sectors.get(t)
        alt[t] = [p for p in tickers if p != t and p not in direct[t] and sectors.get(p) == sector_t]
    return direct, alt

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True); ap.add_argument('--out',required=True); a=ap.parse_args()
    x=json.load(open(a.input,encoding='utf-8')); tickers=[c['ticker'] for c in x['candidates']]
    sectors={c['ticker']:c.get('sector') for c in x['candidates']}
    direct,alt=peer_sets(tickers,sectors)
    for c in x['candidates']:
        t=c['ticker']; c['business_profile']=dict(PROFILES.get(t,{})); c['business_profile']['ticker']=t
        c['business_profile']['sector']=c.get('sector')
        c['direct_peers']=direct.get(t,[]); c['alternative_peers']=alt.get(t,[])
    x['schema']='aegis.p2s.evidence.v1'
    x['business_profiles']=PROFILES
    x['deterministic_disclosures']=x.get('deterministic_disclosures',[])+[
        'Direct peers require verified product/revenue-driver overlap, not GICS industry alone.',
        'Different business models in the same sector are alternative opportunities, not direct comparables.',
        'Stage B runs separately per selected candidate; failed units are retained as failures.']
    json.dump(x,open(a.out,'w',encoding='utf-8'),indent=2,default=str)
    print(json.dumps({'direct':direct,'alternative':alt}))

if __name__=='__main__': main()
