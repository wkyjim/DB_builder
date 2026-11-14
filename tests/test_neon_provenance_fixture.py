from __future__ import annotations

import uuid

from sqlalchemy import text

from db_builder.config import local_engine
from db_builder.neon_sync import ensure_provenance_columns


def test_provenance_migration_is_additive_and_idempotent():
    table = f"yf_provenance_fixture_{uuid.uuid4().hex[:10]}"
    engine = local_engine()
    with engine.begin() as c:
        c.execute(text(f"CREATE TABLE {table} (date date, ticker text, close double precision)"))
        c.execute(text(f"INSERT INTO {table} VALUES ('2026-11-13','AAPL',1.0)"))
    try:
        ensure_provenance_columns(engine, table)
        ensure_provenance_columns(engine, table)  # idempotent
        with engine.connect() as c:
            cols = {r[0] for r in c.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=:t"), {"t": table}).fetchall()}
            row = c.execute(text(f"SELECT close,is_provisional FROM {table}")).one()
        assert {"data_source", "is_provisional", "retrieved_at"} <= cols
        assert row[0] == 1.0
        assert row[1] is False
    finally:
        with engine.begin() as c:
            c.execute(text(f"DROP TABLE IF EXISTS {table}"))
