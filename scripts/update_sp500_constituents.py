"""Refresh S&P 500 membership history; weekly by default on Saturday."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from db_builder.config import local_engine  # noqa: E402
from db_builder.sp500_constituents import run  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weekly", action="store_true", help="run only on Saturday, once per day")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result = run(local_engine(), weekly=args.weekly, force=args.force, dry_run=args.dry_run)
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
