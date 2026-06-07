from __future__ import annotations

import argparse
from collections import Counter

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.news_fetcher import fetch_source_articles
from db_builder.news_sources import sources_for_request
from db_builder.news_storage import setup_news_schema, seed_news_keywords, upsert_articles, upsert_sources


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch investment RSS news into local PostgreSQL.")
    parser.add_argument("--source", default=None, help="Source name or 'google'.")
    parser.add_argument("--category", default=None, help="Filter or label source category.")
    parser.add_argument("--keyword", default=None, help="Google News keyword query.")
    parser.add_argument("--limit", type=int, default=None, help="Limit entries per feed.")
    parser.add_argument("--timeout", type=int, default=20, help="RSS request timeout in seconds.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print only; do not upsert.")
    parser.add_argument("--upsert-local", action="store_true", help="Create local schema and upsert articles.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    sources = sources_for_request(source=args.source, keyword=args.keyword, category=args.category)
    if not sources:
        print("No enabled news sources matched request.")
        return

    all_articles = []
    failed_sources = []
    for source in sources:
        print(f"Fetching {source.source_name}: {source.feed_url}")
        try:
            articles = fetch_source_articles(source, limit=args.limit, timeout=args.timeout)
        except Exception as exc:
            failed_sources.append((source.source_name, str(exc)))
            print(f"[WARN] skipped {source.source_name}: {exc}")
            continue
        all_articles.extend(articles)
        print(f"Fetched {len(articles):,} unique articles")

    if args.dry_run or not args.upsert_local:
        print(f"[dry-run] would upsert {len(all_articles):,} articles from {len(sources):,} source(s)")
        if failed_sources:
            print("Failed sources:")
            for source_name, error in failed_sources:
                print(f"- {source_name}: {error}")
        source_counts = Counter(article["source_name"] for article in all_articles)
        category_counts = Counter(article.get("source_category") or "uncategorized" for article in all_articles)
        print("Source counts:")
        for source_name, count in source_counts.most_common():
            print(f"- {source_name}: {count}")
        print("Category counts:")
        for category, count in category_counts.most_common():
            print(f"- {category}: {count}")
        for article in all_articles[: args.limit or 10]:
            print(f"- {article['published_at']} | {article['source_name']} | {article['title']}")
            print(f"  source_priority={article.get('source_priority')} source_category={article.get('source_category')}")
            if article["matched_keywords"]:
                print(f"  keywords: {', '.join(article['matched_keywords'])}")
        return

    engine = local_engine(use_insertmanyvalues=True)
    setup_news_schema(engine)
    seed_news_keywords(engine)
    upsert_sources(engine, sources)
    upsert_articles(engine, all_articles)
    print(f"Upserted {len(all_articles):,} articles into local PostgreSQL.")
    if failed_sources:
        print(f"Skipped {len(failed_sources):,} source(s) after fetch errors.")


if __name__ == "__main__":
    main()
