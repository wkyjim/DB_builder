from __future__ import annotations

import argparse
import time

import _bootstrap  # noqa: F401

from db_builder.config import local_engine
from db_builder.news_classifier import (
    ClassificationError,
    classify_with_ollama,
    ollama_model,
    persist_classification,
    record_classification_failure,
    select_articles_for_classification,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify local news articles with Ollama/DeepSeek.")
    parser.add_argument("--limit", type=int, default=5, help="Limit articles classified.")
    parser.add_argument("--article-id", default=None, help="Classify one article UUID.")
    parser.add_argument("--timeout", type=int, default=120, help="Ollama request timeout in seconds.")
    parser.add_argument("--max-seconds", type=float, default=None, help="Stop cleanly after this many elapsed seconds.")
    parser.add_argument("--no-repair", action="store_true", help="Disable one-pass JSON repair.")
    parser.add_argument("--dry-run", action="store_true", help="Classify and print only; do not write.")
    parser.add_argument("--upsert-local", action="store_true", help="Persist classifications to local PostgreSQL.")
    return parser.parse_args()


def time_budget_reached(start_time: float, max_seconds: float | None, *, now_fn=time.monotonic) -> bool:
    return max_seconds is not None and (now_fn() - start_time) >= max_seconds


def classify_articles(
    engine,
    articles: list[dict],
    *,
    dry_run: bool,
    model: str,
    timeout: int,
    repair: bool,
    max_seconds: float | None = None,
    start_time: float | None = None,
    now_fn=time.monotonic,
    classify_fn=classify_with_ollama,
    persist_fn=persist_classification,
    failure_fn=record_classification_failure,
) -> dict:
    selected_start = now_fn() if start_time is None else start_time
    completed = 0
    failed = 0

    for article in articles:
        if time_budget_reached(selected_start, max_seconds, now_fn=now_fn):
            print("[TIME BUDGET REACHED]")
            print(f"Completed classifications: {completed}")
            print(f"Failed classifications: {failed}")
            return {"completed": completed, "failed": failed, "time_budget_reached": True}

        article_id = str(article["article_id"])
        print(f"Classifying {article_id} | {article.get('title')}")
        try:
            result = classify_fn(
                article,
                model=model,
                timeout=timeout,
                repair=repair,
            )
            print(
                "  "
                f"sentiment={result['sentiment_score']} "
                f"impact={result['impact_score']} "
                f"confidence={result['confidence_score']} "
                f"themes={','.join(result['themes'])}"
            )
            persist_fn(engine, article, result, dry_run=dry_run, model=model)
            completed += 1
        except (ClassificationError, Exception) as exc:
            failed += 1
            print(f"  ERROR: {exc}")
            if not dry_run:
                failure_fn(engine, article_id, str(exc))

    return {"completed": completed, "failed": failed, "time_budget_reached": False}


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run or not args.upsert_local
    engine = local_engine()
    articles = select_articles_for_classification(
        engine,
        limit=args.limit,
        article_id=args.article_id,
    )
    print(f"Selected {len(articles):,} article(s) for classification")
    if dry_run:
        print("[dry-run] classifications will not be written")

    model = ollama_model()
    classify_articles(
        engine,
        articles,
        dry_run=dry_run,
        model=model,
        timeout=args.timeout,
        repair=not args.no_repair,
        max_seconds=args.max_seconds,
    )
    print("News classification complete.")


if __name__ == "__main__":
    main()
