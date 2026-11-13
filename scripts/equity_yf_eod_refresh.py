"""06:00 HKT provisional yfinance EOD refresh for S&P 500 + AEGIS ETFs.

Scope is intentionally narrow:
  * latest completed NYSE session only
  * current S&P 500 constituents + existing ETF_FLOW_UNIVERSE
  * no macro, rates, VIX, credit or economic datasets
  * no historical backfill
  * no destructive overwrite of existing (Massive) rows

Provenance/audit model
----------------------
The first yfinance row for a session is written with
``data_source='yfinance'`` and ``is_provisional=true``.  Its OHLCV and
retrieval time are stored in ``equity_yfinance_reconciliation`` so they
remain auditable after Massive replaces the canonical row.

A later Massive write is only considered a promotion if it is complete and
explicitly marked as Massive by the Massive fetch path.  Incomplete Massive
rows remain unpromoted.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from db_builder.config import local_engine  # noqa: E402
from db_builder.etf_flows import ETF_FLOW_UNIVERSE  # noqa: E402
from db_builder.indicators import daily_update_missing_indicators  # noqa: E402
from db_builder.sp500_breadth import build as build_breadth, write as write_breadth  # noqa: E402
from session_calendar import last_completed_session, session_open_utc  # noqa: E402

SCHEMA = "public"
YF_CHUNK = 80
YF_WINDOW_DAYS = 12
QC_MIN_COMPLETENESS = 0.95
RECON_TABLE = "public.equity_yfinance_reconciliation"


def _yf_symbol(ticker: str) -> str:
    return ticker.replace(".", "-")


def _as_float(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _valid_ohlcv(r: dict) -> bool:
    o, h, l, c = (_as_float(r.get(k)) for k in ("open", "high", "low", "close"))
    v = _as_float(r.get("volume"))
    if None in (o, h, l, c) or v is None:
        return False
    if min(o, h, l, c) <= 0 or v < 0:
        return False
    if h < l or h < max(o, c) or l > min(o, c):
        return False
    return True


def _download(tickers: list[str], session, dry_run: bool) -> dict[str, dict]:
    """Download a small recent window and return only the requested session."""
    out: dict[str, dict] = {}
    start = session - timedelta(days=YF_WINDOW_DAYS)
    end = session + timedelta(days=1)
    for pos in range(0, len(tickers), YF_CHUNK):
        chunk = tickers[pos:pos + YF_CHUNK]
        symbols = [_yf_symbol(t) for t in chunk]
        try:
            raw = yf.download(
                " ".join(symbols), start=str(start), end=str(end),
                auto_adjust=False, actions=False, progress=False,
                group_by="ticker", threads=True,
            )
        except Exception as exc:
            print(f"[YF CHUNK ERROR] {pos}: {exc}")
            continue
        if raw is None or raw.empty:
            continue
        if isinstance(raw.columns, pd.MultiIndex):
            level0 = set(raw.columns.get_level_values(0))
            for ticker, sym in zip(chunk, symbols):
                if sym not in level0:
                    continue
                sub = raw[sym]
                _parse_sub(sub, ticker, session, out)
        elif len(chunk) == 1:
            _parse_sub(raw, chunk[0], session, out)
        time.sleep(0.2)
    return out


def _parse_sub(sub: pd.DataFrame, ticker: str, session, out: dict) -> None:
    if sub is None or sub.empty:
        return
    sub = sub.copy()
    sub.index = pd.to_datetime(sub.index).normalize()
    if "Close" not in sub.columns:
        return
    sub = sub.dropna(subset=["Close"]).sort_index()
    ts = pd.Timestamp(session)
    if ts not in sub.index:
        return
    row = sub.loc[ts]
    close = _as_float(row.get("Close"))
    if close is None:
        return
    prior = sub[sub.index < ts]
    prev_close = _as_float(prior["Close"].iloc[-1]) if len(prior) else None
    volume = _as_float(row.get("Volume"))
    out[ticker] = {
        "date": session,
        "ticker": ticker,
        "open": _as_float(row.get("Open")),
        "high": _as_float(row.get("High")),
        "low": _as_float(row.get("Low")),
        "close": close,
        "prev_close": prev_close,
        "change": (close - prev_close) if prev_close is not None else None,
        "pct_chg": ((close / prev_close - 1) * 100) if prev_close not in (None, 0) else None,
        "volume": int(volume) if volume is not None else None,
        "turnover": (close * volume) if volume is not None else None,
        "is_provisional": True,
        "data_source": "yfinance",
        "retrieved_at": datetime.now(timezone.utc),
    }


def _tickers(engine, session) -> tuple[list[str], dict[str, str]]:
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT ticker, company_name
            FROM public.sp500_membership_history
            WHERE start_date <= :d
              AND (end_date IS NULL OR end_date > :d)
            ORDER BY ticker
        """), {"d": session}).fetchall()
        names = {str(r[0]).strip(): (str(r[1]).strip() if r[1] else "") for r in rows}
        sp = [t for t in names if t and not t.endswith("|")]
        etf = list(ETF_FLOW_UNIVERSE)
        for t in etf:
            names.setdefault(t, t)
    return list(dict.fromkeys(sp + etf)), names


