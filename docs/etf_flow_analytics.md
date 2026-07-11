# ETF Flow Analytics Layer

## Purpose

The ETF flow analytics layer turns issuer-derived ETF shares outstanding/NAV
history into deterministic allocation-flow signals for the rule-based market
update report.

It does not treat ETF trading volume as fund flow. The preferred production
flow estimate is:

```text
estimated_flow = (shares_outstanding_t - shares_outstanding_t-1) * nav_t
```

The system also stores a lag-NAV estimate:

```text
estimated_flow_lag_nav = (shares_outstanding_t - shares_outstanding_t-1) * nav_t-1
```

The current report uses the current-NAV estimate because it matches the user's
requested daily net flow formula. The lag-NAV field is stored for audit and
future robustness checks.

## Source Tables

- `public.etf_daily_data`: existing issuer-derived NAV/AUM/shares table.
- `public.etf_daily_raw`: minimally transformed raw copy used by analytics.
- `public.etf_master`: ETF classification table.

## Analytics Tables

- `public.etf_flow_daily`
- `public.etf_flow_features`
- `public.etf_flow_segment_daily`
- `public.etf_flow_consensus_daily`
- `public.etf_flow_rotation_daily`
- `public.etf_flow_regime_daily`
- `public.etf_flow_forward_signals`
- `public.etf_flow_audit_flags`

SQL migration:

```text
migrations/20260711_etf_flow_analytics.sql
```

## Python Modules

- `db_builder.etf_flow.config`: thresholds and labels.
- `db_builder.etf_flow.repository`: PostgreSQL reads/writes.
- `db_builder.etf_flow.validation`: missing-data and quality scoring.
- `db_builder.etf_flow.normalization`: winsorization, z-scores, percentiles.
- `db_builder.etf_flow.momentum`: EMA, slope, acceleration.
- `db_builder.etf_flow.consensus`: cross-issuer agreement.
- `db_builder.etf_flow.breadth`: breadth and concentration metrics.
- `db_builder.etf_flow.price_flow`: price x flow state matrix.
- `db_builder.etf_flow.regime`: ETF flow regime score.
- `db_builder.etf_flow.confidence`: confidence adjustment logic.
- `db_builder.etf_flow.forward_signal`: heuristic forward setup score.
- `db_builder.etf_flow.aggregation`: orchestration of analytical layers.
- `db_builder.etf_flow.report_adapter`: markdown report section.
- `db_builder.etf_flow.backtest`: research scaffold, no predictive claims.

## CLI

Dry run:

```bash
python scripts/etf_flow_analytics.py --dry-run --start-date 2026-01-01
```

Persist analytics:

```bash
python scripts/etf_flow_analytics.py --upsert-local --start-date 2026-01-01 --write-report-output
```

JSON sample:

```bash
python scripts/etf_flow_analytics.py --dry-run --start-date 2026-01-01 --json
```

## Current Report Integration

`src/db_builder/rule_based_market_data.py` loads the latest persisted ETF flow
analytics into `etf_flow_analytics`.

`src/db_builder/report_renderer.py` renders:

- ETF Flow Executive Summary
- Market Flow Dashboard
- Flow-Confirmed Forward Setups
- ETF Flow Contradiction Flags

## Implemented Analytical Layers

1. Market Flow Score: segment scores and flow regime.
2. Relative/Normalized Flow: flow/AUM, lag-AUM, winsorized values, z-scores.
3. Flow Momentum/Acceleration: EMA 5/20, slopes, acceleration, persistence.
4. Cross-Issuer Consensus: issuer agreement and concentration penalty.
5. Price x Flow Matrix: accumulation, distribution, buying weakness, etc.
6. Flow Breadth: equal-weight breadth, AUM-weighted breadth, HHI/effective count.
7. Flow Leadership/Rotation: ranks and rank changes.
8. ETF Flow Regime Detector: flow-only regime and combined-regime inputs.
9. Flow-Based Confidence: confidence adjustment module and audit flags.
10. Forward Outperformance Features: heuristic setup score, not calibrated probability.

## Limitations

- Forward setup buckets are heuristic and not statistically calibrated.
- Cross-issuer consensus is limited by current issuer coverage.
- Some segments still have one dominant ETF and are flagged for concentration.
- Issuer publication timing is not yet modeled as an availability lag.
- Backtesting scaffold exists, but no performance claim should be made until
  walk-forward validation is run.
