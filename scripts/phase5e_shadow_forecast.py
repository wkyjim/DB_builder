"""Phase 5E shadow forecast: append-only, research-only, frozen 5D model."""
from __future__ import annotations

import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np, pandas as pd
from sqlalchemy import text

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src")); sys.path.insert(0,str(ROOT/"scripts"))
from db_builder.config import local_engine  # noqa: E402
from phase5c_tail import add_data  # noqa: E402

REG=json.loads((ROOT/"data"/"phase5e_model_registry.json").read_text())
FORECAST=ROOT/"data"/"phase5e_shadow_forecasts.csv"
OUTCOMES=ROOT/"data"/"phase5e_shadow_outcomes.csv"


def sigmoid(z): return 1/(1+np.exp(-np.clip(z,-30,30)))


def main(as_of: str):
    d=pd.read_sql(text("select * from public.research_core_hist order by date"),local_engine())
    d["date"]=pd.to_datetime(d.date).dt.normalize(); d=add_data(d)
    d["hist5"]=d["ret"].rolling(5,min_periods=5).std()*np.sqrt(252)
    target=pd.Timestamp(as_of).normalize()
    row=d[d.date==target]
    if row.empty:
        print("no as-of row"); return 1
    r=row.iloc[0]
    feats=REG["features"]; complete=sum(pd.notna(r[c]) for c in feats)
    z=np.array([(float(r[c])-REG["scaler"]["mean"][c])/REG["scaler"]["std"][c] for c in feats])
    vol=float(REG["vol_model"]["intercept"]+sum(REG["vol_model"]["coefficients"][c]*z[i] for i,c in enumerate(feats)))
    logit=float(REG["spike_model"]["intercept"]+sum(REG["spike_model"]["coefficients"][c]*z[i] for i,c in enumerate(feats)))
    prob=float(sigmoid(np.array([logit]))[0])
    fid=hashlib.sha256(f"{target.date()}|{REG['registry_version']}|{r['date']}".encode()).hexdigest()[:16]
    rec={
        "forecast_id":fid,
        "created_utc":datetime.now(timezone.utc).isoformat(),
        "as_of_market_date":str(target.date()),
        "model_registry":REG["registry_version"],
        "vol_model_version":REG["vol_model"]["version"],
        "spike_model_version":REG["spike_model"]["version"],
        "data_source":REG["data_source"],
        "data_version":f"{d.date.max().date()}|{len(d)}",
        "code_commit":REG["code"]["git_head"],
        "current_vol_5d":float(r.get("hist5",np.nan)),
        "stress_state":bool(float(r.get("hist5",np.nan))>=0.20),
        "forecast_vol_5d":vol,
        "spike_probability":prob,
        "hist20_forecast":float(r.get("hist20",np.nan)),
        "ewma_forecast":float(r.get("ewma",np.nan)),
        "feature_completeness":int(complete),
        "feature_count":len(feats),
        "qc_flags":"OK" if complete==len(feats) else "MISSING_FEATURES",
        "training_cutoff":REG["training_cutoff"],
        "source_row_date":str(r["date"].date()),
    }
    df=pd.DataFrame([rec])
    if FORECAST.exists():
        old=pd.read_csv(FORECAST)
        if fid in set(old.forecast_id):
            print("already recorded",fid); return 0
        df.to_csv(FORECAST,mode="a",header=False,index=False)
    else:
        df.to_csv(FORECAST,index=False)
    print({"forecast_id":fid,"as_of":str(target.date()),"vol":vol,"prob":prob,"complete":complete})
    return 0

if __name__=="__main__":
    if len(sys.argv)<2: print("usage: phase5e_shadow_forecast.py YYYY-MM-DD"); sys.exit(2)
    sys.exit(main(sys.argv[1]))
