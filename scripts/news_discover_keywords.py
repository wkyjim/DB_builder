from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.news_keyword_discovery import fetch_articles_for_discovery, generate_keyword_candidates
from db_builder.news_storage import setup_news_schema, upsert_keyword_candidates


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover candidate investment news keywords.")
    parser.add_argument("--limit", type=int, default=None, help="Limit recent articles analyzed.")
    parser.add_argument("--min-count", type=int, default=2, help="Minimum headline frequency for candidates.")
    parser.add_argument("--dry-run", action="store_true", help="Preview candidates only.")
    parser.add_argument("--upsert-local", action="store_true", help="Upsert candidates into local PostgreSQL.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = local_engine()

    try:
        articles = fetch_articles_for_discovery(engine, limit=args.limit)
    except Exception as exc:
        if args.upsert_local:
            raise
        print(f"[dry-run] could not read local news_articles yet: {exc}")
        articles = []

    candidates = generate_keyword_candidates(articles, min_count=args.min_count)
    print(f"Generated {len(candidates):,} keyword candidates")
    for candidate in candidates[:20]:
        print(
            f"- {candidate['keyword']} | count={candidate['source_headline_count']} "
            f"| quality={candidate['confidence_score']} | status={candidate['status']}"
        )

    if args.dry_run or not args.upsert_local:
        print(f"[dry-run] would upsert {len(candidates):,} keyword candidates")
        return

    setup_news_schema(engine)
    upsert_keyword_candidates(engine, candidates)
    print(f"Upserted {len(candidates):,} keyword candidates into local PostgreSQL.")


if __name__ == "__main__":
    main()
