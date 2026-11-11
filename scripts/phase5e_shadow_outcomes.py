"""Append realized 5D outcomes to Phase 5E shadow forecasts only when complete."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np, pandas as pd
from sqlalchemy import text

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from db_builder.config import local_engine  # noqa: E402
import json
REG=json.loads((ROOT/"data"/"phase5e_model_registry.json").read_text()); THR=float(REG["spike_model"]["threshold"])

F=ROOT/"data"/"phase5e_shadow_forecasts.csv"; O=ROOT/"data"/"phase5e_shadow_outcomes.csv"

def main():
    if not F.exists(): print("no forecasts"); return 1
    d=pd.read_sql(text("select date,spy_close from public.research_core_hist order by date"),local_engine())
    d["date"]=pd.to_datetime(d.date).dt.normalize(); d=d.sort_values("date").reset_index(drop=True); d["ret"]=d.spy_close.pct_change()
    f=pd.read_csv(F); rows=[]
    for _,r in f.iterrows():
        pos=d.index[d.date==pd.Timestamp(r.as_of_market_date)]
        if len(pos)==0: continue
        i=int(pos[0])
        if i+5 >= len(d): continue
        realized=float(np.std(d.ret.iloc[i+1:i+6].to_numpy(float),ddof=1)*np.sqrt(252))
        rows.append({"forecast_id":r.forecast_id,"outcome_available_date":str(d.date.iloc[i+5].date()),
                     "realized_vol_5d":realized,"forecast_vol_5d":float(r.forecast_vol_5d),
                     "squared_error":float((realized-float(r.forecast_vol_5d))**2),
                     "abs_error":float(abs(realized-float(r.forecast_vol_5d))),
                     "spike_threshold":THR,"spike_outcome":bool(realized>=THR)})
    if not rows: print("no completed outcomes yet"); return 0
    df=pd.DataFrame(rows)
    if O.exists():
        old=pd.read_csv(O); df=df[~df.forecast_id.isin(set(old.forecast_id))]
        if len(df): df.to_csv(O,mode="a",header=False,index=False)
    else: df.to_csv(O,index=False)
    print({"appended":len(df),"outcomes":str(O)})
    return 0
if __name__=="__main__": sys.exit(main())
