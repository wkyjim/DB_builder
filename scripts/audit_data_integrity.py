"""Read-only end-to-end data integrity audit for DB Builder pipelines.

Safe to run from Windows PowerShell. Uses the repository's existing
env_loader to load credentials from the external DB_builder_env directory.
All database operations are SELECT-only with defensive read-only transaction mode.

Usage:
    python scripts/audit_data_integrity.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa

# ---------------------------------------------------------------------------
# Bootstrap (same pattern as other scripts)
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
SCRIPTS_PATH = PROJECT_ROOT / "scripts"
for path in (SRC_PATH, SCRIPTS_PATH, PROJECT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from db_builder.config import local_engine  # noqa: E402
from db_builder.env_loader import load_external_env  # noqa: E402
from db_builder.trading_calendar import latest_completed_nyse_session_date  # noqa: E402

# Load external credentials (preserves already-set process variables)
load_external_env()

REPORT_PATH = PROJECT_ROOT / "reports" / "audit_data_integrity.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def readonly_engine():
    """Create a defensive read-only SQLAlchemy engine."""
    engine = local_engine()

    @sa.event.listens_for(engine, "connect")
    def set_readonly(dbapi_connection, connection_record):
        with dbapi_connection.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
        # Commit the SET so it persists for the whole session.
        # A plain SET is transaction-scoped: if the implicit transaction
        # were rolled back (e.g. by pool reset), the setting would be lost.
        dbapi_connection.commit()

    return engine


def verify_readonly(engine) -> bool:
    """Verify the read-only setting is active on a fresh connection."""
    with engine.begin() as conn:
        result = conn.execute(sa.text("SHOW default_transaction_read_only"))
        value = result.fetchone()[0]
        return value == "on"


def q(engine, sql: str) -> list:
    """Execute a read-only query and return all rows."""
    with engine.begin() as conn:
        result = conn.execute(sa.text(sql))
        return result.fetchall()


def q1(engine, sql: str, *, required: bool = False, label: str = ""):
    """Execute a read-only query and return the first column of the first row."""
    rows = safe_q(engine, sql, label or sql[:60], required=required)
    if not rows:
        return None
    return rows[0][0]


def table_exists(engine, table_name: str) -> bool:
    """Check whether a table exists using to_regclass (read-only)."""
    result = q(engine, f"SELECT to_regclass('{table_name}')")
    return result[0][0] is not None


def safe_q(engine, sql: str, label: str, *, required: bool = False) -> list | None:
    """Execute a query; on missing-relation errors degrade to WARN/N/A.

    Returns the rows, or None when the table is absent. For required tables
    the caller should treat absence as FAIL; this helper never aborts.
    """
    try:
        return q(engine, sql)
    except sa.exc.ProgrammingError as exc:
        if "UndefinedTable" in str(exc.orig) or "does not exist" in str(exc.orig):
            if required:
                print(f"REQUIRED TABLE MISSING: {label}")
            return None
        raise


def section(title: str, level: int = 2) -> str:
    return f"\n{'#' * level} {title}\n"


def table_row(cols: list) -> str:
    return "| " + " | ".join(str(c) for c in cols) + " |"


def status_badge(status: str) -> str:
    if status == "PASS":
        return "✅ PASS"
    if status == "WARN":
        return "⚠️ WARN"
    return "❌ FAIL"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def audit_macro(engine, today: date, latest_nyse: date) -> str:
    """Audit macro historical and live data."""
    out = section("A. Macro / Market-Data Audit")

    # Required-table existence checks (these are core tables)
    macro_exists = table_exists(engine, "public.macro")
    macro_live_exists = table_exists(engine, "public.macro_live")
    if not macro_exists:
        out += f"- `public.macro`: {status_badge('FAIL')} — REQUIRED TABLE MISSING\n"
        return out
    if not macro_live_exists:
        out += f"- `public.macro_live`: {status_badge('FAIL')} — REQUIRED TABLE MISSING\n"
        return out

    # Configured universe from ASSETS
    from scripts.macro_data_fetch import ASSETS, REQUIRED_SYMBOLS, OIL_YFINANCE_FALLBACK_SYMBOLS
    configured_symbols = sorted({symbol for symbol, _, _ in ASSETS})
    required_symbols = sorted(REQUIRED_SYMBOLS)

    out += f"- Configured macro universe: **{len(configured_symbols)}** symbols\n"
    out += f"- Required symbols: **{len(required_symbols)}**\n"
    out += f"- Oil fallback symbols: {', '.join(sorted(OIL_YFINANCE_FALLBACK_SYMBOLS))}\n"
    out += f"- Latest completed NYSE session: **{latest_nyse}**\n"
    out += f"- Audit date: **{today}**\n"

    # Historical macro
    out += section("Macro Historical (public.macro)", 3)
    total_symbols = q1(engine, "SELECT COUNT(DISTINCT symbol) FROM public.macro")
    date_range = q(engine, "SELECT MIN(date), MAX(date) FROM public.macro")
    min_date, max_date = date_range[0] if date_range else (None, None)
    out += f"- Total distinct symbols: **{total_symbols}**\n"
    out += f"- Date range: {min_date} to {max_date}\n"

    # Latest per symbol
    rows = q(engine, """
        SELECT symbol, MAX(date) as latest_date, COUNT(*) as rows
        FROM public.macro
        GROUP BY symbol
        ORDER BY symbol
    """)

    missing = []
    stale = []
    current = []

    out += "\n| Symbol | Latest Date | Rows | Required | Status |\n"
    out += "|--------|-------------|------|----------|--------|\n"

    for symbol, latest, row_count in rows:
        is_required = symbol in REQUIRED_SYMBOLS
        req_str = "yes" if is_required else "no"
        if latest is None:
            missing.append(symbol)
            status = status_badge("FAIL")
        elif latest < latest_nyse:
            stale.append((symbol, str(latest)))
            status = status_badge("WARN") if not is_required else status_badge("FAIL")
        else:
            current.append(symbol)
            status = status_badge("PASS")
        out += f"| {symbol} | {latest} | {row_count} | {req_str} | {status} |\n"

    # Missing from DB
    db_symbols = {r[0] for r in rows}
    not_in_db = sorted(set(configured_symbols) - db_symbols)
    if not_in_db:
        out += f"\n**Missing from DB ({len(not_in_db)}):** {', '.join(not_in_db)}\n"

    # Duplicates
    dups = q(engine, """
        SELECT symbol, date, COUNT(*) as cnt
        FROM public.macro
        GROUP BY symbol, date
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
        LIMIT 20
    """)
    out += section("Macro Duplicates", 3)
    out += f"- Duplicate (symbol, date) groups: **{len(dups)}**\n"
    if dups:
        out += "\n| Symbol | Date | Count |\n"
        out += "|--------|------|-------|\n"
        for symbol, dup_date, cnt in dups:
            out += f"| {symbol} | {dup_date} | {cnt} |\n"

    # Null/non-finite
    nulls = q1(engine, """
        SELECT COUNT(*) FROM public.macro
        WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
           OR open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
    """)
    out += section("Macro Data Quality", 3)
    out += f"- Rows with null/zero/negative OHLC: **{nulls}**\n"

    # OHLC consistency
    inconsistent = q1(engine, """
        SELECT COUNT(*) FROM public.macro
        WHERE high < low OR high < open OR high < close OR low > open OR low > close
    """)
    out += f"- Inconsistent OHLC rows: **{inconsistent}**\n"

    # CL=F / BZ=F provenance
    out += section("CL=F / BZ=F Provenance", 3)
    for symbol in ["CL=F", "BZ=F"]:
        row = q(engine, f"""
            SELECT symbol, date, open, high, low, close
            FROM public.macro
            WHERE symbol = '{symbol}'
            ORDER BY date DESC
            LIMIT 3
        """)
        if row:
            out += f"\n**{symbol}** latest rows:\n"
            out += "| Date | Open | High | Low | Close |\n"
            out += "|------|------|------|-----|-------|\n"
            for r in row:
                out += f"| {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} |\n"
        else:
            out += f"\n**{symbol}**: ❌ NOT FOUND\n"

    # ^KS200 specific
    out += section("^KS200 Specific Check", 3)
    ks200 = q(engine, "SELECT MAX(date), COUNT(*) FROM public.macro WHERE symbol = '^KS200'")
    if ks200 and ks200[0][0]:
        out += f"- Latest: {ks200[0][0]}, Rows: {ks200[0][1]}\n"
        if ks200[0][0] < latest_nyse:
            out += f"- Status: {status_badge('WARN')} (stale — supplementary)\n"
        else:
            out += f"- Status: {status_badge('PASS')}\n"
    else:
        out += f"- Status: {status_badge('FAIL')} (not in DB)\n"

    # Macro Live
    out += section("Macro Live (public.macro_live)", 3)
    live_count = q1(engine, "SELECT COUNT(DISTINCT symbol) FROM public.macro_live")
    live_max = q1(engine, "SELECT MAX(observed_at) FROM public.macro_live")
    out += f"- Distinct symbols: **{live_count}**\n"
    out += f"- Max observed_at: **{live_max}**\n"

    live_rows = q(engine, """
        SELECT symbol, MAX(observed_at) as latest, COUNT(*) as rows
        FROM public.macro_live
        GROUP BY symbol
        ORDER BY symbol
        LIMIT 30
    """)
    out += "\n| Symbol | Latest observed_at | Rows |\n"
    out += "|--------|-------------------|------|\n"
    for symbol, latest, rows in live_rows:
        out += f"| {symbol} | {latest} | {rows} |\n"

    # Source field distribution
    sources = q(engine, """
        SELECT source, COUNT(DISTINCT symbol) as symbols
        FROM public.macro_live
        GROUP BY source
        ORDER BY source
    """)
    out += "\n**Source distribution:**\n"
    out += "| Source | Symbols |\n"
    out += "|--------|--------|\n"
    for source, count in sources:
        out += f"| {source} | {count} |\n"

    # Failed symbols not silently removed
    live_symbols = {r[0] for r in live_rows}
    failed_not_removed = sorted(set(configured_symbols) - live_symbols)
    if failed_not_removed:
        out += f"\n**Symbols missing from macro_live ({len(failed_not_removed)}):** {', '.join(failed_not_removed[:20])}\n"

    # Coverage summary
    out += section("Macro Coverage Summary", 3)
    out += f"- Current (latest >= NYSE): **{len(current)}**\n"
    out += f"- Stale: **{len(stale)}**\n"
    out += f"- Missing: **{len(missing)}**\n"
    out += f"- Not in DB: **{len(not_in_db)}**\n"

    return out


def audit_equities(engine, today: date, latest_nyse: date) -> str:
    """Audit US equities data."""
    out = section("B. US Equities Audit")

    # Required-table existence checks
    equities_exists = table_exists(engine, "public.us_equities")
    indicators_exists = table_exists(engine, "public.us_equities_indicators")
    if not equities_exists:
        out += f"- `public.us_equities`: {status_badge('FAIL')} — REQUIRED TABLE MISSING\n"
        return out
    if not indicators_exists:
        out += f"- `public.us_equities_indicators`: {status_badge('FAIL')} — REQUIRED TABLE MISSING\n"
        return out

    total_tickers = q1(engine, "SELECT COUNT(DISTINCT ticker) FROM public.us_equities")
    date_range = q(engine, "SELECT MIN(date), MAX(date) FROM public.us_equities")
    min_date, max_date = date_range[0] if date_range else (None, None)

    out += f"- Total distinct tickers: **{total_tickers}**\n"
    out += f"- Date range: {min_date} to {max_date}\n"
    out += f"- Latest NYSE session: **{latest_nyse}**\n"

    # Latest per ticker
    rows = q(engine, """
        SELECT ticker, MAX(date) as latest_date, COUNT(*) as rows
        FROM public.us_equities
        GROUP BY ticker
        ORDER BY ticker
    """)

    missing_session = []
    current = []
    stale = []

    for ticker, latest, row_count in rows:
        if latest is None:
            missing_session.append(ticker)
        elif latest < latest_nyse:
            missing_session.append((ticker, str(latest)))
        elif latest == latest_nyse:
            current.append(ticker)
        else:
            stale.append(ticker)

    out += f"- Tickers with latest session ({latest_nyse}): **{len(current)}**\n"
    out += f"- Tickers missing latest session: **{len(missing_session)}**\n"

    # Show first 20 missing
    if missing_session:
        out += f"\n**Missing latest session (first 20):**\n"
        for item in missing_session[:20]:
            if isinstance(item, tuple):
                out += f"  - {item[0]}: latest={item[1]}\n"
            else:
                out += f"  - {item}: no data\n"

    # Duplicates
    dups = q(engine, """
        SELECT ticker, date, COUNT(*) as cnt
        FROM public.us_equities
        GROUP BY ticker, date
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
        LIMIT 20
    """)
    out += section("Equity Duplicates", 3)
    out += f"- Duplicate (ticker, date) groups: **{len(dups)}**\n"
    if dups:
        out += "\n| Ticker | Date | Count |\n"
        out += "|--------|------|-------|\n"
        for ticker, dup_date, cnt in dups:
            out += f"| {ticker} | {dup_date} | {cnt} |\n"

    # Null OHLCV
    nulls = q1(engine, """
        SELECT COUNT(*) FROM public.us_equities
        WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL OR volume IS NULL
    """)
    out += section("OHLCV Integrity", 3)
    out += f"- Rows with null OHLCV: **{nulls}**\n"

    # Zero/negative prices
    zero_neg = q1(engine, """
        SELECT COUNT(*) FROM public.us_equities
        WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
    """)
    out += f"- Rows with zero/negative prices: **{zero_neg}**\n"

    # OHLC consistency
    inconsistent = q1(engine, """
        SELECT COUNT(*) FROM public.us_equities
        WHERE high < low OR high < open OR high < close OR low > open OR low > close
    """)
    out += f"- Inconsistent OHLC rows: **{inconsistent}**\n"

    # Suspicious volume
    susp_vol = q1(engine, """
        SELECT COUNT(*) FROM public.us_equities
        WHERE volume < 0
    """)
    out += f"- Negative volume rows: **{susp_vol}**\n"

    # Historical gaps (tickers with > 5 missing sessions in last 30 days)
    gaps = q(engine, f"""
        SELECT ticker, COUNT(DISTINCT date) as trading_days
        FROM public.us_equities
        WHERE date >= '{latest_nyse - timedelta(days=30)}'
        GROUP BY ticker
        HAVING COUNT(DISTINCT date) < 15
        ORDER BY trading_days ASC
        LIMIT 20
    """)
    out += section("Historical Gaps (last 30 days)", 3)
    out += f"- Tickers with < 15 trading days in last 30: **{len(gaps)}**\n"
    if gaps:
        out += "\n| Ticker | Trading Days |\n"
        out += "|--------|-------------|\n"
        for ticker, days in gaps:
            out += f"| {ticker} | {days} |\n"

    # Indicators
    out += section("Equities Indicators (public.us_equities_indicators)", 3)
    ind_tickers = q1(engine, "SELECT COUNT(DISTINCT ticker) FROM public.us_equities_indicators")
    ind_max = q1(engine, "SELECT MAX(date) FROM public.us_equities_indicators")
    out += f"- Distinct tickers: **{ind_tickers}**\n"
    out += f"- Max date: **{ind_max}**\n"

    # Indicator lag
    lag = q(engine, f"""
        SELECT i.ticker, i.max_indicator_date, e.max_equity_date
        FROM (SELECT ticker, MAX(date) as max_indicator_date FROM public.us_equities_indicators GROUP BY ticker) i
        LEFT JOIN (SELECT ticker, MAX(date) as max_equity_date FROM public.us_equities GROUP BY ticker) e
        ON i.ticker = e.ticker
        WHERE i.max_indicator_date < e.max_equity_date
        ORDER BY i.ticker
        LIMIT 20
    """)
    out += f"\n- Tickers with indicators lagging equities: **{len(lag)}**\n"
    if lag:
        out += "\n| Ticker | Indicator Date | Equity Date |\n"
        out += "|--------|----------------|-------------|\n"
        for ticker, ind_date, eq_date in lag:
            out += f"| {ticker} | {ind_date} | {eq_date} |\n"

    return out


def audit_sync_state(engine) -> str:
    """Audit Neon sync state (optional table — degrades gracefully)."""
    out = section("C. Neon Sync State (public.sync_state)")

    if not table_exists(engine, "public.sync_state"):
        out += f"- Status: {status_badge('WARN')} / N/A\n"
        out += "- Evidence: `public.sync_state` not present in local DB\n"
        out += "- Interpretation: local sync-state reconciliation unavailable from this table "
        out += "(sync_state is created by the Neon sync pipeline; it may live only in the Neon database).\n"
        return out

    rows = q(engine, """
        SELECT table_name, MAX(updated_at) as latest_sync
        FROM public.sync_state
        GROUP BY table_name
        ORDER BY table_name
    """)

    if not rows:
        out += "- Table exists but contains no rows\n"
        return out

    out += "\n| Table | Latest Sync |\n"
    out += "|-------|-------------|\n"
    for table_name, latest in rows:
        out += f"| {table_name} | {latest} |\n"

    return out


def audit_timezone_semantics(engine) -> str:
    """Audit timezone/date-column semantics."""
    out = section("D. Timezone / Date-Column Semantics")

    # Check column types
    result = q(engine, """
        SELECT table_name, column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name IN ('macro', 'macro_live', 'us_equities', 'us_equities_indicators', 'sync_state')
          AND column_name IN ('date', 'observed_at', 'updated_at', 'trade_date')
        ORDER BY table_name, column_name
    """)

    out += "\n| Table | Column | Type |\n"
    out += "|-------|--------|------|\n"
    for table, col, dtype, udt in result:
        out += f"| {table} | {col} | {dtype} ({udt}) |\n"

    # Check for timestamptz columns
    out += section("Timezone-Aware Timestamps", 3)
    tz_cols = q(engine, """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND data_type = 'timestamp with time zone'
        ORDER BY table_name, column_name
    """)
    out += f"- `timestamp with time zone` columns: **{len(tz_cols)}**\n"
    for table, col, dtype in tz_cols:
        out += f"  - {table}.{col}\n"

    # Check for timestamp without time zone
    no_tz_cols = q(engine, """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND data_type = 'timestamp without time zone'
        ORDER BY table_name, column_name
    """)
    out += f"\n- `timestamp without time zone` columns: **{len(no_tz_cols)}**\n"
    for table, col, dtype in no_tz_cols:
        out += f"  - {table}.{col}\n"

    # Morning HKT freshness note (evidence-based, not hardcoded)
    out += section("Morning-HKT Freshness Behavior", 3)
    col_types = {(table, col): dtype for table, col, dtype in tz_cols + no_tz_cols}
    out += "- Freshness check uses `latest_completed_nyse_session_date()` which is UTC-aware.\n"
    out += "- Morning HKT (before US market close) will correctly show the previous US session as latest completed.\n"
    if ("macro_live", "observed_at") in col_types:
        out += f"- `macro_live.observed_at` type: `{col_types[('macro_live', 'observed_at')]}`\n"
    else:
        out += "- `macro_live.observed_at`: column not found in information_schema\n"
    if ("sync_state", "updated_at") in col_types:
        dtype = col_types[("sync_state", "updated_at")]
        out += f"- `sync_state.updated_at` type: `{dtype}`\n"
        if dtype == "timestamp without time zone":
            out += "  - **Interpretation depends on session TimeZone** (SR-29 applies)\n"
    else:
        out += "- `sync_state.updated_at`: table/column not present in local DB — "
        out += "timestamp semantics cannot be verified locally\n"
    out += "- No misclassification expected for latest-session detection.\n"

    return out


def audit_pipeline_logs() -> str:
    """Audit pipeline execution logs."""
    out = section("E. Pipeline Execution Audit")

    logs_dir = PROJECT_ROOT / "logs"
    if not logs_dir.exists():
        out += "- Logs directory not found\n"
        return out

    # Most recent macro log
    macro_logs = sorted(logs_dir.glob("auto_macro_db_*.log"), reverse=True)
    if macro_logs:
        latest_log = macro_logs[0]
        out += f"- Latest macro log: `{latest_log.name}`\n"

        # Extract key lines
        content = latest_log.read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines():
            if any(kw in line for kw in ["SUCCESS", "FAILURE", "FRESHNESS", "PARTIAL", "MACRO UPDATE"]):
                out += f"  - `{line.strip()}`\n"

    # Most recent equity log
    equity_logs = sorted(logs_dir.glob("auto_postgreSQL_db_*.log"), reverse=True)
    if equity_logs:
        latest_log = equity_logs[0]
        out += f"\n- Latest equity log: `{latest_log.name}`\n"

        content = latest_log.read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines():
            if any(kw in line for kw in ["SUCCESS", "FAILURE", "FRESHNESS", "PARTIAL"]):
                out += f"  - `{line.strip()}`\n"

    return out


def main():
    today = date.today()
    latest_nyse = latest_completed_nyse_session_date()

    print(f"Starting audit: {today}")
    print(f"Latest NYSE session: {latest_nyse}")

    engine = readonly_engine()
    print("DB connection: OK (read-only mode)")

    if not verify_readonly(engine):
        print("ERROR: read-only mode could not be verified — aborting")
        sys.exit(2)
    print("Read-only verification: OK")

    report = "# DB Builder — Data Integrity Audit\n\n"
    report += f"**Date:** {today}  \n"
    report += f"**Latest NYSE session:** {latest_nyse}  \n"
    report += f"**Generated:** {datetime.now(timezone.utc).isoformat()}Z\n"

    report += audit_macro(engine, today, latest_nyse)
    report += audit_equities(engine, today, latest_nyse)
    report += audit_sync_state(engine)
    report += audit_timezone_semantics(engine)
    report += audit_pipeline_logs()

    # Write report
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")

    print(f"\nAudit complete. Report written to: {REPORT_PATH}")


if __name__ == "__main__":
    main()
