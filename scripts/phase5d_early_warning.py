"""Phase 5D: onset/persistence, probability calibration, tail early warning."""
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

YEARS = range(2018, 2027)
H = 5
EARLY = ["vix_chg5", "vol_ratio", "vol_div", "downside_mom", "credit_chg20"]


def prep(d):
    d = add_data(d)
    d["hist5"] = d.ret.rolling(5, min_periods=5).std() * math.sqrt(252)
    d["spy_vol_60d"] = d.ret.rolling(60, min_periods=60).std() * math.sqrt(252)
    d["vix_chg5"] = d.vixcls.diff(5)
    d["vol_ratio"] = d.spy_vol_20d / d.spy_vol_60d
    d["vol_div"] = d.iwm_vol_20d - d.spy_vol_20d
    d["downside_mom"] = -d.spy_ret_5d
    d["credit_chg20"] = d.credit_proxy.diff(20)
    return d


def logloss(y,q):
    q=np.clip(q,1e-6,1-1e-6); return float(-np.mean(y*np.log(q)+(1-y)*np.log(1-q)))
def brier(y,q): return float(np.mean((np.asarray(q)-np.asarray(y))**2))


def isotonic_fit(x,y):
    order=np.argsort(x); x=x[order]; y=y[order]; w=np.ones(len(y)); blocks=[[i] for i in range(len(y))]; vals=list(y.astype(float)); weights=list(w)
    i=0
    while i < len(vals)-1:
        if vals[i] > vals[i+1]:
            nw=weights[i]+weights[i+1]; vals[i]=(vals[i]*weights[i]+vals[i+1]*weights[i+1])/nw
            weights[i]=nw; blocks[i]+=blocks[i+1]
            del vals[i+1]; del weights[i+1]; del blocks[i+1]
            if i>0: i-=1
        else: i+=1
    xs=[]; ys=[]
    for b,v in zip(blocks,vals):
        xs.append(float(np.mean(x[b]))); ys.append(float(v))
    return np.array(xs),np.array(ys)

def isotonic_pred(x, xs, ys):
    return np.interp(x, xs, ys, left=ys[0], right=ys[-1])


def bootstrap_diff(y,a,b,reps=1000):
    y=np.asarray(y,float);a=np.asarray(a,float);b=np.asarray(b,float);n=len(y); rng=np.random.default_rng(3); nb=int(math.ceil(n/H)); d=[]
    for _ in range(reps):
        idx=np.concatenate([np.arange(s,min(s+H,n)) for s in rng.integers(0,max(1,n-H+1),nb)])[:n]
        d.append(brier(y[idx],a[idx])-brier(y[idx],b[idx]))
    return np.percentile(d,[2.5,97.5])


