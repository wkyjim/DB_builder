"""Phase 5F reproducibility check for the frozen shadow forecast."""
from __future__ import annotations

import hashlib, json, sys
from pathlib import Path

import numpy as np, pandas as pd
from sqlalchemy import text

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src")); sys.path.insert(0,str(ROOT/"scripts"))
from db_builder.config import local_engine  # noqa: E402
from phase5c_tail import add_data  # noqa: E402

REG=json.loads((ROOT/"data"/"phase5e_model_registry.json").read_text())
F=ROOT/"data"/"phase5e_shadow_forecasts.csv"
def sig(z): return 1/(1+np.exp(-np.clip(z,-30,30)))

def main():
    raw=pd.read_sql(text("select * from public.research_core_hist order by date"),local_engine())
    d=raw.copy(); d["date"]=pd.to_datetime(d.date).dt.normalize(); d=add_data(d)
    f=pd.read_csv(F).sort_values("created_utc").iloc[-1]
    row=d[d.date==pd.Timestamp(f.as_of_market_date)].iloc[0]
    feats=REG["features"]
    z=np.array([(float(row[c])-REG["scaler"]["mean"][c])/REG["scaler"]["std"][c] for c in feats])
    vol=float(REG["vol_model"]["intercept"]+sum(REG["vol_model"]["coefficients"][c]*z[i] for i,c in enumerate(feats)))
    prob=float(sig(REG["spike_model"]["intercept"]+sum(REG["spike_model"]["coefficients"][c]*z[i] for i,c in enumerate(feats))))
    snap=hashlib.sha256(raw.to_csv(index=False).encode()).hexdigest()
    checks={
        "forecast_id":bool(f.forecast_id),
        "feature_order_match":feats==REG["features"],
        "model_registry_match":f.model_registry==REG["registry_version"],
        "vol_reproduced":abs(vol-float(f.forecast_vol_5d))<1e-9,
        "probability_reproduced":abs(prob-float(f.spike_probability))<1e-9,
        "data_snapshot_match":snap==REG["data_snapshot"]["sha256_csv"],
        "target_is_80th":bool(REG["spike_model"]["threshold"]==0.18495572148412626)
    }
    print(json.dumps({"checks":checks,"recorded_vol":float(f.forecast_vol_5d),"recomputed_vol":vol,
                      "recorded_prob":float(f.spike_probability),"recomputed_prob":prob,"as_of":str(f.as_of_market_date)},indent=2))
    return 0 if all(checks.values()) else 1
if __name__=="__main__": sys.exit(main())
