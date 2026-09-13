# FINRA Short Analytics

## Architecture

- Local PostgreSQL retains raw FINRA files and the complete historical analytics tables.
- `public.us_equities_short_analytics_latest` contains exactly one latest valid row per ticker.
- Neon receives only this latest snapshot. It never receives `public.us_equities_short_analytics` history.
- The API and GitHub Pages Short Positioning section query the latest Neon snapshot.
- The rule-based Markdown report deliberately excludes short-interest and short-volume analytics.

The latest snapshot refresh is scheduled immediately after `finra_short_incremental.py`:

```powershell
python scripts/short_analytics_latest_sync.py
```

## Formula audit notes

- SVR is `ShortVolume / TotalVolume`; it is transaction activity, not outstanding positioning.
- SVR z-scores and one-year percentiles use strictly prior observations.
- CASV sums abnormal SVR relative to the stock's lagged 60-observation median baseline.
- Short-interest values are available to daily analytics only on or after FINRA publication date.
- Short-interest persistence measures the share of published observations with positive SI change.
- Unclassified securities do not receive an invented industry percentile.
- `short_pct_float` remains null because no sufficiently reliable float source is available.
- Funding Short Quality is separate from Funding Short Likelihood and emphasizes liquidity, moderate DTC, relative weakness, SI stability, moderate volatility, lower squeeze risk, and classification quality.

The activity-score weights were recalibrated from the existing diagnostic results: CASV and SVR acceleration receive more weight, while absolute SVR percentiles and high-SVR persistence receive less. This is a deterministic ranking score, not a calibrated return forecast.

## Existing Architecture Reused

- Local PostgreSQL is accessed through `db_builder.config.local_engine`; credentials remain environment-only.
- Daily prices are stored in `public.us_equities` and technical indicators in `public.us_equities_indicators`.
- Sector and industry mappings come from `public.security_classification` where available.
- The existing `public.finra_short_volume` table remains the canonical raw daily FINRA store so current reports continue to work.
- Scheduled jobs use logged retry wrappers under `scripts/auto_*.bat` and source health is tracked in `public.flow_source_health`.

## Gaps Being Addressed

- Existing daily rolling z-scores were calculated from each download batch rather than complete stored history.
- Source-file metadata, validation status, missing-date audit records, and resumable backfill state were absent.
- There was no biweekly short-interest ingestion, publication-lag handling, daily-to-biweekly bridge, or explainable regime engine.
- Reliable free-float data is not present. `short_pct_float` therefore remains unavailable rather than inferred.
- Legacy rows created by the prior uppercase-only parser are retained for audit but excluded from active analytics. Active rows require an exact `raw_symbol` and a non-null common-security `normalized_ticker`; mixed-case preferred/right symbols remain preserved in raw storage without being joined to common-stock prices.

## Data Semantics

FINRA daily Reg SHO short-sale volume is an off-exchange transaction-activity measure. It is not a change in outstanding short positions and is never cumulatively added to short interest.

FINRA twice-monthly short interest is an outstanding-position snapshot as of its settlement date. Historical models only receive a snapshot on or after its publication date; settlement dates are not treated as information-availability dates.

## Storage Layers

1. `finra_short_volume`: raw daily source fields and source metadata.
2. `finra_short_volume_daily_features`: SVR, lagged normalization, CASV, persistence, and activity score.
3. `finra_short_interest`: raw twice-monthly positions with settlement/publication dates.
4. `finra_short_interest_features`: trend, persistence, stability, acceleration, and own-history percentiles.
5. `finra_short_interest_intervals`: daily activity between successive SI observations and the subsequent actual SI change.
6. `us_equities_short_analytics`: point-in-time price, technical, SI, score, and regime output.

The Funding Short Score is a behavioral likelihood score, not evidence of a particular fund or prime-broker position.

The raw short-interest table retains the complete official files. Derived short-interest features are limited to symbols that overlap the source-preserved, common-security daily universe; this avoids spending production compute on inactive or unmappable historical issues while retaining their raw records for audit.

Long historical final-layer builds can resume at a durable ticker boundary with `scripts/finra_short_analytics.py --start-after-ticker TICKER --skip-daily-features --skip-si-features`. Completed batches are idempotent PostgreSQL upserts.

## Initial Production Verification (2026-08-13 HKT)

- Daily FINRA source: 402 successful sessions from 2025-01-02 through 2026-08-11; 4,425,810 exact source-preserved rows.
- Raw daily table: 4,571,709 rows including 145,899 retained legacy normalization rows that active analytics exclude.
- Daily feature layer: 4,278,681 rows across 14,668 eligible symbols.
- Short interest: 2,413,994 raw rows across 119 published settlement dates from 2021-08-13 through 2026-07-15.
- The expected 2026-07-31 and 2026-08-11 files returned unavailable and are recorded as missing, not zero.
- Final point-in-time analytics: 4,278,681 rows across 14,668 symbols; no publication-date leakage was detected.
- Direct 2026-08-11 source comparisons for AAPL, AMD, MSFT, NVDA, and TSLA matched the official FINRA file exactly.
- Rule-based report generation completed with short analytics intentionally excluded from the text report.
- Repository tests: 435 passed using an explicit writable pytest temporary directory.

## Initial Historical Diagnostics

The publication-ordered interval sample contains 304,113 labeled observations, split 70/30 with an out-of-sample start date of 2026-02-25. Training correlations with subsequent SI change were weak for simple persistence counts (`0.014` to `0.019`) and modest for mean abnormal SVR (`0.156`), acceleration (`0.162`), and interval CASV (`0.173`). Out-of-sample direction hit rates were `0.415`, `0.410`, and `0.401` at 2%, 3%, and 5% SI-change thresholds.

These results do not establish predictive power. They support using daily activity as a conditional confirmation feature, with CASV and acceleration more useful than isolated high-SVR observations, while actual published SI remains the position-confirmation layer.

## Remaining Data Limits

FINRA data does not identify hedge funds or prime-broker books and does not supply stock-borrow fees, utilization, lendable inventory, or reliable free float. The Funding Short Score is therefore an inferred behavioral likelihood score, not proof of a specific investor's position. Historical feature rows from aborted pre-filter runs may remain for noneligible symbols, but production final analytics and reports are built only from the eligible source-preserved daily universe.

## Massive Short-Interest Fallback

FINRA remains the primary short-interest source. The incremental job reads FINRA's official settlement/publication calendar and attempts the FINRA file first. When a published settlement date is absent from local PostgreSQL and the FINRA request is missing or fails, the job may request the complete settlement-date cross section from Massive in one call using `limit=50000` and `sort=ticker.asc`.

Configure the private key only in the external `DB_builder_env\.env` file:

```text
MASSIVE_API_KEY=
```

Missing credentials disable the fallback without changing the FINRA path. Massive rows retain `source_file=massive:...` and `data_quality_status=valid_massive_fallback`; API keys are never stored in PostgreSQL or logs. A response containing `next_url` is rejected to avoid silently ingesting a partial universe. Normalized rows reuse `public.finra_short_interest`, so existing feature generation, publication gating, analytics refresh, and latest-snapshot sync remain unchanged.
