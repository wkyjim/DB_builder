"""Phase 5A: risk forecasting feasibility and downside calibration audit."""
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


def add_targets(d):
    d = d.copy().sort_values("date").reset_index(drop=True)
    d["credit_proxy"] = d["hyg_ret_20d"] - d["lqd_ret_20d"]
    d["dd252"] = d["spy_close"] / d["spy_close"].rolling(252, min_periods=60).max() - 1
    d["ret_1d"] = d["spy_close"].pct_change()
    # EWMA daily variance, annualized
    var = d["ret_1d"].pow(2)
    d["ewma_vol"] = np.sqrt(var.ewm(alpha=0.06, adjust=False).mean() * 252)
    prices = d["spy_close"].to_numpy(float)
    for h in H:
        d[f"fwd_ret_{h}d"] = d["spy_close"].shift(-h) / d["spy_close"] - 1
        fwd_vol = np.full(len(d), np.nan)
        for i in range(len(d) - h):
            r = d["ret_1d"].iloc[i+1:i+h+1].to_numpy(float)
            fwd_vol[i] = float(np.std(r, ddof=1) * math.sqrt(252))
        d[f"fwd_vol_{h}d"] = fwd_vol
        mdd = np.full(len(d), np.nan)
        for i in range(len(d) - h):
            path = prices[i:i+h+1]
            peak = np.maximum.accumulate(path)
            mdd[i] = float(np.min(path / peak - 1.0))
        d[f"fwd_mdd_{h}d"] = mdd
    return d


def fit_predict(train, test, features, target, kind="ols"):
    cols = (["date"] if "date" in test.columns else []) + features + [target]
    tr = train[cols].dropna()
    te = test[cols].dropna()
    if len(tr) < 80 or len(te) < 10:
        return None
    mu = tr[features].mean(); sd = tr[features].std().replace(0, np.nan)
    X = ((tr[features] - mu) / sd).to_numpy(float)
    Xt = ((te[features] - mu) / sd).to_numpy(float)
    y = tr[target].to_numpy(float)
    X = np.column_stack([np.ones(len(X)), X]); Xt = np.column_stack([np.ones(len(Xt)), Xt])
    if kind == "ols":
        b = np.linalg.lstsq(X, y, rcond=None)[0]
        pred = Xt @ b
        resid = y - X @ b
        return te.assign(pred=pred, resid_std=float(np.std(resid, ddof=1)),
                         resid_q05=float(np.quantile(resid, 0.05)))
    return None


def metric(y, p):
    y = np.asarray(y, float); p = np.asarray(p, float)
    ss = ((y - y.mean()) ** 2).sum()
    return {"n": len(y), "mae": float(np.mean(np.abs(y - p))),
            "rmse": float(np.sqrt(np.mean((y - p) ** 2))),
            "r2": float(1 - ((y - p) ** 2).sum() / ss) if ss else np.nan,
            "corr": float(np.corrcoef(y, p)[0, 1]) if len(y) > 2 else np.nan}


def vol_models(d):
    rows = []
    for h in H:
        target = f"fwd_vol_{h}d"
        for name, pred_col in (("hist20", "spy_vol_20d"), ("ewma", "ewma_vol")):
            q = d[["date", target, pred_col]].dropna()
            q = q[q.date >= "2018-01-01"]
            rows.append({"target": "vol", "horizon": h, "model": name, **metric(q[target], q[pred_col])})
        parts = []
        for year in YEARS:
            train = d[d.date < f"{year}-01-01"].iloc[:-h]
            test = d[(d.date >= f"{year}-01-01") & (d.date < f"{year+1}-01-01")]
            out = fit_predict(train, test, FEATURES, target)
            if out is not None: parts.append(out)
        if parts:
            p = pd.concat(parts).sort_values("date")
            rows.append({"target": "vol", "horizon": h, "model": "regression",
                         **metric(p[target], p["pred"])})
    return pd.DataFrame(rows)


def mdd_models(d):
    rows = []
    for h in H:
        target = f"fwd_mdd_{h}d"
        q = d[["date", target, "dd252"]].dropna()
        q = q[q.date >= "2018-01-01"]
        rows.append({"target": "mdd", "horizon": h, "model": "current_dd", **metric(q[target], q["dd252"])})
        parts = []
        for year in YEARS:
            train = d[d.date < f"{year}-01-01"].iloc[:-h]
            test = d[(d.date >= f"{year}-01-01") & (d.date < f"{year+1}-01-01")]
            out = fit_predict(train, test, FEATURES, target)
            if out is not None: parts.append(out)
        if parts:
            p = pd.concat(parts).sort_values("date")
            rows.append({"target": "mdd", "horizon": h, "model": "regression",
                         **metric(p[target], p["pred"])})
    return pd.DataFrame(rows)


