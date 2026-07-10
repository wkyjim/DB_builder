"""ICI flow schema placeholder.

ICI ingestion is planned after CFTC/FINRA stabilization because public pages can
require source-specific parsing and revision handling.
"""

from __future__ import annotations

from sqlalchemy import text


def setup_ici_flows_schema(engine) -> None:
    sql = text(
        """
        CREATE TABLE IF NOT EXISTS public.ici_flows (
            week_ended date NOT NULL,
            category text NOT NULL,
            flow_usd_mn numeric,
            flow_4w_sum numeric,
            flow_z_3y numeric,
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now(),
            PRIMARY KEY (week_ended, category)
        );
        """
    )
    with engine.begin() as conn:
        conn.execute(sql)

