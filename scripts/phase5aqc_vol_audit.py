"""Phase 5A-QC: independent audit of forward-volatility targets."""
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

H = (5, 20, 60)
YEARS = range(2018, 2027)
FEATURES = ["vixcls", "spy_vol_20d", "iwm_vol_20d", "dd252", "credit_proxy", "spy_ret_20d"]


def metrics(y, p):
    y = np.asarray(y, float); p = np.asarray(p, float)
    ss = ((y - y.mean()) ** 2).sum()
    return dict(n=len(y), mae=float(np.mean(np.abs(y-p))), rmse=float(np.sqrt(np.mean((y-p)**2))),
                r2=float(1-((y-p)**2).sum()/ss) if ss else np.nan,
                corr=float(np.corrcoef(y,p)[0,1]) if len(y)>2 else np.nan)


def fit_predict(train, test, target):
    cols = ["date"] + FEATURES + [target]
    tr = train[cols].dropna(); te = test[cols].dropna()
    if len(tr) < 80 or len(te) < 10:
        return None
    mu = tr[FEATURES].mean(); sd = tr[FEATURES].std().replace(0, np.nan)
    X = ((tr[FEATURES]-mu)/sd).to_numpy(float); Xt = ((te[FEATURES]-mu)/sd).to_numpy(float)
    X = np.column_stack([np.ones(len(X)), X]); Xt = np.column_stack([np.ones(len(Xt)), Xt])
    y = tr[target].to_numpy(float)
    b = np.linalg.lstsq(X, y, rcond=None)[0]
    return te.assign(pred=Xt @ b)


def block_ci(y, p, h, reps=1000):
    y = np.asarray(y, float); p = np.asarray(p, float); n = len(y)
    rng = np.random.default_rng(42); r2s=[]; rmses=[]
    nb = int(math.ceil(n/h))
    for _ in range(reps):
        starts = rng.integers(0, max(1, n-h+1), nb)
        idx = np.concatenate([np.arange(s, min(s+h, n)) for s in starts])[:n]
        yy=y[idx]; pp=p[idx]
        ss=((yy-yy.mean())**2).sum()
        r2s.append(1-((yy-pp)**2).sum()/ss if ss else np.nan)
        rmses.append(np.sqrt(np.mean((yy-pp)**2)))
    return np.nanpercentile(r2s,[2.5,97.5]), np.nanpercentile(rmses,[2.5,97.5])


def main():
    engine = local_engine()
    d = pd.read_sql(text("select * from public.research_core_hist order by date"), engine)
    d["date"] = pd.to_datetime(d.date).dt.normalize()
    d = d.sort_values("date").reset_index(drop=True)
    d["ret"] = d.spy_close.pct_change()
    d["credit_proxy"] = d.hyg_ret_20d - d.lqd_ret_20d
    d["dd252"] = d.spy_close / d.spy_close.rolling(252, min_periods=60).max() - 1
    d["hist20"] = d.ret.rolling(20, min_periods=20).std() * math.sqrt(252)
    var = d.ret.pow(2)
    d["ewma"] = np.sqrt(var.ewm(alpha=0.06, adjust=False).mean() * 252)
    print("baseline max diff hist20 vs core spy_vol_20d", float((d.hist20 - d.spy_vol_20d).abs().max()))
    for h in H:
        old = d.ret.shift(-1).rolling(h).std() * math.sqrt(252)
        fwd = np.full(len(d), np.nan)
        for i in range(len(d)-h):
            r = d.ret.iloc[i+1:i+h+1].to_numpy(float)
            fwd[i] = np.std(r, ddof=1) * math.sqrt(252)
        d[f"old_{h}"] = old; d[f"correct_{h}"] = fwd
        fwd_s = pd.Series(fwd, index=d.index)
        print(f"h={h} old-correct maxabs", float(np.nanmax(np.abs(old-fwd_s))), "corr", float(old.corr(fwd_s)))
    # sample explicit ranges
    print("\nSAMPLE RANGES")
    for date in ["2020-03-16", "2022-06-13", "2025-04-08", "2026-11-06"]:
        pos = d.index[d.date == date]
        if len(pos) == 0: continue
        i = int(pos[0])
        print("\n", date, "index", i)
        print("hist window", d.date.iloc[i-19].date(), "to", d.date.iloc[i].date(), "hist20", round(float(d.hist20.iloc[i]),4))
        for h in H:
            if i+h < len(d):
                print(f"forward h={h}", d.date.iloc[i+1].date(), "to", d.date.iloc[i+h].date(),
                      "old", round(float(d[f"old_{h}"].iloc[i]),4), "correct", round(float(d[f"correct_{h}"].iloc[i]),4))
    # OOS on corrected target
    rows=[]; allpred=[]
    for h in H:
        target=f"correct_{h}"
        parts=[]
        for year in YEARS:
            train=d[d.date < f"{year}-01-01"].iloc[:-h]
            test=d[(d.date>=f"{year}-01-01")&(d.date<f"{year+1}-01-01")]
            out=fit_predict(train,test,target)
            if out is not None: parts.append(out)
        reg=pd.concat(parts).sort_values("date") if parts else pd.DataFrame()
        if reg.empty: continue
        reg=reg.merge(d[["date","hist20","ewma"]],on="date",how="left")
        base=reg[["date",target,"hist20","ewma"]].dropna()
        # use same rows as regression
        reg=reg.dropna(subset=["pred",target])
        for name,predcol in (("hist20","hist20"),("ewma","ewma"),("regression","pred")):
            q=reg.dropna(subset=[predcol,target])
            m=metrics(q[target],q[predcol]); m.update(horizon=h,model=name)
            ci_r2,ci_rmse=block_ci(q[target],q[predcol],h)
            m["r2_lo"],m["r2_hi"]=ci_r2; m["rmse_lo"],m["rmse_hi"]=ci_rmse
            rows.append(m)
        # yearly
        for year in (2018,2020,2022,2025,2026):
            q=reg[(reg.date>=f"{year}-01-01")&(reg.date<f"{year+1}-01-01")]
            for name,predcol in (("hist20","hist20"),("ewma","ewma"),("regression","pred")):
                if len(q)<20: continue
                m=metrics(q[target],q[predcol]); m.update(horizon=h,model=name,year=year)
                rows.append(m)
        allpred.append(reg.assign(horizon=h))
    out=pd.DataFrame(rows)
    print("\nCORRECTED OOS RESULTS")
    print(out[out.year.isna()].to_string(index=False) if "year" in out else out.to_string(index=False))
    print("\nYEARLY")
    print(out[out.year.notna()].to_string(index=False))
    out.to_csv(ROOT/"data"/"phase5aqc_vol.csv",index=False)
    # old vs correct regression R2 demonstration
    print("\nOLD TARGET R2 SAMPLE (regression, first horizon only for illustration)")
    for h in H:
        # compare old vs correct correlation with hist20 as a diagnostic
        q=d[[f"old_{h}",f"correct_{h}","hist20"]].dropna()
        print(h,"old/hist corr",float(q[f"old_{h}"].corr(q.hist20)),"correct/hist corr",float(q[f"correct_{h}"].corr(q.hist20)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
