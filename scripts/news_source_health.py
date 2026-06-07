from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.news_source_health import fetch_source_health_summary, setup_source_health_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show RSS source health summary.")
    parser.add_argument("--summary", action="store_true", help="Print source health summary.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum source rows to show.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.summary:
        print("Nothing to do. Pass --summary.")
        return

    engine = local_engine()
    setup_source_health_schema(engine)
    rows = fetch_source_health_summary(engine, limit=args.limit)
    if not rows:
        print("No source health rows found.")
        return

    print("Source health summary:")
    for row in rows:
        recommendation = "enabled" if row["enabled_recommendation"] else "review"
        error = row["last_error"] or ""
        print(
            f"- {row['source_name']} | ok={row['success_count']} fail={row['failure_count']} "
            f"consecutive_fail={row['consecutive_failure_count']} last_articles={row['last_article_count']} "
            f"last_sec={row['last_fetch_seconds']} avg_sec={row['avg_fetch_seconds']} recommendation={recommendation}"
        )
        if error:
            print(f"  last_error={error}")


if __name__ == "__main__":
    main()
