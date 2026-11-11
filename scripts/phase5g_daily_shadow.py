"""Daily Phase 5G shadow wrapper. Research-only; no production pipeline."""
from __future__ import annotations
import json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from db_builder.config import local_engine  # noqa: E402

LOG=ROOT/"data"/"phase5g_shadow_runs.log"
SCHEMA="aegis_shadow"

def main():
    d=pd.read_sql(text("select max(date) latest from public.research_core_hist"),local_engine()).iloc[0]
    latest=pd.Timestamp(d.latest).normalize(); now=datetime.now(timezone.utc)
    start=pd.bdate_range(latest,periods=2)[1].date()
    open_utc=datetime.combine(start,datetime.min.time(),tzinfo=timezone.utc).replace(hour=14,minute=30)
    rec={"run_utc":now.isoformat(),"as_of":str(latest.date()),"target_start":str(start)}
    if now>=open_utc:
        rec.update({"status":"skipped","reason":"retrospective_window_not_eligible"})
        LOG.open("a").write(json.dumps(rec)+"\n")
        with local_engine().begin() as c:
            c.execute(text(f"INSERT INTO {SCHEMA}.run_log(as_of_date,status,reason,details) VALUES (:d,'skipped','retrospective_window_not_eligible',cast(:j as jsonb))"),{"d":latest.date(),"j":json.dumps(rec)})
        print(json.dumps(rec,indent=2)); return 0
    py=sys.executable
    subprocess.run([py,str(ROOT/"scripts"/"phase5g_shadow_db.py"),"insert",str(latest.date())],check=True)
    subprocess.run([py,str(ROOT/"scripts"/"phase5g_outcomes.py")],check=True)
    rec["status"]="completed"
    LOG.open("a").write(json.dumps(rec)+"\n")
    with local_engine().begin() as c:
        c.execute(text(f"INSERT INTO {SCHEMA}.run_log(as_of_date,status,details) VALUES (:d,'completed',cast(:j as jsonb))"),{"d":latest.date(),"j":json.dumps(rec)})
    print(json.dumps(rec,indent=2)); return 0
if __name__=="__main__": sys.exit(main())