def downside_audit(d):
    rows = []
    for h in H:
        target = f"fwd_ret_{h}d"
        parts = []
        for year in YEARS:
            train = d[d.date < f"{year}-01-01"].iloc[:-h]
            test = d[(d.date >= f"{year}-01-01") & (d.date < f"{year+1}-01-01")]
            out = fit_predict(train, test, FEATURES, target)
            if out is not None: parts.append(out)
        if not parts: continue
        p = pd.concat(parts).sort_values("date").dropna(subset=["pred", target, "resid_std"])
        y = p[target].to_numpy(float); pred = p["pred"].to_numpy(float)
        normal_q05 = pred - 1.645 * p["resid_std"].to_numpy(float)
        # train-only empirical residual quantile: use residuals already returned
        emp = pred + p["resid_q05"].to_numpy(float)
        rows.append({
            "horizon": h, "n": len(p),
            "actual_q05": float(np.quantile(y, 0.05)),
            "normal_q05_mean": float(np.mean(normal_q05)),
            "empirical_q05_mean": float(np.mean(emp)),
            "normal_coverage": float((y < normal_q05).mean()),
            "empirical_coverage": float((y < emp).mean()),
            "resid_std_mean": float(p["resid_std"].mean()),
            "exceedance_by_year": ",".join(f"{yr}:{float((y[(p.date >= f'{yr}-01-01') & (p.date < f'{yr+1}-01-01')] < normal_q05[(p.date >= f'{yr}-01-01') & (p.date < f'{yr+1}-01-01')]).mean()):.3f}" for yr in YEARS if ((p.date >= f'{yr}-01-01') & (p.date < f'{yr+1}-01-01')).sum() > 10),
        })
    return pd.DataFrame(rows)


def probability_models(d):
    rows = []
    def sigmoid(z):
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))

    def logistic_fit(X, y, lr=0.1, epochs=600, l2=1e-3):
        X = np.column_stack([np.ones(len(X)), X]); w = np.zeros(X.shape[1])
        for _ in range(epochs):
            p = sigmoid(X @ w)
            grad = X.T @ (p - y) / len(y) + l2 * w
            w -= lr * grad
        return w

    def logistic_predict(w, X):
        X = np.column_stack([np.ones(len(X)), X])
        return sigmoid(X @ w)
    specs = [(5, 0.03), (20, 0.05), (20, 0.10), (60, 0.10)]
    for h, thr in specs:
        target = f"fwd_ret_{h}d"
        y_all = (d[target] <= -thr).astype(float)
        parts = []
        for year in YEARS:
            train = d[d.date < f"{year}-01-01"].iloc[:-h]
            test = d[(d.date >= f"{year}-01-01") & (d.date < f"{year+1}-01-01")]
            tr = train[["date"] + FEATURES + [target]].dropna()
            te = test[["date"] + FEATURES + [target]].dropna()
            if len(tr) < 100 or len(te) < 20 or tr[target].nunique() < 2:
                continue
            mu = tr[FEATURES].mean(); sd = tr[FEATURES].std().replace(0, np.nan)
            X = ((tr[FEATURES] - mu) / sd).to_numpy(float)
            Xt = ((te[FEATURES] - mu) / sd).to_numpy(float)
            y = (tr[target] <= -thr).astype(int).to_numpy()
            w = logistic_fit(X, y)
            prob = logistic_predict(w, Xt)
            parts.append(te.assign(prob=prob, actual=(te[target] <= -thr).astype(int)))
        if not parts: continue
        p = pd.concat(parts).sort_values("date")
        base = float(p["actual"].mean())
        def brier(y, q):
            return float(np.mean((np.asarray(q) - np.asarray(y)) ** 2))
        def logloss(y, q):
            q = np.clip(np.asarray(q), 1e-6, 1-1e-6)
            return float(-np.mean(np.asarray(y)*np.log(q) + (1-np.asarray(y))*np.log(1-q)))
        rows.append({"horizon": h, "threshold": thr, "n": len(p), "event_rate": base,
                     "brier": brier(p.actual, p.prob), "logloss": logloss(p.actual, p.prob),
                     "base_brier": brier(p.actual, np.full(len(p), base)),
                     "base_logloss": logloss(p.actual, np.full(len(p), base)),
                     "mean_prob": float(p.prob.mean())})
    return pd.DataFrame(rows)


def main():
    engine = local_engine()
    d = pd.read_sql(text("SELECT * FROM public.research_core_hist ORDER BY date"), engine)
    d["date"] = pd.to_datetime(d.date).dt.normalize()
    d = add_targets(d)
    v = vol_models(d); print("VOL"); print(v.to_string(index=False))
    m = mdd_models(d); print("\nMDD"); print(m.to_string(index=False))
    p = probability_models(d); print("\nPROB"); print(p.to_string(index=False))
    a = downside_audit(d); print("\nDOWNSIDE"); print(a.to_string(index=False))
    v.to_csv(ROOT / "data/phase5a_vol.csv", index=False)
    m.to_csv(ROOT / "data/phase5a_mdd.csv", index=False)
    p.to_csv(ROOT / "data/phase5a_prob.csv", index=False)
    a.to_csv(ROOT / "data/phase5a_downside.csv", index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
