# Phase 5G — Automated Prospective Shadow Monitoring

Research-only. No production scores, allocations, trade signals or reporting.

## Forecast timing

- Feature cutoff: market close of `as_of_date`.
- Forecast origin: `as_of_date` after close.
- Target window: the next five US trading sessions.
- A forecast is **prospective** only when its database creation timestamp is before the target window begins.
- The 2026-11-09 forecast was generated on 2026-11-11 and is therefore
  **`retrospective_shadow`**. It is excluded from prospective metrics.

## Target definitions

```text
spike_80th:      future 5D vol >= 18.4956% (train-only 80th percentile)
fixed_20:        future 5D vol >= 20% annualized
onset_20:        current 5D vol < 20% and future 5D vol >= 20%
persistence_20:  current 5D vol >= 20% and future 5D vol >= 20%
```

The frozen logistic probability is the 80th-target probability, not a 20% probability.

## Data freshness

The daily wrapper requires:

- `as_of_date` equals the latest `public.research_core_hist` date.
- All six frozen features are present.
- The run is before the target window begins; otherwise it logs a retrospective
  or skipped run and does not create a prospective forecast.

The wrapper does not refresh production data. It must run after the existing
US market ingestion produces a completed daily session.

## PostgreSQL shadow schema

Schema: `aegis_shadow`

Tables:

- `model_versions` — frozen registry, SHA-256.
- `data_snapshots` — source table, max date, row count, CSV SHA-256.
- `forecasts` — immutable forecast records; unique `(model_version, as_of_date)`.
- `outcomes` — append-only, unique `forecast_id`.
- `run_log` — append-only pipeline status and failure records.

Append-only triggers reject UPDATE and DELETE on forecasts, outcomes and run logs.

`created_at` is database-generated. The forecast record stores model version,
features, data snapshot, target window, status, volatility, probability and
baselines. `record_sha256` hashes the forecast payload.

## Commands

```powershell
$env:PYTHONPATH="src"
C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe `
  scripts\phase5g_shadow_db.py status
```

```powershell
$env:PYTHONPATH="src"
C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe `
  scripts\phase5g_shadow_db.py insert 2026-11-09
```

```powershell
$env:PYTHONPATH="src"
C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe `
  scripts\phase5g_outcomes.py
```

Daily wrapper:

```powershell
$env:PYTHONPATH="src"
C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe `
  scripts\phase5g_daily_shadow.py
```

The wrapper:
1. Verifies the latest data row.
2. Refuses retrospective or stale forecasts.
3. Inserts the frozen forecast into PostgreSQL.
4. Appends completed outcomes.
5. Logs every run.

## Schedule

A paused Hermes job exists:

- Name: **AEGIS Shadow 5D Research**
- Job ID: `3397016845ff`
- Schedule: `0 6 * * 2-6` (after US close, profile timezone)
- Mode: no-agent script
- Delivery: local only
- State: **paused**

It must remain paused until the ingestion refresh produces a current completed
session. Resume only after data freshness is confirmed.

## Latest status

- Latest research data: **2026-11-09**
- Current date: **2026-11-11**
- 2026-11-09 forecast: **retrospective_shadow**
- DB forecasts: 1
- Outcomes: 0
- Pending outcomes: 1
- First eligible prospective forecast: **none yet** — requires a fresh completed session and generation before the next target window.

## Prospective evaluation

After outcomes accumulate, evaluate only `status='prospective'` records and
completed outcomes:

- 5D volatility MAE/RMSE vs EWMA and historical volatility.
- 80th-target Brier/log loss and calibration.
- Fixed-20% onset and persistence metrics separately.
- False alerts and first-alert lead time.
- Separate historical backtests from prospective results.
