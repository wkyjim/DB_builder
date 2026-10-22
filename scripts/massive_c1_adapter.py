"""Phase C1: Isolated Massive adapter for read-only dry-run validation.

Supports:
- Grouped US Stocks Daily endpoint (ETFs)
- Grouped FX Daily endpoint
- Grouped Crypto Daily endpoint

Rate limiting: 12s between requests (conservative 5 rpm).
No production routing changes. No database writes. No Neon access.
"""
import os
import sys
import time
import httpx
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

# Production environment loading (read-only)
DB_BUILDER_ROOT = Path(r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder")
sys.path.insert(0, str(DB_BUILDER_ROOT / "src"))
from db_builder.config import local_engine  # noqa: E402 — triggers load_external_env()

API_KEY = os.getenv("MASSIVE_API_KEY", "")
if not API_KEY:
    raise RuntimeError("MASSIVE_API_KEY not configured")

# ============================================================
# Canonical-to-Massive ticker mapping (20 Phase C1 symbols)
# ============================================================
C1_MAPPING: Dict[str, Tuple[str, str]] = {
    # FX pairs: Hermes -> (Massive ticker, asset_class)
    "AUDUSD=X": ("C:AUDUSD", "FX"),
    "CHFUSD=X": ("C:CHFUSD", "FX"),
    "CNY=X": ("C:USDCNY", "FX"),
    "EURUSD=X": ("C:EURUSD", "FX"),
    "GBPUSD=X": ("C:GBPUSD", "FX"),
    "HKD=X": ("C:USDHKD", "FX"),
    "JPY=X": ("C:USDJPY", "FX"),
    "NZDUSD=X": ("C:NZDUSD", "FX"),
    "SGD=X": ("C:USDSGD", "FX"),
    # Crypto: Hermes -> (Massive ticker, asset_class)
    "BTC-USD": ("X:BTCUSD", "Crypto"),
    "ETH-USD": ("X:ETHUSD", "Crypto"),
    # ETFs: Hermes -> (Massive ticker, asset_class)
    # Massive Stocks grouped returns base tickers (e.g. HYG), not .US-suffixed
    "HYG": ("HYG", "ETF"),
    "IEF": ("IEF", "ETF"),
    "IWD": ("IWD", "ETF"),
    "IWF": ("IWF", "ETF"),
    "JNK": ("JNK", "ETF"),
    "LQD": ("LQD", "ETF"),
    "RSP": ("RSP", "ETF"),
    "SHY": ("SHY", "ETF"),
    "TLT": ("TLT", "ETF"),
}

# Reverse mapping: Massive ticker -> Hermes symbol
MASSIVE_TO_HERMES: Dict[str, str] = {v: k for k, v in
    [(v[0], sym) for sym, v in C1_MAPPING.items()]}

# FX pair orientation validation
FX_ORIENTATION: Dict[str, str] = {
    "AUDUSD=X": "AUD/USD",
    "CHFUSD=X": "CHF/USD",
    "CNY=X": "USD/CNY",
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "HKD=X": "USD/HKD",
    "JPY=X": "USD/JPY",
    "NZDUSD=X": "NZD/USD",
    "SGD=X": "USD/SGD",
}

# ============================================================
# Rate limiter
# ============================================================
MIN_REQUEST_INTERVAL = 12.0  # seconds between request starts (5 rpm)
_last_request_time = 0.0
_rate_limit_hits = 0
_retry_count = 0
_api_call_count = 0


def _enforce_rate_limit():
    """Enforce >= 12s between request starts."""
    global _last_request_time, _rate_limit_hits
    now = time.monotonic()
    elapsed = now - _last_request_time
    if _last_request_time > 0 and elapsed < MIN_REQUEST_INTERVAL:
        sleep_time = MIN_REQUEST_INTERVAL - elapsed
        _rate_limit_hits += 1
        time.sleep(sleep_time)
    _last_request_time = time.monotonic()


def massive_get(endpoint: str, params: Optional[Dict] = None, max_retries: int = 2) -> Dict:
    """Make a Massive API call with rate limiting and bounded retries."""
    global _api_call_count, _retry_count
    if params is None:
        params = {}
    params["apiKey"] = API_KEY

    for attempt in range(max_retries + 1):
        _enforce_rate_limit()
        _api_call_count += 1
        try:
            resp = httpx.get(
                f"https://api.polygon.io{endpoint}",
                params=params,
                timeout=30,
            )
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "15"))
                _retry_count += 1
                print(f"[RATE LIMITED] Retry-After={retry_after}s")
                time.sleep(retry_after)
                continue
            if resp.status_code != 200:
                _retry_count += 1
                print(f"[API ERROR] {resp.status_code}: {resp.text[:200]}")
                if attempt < max_retries:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Massive API error: {resp.status_code}")
            return resp.json()
        except httpx.TimeoutException:
            _retry_count += 1
            print(f"[TIMEOUT] attempt {attempt + 1}/{max_retries + 1}")
            if attempt < max_retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError("Max retries exceeded")


