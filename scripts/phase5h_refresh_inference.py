"""Phase 5H: refresh frozen-feature inference data without touching training data."""
from __future__ import annotations

import hashlib, io, json, math, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np, pandas as pd
import yfinance as yf
from sqlalchemy import text

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from db_builder.config import local_engine
from session_calendar import last_completed_session  # noqa: E402

SCHEMA="aegis_shadow"; START="2013-01-01"

def fetch(ticker):
    d=yf.download(ticker,start=START,auto_adjust=False,actions=False,progress=False,threads=False)
    if isinstance(d.columns,pd.MultiIndex): d.columns=d.columns.get_level_values(0)
    d=d.dropna(subset=["Close"]).copy(); d["AdjClose"]=d["Adj Close"].fillna(d["Close"])
    d.index=pd.to_datetime(d.index).normalize(); d=d[~d.index.duplicated(keep="last")]
    return d["AdjClose"].rename(ticker)

def fred_vix():
    url="https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS&cosd=2013-01-01"
    for attempt in range(3):
        p=subprocess.run(["curl.exe" if sys.platform=="win32" else "curl","-sS","--max-time","30",url],capture_output=True,text=True,timeout=40)
        if p.returncode==0 and "observation_date" in p.stdout[:200]:
            df=pd.read_csv(io.StringIO(p.stdout)); df.columns=["date","vixcls"]
            df["date"]=pd.to_datetime(df.date,errors="coerce"); df["vixcls"]=pd.to_numeric(df.vixcls,errors="coerce")
            return df.dropna().drop_duplicates("date").set_index("date")["vixcls"].sort_index()
        time.sleep(1+attempt)
    raise RuntimeError("FRED VIXCLS unavailable")

