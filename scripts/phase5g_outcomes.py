"""Append realized outcomes for AEGIS shadow forecasts; never updates forecasts."""
from __future__ import annotations
import hashlib, json, sys
from pathlib import Path
import numpy as np, pandas as pd
from sqlalchemy import text

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from db_builder.config import local_engine  # noqa: E402
REG=json.loads((ROOT/"data"/"phase5e_model_registry.json").read_text()); THR=float(REG["spike_model"]["threshold"])
SCHEMA="aegis_shadow"

def main():
    d=pd.read_sql(text("select date,spy_close from public.research_core_hist order by date"),local_engine())
    d["date"]=pd.to_datetime(d.date).dt.normalize(); d=d.sort_values("date").reset_index(drop=True); d["ret"]=d.spy_close.pct_change()
    with local_engine().begin() as c:
        rows=c.execute(text(f"select forecast_id,as_of_date,target_end,forecast_vol_5d from {SCHEMA}.forecasts order by as_of_date")).fetchall()
        for fid,asof,tend,fvol in rows:
            if pd.Timestamp(tend) > d.date.max(): continue
            pos=d.index[d.date==pd.Timestamp(asof)]
            if len(pos)==0: continue
            i=int(pos[0])
            if i+5>=len(d): continue
            realized=float(np.std(d.ret.iloc[i+1:i+6].to_numpy(float),ddof=1)*np.sqrt(252))
            h=hashlib.sha256(f"{fid}|{realized}|{THR}".encode()).hexdigest()
            c.execute(text(f"""INSERT INTO {SCHEMA}.outcomes(forecast_id,outcome_available_date,realized_vol_5d,squared_error,abs_error,spike_threshold,spike_outcome,record_sha256)
              VALUES (:fid,:d,:rv,:se,:ae,:thr,:so,:h) ON CONFLICT (forecast_id) DO NOTHING"""),
              {"fid":fid,"d":str(d.date.iloc[i+5].date()),"rv":realized,"se":(realized-fvol)**2,"ae":abs(realized-fvol),"thr":THR,"so":bool(realized>=THR),"h":h})
    print("outcomes updated")
    return 0
if __name__=="__main__": sys.exit(main())
