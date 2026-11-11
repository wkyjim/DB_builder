"""Phase 5G: isolated PostgreSQL shadow store for the frozen 5D model."""
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

SCHEMA="aegis_shadow"
REG=json.loads((ROOT/"data"/"phase5e_model_registry.json").read_text())

DDL=f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE IF NOT EXISTS {SCHEMA}.model_versions (
  model_version text PRIMARY KEY,
  registry jsonb NOT NULL,
  registry_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.data_snapshots (
  snapshot_id text PRIMARY KEY,
  source_table text NOT NULL,
  max_date date NOT NULL,
  row_count integer NOT NULL,
  snapshot_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.forecasts (
  forecast_id text PRIMARY KEY,
  model_registry text NOT NULL,
  model_version text NOT NULL,
  as_of_date date NOT NULL,
  feature_cutoff timestamptz NOT NULL,
  target_start date NOT NULL,
  target_end date NOT NULL,
  forecast_origin text NOT NULL,
  status text NOT NULL CHECK (status IN ('prospective','retrospective_shadow')),
  source_created_utc timestamptz,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  data_snapshot_id text NOT NULL,
  data_snapshot_sha256 text NOT NULL,
  feature_json jsonb NOT NULL,
  feature_completeness smallint NOT NULL,
  qc_flags text NOT NULL,
  forecast_vol_5d double precision NOT NULL,
  spike_probability double precision NOT NULL,
  hist20_forecast double precision NOT NULL,
  ewma_forecast double precision NOT NULL,
  record_sha256 text NOT NULL,
  UNIQUE (model_version, as_of_date)
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.outcomes (
  outcome_id bigserial PRIMARY KEY,
  forecast_id text NOT NULL UNIQUE REFERENCES {SCHEMA}.forecasts(forecast_id),
  outcome_available_date date NOT NULL,
  realized_vol_5d double precision NOT NULL,
  squared_error double precision NOT NULL,
  abs_error double precision NOT NULL,
  spike_threshold double precision NOT NULL,
  spike_outcome boolean NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  record_sha256 text NOT NULL
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.run_log (
  run_id bigserial PRIMARY KEY,
  run_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  as_of_date date,
  status text NOT NULL,
  reason text,
  forecast_id text,
  details jsonb
);
CREATE OR REPLACE FUNCTION {SCHEMA}.reject_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'aegis_shadow is append-only: % on % is forbidden', TG_OP, TG_TABLE_NAME;
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS no_update_forecasts ON {SCHEMA}.forecasts;
CREATE TRIGGER no_update_forecasts BEFORE UPDATE OR DELETE ON {SCHEMA}.forecasts FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation();
DROP TRIGGER IF EXISTS no_update_outcomes ON {SCHEMA}.outcomes;
CREATE TRIGGER no_update_outcomes BEFORE UPDATE OR DELETE ON {SCHEMA}.outcomes FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation();
DROP TRIGGER IF EXISTS no_update_runs ON {SCHEMA}.run_log;
CREATE TRIGGER no_update_runs BEFORE UPDATE OR DELETE ON {SCHEMA}.run_log FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation();
REVOKE UPDATE, DELETE ON ALL TABLES IN SCHEMA {SCHEMA} FROM PUBLIC;
"""

def setup():
    with local_engine().begin() as c: c.execute(text(DDL))
    reg_json=json.dumps(REG,sort_keys=True); h=hashlib.sha256(reg_json.encode()).hexdigest()
    with local_engine().begin() as c:
        c.execute(text(f"""INSERT INTO {SCHEMA}.model_versions(model_version,registry,registry_sha256)
          VALUES (:v,cast(:r as jsonb),:h) ON CONFLICT (model_version) DO NOTHING"""),{"v":REG["registry_version"],"r":reg_json,"h":h})
    print("schema ready",SCHEMA)

def business_days(start, n):
    return pd.bdate_range(start, periods=n+1)[1:n+1].date.tolist()

def compute(as_of, d):
    row=d[d.date==pd.Timestamp(as_of)]
    if row.empty: raise ValueError("as_of row missing")
    r=row.iloc[0]; feats=REG["features"]
    if sum(pd.notna(r[c]) for c in feats)!=len(feats): raise ValueError("incomplete features")
    z=np.array([(float(r[c])-REG["scaler"]["mean"][c])/REG["scaler"]["std"][c] for c in feats])
    vol=float(REG["vol_model"]["intercept"]+sum(REG["vol_model"]["coefficients"][c]*z[i] for i,c in enumerate(feats)))
    logit=float(REG["spike_model"]["intercept"]+sum(REG["spike_model"]["coefficients"][c]*z[i] for i,c in enumerate(feats)))
    prob=float(1/(1+np.exp(-np.clip(logit,-30,30))))
    return r,vol,prob

def main(cmd="status", as_of=None):
    setup()
    d=pd.read_sql(text("select * from public.research_core_hist order by date"),local_engine())
    d["date"]=pd.to_datetime(d.date).dt.normalize(); latest=str(d.date.max().date())
    if cmd=="status":
        print(json.dumps({"latest_data":latest,"registry":REG["registry_version"]},indent=2)); return 0
    if not as_of: raise SystemExit("--as-of required")
    raw=d.copy(); d=add_data(d)
    if str(as_of)!=latest:
        with local_engine().begin() as c: c.execute(text(f"INSERT INTO {SCHEMA}.run_log(as_of_date,status,reason) VALUES (:d,'skipped','stale input: latest=%s' )"),{"d":as_of,"latest":latest}) if False else None
        print(json.dumps({"status":"SKIPPED","reason":"stale_input","as_of":as_of,"latest":latest},indent=2)); return 2
    r,vol,prob=compute(as_of,d)
    feats=REG["features"]; fjson={k:float(r[k]) for k in feats}
    target_start=business_days(as_of,1)[0]; target_end=business_days(as_of,5)[-1]
    now=datetime.now(timezone.utc)
    open_utc=datetime.combine(target_start,datetime.min.time(),tzinfo=timezone.utc).replace(hour=14,minute=30)
    status="prospective" if now<open_utc else "retrospective_shadow"
    snap=hashlib.sha256(raw.to_csv(index=False).encode()).hexdigest()
    fid=hashlib.sha256(f"{REG['registry_version']}|{as_of}|{snap}".encode()).hexdigest()[:16]
    payload={"fid":fid,"ver":REG["registry_version"],"as_of":as_of,"vol":vol,"prob":prob,"feats":fjson,
             "status":status,"target_start":str(target_start),"target_end":str(target_end),"snap":snap}
    rh=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    with local_engine().begin() as c:
        c.execute(text(f"""INSERT INTO {SCHEMA}.data_snapshots(snapshot_id,source_table,max_date,row_count,snapshot_sha256)
          VALUES (:s,'public.research_core_hist',:m,:n,:h) ON CONFLICT (snapshot_id) DO NOTHING"""),
          {"s":snap,"m":latest,"n":len(raw),"h":snap})
        c.execute(text(f"""INSERT INTO {SCHEMA}.forecasts(
          forecast_id,model_registry,model_version,as_of_date,feature_cutoff,target_start,target_end,forecast_origin,status,
          source_created_utc,data_snapshot_id,data_snapshot_sha256,feature_json,feature_completeness,qc_flags,
          forecast_vol_5d,spike_probability,hist20_forecast,ewma_forecast,record_sha256)
          VALUES (:fid,:reg,:ver,:asof,:cut,:ts,:te,'post_close',:status,:src,:snap,:snap,:fj,6,'OK',:vol,:prob,:hist,:ewma,:rh)
          ON CONFLICT (model_version,as_of_date) DO NOTHING"""),
          {"fid":fid,"reg":REG["registry_version"],"ver":REG["vol_model"]["version"],"asof":as_of,
           "cut":f"{as_of} 21:00:00+00","ts":target_start,"te":target_end,"status":status,"src":None,
           "snap":snap,"fj":json.dumps(fjson),"vol":vol,"prob":prob,
           "hist":float(r["hist20"]),"ewma":float(r["ewma"]),"rh":rh})
    print(json.dumps({"status":"INSERTED_OR_EXISTING","forecast_id":fid,"forecast_status":status,"as_of":as_of,"target_window":[str(target_start),str(target_end)],"vol":vol,"prob":prob},indent=2))
    return 0

if __name__=="__main__":
    cmd=sys.argv[1] if len(sys.argv)>1 else "status"
    asof=sys.argv[2] if len(sys.argv)>2 else None
    sys.exit(main(cmd,asof))
