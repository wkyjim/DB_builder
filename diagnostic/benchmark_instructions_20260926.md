# Branch A/B Benchmark Instructions — Must Run on Windows Host

## Prerequisites
Run these commands on the Windows host (C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder):

## 1. Branch A Alone — Session-Aware
Run with the session-aware pgSQL_equities_auto.py:
```
cd scripts\..
python scripts\pgSQL_equities_auto.py --dry-run
```

**Already-Current Benchmark** (typical hourly run):
When the latest completed NYSE session is already stored and indicators are current:
```
python scripts\pgSQL_equities_auto.py
```
Expected: exits in <30s with `[SKIP] Equity prices and indicators already current`
Record per-step timing from stdout metadata.

**New-Session Benchmark**:
Simulate by clearing one day's data or using `--force-refresh`:
```
python scripts\pgSQL_equities_auto.py --force-refresh
```
Expected: fetches equity prices + indicators, <300s

**Measure each step**:
- Start: Get-Date
- End: Get-Date  
- Runtime: (End - Start).TotalSeconds

## 2. Branch B Alone
Run macro_data_fetch.py directly:
```
cd scripts\..
python scripts\macro_data_fetch.py --dry-run --symbols BZ=F,CL=F
```

Or use the full pipeline with only macro symbols:
```
scripts\run_hourly_pipeline_once.bat -DryRun -MacroOnly
```

## 3. A + B Concurrent
Run the full orchestrator:
```
scripts\run_hourly_pipeline_once.bat -DryRun
```

Use PowerShell timing:
```powershell
$sw = [System.Diagnostics.Stopwatch]::StartNew()
# ... run orchestrator ...
$sw.Stop()
Write-Host "Total: $($sw.Elapsed.TotalSeconds)s"
```

## 4. PostgreSQL Contention
During concurrent test, on the Windows host PostgreSQL:
```
psql -U postgres -c "SELECT pid, state, query, now() - pg_stat_activity.query_start AS duration FROM pg_stat_activity WHERE state != 'idle';"
psql -U postgres -c "SELECT * FROM pg_locks WHERE granted = false;"
psql -U postgres -c "SELECT count(*) FROM pg_stat_activity;"
```

## 5. Key Metrics to Record

### Branch A — Session-Aware (Already Current)
| Metric | Target | Notes |
|--------|--------|-------|
| pgSQL_equities_auto.py | <30s | SKIP path when sessions current |
| Total Branch A | <30s | No fetch needed |
| Equity refresh status | SKIPPED_ALREADY_CURRENT | Healthy state |
| Indicator refresh status | SKIPPED_ALREADY_CURRENT | Healthy state |

### Branch A — Session-Aware (New Session)
| Metric | Target | Notes |
|--------|--------|-------|
| pgSQL_equities_auto.py | <300s | Fetch + indicators |
| Total Branch A | <300s | Single process |
| Equity refresh status | UPDATED | Fetched |
| Indicator refresh status | UPDATED | Calculated |

### Branch B
| Step | Expected | Before Fix |
|------|----------|------------|
| macro_data_fetch.py | 30-90s | 540-600s |
| Total Branch B | ~50-150s | 540-600s |

### A + B Concurrent
- Expected wall-clock: max(A, B) = ~30-150s
- Before fix: 600s (global timeout)

## 6. Expected Breaker Behavior

### Investing.com (Branch B)
- If provider returns 403: breaker opens after 1 symbol
- Remaining 11 symbols route to fallback immediately
- Total blocked time: ~25s (1 symbol retry) + ~2s (fallback) = ~27s
- Before fix: 12 symbols × 25s cooldown = 300s+

### Eastmoney (Branch A)
- If provider returns HTTP 403/429/5xx: breaker opens after 3 errors
- Returns [] immediately without iterating remaining pages
- Total blocked time: ~3 × (20s HTTP timeout + 3s sleep) = ~69s
- Before fix: 50+ pages × 15s = 750s+

## 7. Verdict Criteria

PASS if:
- A-alone (already current) < 30s
- A-alone (new session) < 300s
- B-alone < 150s
- A+B concurrent < 200s (well under 600s)
- Provider breaker triggers within 1-2 symbols
- No orphan processes after timeout
- PostgreSQL shows no blocking/contention
- Session metadata correctly reports SKIPPED_ALREADY_CURRENT

FAIL if:
- A-alone > 600s
- B-alone > 600s
- A+B concurrent > 600s
- Provider breaker does NOT trigger
- Orphan processes remain after timeout
- PostgreSQL shows significant blocking
- Session metadata incorrectly reports DEGRADED

## 8. Diagnostic Commands

After running benchmarks, check:
# Process cleanup
Get-Process | Where-Object { $_.Name -like "*python*" } | Select-Object Id, ProcessName, StartTime, CPU

# PowerShell jobs
Get-Job | Select-Object Id, Name, State, HasMoreData

# PostgreSQL
psql -U postgres -c "SELECT datname, state, query_start, now() - query_start AS duration FROM pg_stat_activity;"

# Verify session metadata in pgSQL_equities_auto.py output
# Look for: [METADATA] {latest_completed_equity_session, latest_stored_equity_session, ...}


## Exact Windows Command to Rerun Branch A

On the Windows host, after applying all fixes:

```powershell
cd C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\scripts
.\run_hourly_pipeline_once.bat -DryRun
```

For a live Branch A run:
```powershell
.\run_hourly_pipeline_once.bat
```

To test the new-session scenario (simulate missing data):
```powershell
python C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe pgSQL_equities_auto.py --force-refresh
```

## Verification Checklist

After running, check:
1. [ ] Output contains `[METADATA]` with `equity_refresh_status` and `indicator_refresh_status`
2. [ ] If already-current: `equity_refresh_status = SKIPPED_ALREADY_CURRENT`, runtime < 30s
3. [ ] If new session: `equity_refresh_status = UPDATED`, runtime < 300s
4. [ ] If Eastmoney 502: `[EASTMONEY] Initial request HTTP 502 — provider breaker opened`
5. [ ] `[FALLBACK] Latest session <date> stored via yfinance fallback`
6. [ ] `[INDICATORS] Processing <date>`
7. [ ] `[SUCCESS] Equity session current`
8. [ ] No `RuntimeError("Initial Eastmoney request failed")` in output
9. [ ] No orphan Python processes after completion
10. [ ] PostgreSQL shows no blocking sessions
