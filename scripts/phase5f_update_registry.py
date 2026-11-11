"""Update model registry metadata without retraining or changing model parameters."""
from __future__ import annotations

import hashlib, json, platform, sys
from pathlib import Path
from importlib.metadata import version

import pandas as pd
from sqlalchemy import text

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from db_builder.config import local_engine  # noqa: E402

REG=ROOT/"data"/"phase5e_model_registry.json"

def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def main():
    reg=json.loads(REG.read_text())
    d=pd.read_sql(text("select * from public.research_core_hist order by date"),local_engine())
    snapshot=hashlib.sha256(d.to_csv(index=False).encode()).hexdigest()
    reg["target_definitions"]={
        "spike_80th":"future 5D realized volatility >= train-only 80th-percentile threshold",
        "fixed_20":"future 5D realized volatility >= 20% annualized",
        "onset_20":"current 5D realized volatility < 20% and future 5D realized volatility >= 20%",
        "persistence_20":"current 5D realized volatility >= 20% and future 5D realized volatility >= 20%"
    }
    reg["spike_target_definition"]="future 5D realized volatility >= 18.4956% (train-only 80th percentile)"
    reg["fixed_stress_threshold"]=0.20
    reg["data_snapshot"]={
        "table":"public.research_core_hist","rows":int(len(d)),
        "start":str(d.date.min().date()),"end":str(d.date.max().date()),
        "sha256_csv":snapshot
    }
    reg["dependencies"]={
        "python":platform.python_version(),"pandas":version("pandas"),
        "numpy":version("numpy"),"scipy":version("scipy"),
        "sqlalchemy":version("SQLAlchemy")
    }
    scripts={}
    for name in ("phase5a_risk.py","phase5aqc_vol_audit.py","phase5b_vol_robustness.py",
                 "phase5c_tail.py","phase5d_early_warning.py","phase5e_qc.py",
                 "phase5e_freeze.py","phase5e_shadow_forecast.py","phase5e_shadow_outcomes.py",
                 "phase5f_repro_check.py","phase5f_shadow_qc.py","phase5f_daily_shadow.py",
                 "phase5f_update_registry.py","phase5g_shadow_db.py","phase5g_outcomes.py",
                 "phase5g_daily_shadow.py","phase5h_refresh_inference.py",
                 "phase5h_daily_shadow.py"):
        p=ROOT/"scripts"/name
        if p.exists(): scripts[name]=sha(p)
    reg.setdefault("code",{})["scripts"]=scripts
    reg["code"]["git_head"]="50db4f352ebfecc7fbb8144e607186413102d595"
    reg["code"]["note"]="Base commit before Phase 5F freeze commit; scripts and registry are committed in the Phase 5F commit."
    REG.write_text(json.dumps(reg,indent=2))
    print(json.dumps({"registry":str(REG),"rows":len(d),"snapshot":snapshot,"dependencies":reg["dependencies"]},indent=2))
    return 0
if __name__=="__main__": sys.exit(main())
