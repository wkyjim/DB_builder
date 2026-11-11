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
from db_builder.config import local_engine  # noqa: E402

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
    spy=fetch("SPY"); iwm=fetch("IWM"); hyg=fetch("HYG"); lqd=fetch("LQD"); vix=fred_vix()
    px=pd.concat([spy,iwm,hyg,lqd],axis=1).sort_index().ffill(limit=2)
    df=pd.DataFrame(index=px.index)
    df["spy_ret_1d"]=px.SPY.pct_change()
    df["spy_ret_20d"]=px.SPY.pct_change(20)
    df["spy_vol_20d"]=df.spy_ret_1d.rolling(20,min_periods=20).std()*math.sqrt(252)
    df["iwm_ret_1d"]=px.IWM.pct_change()
    df["iwm_vol_20d"]=df.iwm_ret_1d.rolling(20,min_periods=20).std()*math.sqrt(252)
    df["dd252"]=px.SPY/px.SPY.rolling(252,min_periods=60).max()-1
    df["credit_proxy"]=px.HYG.pct_change(20)-px.LQD.pct_change(20)
    df["hist20"]=df.spy_vol_20d; df["hist5"]=df.spy_ret_1d.rolling(5,min_periods=5).std()*math.sqrt(252)
    df["ewma"]=np.sqrt(df.spy_ret_1d.pow(2).ewm(alpha=.06,adjust=False).mean()*252)
    # Do not forward-fill VIXCLS across a missing session: the frozen feature
    # requires a same-date FRED observation.
    df["vixcls"]=vix.reindex(df.index)
    feats=["vixcls","spy_vol_20d","iwm_vol_20d","dd252","credit_proxy","spy_ret_20d"]
    df=df.dropna(subset=feats)
    latest=df.index.max().normalize()
    src={"spy":str(spy.index.max().date()),"iwm":str(iwm.index.max().date()),
         "hyg":str(hyg.index.max().date()),"lqd":str(lqd.index.max().date()),"vixcls":str(vix.index.max().date())}
    with local_engine().begin() as c:
        c.execute(text(f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.inference_daily (
          date date PRIMARY KEY,vixcls double precision,spy_vol_20d double precision,iwm_vol_20d double precision,
          dd252 double precision,credit_proxy double precision,spy_ret_20d double precision,
          hist20 double precision,hist5 double precision,ewma double precision,
          source_json jsonb,refreshed_at timestamptz DEFAULT clock_timestamp())"""))
        row=df.loc[latest]
        c.execute(text(f"DELETE FROM {SCHEMA}.inference_daily WHERE date > :d"),{"d":latest.date()})
        c.execute(text(f"""INSERT INTO {SCHEMA}.inference_daily(date,vixcls,spy_vol_20d,iwm_vol_20d,dd252,credit_proxy,spy_ret_20d,hist20,hist5,ewma,source_json)
          VALUES (:d,:v,:s,:i,:dd,:c,:m,:h20,:h5,:e,cast(:j as jsonb))
          ON CONFLICT (date) DO UPDATE SET vixcls=EXCLUDED.vixcls,spy_vol_20d=EXCLUDED.spy_vol_20d,
          iwm_vol_20d=EXCLUDED.iwm_vol_20d,dd252=EXCLUDED.dd252,credit_proxy=EXCLUDED.credit_proxy,
          spy_ret_20d=EXCLUDED.spy_ret_20d,hist20=EXCLUDED.hist20,hist5=EXCLUDED.hist5,ewma=EXCLUDED.ewma,
          source_json=EXCLUDED.source_json,refreshed_at=clock_timestamp()"""),
          {"d":latest.date(),"v":float(row.vixcls),"s":float(row.spy_vol_20d),"i":float(row.iwm_vol_20d),
           "dd":float(row.dd252),"c":float(row.credit_proxy),"m":float(row.spy_ret_20d),"h20":float(row.hist20),
           "h5":float(row.hist5),"e":float(row.ewma),"j":json.dumps(src)})
    print(json.dumps({"latest_market_date":str(latest.date()),"source_latest":src,"rows":len(df),
                      "features":feats,"refreshed":True},indent=2))
    return 0
if __name__=="__main__": sys.exit(main())
