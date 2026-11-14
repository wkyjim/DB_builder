#!/usr/bin/env python3
"""Compare current and expanded AEGIS stock discovery on one market snapshot."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from hourly_ai_macro_run import _query, build_stock_discovery  # noqa: E402
from db_builder.config import local_engine  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--out", default=str(ROOT / "data" / "discovery_comparison_latest.json"))
    args = ap.parse_args()
    if args.as_of:
        as_of = date.fromisoformat(args.as_of)
    else:
        as_of = _query("select max(date) from public.us_equities")[0][0]
    current, current_meta = build_stock_discovery(as_of, expanded=False)
    expanded, expanded_meta = build_stock_discovery(as_of, expanded=True)
    payload = {
        "as_of": str(as_of),
        "current": {
            "shortlisted": len(current),
            "tickers": [x["ticker"] for x in current],
            "included": current_meta.get("included", []),
            "method": current_meta.get("method"),
        },
        "expanded": {
            "shortlisted": len(expanded),
            "tickers": [x["ticker"] for x in expanded],
            "included": expanded_meta.get("included", []),
            "method": expanded_meta.get("method"),
        },
        "added": [x["ticker"] for x in expanded if x["ticker"] not in {y["ticker"] for y in current}],
        "removed": [x["ticker"] for x in current if x["ticker"] not in {y["ticker"] for y in expanded}],
        "universe": current_meta.get("universe"),
        "eligible": current_meta.get("eligible"),
        "current_excluded": current_meta.get("excluded"),
        "expanded_excluded": expanded_meta.get("excluded"),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
