"""Phase 5B: volatility forecast robustness for 5D/20D models only."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from db_builder.config import local_engine  # noqa: E402

H = (5, 20)
YEARS = range(2018, 2027)
FEATURES = ["vixcls", "spy_vol_20d", "iwm_vol_20d", "dd252", "credit_proxy", "spy_ret_20d"]


def add_data(d):
    d = d.copy().sort_values("date").reset_index(drop=True)
    d["ret"] = d.spy_close.pct_change()
    d["credit_proxy"] = d.hyg_ret_20d - d.lqd_ret_20d
    d["dd252"] = d.spy_close / d.spy_close.rolling(252, min_periods=60).max() - 1
    d["hist20"] = d.ret.rolling(20, min_periods=20).std() * math.sqrt(252)
    d["ewma"] = np.sqrt(d.ret.pow(2).ewm(alpha=0.06, adjust=False).mean() * 252)
    for h in H:
        fwd = np.full(len(d), np.nan)
        for i in range(len(d)-h):
            fwd[i] = np.std(d.ret.iloc[i+1:i+h+1].to_numpy(float), ddof=1) * math.sqrt(252)
        d[f"vol{h}"] = fwd
    return d


def fit_predict(train, test, target):
    cols = ["date"] + FEATURES + [target]
    tr = train[cols].dropna(); te = test[cols].dropna()
    if len(tr) < 80 or len(te) < 10:
        return None, None
    mu = tr[FEATURES].mean(); sd = tr[FEATURES].std().replace(0, np.nan)
    X = ((tr[FEATURES]-mu)/sd).to_numpy(float); Xt = ((te[FEATURES]-mu)/sd).to_numpy(float)
    X = np.column_stack([np.ones(len(X)), X]); Xt = np.column_stack([np.ones(len(Xt)), Xt])
    y = tr[target].to_numpy(float)
    b = np.linalg.lstsq(X, y, rcond=None)[0]
    return te.assign(pred=Xt @ b), b


def collect(d):
    out = {}
    for h in H:
        target=f"vol{h}"; parts=[]; coefs=[]
        for year in YEARS:
            train=d[d.date < f"{year}-01-01"].iloc[:-h]
            test=d[(d.date>=f"{year}-01-01")&(d.date<f"{year+1}-01-01")]
            pred,b=fit_predict(train,test,target)
            if pred is not None:
                pred["year"]=year; parts.append(pred)
                coefs.append(pd.Series(b[1:], index=FEATURES).rename(year))
        p=pd.concat(parts).sort_values("date")
        p=p.merge(d[["date","hist20","ewma"]],on="date",how="left")
        out[h]={"pred":p,"coefs":pd.DataFrame(coefs)}
    return out


def metrics(y,p):
    y=np.asarray(y,float); p=np.asarray(p,float)
    ss=((y-y.mean())**2).sum()
    return dict(n=len(y), mae=float(np.mean(np.abs(y-p))), rmse=float(np.sqrt(np.mean((y-p)**2))),
                r2=float(1-((y-p)**2).sum()/ss) if ss else np.nan,
                bias=float(np.mean(p-y)), corr=float(np.corrcoef(y,p)[0,1]) if len(y)>2 else np.nan)


def block_diff(y, pred, base, h, reps=1000):
    y=np.asarray(y,float); pred=np.asarray(pred,float); base=np.asarray(base,float); n=len(y)
    rng=np.random.default_rng(42); nb=int(math.ceil(n/h)); dmae=[]; drmse=[]; maes=[]; rmse_diffs=[]
    for _ in range(reps):
        starts=rng.integers(0,max(1,n-h+1),nb)
        idx=np.concatenate([np.arange(s,min(s+h,n)) for s in starts])[:n]
        yy=y[idx]; pp=pred[idx]; bb=base[idx]
        mae_p=np.mean(np.abs(yy-pp)); mae_b=np.mean(np.abs(yy-bb))
        rmse_p=np.sqrt(np.mean((yy-pp)**2)); rmse_b=np.sqrt(np.mean((yy-bb)**2))
        dmae.append(mae_p-mae_b); drmse.append(rmse_p-rmse_b)
    return np.percentile(dmae,[2.5,97.5]), np.percentile(drmse,[2.5,97.5])


def hac_t(x, lag):
    x=np.asarray(x,float); x=x[np.isfinite(x)]; n=len(x); mu=x.mean(); e=x-mu
    g0=np.mean(e*e); s=g0
    for l in range(1,lag+1):
        gl=np.mean(e[l:]*e[:-l]); s += 2*(1-l/(lag+1))*gl
    return mu/(np.sqrt(s/n)) if s>0 else np.nan


def main():
    engine=local_engine(); d=pd.read_sql(text("select * from public.research_core_hist order by date"),engine)
    d["date"]=pd.to_datetime(d.date).dt.normalize(); d=add_data(d)
    res=collect(d)
    paired=[]; yearly=[]; regimes=[]; spikes=[]; coeff_rows=[]; diag=[]
    for h,obj in res.items():
        p=obj["pred"].dropna(subset=[f"vol{h}","pred","hist20","ewma"]).copy()
        y=p[f"vol{h}"].to_numpy(float)
        for base in ("hist20","ewma"):
            for loss in ("abs","sq"):
                diff=(np.abs(y-p.pred) - np.abs(y-p[base])) if loss=="abs" else ((y-p.pred)**2-(y-p[base])**2)
                paired.append({"horizon":h,"baseline":base,"loss":loss,"mean_diff":float(diff.mean()),
                               "hac_t":hac_t(diff,h),"ci_lo":float(np.percentile(diff,2.5)),
                               "ci_hi":float(np.percentile(diff,97.5))})
            ci_mae,ci_rmse=block_diff(y,p.pred,p[base],h)
            paired.append({"horizon":h,"baseline":base,"loss":"boot_delta_mae","mean_diff":float(np.mean(np.abs(y-p.pred))-np.mean(np.abs(y-p[base]))),
                           "ci_lo":ci_mae[0],"ci_hi":ci_mae[1]})
            paired.append({"horizon":h,"baseline":base,"loss":"boot_delta_rmse","mean_diff":float(np.sqrt(np.mean((y-p.pred)**2))-np.sqrt(np.mean((y-p[base])**2))),
                           "ci_lo":ci_rmse[0],"ci_hi":ci_rmse[1]})
        for year in (2018,2020,2022,2025,2026):
            q=p[(p.date>=f"{year}-01-01")&(p.date<f"{year+1}-01-01")]
            for name,col in (("reg","pred"),("hist","hist20"),("ewma","ewma")):
                if len(q)>=20:
                    m=metrics(q[f"vol{h}"],q[col]); m.update(horizon=h,model=name,year=year)
                    yearly.append(m)
        # regimes
        masks={
            "high_vix":p.vixcls>=25,"normal_vix":p.vixcls<25,
            "high_vol":p.hist20>=0.20,"low_vol":p.hist20<0.20,
            "drawdown":p.dd252<=-0.10,"recovery":(p.dd252>-0.10)&(p.spy_ret_20d>0),
        }
        for regime,mask in masks.items():
            q=p[mask]
            if len(q)<30: continue
            for name,col in (("reg","pred"),("hist","hist20"),("ewma","ewma")):
                m=metrics(q[f"vol{h}"],q[col]); m.update(horizon=h,regime=regime,model=name)
                regimes.append(m)
        # diagnostics
        resid=p.pred-p[f"vol{h}"]
        diag.append({"horizon":h,"bias":float(resid.mean()),"median_resid":float(resid.median()),
                     "resid_skew":float(resid.skew()),"resid_kurt":float(resid.kurt()),
                     "mae":float(np.mean(np.abs(resid))),"rmse":float(np.sqrt(np.mean(resid**2)))})
        # coefficients
        cf=obj["coefs"]
        for f in FEATURES:
            coeff_rows.append({"horizon":h,"feature":f,"mean":float(cf[f].mean()),"std":float(cf[f].std()),
                               "sign_consistency":float((np.sign(cf[f])==np.sign(cf[f].mean())).mean())})
        # spike threshold train-only 80th percentile; evaluate 5D only
        if h==5:
            for year in YEARS:
                train=d[d.date<f"{year}-01-01"].iloc[:-h]
                test=d[(d.date>=f"{year}-01-01")&(d.date<f"{year+1}-01-01")]
                if len(train)<100 or len(test)<20: continue
                thr=float(train[f"vol{h}"].quantile(0.80))
                q=test.dropna(subset=[f"vol{h}","hist20","ewma"])
                if len(q)<20: continue
                # align regression prediction to test dates
                qq=q.merge(p[["date","pred"]],on="date",how="inner")
                for name,col in (("reg","pred"),("hist","hist20"),("ewma","ewma")):
                    actual=(qq[f"vol{h}"]>=thr).astype(int)
                    pred=(qq[col]>=thr).astype(int)
                    tp=int(((actual==1)&(pred==1)).sum()); fp=int(((actual==0)&(pred==1)).sum()); fn=int(((actual==1)&(pred==0)).sum()); tn=int(((actual==0)&(pred==0)).sum())
                    spikes.append({"year":year,"threshold":thr,"model":name,"n":len(qq),"event_rate":float(actual.mean()),
                                   "precision":tp/(tp+fp) if tp+fp else np.nan,"recall":tp/(tp+fn) if tp+fn else np.nan,
                                   "false_negative":fn/(tp+fn) if tp+fn else np.nan,
                                   "brier":float(np.mean((pred-actual)**2))})
    print("PAIRED"); print(pd.DataFrame(paired).to_string(index=False))
    print("\nYEARLY"); print(pd.DataFrame(yearly).to_string(index=False))
    print("\nREGIMES"); print(pd.DataFrame(regimes).to_string(index=False))
    print("\nDIAGNOSTICS"); print(pd.DataFrame(diag).to_string(index=False))
    print("\nCOEFFICIENTS"); print(pd.DataFrame(coeff_rows).to_string(index=False))
    print("\nSPIKES"); print(pd.DataFrame(spikes).groupby(["model"]).agg({"precision":"mean","recall":"mean","false_negative":"mean","brier":"mean"}).to_string())
    for name,df in [("phase5b_paired",pd.DataFrame(paired)),("phase5b_yearly",pd.DataFrame(yearly)),("phase5b_regimes",pd.DataFrame(regimes)),("phase5b_spikes",pd.DataFrame(spikes)),("phase5b_coefs",pd.DataFrame(coeff_rows)),("phase5b_diag",pd.DataFrame(diag))]:
        df.to_csv(ROOT/"data"/f"{name}.csv",index=False)
    return 0


if __name__=="__main__":
    sys.exit(main())
