"""Remove OilPriceAPI Brent/WTI spot observations from local and Neon macro tables."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import text  # noqa: E402

from db_builder.config import local_engine, neon_engine  # noqa: E402

SYMBOLS = ("BRENT_OILPRICE", "WTI_OILPRICE")
TABLES = ("macro", "macro_live", "macro_indicators")


def tables_with_symbol(conn) -> list[str]:
    rows = conn.execute(text("""
        SELECT table_name FROM information_schema.columns
        WHERE table_schema='public' AND column_name='symbol'
    """)).fetchall()
    return [r[0] for r in rows]


def counts(conn) -> dict:
    out = {}
    for table in tables_with_symbol(conn):
        if not table.startswith("macro"):
            continue
        try:
            n = conn.execute(text(
                f"SELECT count(*) FROM public.{table} WHERE symbol = ANY(:s)"
            ), {"s": list(SYMBOLS)}).scalar()
        except Exception:
            continue
        if n:
            out[table] = n
    return out


def purge(engine, label: str, apply: bool) -> None:
    with engine.connect() as conn:
        before = counts(conn)
    print(f"{label} before: {before or 'none'}")
    if not apply:
        return
    with engine.begin() as conn:
        for table, _ in before.items():
            conn.execute(text(
                f"DELETE FROM public.{table} WHERE symbol = ANY(:s)"
            ), {"s": list(SYMBOLS)})
    with engine.connect() as conn:
        after = counts(conn)
    print(f"{label} after: {after or 'none'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    purge(local_engine(), "local", args.apply)
    purge(neon_engine(), "neon", args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
