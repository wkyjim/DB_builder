# Phase 5H Post-Activation QC

## First prospective forecast PIT check

```text
forecast_id: 4483e85bf2b109fe
as_of:       2026-11-10
feature cutoff: 2026-11-10 21:00 UTC
target start:   2026-11-11 14:30 UTC
created:        2026-11-11 22:26:02 HKT
status:         prospective
```

`vixcls` in the frozen feature vector is **19.459999**, equal to the yfinance
`^VIX` close for 2026-11-10. The 2026-11-11 `^VIX` close (18.32) was not used.
The refresh pipeline now filters out any session that has not completed on the
NYSE calendar before computing features.

## VIX source outlier

```text
date:        2026-02-06
yfinance:    20.370000839
FRED VIXCLS: 17.76
difference:  2.610000839
```

This is an isolated FRED observation inconsistent with adjacent sessions
(2026-02-05 yfinance 21.77; 2026-02-09 17.36). It is treated as a historical
FRED data anomaly, not a market move.

## Calendar and timezone

- Hermes job timezone: `Asia/Hong_Kong`.
- Schedule: `0 6 * * 2-6`.
- NYSE calendar is used for target sessions, holidays and DST.
- DST check: 2026-03-09 open 13:30 UTC vs 2026-03-06 open 14:30 UTC.
- Holiday check: 2026-11-26 is not a session; next sessions after 2026-11-24
  are 11/25, 11/27, 11/30.

## Health and outcome policy

The daily wrapper skips and logs `stale_latest_session`, `already_forecast` or
`retrospective_window_not_eligible`. Outcomes are appended only when six
consecutive inference closes (as-of plus five future sessions) exist and the
last row equals the stored target end.
