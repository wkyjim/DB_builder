"""Build the standalone point-in-time S&P 500 breadth dataset."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import text  # noqa: E402

from db_builder.config import local_engine  # noqa: E402
from db_builder.sp500_breadth import build, coverage_summary, write, write_docs  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-02")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    engine = local_engine()
    end = date.fromisoformat(args.end) if args.end else engine.connect().execute(
        text("SELECT max(date) FROM public.us_equities")
    ).scalar()
    df = build(engine, start=date.fromisoformat(args.start), end=end)
    write(local_engine(), df)
    out = ROOT / "data" / "sp500_breadth_daily.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    write_docs(df)
    print(coverage_summary(df))
    return 0


if __name__ == "__main__":
    sys.exit(main())