def fetch_grouped_daily(market: str, target_date: str) -> Dict[str, Any]:
    """Fetch grouped daily data for stocks, FX, or crypto.

    market: 'stocks', 'fx', or 'crypto'
    target_date: YYYY-MM-DD
    """
    endpoint = f"/v2/aggs/grouped/locale/global/market/{market}/{target_date}"
    return massive_get(endpoint)


def normalize_grouped_row(row: Dict, massive_ticker: str) -> Dict[str, Any]:
    """Normalize a Massive grouped row to Hermes schema.

    Returns dict with: open, high, low, close, volume, date, symbol
    """
    return {
        "date": None,  # Will be set from grouped endpoint date
        "symbol": MASSIVE_TO_HERMES.get(massive_ticker, massive_ticker),
        "open": row.get("o"),
        "high": row.get("h"),
        "low": row.get("l"),
        "close": row.get("c"),
        "volume": row.get("v"),
        "adj_close": row.get("c"),  # Daily close as adj close
        "massive_ticker": massive_ticker,
    }


def validate_ohlc(row: Dict) -> Tuple[bool, Optional[str]]:
    """Validate OHLC values are non-null, finite, and consistent."""
    for key in ["open", "high", "low", "close"]:
        val = row.get(key)
        if val is None:
            return False, f"Missing {key}"
        if not isinstance(val, (int, float)):
            return False, f"Invalid {key} type: {type(val)}"
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    if h < l:
        return False, f"high ({h}) < low ({l})"
    if h < max(o, c):
        return False, f"high ({h}) < max(open={o}, close={c})"
    if l > min(o, c):
        return False, f"low ({l}) > min(open={o}, close={c})"
    return True, None


def detect_missing_symbols(results: List[Dict], expected_tickers: List[str]) -> List[str]:
    """Detect missing symbols in grouped response."""
    present = {r.get("T") for r in results}
    return [t for t in expected_tickers if t not in present]


def detect_duplicate_tickers(results: List[Dict]) -> List[str]:
    """Detect duplicate tickers in grouped response."""
    seen = {}
    duplicates = []
    for r in results:
        t = r.get("T")
        if t in seen:
            duplicates.append(t)
        seen[t] = True
    return sorted(set(duplicates))


def validate_fx_orientation(hermes_symbol: str, massive_ticker: str) -> Tuple[bool, str]:
    """Validate FX pair orientation matches between Hermes and Massive.

    JPY=X -> C:USDJPY means USD/JPY — same pair.
    CNY=X -> C:USDCNY means USD/CNY — same pair.
    """
    expected_pair = FX_ORIENTATION.get(hermes_symbol)
    if not expected_pair:
        return True, "No orientation mapping (not FX)"

    base, quote = expected_pair.split("/")
    # Massive ticker format: C:USDJPY -> base=USD, quote=JPY
    massive_base = massive_ticker[2:5] if massive_ticker.startswith("C:") else ""
    massive_quote = massive_ticker[5:] if massive_ticker.startswith("C:") else ""

    if massive_base == base and massive_quote == quote:
        return True, "OK"
    return False, f"Expected {base}/{quote}, got {massive_base}/{massive_quote}"


