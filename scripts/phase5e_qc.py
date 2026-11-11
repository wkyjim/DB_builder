"""Phase 5E: final QC of conformal tails and onset/persistence labels."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts"))
from db_builder.config import local_engine  # noqa: E402
from phase5c_tail import add_data, FEATURES, qreg_fit, logit_fit, logit_pred  # noqa: E402

YEARS=range(2018,2027); H=5

def prep(d):
    d=add_data(d)
    d["hist5"]=d.ret.rolling(5,min_periods=5).std()*math.sqrt(252)
    d["vix_chg5"]=d.vixcls.diff(5)
    d["credit_chg20"]=d.credit_proxy.diff(20)
    return d

def brier(y,q): return float(np.mean((np.asarray(q)-np.asarray(y))**2))
def logloss(y,q):
    q=np.clip(q,1e-6,1-1e-6); return float(-np.mean(np.asarray(y)*np.log(q)+(1-np.asarray(y))*np.log(1-q)))

def main():
    d=pd.read_sql(text("select * from public.research_core_hist order by date"),local_engine())
    d["date"]=pd.to_datetime(d.date).dt.normalize(); d=prep(d)
    qrows=[]; qpred=[]
    for year in YEARS:
        fit=d[d.date<f"{year-2}-01-01"].iloc[:-H]
        cal=d[(d.date>=f"{year-2}-01-01")&(d.date<f"{year}-01-01")].iloc[:-H]
        test=d[(d.date>=f"{year}-01-01")&(d.date<f"{year+1}-01-01")]
        for tau in (.90,.95):
            qf=fit[["date","vol5"]+FEATURES].dropna(); qc=cal[["date","vol5"]+FEATURES].dropna(); qt=test[["date","vol5"]+FEATURES].dropna()
            if len(qf)<60 or len(qc)<12 or len(qt)<10: continue
            mu=qf[FEATURES].mean(); sd=qf[FEATURES].std().replace(0,np.nan)
            X=np.column_stack([np.ones(len(qf)),((qf[FEATURES]-mu)/sd).to_numpy(float)])
            Xc=np.column_stack([np.ones(len(qc)),((qc[FEATURES]-mu)/sd).to_numpy(float)])
            Xt=np.column_stack([np.ones(len(qt)),((qt[FEATURES]-mu)/sd).to_numpy(float)])
            b=qreg_fit(X,qf.vol5.to_numpy(float),tau)
            qcraw=Xc@b; qtraw=Xt@b
            adj=float(np.quantile(qc.vol5.to_numpy(float)-qcraw,tau))
            qtadj=qtraw+adj
            qrows.append({"year":year,"tau":tau,"n":len(qt),"raw_cov":float((qt.vol5>qtraw).mean()),
                          "adj_cov":float((qt.vol5>qtadj).mean()),"adj":adj})
            for dt,y,qr,qa in zip(qt.date,qt.vol5,qtraw,qtadj):
                qpred.append({"date":dt,"year":year,"tau":tau,"actual":y,"raw":qr,"adj":qa})
    qdf=pd.DataFrame(qrows); qp=pd.DataFrame(qpred)
    print("CONFORMAL"); print(qdf.to_string(index=False))
    runs=[]
    for tau,g in qp.groupby("tau"):
        g=g.sort_values("date"); ex=(g.actual>g.adj).astype(int).to_numpy()
        # cluster exceedance runs with gap <= H
        idx=np.where(ex==1)[0]; clusters=[]; last=-999
        for i in idx:
            if i-last>H: clusters.append([i,i])
            else: clusters[-1][1]=i
            last=i
        lens=[b-a+1 for a,b in clusters]
        runs.append({"tau":tau,"coverage":float(ex.mean()),"runs":len(clusters),"avg_len":float(np.mean(lens)) if lens else 0,"max_len":max(lens) if lens else 0})
    print("RUNS"); print(pd.DataFrame(runs).to_string(index=False))
    # onset/persistence consistent 20% both sides
    onset=[]; persist=[]; alerts=[]; episode_rows=[]
    for year in YEARS:
        fit=d[d.date<f"{year-2}-01-01"].iloc[:-H]
        test=d[(d.date>=f"{year}-01-01")&(d.date<f"{year+1}-01-01")]
        for task,cur in (("onset",fit.hist5<.20),("persist",fit.hist5>=.20)):
            t=test[(test.hist5<.20) if task=="onset" else (test.hist5>=.20)]
            f=fit[cur]
            if len(f)<60 or len(t)<10: continue
            qf=f[["date","vol5"]+FEATURES].dropna(); qt=t[["date","vol5"]+FEATURES].dropna()
            mu=qf[FEATURES].mean(); sd=qf[FEATURES].std().replace(0,np.nan)
            X=np.column_stack([np.ones(len(qf)),((qf[FEATURES]-mu)/sd).to_numpy(float)])
            Xt=np.column_stack([np.ones(len(qt)),((qt[FEATURES]-mu)/sd).to_numpy(float)])
            yf=(qf.vol5>=.20).astype(int).to_numpy(); yt=(qt.vol5>=.20).astype(int).to_numpy()
            if len(np.unique(yf))<2: continue
            w=logit_fit(X,yf); prob=logit_pred(w,Xt)
            pred=(prob>=.3).astype(int); tp=int(((pred==1)&(yt==1)).sum()); fp=int(((pred==1)&(yt==0)).sum()); fn=int(((pred==0)&(yt==1)).sum())
            rec={"year":year,"task":task,"n":len(yt),"event_rate":float(yt.mean()),"brier":brier(yt,prob),"logloss":logloss(yt,prob),
                 "precision":tp/(tp+fp) if tp+fp else np.nan,"recall":tp/(tp+fn) if tp+fn else np.nan,"false_pos":fp,"false_neg":fn}
            (onset if task=="onset" else persist).append(rec)
            # episode lead-time for onset
            if task=="onset":
                ev=(qt.vol5>=.20).astype(int).to_numpy(); idx=np.where(ev==1)[0]
                starts=[]; last=-999
                for i in idx:
                    if i-last>H: starts.append(i)
                    last=i
                alert_idx=set(np.where(pred==1)[0].tolist()); detected=0; leads=[]; false_alerts=0
                for s in starts:
                    window=set(range(max(0,s-H),s+1))
                    hits=sorted(window & alert_idx)
                    if hits:
                        detected+=1; leads.append(s-hits[0])
                    else: leads.append(np.nan)
                for a in alert_idx:
                    if not any(max(0,s-H)<=a<=s for s in starts): false_alerts+=1
                episode_rows.append({"year":year,"episodes":len(starts),"detected":detected,"missed":len(starts)-detected,
                                     "mean_lead":float(np.nanmean(leads)) if leads else np.nan,"alerts":int(pred.sum()),"false_alerts":false_alerts})
    print("\nONSET CONSISTENT 20/20"); print(pd.DataFrame(onset).to_string(index=False))
    print("\nPERSIST CONSISTENT 20/20"); print(pd.DataFrame(persist).to_string(index=False))
    print("\nEPISODES"); print(pd.DataFrame(episode_rows).to_string(index=False))
    qdf.to_csv(ROOT/"data"/"phase5e_qc_quantiles.csv",index=False)
    qp.to_csv(ROOT/"data"/"phase5e_qc_quant_predictions.csv",index=False)
    pd.DataFrame(onset).to_csv(ROOT/"data"/"phase5e_qc_onset.csv",index=False)
    pd.DataFrame(persist).to_csv(ROOT/"data"/"phase5e_qc_persist.csv",index=False)
    pd.DataFrame(episode_rows).to_csv(ROOT/"data"/"phase5e_qc_episodes.csv",index=False)
    return 0

if __name__=="__main__": sys.exit(main())
