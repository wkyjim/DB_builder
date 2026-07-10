"""ETF issuer holdings schema placeholder."""

from __future__ import annotations

from sqlalchemy import text


def setup_etf_holdings_schema(engine) -> None:
    sql = text(
        """
        CREATE TABLE IF NOT EXISTS public.etf_holdings (
            as_of_date date NOT NULL,
            etf_ticker text NOT NULL,
            holding_ticker text NOT NULL,
            holding_name text,
            weight numeric,
            shares numeric,
            market_value numeric,
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now(),
            PRIMARY KEY (as_of_date, etf_ticker, holding_ticker)
        );
        """
    )
    with engine.begin() as conn:
        conn.execute(sql)

