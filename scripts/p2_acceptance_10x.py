#!/usr/bin/env python3
"""Run deterministic P1/P2 discovery comparisons on a fixed snapshot.

No AI calls, no publication. Records whether candidate construction is stable
across repeated runs and summarizes current-vs-expanded differences.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from db_builder.config import local_engine  # noqa: E402
from hourly_ai_macro_run import _query, build_stock_discovery  # noqa: E402


def _hash(candidates):
    return hashlib.sha256(json.dumps([x["ticker"] for x in candidates], sort_keys=True).encode()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--out", default=str(ROOT / "data" / "p2_acceptance_10x.json"))
    args = ap.parse_args()
    as_of = date.fromisoformat(args.as_of) if args.as_of else _query("select max(date) from public.us_equities")[0][0]
    results = []
    started = time.monotonic()
    for n in range(1, args.runs + 1):
        t0 = time.monotonic()
        current, cm = build_stock_discovery(as_of, expanded=False)
        expanded, em = build_stock_discovery(as_of, expanded=True)
        results.append({
            "run": n,
            "runtime_s": round(time.monotonic() - t0, 3),
            "current_count": len(current), "expanded_count": len(expanded),
            "current_hash": _hash(current), "expanded_hash": _hash(expanded),
            "current_tickers": [x["ticker"] for x in current],
            "expanded_tickers": [x["ticker"] for x in expanded],
            "added": [x["ticker"] for x in expanded if x["ticker"] not in {y["ticker"] for y in current}],
            "removed": [x["ticker"] for x in current if x["ticker"] not in {y["ticker"] for y in expanded}],
        })
    payload = {
        "schema": "aegis.p2_acceptance_10x.v1",
        "as_of": str(as_of),
        "runs": results,
        "summary": {
            "runs": len(results),
            "current_stable": len({r["current_hash"] for r in results}) == 1,
            "expanded_stable": len({r["expanded_hash"] for r in results}) == 1,
            "current_count": results[0]["current_count"],
            "expanded_count": results[0]["expanded_count"],
            "added": results[0]["added"],
            "removed": results[0]["removed"],
            "elapsed_s": round(time.monotonic() - started, 3),
            "ai_tokens": 0,
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2))
    print(f"WROTE {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