def _ensure_columns(engine) -> None:
    with engine.begin() as c:
        c.execute(text("ALTER TABLE public.us_equities ADD COLUMN IF NOT EXISTS data_source text"))
        c.execute(text("ALTER TABLE public.us_equities ADD COLUMN IF NOT EXISTS is_provisional boolean NOT NULL DEFAULT false"))
        c.execute(text("ALTER TABLE public.us_equities ADD COLUMN IF NOT EXISTS retrieved_at timestamptz"))
        c.execute(text("""
            CREATE TABLE IF NOT EXISTS public.equity_yfinance_reconciliation (
                session_date date NOT NULL,
                ticker text NOT NULL,
                yf_open double precision, yf_high double precision,
                yf_low double precision, yf_close double precision,
                yf_volume double precision, yf_retrieved_at timestamptz,
                primary_open double precision, primary_high double precision,
                primary_low double precision, primary_close double precision,
                primary_volume double precision, primary_retrieved_at timestamptz,
                abs_diff double precision, rel_diff double precision,
                status text NOT NULL DEFAULT 'pending_massive',
                checked_at timestamptz NOT NULL DEFAULT now(),
                promoted_at timestamptz,
                PRIMARY KEY (session_date, ticker)
            )
        """))
        for col, typ in (
            ("yf_open", "double precision"), ("yf_high", "double precision"),
            ("yf_low", "double precision"), ("yf_volume", "double precision"),
            ("yf_retrieved_at", "timestamptz"),
            ("primary_open", "double precision"), ("primary_high", "double precision"),
            ("primary_low", "double precision"), ("primary_volume", "double precision"),
            ("primary_retrieved_at", "timestamptz"), ("rel_diff", "double precision"),
            ("promoted_at", "timestamptz"),
        ):
            c.execute(text(f"ALTER TABLE {RECON_TABLE} ADD COLUMN IF NOT EXISTS {col} {typ}"))


def _recon_values(r: dict, *, yf: bool = True, primary: dict | None = None,
                  status: str = "pending_massive") -> dict:
    out = {
        "session_date": r["date"], "ticker": r["ticker"],
        "yf_open": None, "yf_high": None, "yf_low": None, "yf_close": None,
        "yf_volume": None, "yf_retrieved_at": None,
        "primary_open": None, "primary_high": None, "primary_low": None,
        "primary_close": None, "primary_volume": None, "primary_retrieved_at": None,
        "abs_diff": None, "rel_diff": None, "status": status, "promoted_at": None,
    }
    if yf:
        for k in ("open", "high", "low", "close", "volume", "retrieved_at"):
            out["yf_" + k] = r.get(k)
    if primary:
        for k in ("open", "high", "low", "close", "volume", "retrieved_at"):
            out["primary_" + k] = primary.get(k)
        yfc, pc = _as_float(out["yf_close"]), _as_float(primary.get("close"))
        if yfc is not None and pc is not None:
            out["abs_diff"] = abs(yfc - pc)
            out["rel_diff"] = (out["abs_diff"] / abs(yfc)) if yfc else None
    if status == "promoted_massive":
        out["promoted_at"] = datetime.now(timezone.utc)
    return out


