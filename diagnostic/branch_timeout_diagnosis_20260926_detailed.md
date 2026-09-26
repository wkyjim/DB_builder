# Branch A/B Diagnostic — Detailed Findings

## Executive Summary

Branch A and Branch B both hit the 600-second global timeout. The cycle (20260926_0441_HKT) started at 04:41:37 HKT and completed at 04:53:43 HKT (726s total). Branches C/D/E completed normally (102s-270s).

## Exact Branch B Slow Step

**`macro_data_fetch.py` — Investing.com per-symbol exponential backoff**

The Investing.com circuit breaker (`INVESTINY_CIRCUIT_BREAKER_TRIPS = 2`) causes per-symbol delays:

```
_investiny_retry_delay(error, attempt):
    if 403: return min(60.0, 8.0 * (2 ** (attempt - 1))) + random.uniform(0.0, 3.0)
    else:  return random.uniform(3.0, 8.0)
```

Per-symbol delay on 403:
- Attempt 1: ~8-11s
- Attempt 2: ~16-19s
- After 2 trips: circuit breaker opens → fallback

With 12 symbols, the worst case is 2 trips × ~15s per symbol = ~360s just for cooldowns before fallback kicks in.

**Comment in source (line 790):** "20+ minutes of per-symbol exponential backoff across 12 symbols."

The circuit breaker at 2 trips limits this, but with `INVESTINY_CIRCUIT_BREAKER_TRIPS = 2`, each symbol can accumulate ~25s of cooldown before falling back. If the circuit breaker trips early for some symbols but not others, and all 12 symbols encounter 403s before the breaker opens, the cumulative delay is: 12 symbols × 25s = ~300s of pure backoff time.

**Plus**: Each symbol fetch has a 20s HTTP timeout (`requests.get(..., timeout=20)`). If the server is slow, each symbol waits 20s before even getting a response.

**Worst case per symbol**: 20s (HTTP timeout) + 25s (backoff) = ~45s
**Worst case for 12 symbols**: 12 × 45s = **540s** — nearly the entire global timeout.

This is the EXACT cause of Branch B's 600s timeout.

## Branch A Slow Step

**`pgSQL_equities_auto.py` → `eastmoney.py` `fetch_and_save_all`**

- `PAGE_SIZE = 100` equities per page
- Per page: up to 5 retries × 3s sleep = 15s maximum
- If Eastmoney returns empty/no-data for many pages: 50+ pages × 15s = 750s+
- The `while not success and retries < 5` loop has NO upper bound on total fetch time
- **Critical**: Branch A used `& $Python script.py` (call operator) with NO `WaitForExit` — the process could run indefinitely

The exact slow step depends on Eastmoney's behavior on the host. If Eastmoney returned 403s or empty data for many pages, `pgSQL_equities_auto.py` would hang in the retry loop.

## A-Alone Runtime (estimated)

Cannot determine from Docker container — requires Windows host execution.

**Estimated based on code:**
- Best case (Eastmoney responsive): 60-120s (pgSQL + repair + backfill)
- Worst case (Eastmoney 403s): >600s (unbounded)
- With new fixes (Process.Start + WaitForExit(300s)): max 300s per script

## B-Alone Runtime (estimated)

Cannot determine from Docker container — requires Windows host execution.

**Estimated based on code:**
- Best case (Investing.com responsive): 60-90s
- Worst case (all 12 symbols hit 403s): 300-540s (circuit breaker + backoff)
- With macro_data_fetch.py's own `WaitForExit(MacroTimeoutSeconds=540)`: max 540s

## A+B Concurrent Runtime

726s observed (cycle_20260926_0441_HKT). Both branches ran in parallel as background PowerShell jobs. The global `Wait-Job -Timeout 600` killed both at 600s, then the orchestrator continued with remaining branches.

**Key finding**: If A alone takes 600s and B alone takes 540s, running them sequentially would take ~1140s. Running them in parallel hit 600s first on both. Serialization would not improve the total cycle time.

## Retry/Backoff Contribution

| Source | Max Retry | Per-Symbol Delay | Total Worst Case |
|--------|-----------|-----------------|-----------------|
| Eastmoney (Branch A) | 5 × 3s = 15s/page | 15s per page | 50 pages × 15s = 750s |
| Investing.com (Branch B) | 2 trips × ~25s = ~50s/symbol | 8-60s per attempt | 12 symbols × 50s = 600s |
| Combined with HTTP timeout | 20s per request | 20s per symbol | 12 × 20s = 240s |

## DB Contention Findings

- Both branches use `local_engine` with `pool_pre_ping=True, pool_recycle=3600`
- Default SQLAlchemy pool size = 5 connections
- Both branches run concurrently as background jobs
- **Potential issue**: Both branches write to overlapping `local_engine` connections. With default pool_size=5, if both branches open multiple connections (Eastmoney fetches + DB writes), they could exhaust the pool and wait for connections to be returned
- **Both branches write to the same local PostgreSQL instance**, so there could be lock contention on shared tables

**Cannot confirm** without inspecting PostgreSQL active sessions on the Windows host.

## Provider Circuit Breakers — Current State

| Provider | Circuit Breaker | Status |
|----------|----------------|--------|
| Investing.com (Branch B) | `INVESTINY_CIRCUIT_BREAKER_TRIPS = 2` | ✅ Present |
| Eastmoney (Branch A) | None | ❌ **Just added**: `MAX_NO_DATA_PAGES = 5`, `MAX_PAGE_RETRIES = 3` |
| Investing.com per-symbol | Exponential backoff | ✅ Present |

