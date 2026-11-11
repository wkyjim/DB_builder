"""Phase 5E: freeze the Phase 5C 5D models and write a registry."""
from __future__ import annotations

import hashlib, json, math, sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np, pandas as pd
from sqlalchemy import text

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src")); sys.path.insert(0,str(ROOT/"scripts"))
from db_builder.config import local_engine  # noqa: E402
from phase5c_tail import add_data, FEATURES, sigmoid  # noqa: E402

CUTOFF="2026-11-02"; ASOF="2026-11-09"

def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def main():
    d=pd.read_sql(text("select * from public.research_core_hist order by date"),local_engine())
    d["date"]=pd.to_datetime(d.date).dt.normalize(); d=add_data(d)
    train=d[d.date<=CUTOFF].dropna(subset=FEATURES+["vol5"])
    mu=train[FEATURES].mean(); sd=train[FEATURES].std().replace(0,np.nan)
    X=((train[FEATURES]-mu)/sd).to_numpy(float); X=np.column_stack([np.ones(len(X)),X])
    y=train.vol5.to_numpy(float)
    b=np.linalg.lstsq(X,y,rcond=None)[0]
    thr=float(train.vol5.quantile(.80))
    yb=(train.vol5>=thr).astype(int).to_numpy()
    # logistic gradient descent, same fixed settings as research
    w=np.zeros(X.shape[1])
    for _ in range(800):
        p=sigmoid(X@w); w-=0.1*(X.T@(p-yb)/len(yb)+1e-3*w)
    registry={
      "registry_version":"phase5e-2026-11-09",
      "created_at_utc":datetime.now(timezone.utc).isoformat(),
      "as_of_date":ASOF,"training_cutoff":CUTOFF,
      "data_source":"public.research_core_hist",
      "target":"future 5D realized volatility: std(spy ret[t+1..t+5], ddof=1)*sqrt(252)",
      "features":FEATURES,
      "scaler":{"mean":mu.to_dict(),"std":sd.to_dict()},
      "vol_model":{"version":"vol5_ols_v1","intercept":float(b[0]),"coefficients":dict(zip(FEATURES,b[1:].tolist())),
                   "train_n":int(len(train)),"oos_2018_2026_r2":0.4826,"oos_mae":0.0564,"oos_rmse":0.0878},
      "spike_model":{"version":"spike_logit_v1","intercept":float(w[0]),"coefficients":dict(zip(FEATURES,w[1:].tolist())),
                     "threshold":thr,"threshold_rule":"train-only 80th percentile of future 5D volatility",
                     "train_n":int(len(train))},
      "baselines":{"hist20":{"version":"hist20_v1","definition":"trailing 20D realized vol * sqrt(252)"},
                   "ewma":{"version":"ewma_v1","lambda":0.06,"definition":"RiskMetrics EWMA daily variance, annualized"}},
      "code":{"git_head":"50db4f352ebfecc7fbb8144e607186413102d595",
              "scripts":{"phase5c_tail.py":sha(ROOT/"scripts/phase5c_tail.py"),
                         "phase5e_freeze.py":sha(ROOT/"scripts/phase5e_freeze.py")},
              "notes":"Phase 5C/5D scripts are not committed; hashes identify frozen code."}
    }
    (ROOT/"data"/"phase5e_model_registry.json").write_text(json.dumps(registry,indent=2))
    print(json.dumps(registry,indent=2))
    return 0
if __name__=="__main__": sys.exit(main())
