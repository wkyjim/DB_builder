-- Macro technical indicators, mirrored from local public.macro_indicators to Neon.
-- Local stays the system of record; Neon serves the API/dashboard read path.
-- Full history remains local — Neon carries the rolling window that the API reads.
CREATE TABLE IF NOT EXISTS public.macro_indicators (
    symbol varchar NOT NULL,
    date date NOT NULL,
    ma_5 numeric(12,4),
    ma_20 numeric(12,4),
    ma_50 numeric(12,4),
    ma_100 numeric(12,4),
    ma_200 numeric(12,4),
    ema_12 numeric(12,4),
    ema_26 numeric(12,4),
    rsi_14 numeric(12,4),
    atr_14 numeric(12,4),
    volume_ma_20 numeric,
    volume_ratio_20 numeric(12,4),
    high_52w numeric(12,4),
    low_52w numeric(12,4),
    return_5d numeric(12,4),
    return_20d numeric(12,4),
    return_60d numeric(12,4),
    volatility_20d numeric(12,4),
    macd numeric(12,4),
    macd_signal numeric(12,4),
    macd_hist numeric(12,4),
    CONSTRAINT macro_indicators_pkey PRIMARY KEY (symbol, date)
);

CREATE INDEX IF NOT EXISTS idx_macro_indicators_symbol_date
    ON public.macro_indicators (symbol, date DESC);
