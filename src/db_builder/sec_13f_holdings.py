"""SEC 13F schema placeholder.

Production 13F ingestion is intentionally deferred until CFTC and FINRA are
stable. The table setup is kept here so source availability and later ingestion
can share a consistent schema.
"""

from __future__ import annotations

from sqlalchemy import text


def setup_sec_13f_schema(engine) -> None:
    sql = text(
        """
        CREATE TABLE IF NOT EXISTS public.sec_13f_holdings (
            quarter date NOT NULL,
            manager_cik text NOT NULL,
            manager_name text,
            cusip text NOT NULL,
            ticker text,
            issuer_name text,
            value_usd numeric,
            shares numeric,
            put_call text NOT NULL DEFAULT '',
            qoq_value_change numeric,
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now(),
            PRIMARY KEY (quarter, manager_cik, cusip, put_call)
        );
        """
    )
    with engine.begin() as conn:
        conn.execute(sql)

