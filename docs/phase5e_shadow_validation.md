# Phase 5E — Frozen 5D Research Model & Shadow Validation

Status: **research-only**. Not connected to AEGIS production scores, allocations,
trade signals or reporting.

## Frozen models

Registry: `data/phase5e_model_registry.json`

| Component | Version | Definition |
|---|---|---|
| 5D volatility | `vol5_ols_v1` | OLS on 6 point-in-time features |
| Spike probability | `spike_logit_v1` | Logistic probability; 80th-percentile train-only threshold |
| Historical baseline | `hist20_v1` | Trailing 20D realized volatility |
| EWMA baseline | `ewma_v1` | RiskMetrics lambda 0.06 |

- Training cutoff: **2026-11-02** (last complete 5D outcome)
- Shadow forecast as-of: **2026-11-09**
- Source: `public.research_core_hist`
- Base Git commit: `50db4f352ebfecc7fbb8144e607186413102d595`
- Freeze commit: `2ce28fedea791fe102fb1117f9699e959c2fea8e`
- Phase 5C/5D/5E/5F scripts, registry and this document are frozen in the Phase 5F commit. Their SHA-256 hashes are in the registry.

Target definitions:

```text
spike_80th:      future 5D vol >= 18.4956% (train-only 80th percentile)
fixed_20:        future 5D vol >= 20% annualized
onset_20:        current 5D vol < 20% and future 5D vol >= 20%
persistence_20:  current 5D vol >= 20% and future 5D vol >= 20%
```

The frozen logistic probability is the **80th-percentile target probability**.
It is **not** a probability of crossing 20%. Fixed-20% onset/persistence results
are reported separately.

Features:

```text
vixcls, spy_vol_20d, iwm_vol_20d, dd252, credit_proxy, spy_ret_20d
```

No features, thresholds, calibration or model structure may be changed without
a new registry version.

## Shadow forecast command

Run only after the US session is complete and the daily data row exists:

```powershell
$env:PYTHONPATH="src"
C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe `
  scripts\phase5f_daily_shadow.py 2026-11-09
```

This wrapper refuses stale input, runs the frozen forecast, appends completed
outcomes, runs shadow QC and reproduces the latest forecast from the registry.
It appends a run record to `data/phase5f_shadow_runs.log`.

Individual commands:

```powershell
$env:PYTHONPATH="src"
C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe `
  scripts\phase5e_shadow_forecast.py 2026-11-09
```

The command appends one row to `data/phase5e_shadow_forecasts.csv`. It never
overwrites or retrains prior forecasts. Re-running the same date/model is a no-op.

## Outcome command

Run after the full five-trading-day window has completed:

```powershell
$env:PYTHONPATH="src"
C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe `
  scripts\phase5e_shadow_outcomes.py
```

Appends completed outcomes to `data/phase5e_shadow_outcomes.csv`, keyed by
`forecast_id`. Pending forecasts produce no outcome row.

## Forecast schema

```text
forecast_id
created_utc
as_of_market_date
model_registry
vol_model_version
spike_model_version
data_source
data_version
code_commit
current_vol_5d
stress_state
forecast_vol_5d
spike_probability
hist20_forecast
ewma_forecast
feature_completeness
feature_count
qc_flags
training_cutoff
source_row_date
```

Outcome schema:

```text
forecast_id
outcome_available_date
realized_vol_5d
forecast_vol_5d
squared_error
abs_error
spike_threshold
spike_outcome
```

## Prospective validation plan

Do not evaluate until at least 8–12 weeks of forecasts have completed outcomes.

Report prospectively:

- 5D volatility MAE/RMSE vs hist20 and EWMA.
- Spike Brier/log loss vs base rate and EWMA.
- Calibration by probability bin.
- Onset episode recall and first-alert lead time.
- False alarms, misses and regime breakdown.
- Separate prospective results from historical backtests.

The current historical freeze is not a production approval. Phase 5E-QC showed
that stress-onset and upper-tail claims remain weak.