def main():
    d=pd.read_sql(text("select * from public.research_core_hist order by date"),local_engine())
    d["date"]=pd.to_datetime(d.date).dt.normalize(); d=prep(d)
    # event tables / fixed thresholds
    print("THRESHOLD EVENT RATES")
    for thr in (.20,.25,.30):
        ev=(d.vol5>=thr)
        print("fixed",thr,"n",int(ev.sum()),"rate",float(ev.mean()))
    rows=[]; onset_rows=[]; prob_rows=[]; yearly=[]; calib_rows=[]; early_rows=[]; quant=[]; prob_records=[]; quant_records=[]
    for year in YEARS:
        train=d[d.date<f"{year}-01-01"].iloc[:-H]
        calib=d[(d.date>=f"{year-2}-01-01")&(d.date<f"{year}-01-01")].iloc[:-H]
        fit=d[d.date<f"{year-2}-01-01"].iloc[:-H]
        test=d[(d.date>=f"{year}-01-01")&(d.date<f"{year+1}-01-01")]
        if len(test)<20 or len(fit)<100 or len(calib)<20: continue
        thr=float(train.vol5.quantile(.80))
        # labels all, onset, persistence (current 5D vol 20% threshold)
        for task,mask_fit,mask_test in (("all",pd.Series(True,index=fit.index),pd.Series(True,index=test.index)),
                                         ("onset",fit.hist5<.20,test.hist5<.20),
                                         ("persist",fit.hist5>=.20,test.hist5>=.20)):
            f=fit[mask_fit]; c=calib[(calib.hist5<.20) if task=="onset" else (calib.hist5>=.20 if task=="persist" else pd.Series(True,index=calib.index))]
            t=test[mask_test]
            if len(f)<60 or len(c)<12 or len(t)<10 or f.vol5.nunique()<2: continue
            def xy(frame, cols):
                q=frame[["date","vol5","ewma"]+cols].dropna(); return q
            qf=xy(f,FEATURES); qc=xy(c,FEATURES); qt=xy(t,FEATURES)
            if len(qf)<60 or len(qc)<12 or len(qt)<10: continue
            mu=qf[FEATURES].mean(); sd=qf[FEATURES].std().replace(0,np.nan)
            X=((qf[FEATURES]-mu)/sd).to_numpy(float); Xc=((qc[FEATURES]-mu)/sd).to_numpy(float); Xt=((qt[FEATURES]-mu)/sd).to_numpy(float)
            yf=(qf.vol5>=thr).astype(int).to_numpy(); yc=(qc.vol5>=thr).astype(int).to_numpy(); yt=(qt.vol5>=thr).astype(int).to_numpy()
            w=logit_fit(X,yf); raw_c=logit_pred(w,Xc); raw_t=logit_pred(w,Xt)
            # Platt on calibration logits
            eps=1e-5; lc=np.log(np.clip(raw_c,eps,1-eps)/(1-np.clip(raw_c,eps,1-eps)))[:,None]; lt=np.log(np.clip(raw_t,eps,1-eps)/(1-np.clip(raw_t,eps,1-eps)))[:,None]
            wp=logit_fit(lc,yc); platt_t=logit_pred(wp,lt)
            # isotonic
            xs,ys=isotonic_fit(raw_c,yc); iso_t=isotonic_pred(raw_t,xs,ys)
            # EWMA-only
            mue=qf.ewma.mean(); sde=qf.ewma.std()
            Xe=((qf.ewma-mue)/sde).to_numpy(float)[:,None]; Xce=((qc.ewma-mue)/sde).to_numpy(float)[:,None]; Xte=((qt.ewma-mue)/sde).to_numpy(float)[:,None]
            we=logit_fit(Xe,yf); ewma_t=logit_pred(we,Xte)
            base=float(yf.mean())
            for name,prob in (("logit",raw_t),("platt",platt_t),("isotonic",iso_t),("ewma",ewma_t),("base",np.full(len(yt),base))):
                for _dt,_pp,_yy in zip(qt.date,prob,yt):
                    prob_records.append({"date":_dt,"year":year,"task":task,"model":name,"prob":float(_pp),"actual":int(_yy)})
                prob_rows.append({"year":year,"task":task,"model":name,"n":len(yt),"event_rate":float(yt.mean()),
                                  "brier":brier(yt,prob),"logloss":logloss(yt,prob),
                                  "high_bias":float(prob[yt==1].mean()-1) if (yt==1).any() else np.nan,
                                  "mean_prob":float(np.mean(prob))})
                if name in ("platt","isotonic","logit"):
                    for lo,hi in ((0,.2),(.2,.4),(.4,.6),(.6,.8),(.8,1.01)):
                        m=(prob>=lo)&(prob<hi)
                        if m.sum()>0: calib_rows.append({"year":year,"task":task,"model":name,"bin":f"{lo}-{hi}","n":int(m.sum()),"mean_prob":float(prob[m].mean()),"actual":float(yt[m].mean())})
            # early warning model
            qfe=xy(f,FEATURES+EARLY); qce=xy(c,FEATURES+EARLY); qte=xy(t,FEATURES+EARLY)
            if len(qfe)>=60 and len(qce)>=12 and len(qte)>=10:
                mue_=qfe[FEATURES+EARLY].mean(); sde_=qfe[FEATURES+EARLY].std().replace(0,np.nan)
                Xe_=((qfe[FEATURES+EARLY]-mue_)/sde_).to_numpy(float); Xce_=((qce[FEATURES+EARLY]-mue_)/sde_).to_numpy(float); Xte_=((qte[FEATURES+EARLY]-mue_)/sde_).to_numpy(float)
                ye=(qfe.vol5>=thr).astype(int).to_numpy(); yce=(qce.vol5>=thr).astype(int).to_numpy(); yte=(qte.vol5>=thr).astype(int).to_numpy()
                if len(np.unique(ye))>1:
                    ww=logit_fit(Xe_,ye); rc=logit_pred(ww,Xce_); rt=logit_pred(ww,Xte_)
                    # platt early
                    lc2=np.log(np.clip(rc,eps,1-eps)/(1-np.clip(rc,eps,1-eps)))[:,None]; lt2=np.log(np.clip(rt,eps,1-eps)/(1-np.clip(rt,eps,1-eps)))[:,None]
                    wp2=logit_fit(lc2,yce); pt2=logit_pred(wp2,lt2)
                    early_rows.append({"year":year,"task":task,"model":"early_platt","n":len(yte),"event_rate":float(yte.mean()),"brier":brier(yte,pt2),"logloss":logloss(yte,pt2)})
            # onset episode counts (distinct clusters, gap > H)
            if task=="onset":
                ev=((t.hist5<.20)&(t.vol5>=thr)).astype(int).to_numpy(); idx=np.where(ev==1)[0]
                ep=0; last=-999
                for i in idx:
                    if i-last>H: ep+=1
                    last=i
                onset_rows.append({"year":year,"onset_days":len(idx),"episodes":ep,"rate":float(ev.mean())})
        # quantile upper tail with calibration split
        for tau in (.90,.95):
            qf=fit[["date","vol5"]+FEATURES].dropna(); qc=calib[["date","vol5"]+FEATURES].dropna(); qt=test[["date","vol5"]+FEATURES].dropna()
            if len(qf)<60 or len(qc)<12 or len(qt)<10: continue
            mu=qf[FEATURES].mean(); sd=qf[FEATURES].std().replace(0,np.nan)
            X=np.column_stack([np.ones(len(qf)),((qf[FEATURES]-mu)/sd).to_numpy(float)])
            Xc=np.column_stack([np.ones(len(qc)),((qc[FEATURES]-mu)/sd).to_numpy(float)])
            Xt=np.column_stack([np.ones(len(qt)),((qt[FEATURES]-mu)/sd).to_numpy(float)])
            b=qreg_fit(X,qf.vol5.to_numpy(float),tau)
            qcraw=Xc@b; qtraw=Xt@b
            adj=float(np.quantile(qc.vol5.to_numpy(float)-qcraw,1-tau))
            qtadj=qtraw+adj
            quant.append({"year":year,"tau":tau,"n":len(qt),"raw_coverage":float((qt.vol5>qtraw).mean()),
                          "adj_coverage":float((qt.vol5>qtadj).mean()),"mean_q_raw":float(qtraw.mean()),"mean_q_adj":float(qtadj.mean())})
            for _dt,_y,_qr,_qa in zip(qt.date,qt.vol5,qtraw,qtadj):
                quant_records.append({"date":_dt,"year":year,"tau":tau,"actual":_y,"raw":_qr,"adj":_qa})
    pr=pd.DataFrame(prob_rows); print("\nPROB AGG"); print(pr.groupby(["task","model"]).agg({"brier":"mean","logloss":"mean","high_bias":"mean","n":"sum"}).to_string())
    print("\nONSET"); print(pd.DataFrame(onset_rows).to_string(index=False))
    print("\nEARLY"); print(pd.DataFrame(early_rows).to_string(index=False))
    print("\nQUANT"); print(pd.DataFrame(quant).groupby("tau").agg({"raw_coverage":"mean","adj_coverage":"mean","n":"sum"}).to_string())
    print("\nYEARLY PROB PLATT"); print(pr[pr.model=="platt"].to_string(index=False))

    prd=pd.DataFrame(prob_records)
    tm=[]
    for task in ("all","onset","persist"):
        for model in ("logit","platt","isotonic","ewma","base"):
            q=prd[(prd.task==task)&(prd.model==model)]
            if not len(q): continue
            for th in (.3,.5):
                pred=(q.prob>=th).astype(int)
                tp=int(((pred==1)&(q.actual==1)).sum()); fp=int(((pred==1)&(q.actual==0)).sum()); fn=int(((pred==0)&(q.actual==1)).sum())
                tm.append({"task":task,"model":model,"threshold":th,"n":len(q),"precision":tp/(tp+fp) if tp+fp else np.nan,"recall":tp/(tp+fn) if tp+fn else np.nan,"false_positive":fp,"false_negative":fn})
    print("\nTASK METRICS"); print(pd.DataFrame(tm).to_string(index=False))
    pr.to_csv(ROOT/"data"/"phase5d_probs.csv",index=False)
    pd.DataFrame(calib_rows).to_csv(ROOT/"data"/"phase5d_calibration.csv",index=False)
    pd.DataFrame(onset_rows).to_csv(ROOT/"data"/"phase5d_onset.csv",index=False)
    pd.DataFrame(early_rows).to_csv(ROOT/"data"/"phase5d_early.csv",index=False)
    pd.DataFrame(quant).to_csv(ROOT/"data"/"phase5d_quantiles.csv",index=False)
    prd.to_csv(ROOT/"data"/"phase5d_prob_predictions.csv",index=False)
    pd.DataFrame(tm).to_csv(ROOT/"data"/"phase5d_task_metrics.csv",index=False)
    pd.DataFrame(quant_records).to_csv(ROOT/"data"/"phase5d_quant_predictions.csv",index=False)
    return 0

if __name__=="__main__": sys.exit(main())
