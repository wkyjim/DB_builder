"""Phase 5H daily refresh + prospective shadow workflow."""
from __future__ import annotations
import json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
from sqlalchemy import text

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from db_builder.config import local_engine  # noqa: E402
from session_calendar import last_completed_session, next_sessions, session_open_utc

LOG=ROOT/"data"/"phase5h_shadow_runs.log"; SCHEMA="aegis_shadow"
def main():
    py=sys.executable
    r=subprocess.run([py,str(ROOT/"scripts"/"phase5h_refresh_inference.py")],capture_output=True,text=True)
    rec={"run_utc":datetime.now(timezone.utc).isoformat(),"refresh_rc":r.returncode,"refresh":r.stdout[-2000:]}
    if r.returncode!=0:
        rec.update({"status":"failed","reason":"refresh_failed","stderr":r.stderr[-1000:]})
    else:
        d=pd.read_sql(text(f"select max(date) latest from {SCHEMA}.inference_daily"),local_engine()).iloc[0]
        latest=pd.Timestamp(d.latest).normalize()
        last=last_completed_session()
        rec.update({"as_of":str(latest.date()),"last_completed_session":str(last)})
        if latest.date() < last:
            rec.update({"status":"skipped","reason":"stale_latest_session"})
        else:
            maxasof=pd.read_sql(text(f"select max(as_of_date) m from {SCHEMA}.forecasts"),local_engine()).iloc[0].m
            if maxasof is not None and pd.Timestamp(maxasof).date() >= latest.date():
                rec.update({"status":"skipped","reason":"already_forecast"})
            else:
                start=next_sessions(latest.date(),1)[0]
                open_utc=session_open_utc(start)
                rec.update({"target_start":str(start),"target_open_utc":open_utc.isoformat()})
                if datetime.now(timezone.utc)>=open_utc:
                    rec.update({"status":"skipped","reason":"retrospective_window_not_eligible"})
                else:
                    ins=subprocess.run([py,str(ROOT/"scripts"/"phase5g_shadow_db.py"),"insert",str(latest.date()),"inference"],capture_output=True,text=True)
                    out=subprocess.run([py,str(ROOT/"scripts"/"phase5g_outcomes.py")],capture_output=True,text=True)
                    rec.update({"status":"completed" if ins.returncode==0 and out.returncode==0 else "failed","insert":ins.stdout[-1000:],"outcomes":out.stdout[-1000:]})
    LOG.open("a").write(json.dumps(rec)+"\n")
    with local_engine().begin() as c:
        c.execute(text(f"INSERT INTO {SCHEMA}.run_log(as_of_date,status,reason,details) VALUES (:d,:s,:r,cast(:j as jsonb))"),
                  {"d":rec.get("as_of"),"s":rec["status"],"r":rec.get("reason"),"j":json.dumps(rec)})
    print(json.dumps(rec,indent=2)); return 0 if rec["status"] in ("completed","skipped") else 1
if __name__=="__main__": sys.exit(main())