@dataclass
class DryRunResult:
    """Result of a single Phase C1 dry-run."""
    symbols_fetched: List[str] = field(default_factory=list)
    mapping_errors: List[Tuple[str, str]] = field(default_factory=list)
    validation_failures: List[Tuple[str, str]] = field(default_factory=list)
    missing_symbols: List[str] = field(default_factory=list)
    duplicate_tickers: List[str] = field(default_factory=list)
    fx_orientation_issues: List[Tuple[str, str]] = field(default_factory=list)
    api_calls: int = 0
    rate_limit_hits: int = 0
    retries: int = 0
    rows_by_symbol: Dict[str, List[Dict]] = field(default_factory=dict)
    grouped_responses: Dict[str, Any] = field(default_factory=dict)


def run_c1_dry_run(target_date: str = "2026-10-14") -> DryRunResult:
    """Execute Phase C1 dry-run: fetch, normalize, validate 20 symbols."""
    global _api_call_count, _rate_limit_hits, _retry_count

    result = DryRunResult()
    _api_call_count = 0
    _rate_limit_hits = 0
    _retry_count = 0

    # Separate symbols by asset class
    fx_syms = [s for s, v in C1_MAPPING.items() if v[1] == "FX"]
    crypto_syms = [s for s, v in C1_MAPPING.items() if v[1] == "Crypto"]
    etf_syms = [s for s, v in C1_MAPPING.items() if v[1] == "ETF"]

    # Fetch grouped data
    grouped_configs = [
        ("fx", fx_syms, "FX_GROUPED"),
        ("crypto", crypto_syms, "CRYPTO_GROUPED"),
        ("stocks", etf_syms, "STOCKS_GROUPED"),
    ]

    for market, symbols, label in grouped_configs:
        massive_tickers = [C1_MAPPING[s][0] for s in symbols]
        print(f"\n[{label}] Fetching {len(symbols)} symbols for {target_date}")

        try:
            data = fetch_grouped_daily(market, target_date)
            results = data.get("results", [])
            result.grouped_responses[label] = data

            # Check for duplicates
            dupes = detect_duplicate_tickers(results)
            if dupes:
                result.duplicate_tickers.extend(dupes)

            # Check for missing
            missing = detect_missing_symbols(results, massive_tickers)
            if missing:
                for m in missing:
                    hermes = MASSIVE_TO_HERMES.get(m, m)
                    result.missing_symbols.append(hermes)
                    result.mapping_errors.append((hermes, f"Massive ticker {m} missing from response"))

            # Normalize and validate each row
            for r in results:
                ticker = r.get("T")
                hermes = MASSIVE_TO_HERMES.get(ticker)
                if not hermes:
                    continue

                row = normalize_grouped_row(r, ticker)
                row["date"] = target_date

                # FX orientation validation
                if hermes in FX_ORIENTATION:
                    ok, msg = validate_fx_orientation(hermes, ticker)
                    if not ok:
                        result.fx_orientation_issues.append((hermes, msg))

                # OHLC validation
                valid, err = validate_ohlc(row)
                if not valid:
                    result.validation_failures.append((hermes, f"Invalid OHLC: {err}"))

                result.rows_by_symbol.setdefault(hermes, []).append(row)

            # Only count symbols that were found
            found = {r.get("T") for r in results}
            for s in symbols:
                mt = C1_MAPPING[s][0]
                if mt in found:
                    result.symbols_fetched.append(s)

        except Exception as e:
            print(f"[{label}] FAILED: {type(e).__name__}: {e}")
            for s in symbols:
                result.mapping_errors.append((s, f"Fetch failed: {e}"))

    result.api_calls = _api_call_count
    result.rate_limit_hits = _rate_limit_hits
    result.retries = _retry_count
    return result