_RECON_UPSERT = text("""
    INSERT INTO public.equity_yfinance_reconciliation
        (session_date,ticker,yf_open,yf_high,yf_low,yf_close,yf_volume,yf_retrieved_at,
         primary_open,primary_high,primary_low,primary_close,primary_volume,
         primary_retrieved_at,abs_diff,rel_diff,status,promoted_at,checked_at)
    VALUES
        (:session_date,:ticker,:yf_open,:yf_high,:yf_low,:yf_close,:yf_volume,:yf_retrieved_at,
         :primary_open,:primary_high,:primary_low,:primary_close,:primary_volume,
         :primary_retrieved_at,:abs_diff,:rel_diff,:status,:promoted_at,now())
    ON CONFLICT (session_date,ticker) DO UPDATE SET
        yf_open=COALESCE(public.equity_yfinance_reconciliation.yf_open, EXCLUDED.yf_open),
        yf_high=COALESCE(public.equity_yfinance_reconciliation.yf_high, EXCLUDED.yf_high),
        yf_low=COALESCE(public.equity_yfinance_reconciliation.yf_low, EXCLUDED.yf_low),
        yf_close=COALESCE(public.equity_yfinance_reconciliation.yf_close, EXCLUDED.yf_close),
        yf_volume=COALESCE(public.equity_yfinance_reconciliation.yf_volume, EXCLUDED.yf_volume),
        yf_retrieved_at=COALESCE(public.equity_yfinance_reconciliation.yf_retrieved_at, EXCLUDED.yf_retrieved_at),
        primary_open=COALESCE(EXCLUDED.primary_open, public.equity_yfinance_reconciliation.primary_open),
        primary_high=COALESCE(EXCLUDED.primary_high, public.equity_yfinance_reconciliation.primary_high),
        primary_low=COALESCE(EXCLUDED.primary_low, public.equity_yfinance_reconciliation.primary_low),
        primary_close=COALESCE(EXCLUDED.primary_close, public.equity_yfinance_reconciliation.primary_close),
        primary_volume=COALESCE(EXCLUDED.primary_volume, public.equity_yfinance_reconciliation.primary_volume),
        primary_retrieved_at=COALESCE(EXCLUDED.primary_retrieved_at, public.equity_yfinance_reconciliation.primary_retrieved_at),
        abs_diff=COALESCE(EXCLUDED.abs_diff, public.equity_yfinance_reconciliation.abs_diff),
        rel_diff=COALESCE(EXCLUDED.rel_diff, public.equity_yfinance_reconciliation.rel_diff),
        status=EXCLUDED.status,
        promoted_at=COALESCE(public.equity_yfinance_reconciliation.promoted_at, EXCLUDED.promoted_at),
        checked_at=now()
""")


def _write_prices(engine, rows: list[dict], session) -> dict:
    inserted = updated = primary = 0
    promoted = incomplete = 0
    with engine.begin() as c:
        existing = {
            str(r[0]): dict(r._mapping)
            for r in c.execute(text("""
                SELECT ticker,data_source,is_provisional,open,high,low,close,volume,retrieved_at
                FROM public.us_equities WHERE date = :d
            """), {"d": session}).fetchall()
        }
        for r in rows:
            t = r["ticker"]
            prev = existing.get(t)
            if prev is None:
                inserted += 1
                c.execute(text("""
                    INSERT INTO public.us_equities
                        (date,ticker,name,open,high,low,close,change,pct_chg,prev_close,
                         turnover,volume,market,data_source,is_provisional,retrieved_at)
                    VALUES
                        (:date,:ticker,:name,:open,:high,:low,:close,:change,:pct_chg,:prev_close,
                         :turnover,:volume,'US',:data_source,:is_provisional,:retrieved_at)
                    ON CONFLICT (date,ticker) DO NOTHING
                """), {**r, "name": r.get("name") or t})
                c.execute(_RECON_UPSERT, _recon_values(r, status="pending_massive"))
            elif prev["data_source"] == "yfinance" or prev["is_provisional"]:
                updated += 1
                c.execute(text("""
                    UPDATE public.us_equities SET
                        open=:open, high=:high, low=:low, close=:close,
                        change=:change, pct_chg=:pct_chg, prev_close=:prev_close,
                        turnover=:turnover, volume=:volume,
                        data_source='yfinance', is_provisional=true, retrieved_at=:retrieved_at
                    WHERE date=:date AND ticker=:ticker
                """), r)
                c.execute(_RECON_UPSERT, _recon_values(r, status="pending_massive"))
            else:
                primary += 1
                p = {k: prev.get(k) for k in ("open", "high", "low", "close", "volume", "retrieved_at")}
                p["retrieved_at"] = prev.get("retrieved_at")
                complete = _valid_ohlcv(p) and prev["data_source"] == "massive" and not prev["is_provisional"]
                status = "promoted_massive" if complete else "incomplete_massive"
                if complete:
                    promoted += 1
                else:
                    incomplete += 1
                c.execute(_RECON_UPSERT, _recon_values(r, primary=p, status=status))
    return {
        "inserted": inserted, "updated_yfinance": updated,
        "existing_primary_unchanged": primary,
        "promoted_massive": promoted, "incomplete_massive": incomplete,
    }


