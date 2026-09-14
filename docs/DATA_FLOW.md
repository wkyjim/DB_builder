# Data Flow Documentation

## Data Sources

| Source | Type | Tables Fed | Frequency |
|--------|------|------------|-----------|
| Eastmoney API | REST | `us_equities` | Daily |
| yfinance | Python library | `us_equities`, `macro`, `macro_live` | Daily |
| FINRA | API | `finra_short_volume`, `finra_short_interest` | Daily |
| CFTC COT | Weekly report | `cot_positions` | Weekly |
| RSS/Atom feeds | Feeds | `news` | Continuous |
| FRED / World Bank / ECB | Economic APIs | `economic_indicators` | Monthly/Quarterly |
| ETF issuers | Various | `etf_daily_raw`, `etf_daily_data` | Daily |

## Ingestion Pipelines

### Equity Pipeline

```
Eastmoney API ──► pgSQL_equities_auto.py ──► public.us_equities
                      │
                      ├── yfinance fallback for missing/failed rows
                      ├── YTD replacement (local calculation)
                      └── Session coverage validation

repair_historical_equity_gaps.py ──► yfinance multi-ticker ──► public.us_equities
    (repairs missing historical rows)

indicator_staged_backfill.py ──► public.us_equities_indicators
    (MA, EMA, RSI, MACD, ATR, returns, volatility)

pgSQL_daily_bulk_sync_to_neon.py ──► Neon PostgreSQL
    (syncs us_equities + us_equities_indicators)
```

### Macro Pipeline

```
yfinance ──► macro_data_fetch.py ──► public.macro (close data)
                              ──► public.macro_live (latest snapshot)
                              ──► Neon (macro_live only)

economic_data_fetch.py ──► public.economic_indicators (local-only, not synced)

global_economic_data_fetch.py ──► public.global_economic_indicators (local-only)
```

### News Pipeline

```
RSS/Atom feeds ──► news_fetch.py ──► public.news
                              │
                              ▼
                    news_classify.py ──► public.news (updated with classification)
                              │
                              ▼
                    news_signals.py ──► public.news_signals
                              │
                              ▼
                    news_scoring.py ──► public.news_scores
```

### ETF Flow Pipeline

```
ETF issuer data ──► etf_flows_fetch.py ──► public.etf_daily_raw
                                   ──► public.etf_master
                                   ──► public.etf_daily_data
                                        │
                                        ▼
                              etf_flow_analytics.py ──► public.etf_flow_daily
                                                    ──► public.etf_flow_features
                                                    ──► public.etf_flow_segment_daily
                                                    ──► public.etf_flow_consensus_daily
                                                    ──► public.etf_flow_regime_daily
                                                    ──► public.etf_flow_forward_signals
                                                    ──► public.etf_flow_audit_flags
```

### Short Analytics Pipeline

```
FINRA API ──► finra_short_volume_fetch.py ──► public.finra_short_volume
           ──► finra_short_interest_fetch.py ──► public.finra_short_interest
                                                  │
                                                  ▼
                              finra_short_incremental.py ──► public.finra_short_volume
                                                          ──► public.finra_short_interest
                                                                │
                                                                ▼
                                              short_pipeline.py ──► public.us_equities_short_analytics_latest
```

### Positioning Pipeline

```
CFTC COT ──► cot_fetch.py ──► public.cot_positions

cot_positions + finra_short_volume ──► positioning_flow_signals.py ──► public.positioning_flow_signals
```

## Database Tables

### Raw Data Tables

| Table | Purpose | Primary Key | Written By |
|-------|---------|-------------|------------|
| `public.us_equities` | Raw OHLCV equity data | `(date, ticker)` | `pgSQL_equities_auto.py`, `repair_historical_equity_gaps.py` |
| `public.macro` | Historical macro/index data | `(date, symbol)` | `macro_data_fetch.py` |
| `public.macro_live` | Latest intraday macro snapshot | `(symbol)` | `macro_data_fetch.py` |
| `public.news` | Raw news articles | `(url_hash)` | `news_fetch.py` |
| `public.etf_daily_raw` | Raw ETF issuer data | `(date, ticker, source)` | `etf_flows_fetch.py` |
| `public.etf_daily_data` | Canonical ETF daily data | `(date, ticker)` | `etf_flows_fetch.py` |
| `public.finra_short_volume` | FINRA short sale volume | `(date, ticker)` | `finra_short_volume_fetch.py` |
| `public.finra_short_interest` | FINRA short interest | `(date, ticker)` | `finra_short_interest_fetch.py` |
| `public.cot_positions` | CFTC COT positions | `(date, ticker)` | `cot_fetch.py` |
| `public.economic_indicators` | Economic time series | `(date, series_id)` | `economic_data_fetch.py` |
| `public.global_economic_indicators` | Global economic data | `(date, series_id)` | `global_economic_data_fetch.py` |
| `public.security_classification` | Instrument classification | `(ticker)` | `security_classification_fetch.py` |

### Derived/Analytics Tables