def print_comparison_table(result: DryRunResult, local_baseline: Dict[str, List[Dict]]):
    """Print comparison table (read-only)."""
    print("\n" + "=" * 100)
    print("PHASE C1 COMPARISON TABLE (READ-ONLY)")
    print("=" * 100)
    print(f"{'Symbol':<12} {'Massive Ticker':<12} {'Asset':<6} {'Local Date':<12} {'Massive Date':<12} "
          f"{'Local Close':<12} {'Massive Close':<12} {'Abs Diff':<10} {'Rel Diff':<10} {'Status':<10}")
    print("-" * 100)

    matched = 0
    for sym in sorted(C1_MAPPING.keys()):
        massive_ticker, asset = C1_MAPPING[sym]
        local_rows = local_baseline.get(sym, [])
        massive_rows = result.rows_by_symbol.get(sym, [])

        if not local_rows or not massive_rows:
            print(f"{sym:<12} {massive_ticker:<12} {asset:<6} {'N/A':<12} {'N/A':<12} "
                  f"{'N/A':<12} {'N/A':<12} {'N/A':<10} {'N/A':<10} {'MISSING':<10}")
            continue

        local_row = local_rows[-1]  # Latest local
        massive_row = massive_rows[0]

        local_close = local_row.get("close")
        massive_close = massive_row.get("close")
        local_date = local_row.get("date", "?")
        massive_date = massive_row.get("date", "?")

        if local_close and massive_close:
            abs_diff = abs(local_close - massive_close)
            rel_diff = (abs_diff / local_close) * 100 if local_close else 0
            status = "MATCH" if rel_diff < 1.0 else "DIFF"
            if status == "MATCH":
                matched += 1
            print(f"{sym:<12} {massive_ticker:<12} {asset:<6} {str(local_date):<12} {str(massive_date):<12} "
                  f"{local_close:<12.4f} {massive_close:<12.4f} {abs_diff:<10.4f} {rel_diff:<10.2f}% {status:<10}")
        else:
            print(f"{sym:<12} {massive_ticker:<12} {asset:<6} {str(local_date):<12} {str(massive_date):<12} "
                  f"{'N/A':<12} {'N/A':<12} {'N/A':<10} {'N/A':<10} {'ERROR':<10}")

    print(f"\nMatched: {matched}/20 (within 1% tolerance)")


def load_local_baseline(symbols: List[str], start_date: str, end_date: str) -> Dict[str, List[Dict]]:
    """Load Local baseline for comparison (read-only)."""
    from sqlalchemy import text
    conn = local_engine().connect()
    result = conn.execute(
        text("""
            SELECT symbol, date, open, high, low, close, volume
            FROM public.macro
            WHERE symbol = ANY(:symbols)
              AND date BETWEEN :start AND :end
            ORDER BY symbol, date
        """),
        {"symbols": symbols, "start": start_date, "end": end_date},
    )

    baseline: Dict[str, List[Dict]] = {}
    for r in result:
        sym = r[0]
        baseline.setdefault(sym, []).append({
            "date": str(r[1]),
            "open": float(r[2]) if r[2] else None,
            "high": float(r[3]) if r[3] else None,
            "low": float(r[4]) if r[4] else None,
            "close": float(r[5]) if r[5] else None,
            "volume": float(r[6]) if r[6] else None,
        })
    return baseline


def main():
    target_date = "2026-10-14"
    print(f"Phase C1 Dry-Run: {target_date}")
    print(f"Target: 20 symbols across 3 grouped endpoints")

    # Load local baseline
    symbols = list(C1_MAPPING.keys())
    local_baseline = load_local_baseline(symbols, "2026-10-10", "2026-10-17")

    # Run dry-run
    result = run_c1_dry_run(target_date)

    # Print results
    print("\n" + "=" * 80)
    print("DRY-RUN RESULTS")
    print("=" * 80)
    print(f"Symbols fetched: {len(result.symbols_fetched)}/20")
    print(f"Mapping errors: {len(result.mapping_errors)}")
    print(f"Validation failures: {len(result.validation_failures)}")
    print(f"Missing symbols: {result.missing_symbols}")
    print(f"Duplicate tickers: {result.duplicate_tickers}")
    print(f"FX orientation issues: {len(result.fx_orientation_issues)}")
    print(f"API calls: {result.api_calls}")
    print(f"Rate limit hits: {result.rate_limit_hits}")
    print(f"Retries: {result.retries}")

    # Comparison table
    print_comparison_table(result, local_baseline)

    # Final verdict
    print("\n" + "=" * 80)
    print("FINAL VERDICT")
    print("=" * 80)
    if len(result.symbols_fetched) == 20 and not result.mapping_errors:
        print("PHASE_C1_READY = YES")
    else:
        print("PHASE_C1_READY = NO")
        if result.mapping_errors:
            print(f"  Blocking errors: {result.mapping_errors[:3]}")

    return result


if __name__ == "__main__":
    main()
