# Branch A/B Runtime Bottleneck Diagnosis — cycle 20260926_0441_HKT

## Summary

Branch A (Equities) and Branch B (Macro) both timed out at the 600-second global `$OverallTimeoutSeconds`. Branches C/D/E completed normally (102s–270s).

## Root Cause 1: Branch A — No Per-Script Timeout

**File:** `scripts/hourly_full_pipeline.ps1`, lines 155-164

Branch A invokes three Python scripts sequentially with **zero per-script timeout**:

```powershell
# Lines 156-160: 3 attempts with 60s gaps, NO timeout on process
for ($attempt = 1; $attempt -le 3; $attempt++) {
    & $Python (Join-Path $Root 'scripts\pgSQL_equities_auto.py') 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $eqOk = $true; break }
    if ($attempt -lt 3) { Start-Sleep -Seconds 60 }
}
$r2 = & $Python (Join-Path $Root 'scripts\repair_historical_equity_gaps.py') 2>&1  # NO TIMEOUT
$r3 = & $Python (Join-Path $Root 'scripts\indicator_staged_backfill.py') --stage all 2>&1  # NO TIMEOUT
```

The `& $Python ...` call operator waits indefinitely — no `WaitForExit`, no `Process` object, no cancellation token. If `pgSQL_equities_auto.py` hangs inside Eastmoney retries, the entire branch hangs.

## Root Cause 2: Eastmoney Per-Page Retry Accumulation

**File:** `src/db_builder/eastmoney.py`, lines 193-202

```python
while not success and retries < 5:
    try:
        resp = requests.get(get_api_url(pn, pz), headers=headers, timeout=20)
        resp.raise_for_status()
        stocks = resp.json().get("data", {}).get("diff", [])
        if not stocks:
            print(f"[page {pn}] no data; retrying")
            retries += 1
            time.sleep(3)
            continue
        ...
    except Exception:
        retries += 1
        time.sleep(3)
```

- **PAGE_SIZE = 100** equities per page
- **Up to 5 retries × 3s sleep = 15s per page maximum**
- **Total equities could be thousands → 50+ pages**
- **Worst case: 50 pages × 15s = 750s** just for Eastmoney alone
- **Plus**: random delays (`time.sleep(random.uniform(0.6, 1.5))`) between pages
- **Plus**: connection-interrupted backoff (`time.sleep(wait_time)`) on network issues
- **Plus**: 3 attempts of the entire loop in Branch A = up to **2,250s** (37.5 min)

## Root Cause 3: MacroTimeoutSeconds > OverallTimeoutSeconds

**File:** `scripts/hourly_full_pipeline.ps1`, line 37

```powershell
[int]$MacroTimeoutSeconds = 900
```

vs. global:

```powershell
[int]$OverallTimeoutSeconds = 600  # line 260
```

Branch B's `WaitForExit($MacroTimeoutSeconds * 1000)` would wait 900s — but the global `Wait-Job -Job $Jobs -Timeout 600` kills all jobs at 600s anyway. **The 900s timeout is dead code.** Branch B never gets the full 900s.

## Root Cause 4: Branch A Retry Logic is Cumulative, Not Per-Attempt

Branch A retries `pgSQL_equities_auto.py` 3 times with 60s gaps = **up to 180s of retry sleep alone**. Each retry calls the full Eastmoney fetch-and-save with potentially 50+ pages. If Eastmoney is returning empty/no-data responses, each attempt takes minutes, and the 3 attempts × 60s sleep = 3+ minutes of overhead before the next step even runs.

## Branch Breakdown

| Branch | Script(s) | Timeout | Observed | Issue |
|--------|-----------|---------|----------|-------|
| A_equities | pgSQL_equities_auto.py (×3 attempts), repair_historical_equity_gaps.py, indicator_staged_backfill.py | NONE | TIMEOUT 600s | Eastmoney per-page retries; no per-script timeout; 3× cumulative retries |
| B_macro | macro_data_fetch.py | 900s (dead) | TIMEOUT 600s | $MacroTimeoutSeconds > $OverallTimeoutSeconds |
| C_economic | economic_data_fetch.py, global_economic_data_fetch.py | NONE | 166s OK | No issue |
| D_finra | finra_short_incremental.py, short_analytics_latest_sync.py | NONE | 270s OK | No issue |
| E_news | news_fetch, news_classify, news_signals | NONE | 102s OK | No issue |

## Required Fixes

1. **Add per-script timeout to Branch A** using `[System.Diagnostics.Process]::Start()` with `WaitForExit()` like Branch B does
2. **Fix MacroTimeoutSeconds to ≤ $OverallTimeoutSeconds** (set to 300 or 540)
3. **Add Eastmoney provider-level circuit breaker** — if >N pages return no data across all pages, abort early and mark degraded
4. **Add per-symbol timeout to Eastmoney retries** — cap total retry time per page
5. **Reduce Branch A retry count from 3 to 2** or reduce 60s sleep to 30s