| Table | Purpose | Primary Key | Written By |
|-------|---------|-------------|------------|
| `public.us_equities_indicators` | Technical indicators | `(ticker, date)` | `indicator_staged_backfill.py` |
| `public.etf_master` | ETF reference data | `(ticker)` | `etf_flows_fetch.py` |
| `public.etf_flow_daily` | Daily flow calculations | `(date, ticker)` | `etf_flow/run.py` |
| `public.etf_flow_features` | Flow features and z-scores | `(date, ticker)` | `etf_flow/run.py` |
| `public.etf_flow_segment_daily` | Segment-level flow scores | `(date, segment_type, segment)` | `etf_flow/run.py` |
| `public.etf_flow_consensus_daily` | Cross-issuer consensus | `(date, segment)` | `etf_flow/run.py` |
| `public.etf_flow_regime_daily` | ETF flow regime | `(date)` | `etf_flow/run.py` |
| `public.etf_flow_forward_signals` | Forward setup scores | `(date, ticker)` | `etf_flow/run.py` |
| `public.etf_flow_audit_flags` | Data quality audit flags | `(date, ticker)` | `etf_flow/run.py` |
| `public.us_equities_short_analytics_latest` | Short analytics snapshot | `(ticker)` | `short_pipeline.py` |
| `public.positioning_flow_signals` | Unified positioning signals | `(date, ticker)` | `positioning_flow_signals.py` |
| `public.news_signals` | Aggregated news signals | `(window, ticker)` | `news_signals.py` |
| `public.sector_intelligence` | Sector intelligence | `(date, sector)` | `sector_intelligence.py` |
| `public.market_regime` | Market regime history | `(date)` | `market_regime.py` |

### Sync/State Tables

| Table | Purpose | Primary Key | Written By |
|-------|---------|-------------|------------|
| `public.sync_state` | Neon sync checkpoints | `(table_name)` | `neon_sync.py` |
| `public.flow_source_health` | Source health tracking | `(source, date)` | `flow_source_check.py` |

## Derived Data and Analytics

### Technical Indicators

Calculated by `src/db_builder/indicators.py`:
- **Moving Averages:** MA(5), MA(20), MA(50), MA(100), MA(200)
- **Exponential MAs:** EMA(12), EMA(26)
- **RSI:** RSI(14)
- **MACD:** MACD line, signal line, histogram
- **ATR:** ATR(14)
- **Volume:** Volume MA(20), volume ratio
- **52-week:** High and low
- **Returns:** 5-day, 20-day, 60-day
- **Volatility:** 20-day annualized

### ETF Flow Analytics

Calculated by `src/db_builder/etf_flow/`:
- **Clean Flow:** `(shares_outstanding_t - shares_outstanding_t-1) * nav_t`
- **Lag-NAV Audit:** `(shares_outstanding_t - shares_outstanding_t-1) * nav_t-1`
- **Normalized:** Flow as percentage of AUM
- **Winsorized:** Outlier-clipped flow/AUM
- **Rolling Features:** 5/20/60-day sums and EMAs
- **Momentum:** Slope, acceleration, persistence
- **Z-Score:** 20-day and 60-day z-scores
- **Percentile:** 252-day percentile rank
- **Consensus:** Cross-issuer agreement
- **Segment Scores:** Aggregated by segment type
- **Regime:** Flow regime classification
- **Forward Setups:** Heuristic scoring

### Short Analytics

Calculated by `src/db_builder/short_pipeline.py`:
- **Short Interest Metrics:** Days to cover, change percentages
- **Short Volume Ratios:** SVR(5d), SVR(20d), SVR z-score
- **Composite Scores:** Funding short, activity, positioning, unwind risk
- **Regime Classification:** Short regime labels

### Market Regime

Calculated by `src/db_builder/rule_based_regime.py`:
- **Composite Score:** Weighted average of subscores
- **Subscores:** Equity trend, momentum, breadth, volatility, rates, credit, dollar, commodity, ETF flow, news
- **Labels:** Strong Risk-On (80+), Moderate Risk-On (65+), Mild Risk-On (55+), Mixed (45+), Mild Risk-Off (35+), Moderate Risk-Off (20+), Defensive (<20)

## Sync to Neon

Selected tables are synced from local PostgreSQL to Neon:

| Table | Direction | Mechanism |
|-------|-----------|-----------|
| `us_equities` | Local → Neon | `pgSQL_daily_bulk_sync_to_neon.py` |
| `us_equities_indicators` | Local → Neon | `pgSQL_daily_bulk_sync_to_neon.py` |
| `macro_live` | Local → Neon | `macro_data_fetch.py` |
| `us_equities_short_analytics_latest` | Local → Neon | `short_analytics_latest_sync.py` |

Sync uses `public.sync_state` table to track latest synced dates. Idempotent upserts via temp staging tables.

## Report Generation

```
Local PostgreSQL ──► rule_based_market_data.py ──► collect_rule_based_inputs()
                                                       │
                                                       ▼
                              report_renderer.py ──► score_all()
                                                   ──► render_rule_based_market_update()
                                                       │
                                                       ▼
                                                   reports/*.md
                                                       │
                                                       ▼
                                                   market-dashboard/data/latest-report.md
                                                   (via git commit + push)
```

## Data Freshness

| Data | Expected Freshness | Monitoring |
|------|-------------------|------------|
| Equities | Daily (after market close) | `health_check.py` |
| Macro | Daily (after market close) | `health_check.py` |
| News | Continuous | None |
| ETF Flows | Daily (issuer-dependent) | `etf_flow/availability.py` |
| Short Volume | Daily (T+1) | None |
| COT | Weekly | None |
| Economic | Monthly/Quarterly | None |

## Data Retention

- **Local PostgreSQL:** All historical data retained
- **Neon:** Selected tables with `minimum-date` boundary (2026-05-01 for equities)
- **macro_live:** Replace-only (latest snapshot only)
- **artifacts/:** Generated CSV/JSON repair outputs (gitignored)
- **reports/:** Generated markdown reports (gitignored)
- **logs/:** Scheduled run logs (gitignored)
