"""Phase 5C: tail-aware 5D volatility research."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from db_builder.config import local_engine  # noqa: E402

FEATURES = ["vixcls", "spy_vol_20d", "iwm_vol_20d", "dd252", "credit_proxy", "spy_ret_20d"]
YEARS = range(2018, 2027)
H = 5


def add_data(d):
    d = d.copy().sort_values("date").reset_index(drop=True)
    d["ret"] = d.spy_close.pct_change()
    d["credit_proxy"] = d.hyg_ret_20d - d.lqd_ret_20d
    d["dd252"] = d.spy_close / d.spy_close.rolling(252, min_periods=60).max() - 1
    d["hist20"] = d.ret.rolling(20, min_periods=20).std() * math.sqrt(252)
    d["ewma"] = np.sqrt(d.ret.pow(2).ewm(alpha=0.06, adjust=False).mean() * 252)
    d["vol5"] = np.nan
    for i in range(len(d)-H):
        d.loc[i, "vol5"] = np.std(d.ret.iloc[i+1:i+H+1].to_numpy(float), ddof=1) * math.sqrt(252)
    return d


def ols(train, test):
    cols = ["date"] + FEATURES + ["vol5"]
    tr=train[cols].dropna(); te=test[cols].dropna()
    if len(tr)<80: return None
    mu=tr[FEATURES].mean(); sd=tr[FEATURES].std().replace(0,np.nan)
    X=((tr[FEATURES]-mu)/sd).to_numpy(float); Xt=((te[FEATURES]-mu)/sd).to_numpy(float)
    X=np.column_stack([np.ones(len(X)),X]); Xt=np.column_stack([np.ones(len(Xt)),Xt])
    b=np.linalg.lstsq(X,tr.vol5.to_numpy(float),rcond=None)[0]
    return te.assign(pred=Xt@b)


def garch_fit(r):
    r=np.asarray(r,float); v0=float(np.var(r,ddof=1))
    def nll(th):
        omega,alpha,beta=th
        if omega<=0 or alpha<0 or beta<0 or alpha+beta>=0.999: return 1e10
        s2=np.empty(len(r)); s2[0]=v0
        for i in range(1,len(r)):
            s2[i]=omega+alpha*r[i-1]**2+beta*s2[i-1]
        if np.any(s2<=0): return 1e10
        return 0.5*np.sum(np.log(s2)+r*r/s2)
    res=minimize(nll,[v0*0.05,0.08,0.90],method="L-BFGS-B",
                 bounds=[(1e-10,None),(0,0.5),(0,0.999)])
    if not res.success: return np.array([v0*0.05,0.08,0.90])
    return res.x


def garch_forecast(r, params, h):
    omega,alpha,beta=params; v0=float(np.var(r,ddof=1)); n=len(r)
    s2=np.empty(n); s2[0]=v0
    for i in range(1,n): s2[i]=omega+alpha*r[i-1]**2+beta*s2[i-1]
    # forecast at each t from s2[t], using returns through t
    out=np.full(n,np.nan); inf=omega/(1-alpha-beta) if alpha+beta<1 else np.nan
    for t in range(n-h):
        f=s2[t+1] if t+1<n else s2[t]
        vals=[]
        for k in range(h):
            vals.append(f)
            f=inf+(alpha+beta)*(f-inf)
        out[t]=math.sqrt(252*np.mean(vals))
    return out


def collect(d):
    parts=[]; gparts=[]; coefs=[]
    for year in YEARS:
        train=d[d.date < f"{year}-01-01"].iloc[:-H]
        test=d[(d.date>=f"{year}-01-01")&(d.date<f"{year+1}-01-01")]
        p=ols(train,test)
        if p is not None:
            p["year"]=year; parts.append(p)
        # GARCH fit on train returns; forecast for all then select test
        try:
            params=garch_fit(train.ret.dropna().to_numpy(float))
            gv=garch_forecast(d.ret.fillna(0).to_numpy(float),params,H)
            g=test[["date","vol5","hist20","ewma"]].copy(); g["garch"]=gv[test.index]
            gparts.append(g); coefs.append((year,params))
        except Exception as e:
            print("garch fail",year,e)
    p=pd.concat(parts).sort_values("date")
    p=p.merge(d[["date","hist20","ewma"]],on="date",how="left")
    g=pd.concat(gparts).sort_values("date").merge(p[["date","pred"]],on="date",how="left")
    return p,g,pd.DataFrame(coefs,columns=["year","params"])


def qreg_fit(X,y,tau,iters=30):
    X=np.asarray(X,float); y=np.asarray(y,float)
    b=np.linalg.lstsq(X,y,rcond=None)[0]
    for _ in range(iters):
        r=y-X@b
        w=np.where(r>=0,tau,1-tau)/np.maximum(np.abs(r),1e-4)
        sw=np.sqrt(w)
        b=np.linalg.lstsq(X*sw[:,None],y*sw,rcond=None)[0]
    return b


def quantiles(d):
    rows=[]
    for tau in (0.80,0.90,0.95):
        parts=[]
        for year in YEARS:
            train=d[d.date<f"{year}-01-01"].iloc[:-H]
            test=d[(d.date>=f"{year}-01-01")&(d.date<f"{year+1}-01-01")]
            tr=train[["date"]+FEATURES+["vol5","ewma"]].dropna(); te=test[["date"]+FEATURES+["vol5","ewma"]].dropna()
            if len(tr)<80 or len(te)<10: continue
            mu=tr[FEATURES].mean(); sd=tr[FEATURES].std().replace(0,np.nan)
            X=((tr[FEATURES]-mu)/sd).to_numpy(float); Xt=((te[FEATURES]-mu)/sd).to_numpy(float)
            X=np.column_stack([np.ones(len(X)),X]); Xt=np.column_stack([np.ones(len(Xt)),Xt])
            b=qreg_fit(X,tr.vol5.to_numpy(float),tau)
            parts.append(te.assign(q=Xt@b))
        if parts:
            p=pd.concat(parts).sort_values("date")
            rows.append({"tau":tau,"n":len(p),"coverage":float((p.vol5>p.q).mean()),
                         "mean_q":float(p.q.mean()),"mean_actual":float(p.vol5.mean())})
    return pd.DataFrame(rows)


def sigmoid(z): return 1/(1+np.exp(-np.clip(z,-30,30)))
def logit_fit(X,y,lr=.1,epochs=800,l2=1e-3):
    X=np.column_stack([np.ones(len(X)),X]); w=np.zeros(X.shape[1])
    for _ in range(epochs):
        p=sigmoid(X@w); w -= lr*(X.T@(p-y)/len(y)+l2*w)
    return w
def logit_pred(w,X): return sigmoid(np.column_stack([np.ones(len(X)),X])@w)


def spike_probs(d):
    rows=[]; allp=[]
    for year in YEARS:
        train=d[d.date<f"{year}-01-01"].iloc[:-H]
        test=d[(d.date>=f"{year}-01-01")&(d.date<f"{year+1}-01-01")]
        if len(train)<80 or len(test)<20: continue
        thr=float(train.vol5.quantile(.80))
        tr=train[["date"]+FEATURES+["vol5","ewma"]].dropna(); te=test[["date"]+FEATURES+["vol5","ewma"]].dropna()
        if tr.vol5.nunique()<2: continue
        mu=tr[FEATURES].mean(); sd=tr[FEATURES].std().replace(0,np.nan)
        X=((tr[FEATURES]-mu)/sd).to_numpy(float); Xt=((te[FEATURES]-mu)/sd).to_numpy(float)
        y=(tr.vol5>=thr).astype(int).to_numpy()
        w=logit_fit(X,y); prob=logit_pred(w,Xt)
        # EWMA-only logistic baseline
        mu_e=tr.ewma.mean(); sd_e=tr.ewma.std()
        Xe=((tr.ewma-mu_e)/sd_e).to_numpy(float)[:,None]; Xte=((te.ewma-mu_e)/sd_e).to_numpy(float)[:,None]
        if tr.ewma.nunique()<2: continue
        we=logit_fit(Xe,y); probe=logit_pred(we,Xte)
        allp.append(pd.DataFrame({"date":te.date.values,"year":year,"threshold":thr,
                                  "prob":prob,"prob_ewma":probe,"actual":(te.vol5>=thr).astype(int).values,
                                  "vol5":te.vol5.values}))
    p=pd.concat(allp)
    def brier(y,q): return float(np.mean((q-y)**2))
    def ll(y,q):
        q=np.clip(q,1e-6,1-1e-6); return float(-np.mean(y*np.log(q)+(1-y)*np.log(1-q)))
    base=float(p.actual.mean())
    out={"n":len(p),"event_rate":base,"base_brier":brier(p.actual,np.full(len(p),base)),
         "base_logloss":ll(p.actual,np.full(len(p),base)),
         "reg_brier":brier(p.actual,p.prob),"reg_logloss":ll(p.actual,p.prob),
         "ewma_brier":brier(p.actual,p.prob_ewma),"ewma_logloss":ll(p.actual,p.prob_ewma)}
    # reliability bins
    bins=[]
    for lo,hi in ((0,.2),(.2,.4),(.4,.6),(.6,.8),(.8,1.01)):
        q=p[(p.prob>=lo)&(p.prob<hi)]
        bins.append({"bin":f"{lo:.1f}-{hi:.1f}","n":len(q),"mean_prob":float(q.prob.mean()) if len(q) else np.nan,
                     "actual":float(q.actual.mean()) if len(q) else np.nan})
    # threshold tradeoff
    trade=[]
    for th in (.2,.3,.4,.5):
        pred=(p.prob>=th).astype(int); tp=int(((pred==1)&(p.actual==1)).sum()); fp=int(((pred==1)&(p.actual==0)).sum()); fn=int(((pred==0)&(p.actual==1)).sum())
        trade.append({"threshold":th,"precision":tp/(tp+fp) if tp+fp else np.nan,"recall":tp/(tp+fn) if tp+fn else np.nan,
                      "false_negative":fn/(tp+fn) if tp+fn else np.nan,"alerts":int(pred.sum())})
    return p,out,pd.DataFrame(bins),pd.DataFrame(trade)


def boot_pair(y,pred,base,h=H,reps=1000):
    y=np.asarray(y,float); pred=np.asarray(pred,float); base=np.asarray(base,float); n=len(y)
    rng=np.random.default_rng(1); nb=int(math.ceil(n/h)); da=[]; dr=[]
    for _ in range(reps):
        idx=np.concatenate([np.arange(s,min(s+h,n)) for s in rng.integers(0,max(1,n-h+1),nb)])[:n]
        yy=y[idx]; pp=pred[idx]; bb=base[idx]
        da.append(np.mean(abs(yy-pp))-np.mean(abs(yy-bb)))
        dr.append(np.sqrt(np.mean((yy-pp)**2))-np.sqrt(np.mean((yy-bb)**2)))
    return np.percentile(da,[2.5,97.5]),np.percentile(dr,[2.5,97.5])


def main():
    d=pd.read_sql(text("select * from public.research_core_hist order by date"),local_engine())
    d["date"]=pd.to_datetime(d.date).dt.normalize(); d=add_data(d)
    p,g,coefs=collect(d)
    p=p.merge(g[["date","garch"]],on="date",how="left")
    p=p.dropna(subset=["vol5","pred","hist20","ewma"])
    y=p.vol5.to_numpy(float)
    # verify Phase5B residual sign and Brier
    print("VERIFY")
    print("bias pred-actual",float((p.pred-y).mean()),"mean actual",float(y.mean()),"mean pred",float(p.pred.mean()))
    print("resid skew",float(pd.Series(p.pred-y).skew()),"kurt",float(pd.Series(p.pred-y).kurt()))
    print("phase5b_brier_is_recomputed_in_spike_section")
    # point models
    for name,col in (("hist","hist20"),("ewma","ewma"),("reg","pred"),("garch","garch")):
        q=p.dropna(subset=[col])
        print(name,"n",len(q),"mae",float(np.mean(abs(q.vol5-q[col]))),"rmse",float(np.sqrt(np.mean((q.vol5-q[col])**2))),
              "r2",float(1-((q.vol5-q[col])**2).sum()/((q.vol5-q.vol5.mean())**2).sum()),
              "bias",float((q[col]-q.vol5).mean()))
    for base in ("hist20","ewma"):
        ci=boot_pair(y,p.pred,p[base]); print("paired",base,ci)
    # garch pair
    qg=p.dropna(subset=["garch"])
    print("garch pair vs ewma",boot_pair(qg.vol5,qg.garch,qg.ewma))
    q=quantiles(d); print("\nQUANTILES"); print(q.to_string(index=False))
    sp,out,bins,trade=spike_probs(d); print("\nSPIKE"); print(out); print("bins"); print(bins.to_string(index=False)); print("trade"); print(trade.to_string(index=False))
    # yearly spike metrics
    yr=[]
    for year,gp in sp.groupby("year"):
        for name,col in (("reg","prob"),("ewma","prob_ewma"),("base","base")):
            if name=="base":
                prob=np.full(len(gp),gp.actual.mean()); # not proper base; diagnostic
            else: prob=gp[col].to_numpy(float)
            pred=(prob>=.3).astype(int); tp=int(((pred==1)&(gp.actual==1)).sum()); fn=int(((pred==0)&(gp.actual==1)).sum()); fp=int(((pred==1)&(gp.actual==0)).sum())
            yr.append({"year":year,"model":name,"brier":float(np.mean((prob-gp.actual)**2)),
                       "precision":tp/(tp+fp) if tp+fp else np.nan,"recall":tp/(tp+fn) if tp+fn else np.nan})
    print("\nYEARLY SPIKE"); print(pd.DataFrame(yr).to_string(index=False))
    q.to_csv(ROOT/"data"/"phase5c_quantiles.csv",index=False)
    pd.DataFrame([out]).to_csv(ROOT/"data"/"phase5c_spike.csv",index=False)
    bins.to_csv(ROOT/"data"/"phase5c_calibration.csv",index=False)
    trade.to_csv(ROOT/"data"/"phase5c_tradeoff.csv",index=False)
    pd.DataFrame(yr).to_csv(ROOT/"data"/"phase5c_spike_yearly.csv",index=False)
    return 0

if __name__=="__main__": sys.exit(main())
