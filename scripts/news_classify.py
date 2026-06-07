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
    reset_new_for_premium_sources,
    select_articles_for_classification,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify local news articles with Ollama/DeepSeek.")
    parser.add_argument("--limit", type=int, default=5, help="Limit articles classified.")
    parser.add_argument("--article-id", default=None, help="Classify one article UUID.")
    parser.add_argument("--timeout", type=int, default=120, help="Ollama request timeout in seconds.")
    parser.add_argument("--max-seconds", type=float, default=None, help="Stop cleanly after this many elapsed seconds.")
    parser.add_argument("--min-importance", type=float, default=None, help="Only classify articles at or above this importance score.")
    parser.add_argument("--source-priority-min", type=float, default=None, help="Only classify sources at or above this priority.")
    parser.add_argument("--lookback-hours", type=float, default=24, help="Recent article lookback for premium-source filtering/reset.")
    parser.add_argument("--source-category", default=None, help="Only classify articles from this source category.")
    parser.add_argument("--reset-new-for-premium", action="store_true", help="Reset recent failed/skipped premium-source articles back to new.")
    parser.add_argument("--show-queue", action="store_true", help="Print selected queue ordering before classification.")
    parser.add_argument("--no-repair", action="store_true", help="Disable one-pass JSON repair.")
    parser.add_argument("--dry-run", action="store_true", help="Classify and print only; do not write.")
    parser.add_argument("--upsert-local", action="store_true", help="Persist classifications to local PostgreSQL.")
    return parser.parse_args()


def print_queue(articles: list[dict]) -> None:
    print("Classification queue:")
    for rank, article in enumerate(articles, start=1):
        reasons = article.get("importance_reasons") or []
        print(
            f"{rank}. {article.get('title')} | source={article.get('source_name')} "
            f"source_priority={article.get('source_priority')} "
            f"source_category={article.get('source_category')} "
            f"importance_score={article.get('importance_score')}"
        )
        if reasons:
            print(f"   importance_reasons={'; '.join(reasons)}")


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
    if args.reset_new_for_premium:
        if args.source_priority_min is None:
            raise SystemExit("--reset-new-for-premium requires --source-priority-min")
        if dry_run:
            print("[dry-run] premium reset was not written")
        else:
            reset_count = reset_new_for_premium_sources(
                engine,
                source_priority_min=args.source_priority_min,
                lookback_hours=args.lookback_hours,
                source_category=args.source_category,
            )
            print(f"Reset {reset_count:,} premium article(s) to classification_status='new'")

    articles = select_articles_for_classification(
        engine,
        limit=args.limit,
        article_id=args.article_id,
        min_importance=args.min_importance,
        source_priority_min=args.source_priority_min,
        lookback_hours=args.lookback_hours if args.source_priority_min is not None or args.source_category else None,
        source_category=args.source_category,
    )
    print(f"Selected {len(articles):,} article(s) for classification")
    if args.show_queue:
        print_queue(articles)
        if dry_run:
            print("[dry-run] queue displayed; classifications were not run")
            return
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
