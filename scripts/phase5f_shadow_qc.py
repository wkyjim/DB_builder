"""Phase 5F shadow-record integrity and QC."""
from __future__ import annotations

import json, sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from db_builder.config import local_engine  # noqa: E402

F=ROOT/"data"/"phase5e_shadow_forecasts.csv"; O=ROOT/"data"/"phase5e_shadow_outcomes.csv"

def main():
    if not F.exists():
        print(json.dumps({"status":"FAIL","reason":"no forecasts"})); return 1
    f=pd.read_csv(F); issues=[]
    if f.forecast_id.duplicated().any(): issues.append("duplicate forecast_id")
    if f.duplicated(["model_registry","as_of_market_date"]).any(): issues.append("duplicate model/as_of")
    if (pd.to_datetime(f.created_utc, utc=True).dt.tz_localize(None) < pd.to_datetime(f.as_of_market_date)).any(): issues.append("timestamp before as_of")
    if (f.source_row_date.astype(str)!=f.as_of_market_date.astype(str)).any(): issues.append("source row date mismatch")
    if (f.feature_completeness!=f.feature_count).any(): issues.append("incomplete features")
    raw=pd.read_sql(text("select max(date) latest,count(*) n from public.research_core_hist"),local_engine()).iloc[0]
    if str(raw.latest)[:10] != str(f.as_of_market_date.max()): issues.append("latest forecast as_of differs from latest data")
    outcomes=pd.DataFrame()
    if O.exists():
        outcomes=pd.read_csv(O)
        if outcomes.forecast_id.duplicated().any(): issues.append("duplicate outcome")
        if not set(outcomes.forecast_id).issubset(set(f.forecast_id)): issues.append("orphan outcome")
    # missing outcomes when full 5-day window exists
    d=pd.read_sql(text("select date from public.research_core_hist order by date"),local_engine())
    dates=pd.to_datetime(d.date).dt.normalize().tolist()
    missing=[]
    for _,r in f.iterrows():
        dt=pd.Timestamp(r.as_of_market_date)
        if dt not in dates: continue
        i=dates.index(dt)
        if i+5 < len(dates):
            if r.forecast_id not in set(outcomes.forecast_id if len(outcomes) else []): missing.append(r.forecast_id)
    if missing: issues.append(f"missing outcomes: {len(missing)}")
    print(json.dumps({"status":"PASS" if not issues else "FAIL","issues":issues,
                      "forecasts":len(f),"outcomes":len(outcomes),"latest_data":str(raw.latest),
                      "pending_outcomes":len(f)-len(outcomes)},indent=2))
    return 0 if not issues else 1
if __name__=="__main__": sys.exit(main())