def main():
    spy=fetch("SPY"); iwm=fetch("IWM"); hyg=fetch("HYG"); lqd=fetch("LQD")
    vix_yf=fetch("^VIX")
    try:
        vix_fred=fred_vix()
    except Exception as exc:
        print("FRED comparison unavailable", exc)
        vix_fred=pd.Series(dtype=float)
    px=pd.concat([spy,iwm,hyg,lqd],axis=1).sort_index().ffill(limit=2)
    df=pd.DataFrame(index=px.index)
    df["spy_close"]=px.SPY
    df["spy_ret_1d"]=px.SPY.pct_change()
    df["spy_ret_20d"]=px.SPY.pct_change(20)
    df["spy_vol_20d"]=df.spy_ret_1d.rolling(20,min_periods=20).std()*math.sqrt(252)
    df["iwm_ret_1d"]=px.IWM.pct_change()
    df["iwm_vol_20d"]=df.iwm_ret_1d.rolling(20,min_periods=20).std()*math.sqrt(252)
    df["dd252"]=px.SPY/px.SPY.rolling(252,min_periods=60).max()-1
    df["credit_proxy"]=px.HYG.pct_change(20)-px.LQD.pct_change(20)
    df["hist20"]=df.spy_vol_20d; df["hist5"]=df.spy_ret_1d.rolling(5,min_periods=5).std()*math.sqrt(252)
    df["ewma"]=np.sqrt(df.spy_ret_1d.pow(2).ewm(alpha=.06,adjust=False).mean()*252)
    # Primary daily inference source: yfinance ^VIX close.  FRED VIXCLS is
    # retained for training/backtests only; never forward-fill VIX.
    df["vixcls"]=vix_yf.reindex(df.index)
    feats=["vixcls","spy_vol_20d","iwm_vol_20d","dd252","credit_proxy","spy_ret_20d"]
    df=df.dropna(subset=feats)
    last=last_completed_session()
    df=df[df.index.date <= last]
    latest=df.index.max().normalize()
    # Overlap comparison; FRED is reference only and cannot block inference.
    comp={"n":0,"mae":None,"max_abs":None,"corr":None,"latest_diff":None,"qc":"NO_FRED_REFERENCE"}
    if len(vix_fred):
        both=pd.concat([vix_yf.rename("yf"),vix_fred.rename("fred")],axis=1,join="inner").dropna()
        if len(both):
            diff=both.yf-both.fred
            comp={"n":int(len(both)),"mae":float(diff.abs().mean()),"max_abs":float(diff.abs().max()),
                  "corr":float(both.yf.corr(both.fred)),"latest_diff":float(diff.iloc[-1]),
                  "latest_date":str(both.index[-1].date()),"qc":"PASS" if diff.iloc[-1:].abs().max()<=0.5 else "MATERIAL_DISCREPANCY"}
    src={"spy":str(spy.index.max().date()),"iwm":str(iwm.index.max().date()),
         "hyg":str(hyg.index.max().date()),"lqd":str(lqd.index.max().date()),
         "vix_primary":f"yfinance:^VIX {vix_yf.index.max().date()}",
         "vix_training":"FRED:VIXCLS", "comparison":comp}
    with local_engine().begin() as c:
        c.execute(text(f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.vix_source_qc (
          as_of date NOT NULL, yf_latest date, fred_latest date, n_overlap integer, mae double precision,
          max_abs double precision, corr double precision, latest_diff double precision,
          qc_status text, details jsonb, created_at timestamptz DEFAULT clock_timestamp())"""))
        c.execute(text(f"INSERT INTO {SCHEMA}.vix_source_qc(as_of,yf_latest,fred_latest,n_overlap,mae,max_abs,corr,latest_diff,qc_status,details) VALUES (:a,:y,:f,:n,:mae,:mx,:c,:ld,:q,cast(:j as jsonb))"),
                  {"a":latest.date(),"y":vix_yf.index.max().date(),"f":vix_fred.index.max().date() if len(vix_fred) else None,
                   "n":comp["n"],"mae":comp["mae"],"mx":comp["max_abs"],"c":comp["corr"],"ld":comp["latest_diff"],
                   "q":comp["qc"],"j":json.dumps(comp)})
        c.execute(text(f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.inference_daily (
          date date PRIMARY KEY,vixcls double precision,spy_vol_20d double precision,iwm_vol_20d double precision,
          dd252 double precision,credit_proxy double precision,spy_ret_20d double precision,
          spy_close double precision,hist20 double precision,hist5 double precision,ewma double precision,
          source_json jsonb,refreshed_at timestamptz DEFAULT clock_timestamp())"""))
        c.execute(text(f"ALTER TABLE {SCHEMA}.inference_daily ADD COLUMN IF NOT EXISTS spy_close double precision"))
        row=df.loc[latest]
        c.execute(text(f"DELETE FROM {SCHEMA}.inference_daily WHERE date > :d"),{"d":latest.date()})
        c.execute(text(f"""INSERT INTO {SCHEMA}.inference_daily(date,vixcls,spy_vol_20d,iwm_vol_20d,dd252,credit_proxy,spy_ret_20d,spy_close,hist20,hist5,ewma,source_json)
          VALUES (:d,:v,:s,:i,:dd,:c,:m,:pc,:h20,:h5,:e,cast(:j as jsonb))
          ON CONFLICT (date) DO UPDATE SET vixcls=EXCLUDED.vixcls,spy_vol_20d=EXCLUDED.spy_vol_20d,
          iwm_vol_20d=EXCLUDED.iwm_vol_20d,dd252=EXCLUDED.dd252,credit_proxy=EXCLUDED.credit_proxy,
          spy_ret_20d=EXCLUDED.spy_ret_20d,spy_close=EXCLUDED.spy_close,hist20=EXCLUDED.hist20,hist5=EXCLUDED.hist5,ewma=EXCLUDED.ewma,
          source_json=EXCLUDED.source_json,refreshed_at=clock_timestamp()"""),
          {"d":latest.date(),"v":float(row.vixcls),"s":float(row.spy_vol_20d),"i":float(row.iwm_vol_20d),
           "dd":float(row.dd252),"c":float(row.credit_proxy),"m":float(row.spy_ret_20d),"pc":float(row.spy_close),"h20":float(row.hist20),
           "h5":float(row.hist5),"e":float(row.ewma),"j":json.dumps(src)})
    print(json.dumps({"latest_market_date":str(latest.date()),"source_latest":src,"vix_comparison":comp,
                      "rows":len(df),"features":feats,"refreshed":True},indent=2))
    return 0
if __name__=="__main__": sys.exit(main())
