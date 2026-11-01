"""Backfill news_articles.published_at from the raw feed payload.

Why this exists: ``news_fetcher.parse_entry_datetime`` only used
``parsedate_to_datetime`` (RFC 822). Modern feeds emit ISO 8601
(``2026-06-05T21:59:42Z``), so parsing returned None and the column stayed NULL
for 74.6% of rows even though every raw payload carried a usable date. The
parser is fixed; this script repairs the rows already stored.

Safety:
  * Dry run by default. Pass --apply to write.
  * Read-only pass first, prints the exact count and a sample of what would change.
  * Only fills NULLs. Never overwrites an existing published_at.
  * Batched executemany; single transaction; rollback on any error.

Usage (host):
  python scripts/backfill_news_published_at.py            # dry run
  python scripts/backfill_news_published_at.py --apply    # write
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:  # psycopg 3 in the container
    import psycopg as _pg
    _connect = _pg.connect
except ImportError:  # psycopg2 in the host anaconda env
    import psycopg2 as _pg  # type: ignore
    _connect = _pg.connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from db_builder.news_fetcher import parse_entry_datetime  # noqa: E402

ENV_CANDIDATES = [
    Path("/run/secrets/db_builder.env"),                                  # container
    Path(r"C:\Users\User\OneDrive\Coding\DB_builder_env\.env"),           # windows host
]
BATCH = 500


def find_env() -> Path:
    for path in ENV_CANDIDATES:
        if path.exists():
            return path
    raise FileNotFoundError("db_builder env not found in %s" % ENV_CANDIDATES)


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_bytes().decode("utf-8", "replace").splitlines():
        line = raw.strip().replace("\r", "")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def connection_url(env: dict[str, str], db_host: str | None = None) -> str:
    host = db_host or os.environ.get("LOCAL_DB_HOST_OVERRIDE") or env.get("LOCAL_DB_HOST") or "host.docker.internal"
    return (
        f"postgresql://{env['LOCAL_DB_USER']}:{env['LOCAL_DB_PASSWORD']}"
        f"@{host}"
        f":{env.get('LOCAL_DB_PORT', '5432')}"
        f"/{env.get('LOCAL_DB_NAME', 'us_equities_historical')}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the backfill")
    ap.add_argument("--limit", type=int, default=None, help="cap rows for a trial run")
    ap.add_argument("--db-host", default=None, help="override DB host (host run uses 127.0.0.1)")
    args = ap.parse_args()

    env = load_env(find_env())
    url = connection_url(env, args.db_host)

    with _connect(url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE published_at IS NULL) AS nulls,
                       count(*) FILTER (
                           WHERE published_at IS NULL
                             AND coalesce(raw_payload->>'published',
                                          raw_payload->>'updated',
                                          raw_payload->>'pubDate') IS NOT NULL
                       ) AS recoverable
                FROM public.news_articles
            """)
            total, nulls, recoverable = cur.fetchone()
            print(f"total={total} nulls={nulls} recoverable={recoverable}")

            sql = """
                SELECT article_id, source_name,
                       coalesce(raw_payload->>'published',
                                raw_payload->>'updated',
                                raw_payload->>'pubDate') AS raw_date
                FROM public.news_articles
                WHERE published_at IS NULL
                  AND coalesce(raw_payload->>'published',
                               raw_payload->>'updated',
                               raw_payload->>'pubDate') IS NOT NULL
                ORDER BY fetched_at DESC NULLS LAST
            """
            if args.limit:
                sql += f" LIMIT {int(args.limit)}"
            cur.execute(sql)
            rows = cur.fetchall()

        updates: list[tuple] = []
        unparsed = 0
        for article_id, source_name, raw_date in rows:
            parsed = parse_entry_datetime({"published": raw_date})
            if parsed is None:
                unparsed += 1
                continue
            updates.append((parsed, article_id))

        print(f"candidates={len(rows)} parsed={len(updates)} unparsed={unparsed}")
        for article_id, source_name, raw_date in rows[:5]:
            parsed = parse_entry_datetime({"published": raw_date})
            print(f"  sample: {source_name[:28]:28} | {str(raw_date)[:30]:30} -> {parsed}")

        if not args.apply:
            print("DRY-RUN: no rows written. Re-run with --apply to backfill.")
            return 0

        with conn.cursor() as cur:
            written = 0
            for start in range(0, len(updates), BATCH):
                chunk = updates[start:start + BATCH]
                cur.executemany(
                    "UPDATE public.news_articles SET published_at = %s "
                    "WHERE article_id = %s AND published_at IS NULL",
                    chunk,
                )
                written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(chunk)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) FILTER (WHERE published_at IS NULL), count(*)
                FROM public.news_articles
            """)
            remaining, now_total = cur.fetchone()
        print(f"APPLIED: wrote ~{written} rows; remaining nulls={remaining}/{now_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
