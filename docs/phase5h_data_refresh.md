# Phase 5H — Daily Inference Refresh & Activation Gate

Research-only. Frozen models are unchanged.

## Training / inference separation

- Training snapshot: **2026-11-09** (`research_core_hist`), preserved.
- Training cutoff for the frozen models: **2026-11-02**.
- Inference table: `aegis_shadow.inference_daily`.
- The inference hash changes daily and is not required to match the frozen
  training snapshot hash.

## Refresh

Command:

```powershell
$env:PYTHONPATH="src"
C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe `
  scripts\phase5h_refresh_inference.py
```

Sources:

- Yahoo/yfinance: SPY, IWM, HYG, LQD
- FRED: VIXCLS

The refresh computes exactly:

```text
vixcls
spy_vol_20d
iwm_vol_20d
dd252
credit_proxy
spy_ret_20d
```

`VIXCLS` is **not forward-filled across a missing session**. All six features
must exist on the same date.

## Latest refresh status

Primary VIX source is yfinance ^VIX close. FRED VIXCLS is training/reference only.

```text
SPY latest:        2026-11-10
IWM latest:        2026-11-10
HYG latest:        2026-11-10
LQD latest:        2026-11-10
^VIX latest:       2026-11-11 (not used for 11-10)
FRED VIXCLS latest:2026-11-09

Latest complete feature date: 2026-11-10
All six features:              PASS
First prospective forecast:    4483e85bf2b109fe
```

No forecast is issued from an incomplete session.

## Daily workflow

```powershell
$env:PYTHONPATH="src"
C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe `
  scripts\phase5h_daily_shadow.py
```

Order:

```text
refresh → feature completeness/timing gate → frozen forecast
→ immutable PostgreSQL insert → completed outcomes → run log
```

The existing Hermes job `3397016845ff` runs this wrapper on
`0 6 * * 2-6` in the profile timezone.

## Activation gate

The job remains **paused** until:

- a fresh completed US session exists, and
- all six features are available on that same date, and
- the forecast can be created before the next target session.

Current status: **not eligible**; latest complete date is 2026-11-09, and the
2026-11-10 row lacks VIXCLS.

No retrospective record was promoted to prospective.
