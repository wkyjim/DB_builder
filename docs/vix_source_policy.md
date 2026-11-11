# AEGIS VIX Source Policy

## Primary daily inference source

`yfinance ^VIX` daily **Close** is the primary VIX source for:

- daily market-data refresh
- indicators and research inference features
- 5D volatility and spike-probability shadow forecasts
- future daily model inference

The yfinance close is mapped to the frozen `vixcls` inference feature. No
forward-fill is permitted.

## Training/research source

FRED `VIXCLS` remains the historical training, backtest and research-validation
source. FRED availability must not block daily inference.

## Frozen-model compatibility

The frozen models, coefficients, scalers, feature order, training snapshot and
target definitions are unchanged.

```text
vol5_ols_v1
spike_logit_v1
hist20_v1
ewma_v1
spike threshold: 18.4956%
```

## Source comparison

Overlap: 2013-01-01 through the latest common FRED date.

```text
Overlapping observations: 3487
Correlation:              0.99997
Mean absolute difference: 0.00131 VIX points
Maximum absolute difference: 2.61 VIX points
Latest common-date difference: 0.00000008
QC: PASS
```

The maximum is a historical outlier; the latest common-date agreement is
effectively exact. A latest-session difference above 0.5 VIX points is treated
as a material source discrepancy and blocks the forecast.

## Provenance

Each refresh records:

- primary source and latest yfinance date
- FRED training source/date when available
- overlap count, MAE, maximum difference, correlation
- latest common-date difference
- QC status
- inference timestamp

Rows are stored in `aegis_shadow.vix_source_qc`; inference provenance is also in
`aegis_shadow.inference_daily.source_json`.

## Current status

```text
Latest complete inference date: 2026-11-10
Six frozen features:            6 / 6
First prospective forecast:     4483e85bf2b109fe
Forecast origin:                2026-11-10 close
Target window:                  2026-11-11 → 2026-11-17
Status:                         prospective
Forecast 5D vol:                0.16612
Spike probability:              0.21009
```

No retrospective forecast may be relabeled prospective.