def reconcile_session(engine, session, tickers: list[str]) -> dict:
    """Reconcile already-present Massive rows with stored yfinance audit rows."""
    changed = promoted = incomplete = 0
    with engine.begin() as c:
        rows = c.execute(text("""
            SELECT e.ticker,e.data_source,e.is_provisional,e.open,e.high,e.low,e.close,e.volume,e.retrieved_at,
                   r.yf_close,r.status
            FROM public.us_equities e
            LEFT JOIN public.equity_yfinance_reconciliation r
              ON r.session_date=e.date AND r.ticker=e.ticker
            WHERE e.date=:d AND e.ticker = ANY(:t)
        """), {"d": session, "t": tickers}).fetchall()
        for row in rows:
            m = dict(row._mapping)
            if m["data_source"] != "massive" or m["is_provisional"]:
                continue
            p = {k: m[k] for k in ("open", "high", "low", "close", "volume", "retrieved_at")}
            complete = _valid_ohlcv(p)
            status = "promoted_massive" if complete else "incomplete_massive"
            if complete:
                promoted += 1
            else:
                incomplete += 1
            if m["yf_close"] is None:
                continue
            # The original yfinance close is already stored; feed it into the
            # diff calculation rather than blanking the yfinance side.
            vals = _recon_values(
                {"date": session, "ticker": m["ticker"], "close": m["yf_close"]},
                yf=True, primary=p, status=status,
            )
            vals["yf_open"] = None
            vals["yf_high"] = None
            vals["yf_low"] = None
            vals["yf_volume"] = None
            vals["yf_retrieved_at"] = None
            # Preserve the original yfinance close already stored.
            vals["yf_close"] = m["yf_close"]
            c.execute(_RECON_UPSERT, vals)
            changed += 1
    return {"checked": len(rows) if "rows" in locals() else 0,
            "reconciled": changed, "promoted": promoted, "incomplete": incomplete}


def _corporate_actions(engine, session, tickers: list[str]) -> dict:
    try:
        with engine.connect() as c:
            rows = c.execute(text("""
                SELECT ticker, ratio, price_factor
                FROM public.stock_split_events
                WHERE split_date=:d AND ticker = ANY(:t)
                ORDER BY ticker
            """), {"d": session, "t": tickers}).fetchall()
    except Exception:
        return {"count": 0, "tickers": [], "error": "stock_split_events unavailable"}
    return {"count": len(rows), "tickers": [str(r[0]) for r in rows]}


