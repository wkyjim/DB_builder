from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.security_classification import (
    fetch_sp500_constituents,
    sp500_classification_summary,
    upsert_sp500_constituents,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch and update S&P 500 security classification mapping.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--upsert-local", action="store_true")
    parser.add_argument("--no-mark-removed", action="store_true", help="Do not mark missing prior constituents inactive.")
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = local_engine(use_insertmanyvalues=True)
    if args.summary:
        summary = sp500_classification_summary(engine)
        print(summary.to_string(index=False) if not summary.empty else "No S&P 500 classification rows found.")
        return

    rows = fetch_sp500_constituents()
    print(f"[FETCHED] sp500_constituents={len(rows):,}")
    if rows:
        for row in rows[:10]:
            print(f"{row['ticker']} | {row['company_name']} | {row['sector']} | {row['industry']}")

    if args.dry_run:
        print("[DRY RUN] no database writes")
        return
    if not args.upsert_local:
        print("[NO WRITE] pass --upsert-local to update local PostgreSQL")
        return

    result = upsert_sp500_constituents(engine, rows, mark_removed=not args.no_mark_removed)
    print(f"[UPSERT LOCAL] rows={result['upserted']:,} marked_inactive={result['marked_inactive']:,}")
    summary = sp500_classification_summary(engine)
    print(summary.to_string(index=False) if not summary.empty else "No S&P 500 classification rows found.")


if __name__ == "__main__":
    main()
