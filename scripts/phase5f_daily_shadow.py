"""Reproducible daily shadow workflow; research-only, no scheduler."""
from __future__ import annotations

import json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from db_builder.config import local_engine  # noqa: E402

LOG=ROOT/"data"/"phase5f_shadow_runs.log"

def run(cmd):
    p=subprocess.run(cmd,capture_output=True,text=True)
    return {"cmd":" ".join(cmd),"returncode":p.returncode,"stdout":p.stdout[-4000:],"stderr":p.stderr[-2000:]}

def main(as_of):
    raw=pd.read_sql(text("select max(date) latest,count(*) n from public.research_core_hist"),local_engine()).iloc[0]
    latest=str(raw.latest)[:10]
    rec={"run_utc":datetime.now(timezone.utc).isoformat(),"as_of":as_of,"latest_data":latest}
    if as_of != latest:
        rec["status"]="FAIL_STALE_INPUT"; print(json.dumps(rec,indent=2)); LOG.open("a").write(json.dumps(rec)+"\n"); return 1
    py=sys.executable
    rec["forecast"]=run([py,str(ROOT/"scripts"/"phase5e_shadow_forecast.py"),as_of])
    rec["outcomes"]=run([py,str(ROOT/"scripts"/"phase5e_shadow_outcomes.py")])
    rec["qc"]=run([py,str(ROOT/"scripts"/"phase5f_shadow_qc.py")])
    rec["repro"]=run([py,str(ROOT/"scripts"/"phase5f_repro_check.py")])
    rec["status"]="PASS" if all(x["returncode"]==0 for x in (rec["forecast"],rec["outcomes"],rec["qc"],rec["repro"])) else "FAIL"
    LOG.open("a").write(json.dumps(rec)+"\n")
    print(json.dumps({"status":rec["status"],"as_of":as_of,"log":str(LOG)},indent=2))
    return 0 if rec["status"]=="PASS" else 1
if __name__=="__main__":
    if len(sys.argv)<2: print("usage: phase5f_daily_shadow.py YYYY-MM-DD"); sys.exit(2)
    sys.exit(main(sys.argv[1]))
