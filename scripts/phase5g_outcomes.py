"""Append realized outcomes for AEGIS shadow forecasts; never updates forecasts."""
from __future__ import annotations
import hashlib, json, sys
from pathlib import Path
import pandas as pd
from sqlalchemy import text

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from db_builder.config import local_engine  # noqa: E402
REG=json.loads((ROOT/"data"/"phase5e_model_registry.json").read_text()); THR=float(REG["spike_model"]["threshold"])
SCHEMA="aegis_shadow"

def main():
    with local_engine().begin() as c:
        rows=c.execute(text(f"select forecast_id,as_of_date,target_end,forecast_vol_5d from {SCHEMA}.forecasts order by as_of_date")).fetchall()
        for fid,asof,tend,fvol in rows:
            hist=c.execute(text(f"select date,spy_close from {SCHEMA}.inference_daily where date>=:a and date<=:e order by date"),
                           {"a":asof,"e":tend}).fetchall()
            if len(hist) != 6 or any(x[1] is None for x in hist):
                continue
            if pd.Timestamp(hist[-1][0]) != pd.Timestamp(tend):
                continue
            px=pd.Series([float(x[1]) for x in hist])
            realized=float(px.pct_change().dropna().std(ddof=1)*np.sqrt(252))
            h=hashlib.sha256(f"{fid}|{realized}|{THR}".encode()).hexdigest()
            c.execute(text(f"""INSERT INTO {SCHEMA}.outcomes(forecast_id,outcome_available_date,realized_vol_5d,squared_error,abs_error,spike_threshold,spike_outcome,record_sha256)
              VALUES (:fid,:d,:rv,:se,:ae,:thr,:so,:h) ON CONFLICT (forecast_id) DO NOTHING"""),
              {"fid":fid,"d":str(tend),"rv":realized,"se":(realized-fvol)**2,"ae":abs(realized-fvol),
               "thr":THR,"so":bool(realized>=THR),"h":h})
    print("outcomes updated")
    return 0
if __name__=="__main__": sys.exit(main())