def _summary_diff(engine, session) -> dict:
    with engine.connect() as c:
        row = c.execute(text(f"""
            SELECT count(*) FILTER (WHERE status='promoted_massive') AS promoted,
                   count(*) FILTER (WHERE status='incomplete_massive') AS incomplete,
                   count(*) FILTER (WHERE status='pending_massive') AS pending,
                   max(abs_diff) AS max_abs,
                   max(rel_diff) AS max_rel
            FROM {RECON_TABLE} WHERE session_date=:d
        """), {"d": session}).mappings().one()
    return {k: (float(v) if v is not None and k in ("max_abs", "max_rel") else int(v or 0))
            for k, v in dict(row).items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reconcile-only", action="store_true")
    ap.add_argument("--session", default=None, help="YYYY-MM-DD; defaults to last completed NYSE session")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    session = (pd.Timestamp(args.session).date() if args.session
               else last_completed_session(now))
    # Reject accidental historical backfills: this job is only for the latest
    # completed NYSE session. Tests use an isolated DB with the same date.
    latest = last_completed_session(now)
    if session != latest:
        raise SystemExit(f"refusing non-latest session {session}; latest completed is {latest}")
    try:
        session_open_utc(session)
    except Exception as exc:
        raise SystemExit(f"not an NYSE session: {session} ({exc})")
    engine = local_engine()
    _ensure_columns(engine)
    tickers, names = _tickers(engine, session)
    print(f"[YF EOD] session={session} target_tickers={len(tickers)} "
          f"dots={sum('.' in t for t in tickers)} etfs={len(ETF_FLOW_UNIVERSE)}")
    if args.reconcile_only:
        stats = reconcile_session(engine, session, tickers)
        print("YF_EOD_RECONCILE " + json.dumps(stats))
        return 0
    if args.dry_run:
        print(json.dumps({"session": str(session), "target": len(tickers), "dry_run": True}))
        return 0
    started = time.time()
    rows_by_ticker = _download(tickers, session, args.dry_run)
    rows = []
    for t, r in rows_by_ticker.items():
        r = dict(r)
        r["name"] = names.get(t, t)
        rows.append(r)
    missing = sorted(set(tickers) - set(rows_by_ticker))
    valid_rows = [r for r in rows if _valid_ohlcv(r)]
    invalid = sorted({r["ticker"] for r in rows if not _valid_ohlcv(r)})
    completeness = len(valid_rows) / len(tickers) if tickers else 0.0
    print(f"[YF EOD] received={len(rows)} valid={len(valid_rows)} "
          f"missing={len(missing)} invalid={len(invalid)} completeness={completeness:.2%}")
    write_stats = _write_prices(engine, valid_rows, session) if valid_rows else {
        "inserted": 0, "updated_yfinance": 0, "existing_primary_unchanged": 0,
        "promoted_massive": 0, "incomplete_massive": 0,
    }
    qc_ok = completeness >= QC_MIN_COMPLETENESS and not invalid
    indicator_ok = breadth_ok = False
    indicator_error = breadth_error = None
    if qc_ok:
        try:
            daily_update_missing_indicators(engine, tickers=tickers, lookback_rows=320)
            indicator_ok = True
        except Exception as exc:
            indicator_error = repr(exc)
            print(f"[YF EOD] indicator update failed: {exc}")
        try:
            bdf = build_breadth(engine, start=session - timedelta(days=60), end=session)
            latest_rows = bdf[pd.to_datetime(bdf["date"]).dt.date == session]
            if not latest_rows.empty:
                write_breadth(engine, latest_rows)
                breadth_ok = True
        except Exception as exc:
            breadth_error = repr(exc)
            print(f"[YF EOD] breadth update failed: {exc}")
    else:
        print("[YF EOD] price QC below threshold; indicators/breadth not updated")
    summary = {
        "session": str(session),
        "target_tickers": len(tickers),
        "sp500_target": len([t for t in tickers if t not in ETF_FLOW_UNIVERSE]),
        "etf_target": len(ETF_FLOW_UNIVERSE),
        "received": len(rows), "valid": len(valid_rows),
        "missing": len(missing), "missing_sample": missing[:20],
        "invalid": len(invalid), "invalid_sample": invalid[:20],
        "completeness_pct": round(completeness * 100, 2),
        "qc_ok": qc_ok, "write": write_stats,
        "corporate_actions": _corporate_actions(engine, session, tickers),
        "reconciliation": _summary_diff(engine, session),
        "indicators_ok": indicator_ok, "indicator_error": indicator_error,
        "breadth_ok": breadth_ok, "breadth_error": breadth_error,
        "runtime_seconds": round(time.time() - started, 1),
        "source": "yfinance", "provisional": True,
    }
    print("YF_EOD_SUMMARY " + json.dumps(summary, default=str))
    return 0 if qc_ok else 1


if __name__ == "__main__":
    sys.exit(main())