**Extension needed**: The Eastmoney circuit breaker only checks consecutive no-data pages. It does NOT check for HTTP 403/503 errors specifically. A provider-level 403 could cause all pages to fail with errors rather than empty data, which would bypass the circuit breaker.

## Proposed Per-Step Timeout Values

| Step | Proposed Timeout | Rationale |
|------|-----------------|-----------|
| `pgSQL_equities_auto.py` | 300s | 3 × 100s for Eastmoney with circuit breaker |
| `repair_historical_equity_gaps.py` | 300s | DB repair operations |
| `indicator_staged_backfill.py` | 300s | DB backfill |
| `macro_data_fetch.py` | 540s | 12 symbols × 45s worst case |
| `economic_data_fetch.py` | 120s | Stooq/AlphaVantage, fast |
| `news_fetch.py` | 120s | Finnhub, fast |

Total with parallel execution: max(300, 540, 120, ...) = 540s < 600s ✓

## Parallel vs Serialized

**Keep parallel.** Serialization would increase total cycle time to ~1140s+ (A: 540s + B: 540s = 1080s minimum). Parallel execution hit 600s first but both branches completed their data collection within the window.

The fix is to ensure each branch finishes within 600s individually, not to serialize them.

## Exact Code Changes Required (Summary)

Already applied in commit `8749d46`:

1. **`hourly_full_pipeline.ps1`**: 
   - `MacroTimeoutSeconds`: 900 → 540
   - Branch A: `& $Python` → `[System.Diagnostics.Process]::Start` + `WaitForExit(300000)`
   - Early exit from Branch A if `pgSQL_equities_auto.py` fails all 3 attempts

2. **`src/db_builder/eastmoney.py`**:
   - `MAX_PAGE_RETRIES = 3` (was inline `5`)
   - `MAX_NO_DATA_PAGES = 5` circuit breaker
   - Early abort after 5+ consecutive no-data pages

3. **`tests/test_rule_based_market_update_script.py`**:
   - `test_hourly_pipeline_ps1_macro_timeout_leq_overall`
   - `test_hourly_pipeline_ps1_branch_a_has_process_timeout`
   - `test_eastmoney_circuit_breaker_present`

## Requiring Host Execution (Cannot Determine from Container)

1. **A-alone runtime** — must run `scripts\run_hourly_pipeline_once.bat` with A-only execution
2. **B-alone runtime** — must run `scripts\run_hourly_pipeline_once.bat` with B-only execution
3. **A+B concurrent runtime** — must run full pipeline and measure
4. **PostgreSQL lock contention** — must inspect `pg_stat_activity` and `pg_locks` on Windows host
5. **Exact Eastmoney page count** — depends on total equities in the database
6. **Exact Investing.com symbol count** — depends on macro config


## Session-Aware Equity Refresh Architecture (Post-fix)

### Overview
After the provider-level circuit breaker and session-aware refresh fixes, the hourly pipeline's Branch A architecture is:

```
Branch A (hourly):
  pgSQL_equities_auto.py (single process, 300s timeout)
    ├── latest_completed_nyse_session_date() from trading_calendar
    ├── get_latest_stored_equity_session() → MAX(date) from us_equities
    ├── get_latest_indicator_session() → MAX(date) from us_equities_indicators
    ├── Decision matrix:
    │   ├── Case A (both current): SKIP, exit <30s
    │   ├── Case B (missing): fetch equities + indicators
    │   ├── Case C (prices current, indicators missing): indicators only
    │   └── Case D (fetch fails): provider fallback + breaker
    └── daily_bulk_sync_to_neon()

Removed from hourly Branch A:
  - repair_historical_equity_gaps.py (moved to maintenance workflow)
  - indicator_staged_backfill.py (superseded by pgSQL_equities_auto.py)

Historical Gap Repair (separate maintenance):
  - daily off-hours or weekly deep repair
  - Identify historical gaps
  - Repair them
  - Validate completeness
  - Update health status
```

### Branch A Decision Matrix

| Case | Prices | Indicators | Action | Expected Runtime |
|------|--------|-----------|--------|-----------------|
| A | Current | Current | Skip everything | <30s |
| B | Missing | Missing | Fetch + indicators | <300s |
| C | Current | Missing | Indicators only | <60s |
| D | Fetch fails | — | Provider fallback + breaker | <300s |

### Trading Calendar Source
- `latest_completed_nyse_session_date()` from `db_builder.trading_calendar`
- Uses `pandas_market_calendars` NYSE calendar
- Handles weekends, holidays, early closes
- Timezone-aware: HKT/NY difference handled via `astimezone(timezone.utc)`

### Cycle Metadata Fields
- `latest_completed_equity_session`: NYSE trading calendar's latest session
- `latest_stored_equity_session`: DB MAX(date) from raw equities table
- `latest_indicator_session`: DB MAX(date) from indicators table
- `equity_refresh_status`: `UPDATED` or `SKIPPED_ALREADY_CURRENT`
- `indicator_refresh_status`: `UPDATED` or `SKIPPED_ALREADY_CURRENT`

### Healthy State Definition
`SKIPPED_ALREADY_CURRENT` is a **successful** state, NOT degradation.
It means the data is already current and no work is needed.
