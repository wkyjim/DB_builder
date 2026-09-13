-- FINRA short-volume and short-interest analytics.
-- Daily short-sale volume is transaction activity, not outstanding short interest.

ALTER TABLE IF EXISTS public.finra_short_volume
    ADD COLUMN IF NOT EXISTS source_file text,
    ADD COLUMN IF NOT EXISTS fetched_at timestamptz,
    ADD COLUMN IF NOT EXISTS data_quality_status text DEFAULT 'valid',
    ADD COLUMN IF NOT EXISTS raw_symbol text,
    ADD COLUMN IF NOT EXISTS normalized_ticker text;

CREATE INDEX IF NOT EXISTS idx_finra_short_volume_ticker_date
    ON public.finra_short_volume (ticker, trade_date);

CREATE TABLE IF NOT EXISTS public.finra_ingestion_runs (
    source_type text NOT NULL,
    source_date date NOT NULL,
    source_file text,
    status text NOT NULL,
    row_count integer,
    error_message text,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_type, source_date)
);

CREATE TABLE IF NOT EXISTS public.finra_short_volume_daily_features (
    trade_date date NOT NULL,
    ticker text NOT NULL,
    short_volume_ratio numeric,
    short_exempt_ratio numeric,
    short_exempt_share_of_short numeric,
    svr_5d numeric,
    svr_20d numeric,
    svr_60d numeric,
    svr_z20 numeric,
    svr_z60 numeric,
    svr_pctile_5d_1y numeric,
    svr_pctile_20d_1y numeric,
    svr_pctile_60d_1y numeric,
    abnormal_svr_60d numeric,
    abnormal_svr_252d numeric,
    casv_5d numeric,
    casv_10d numeric,
    casv_20d numeric,
    svr_acceleration_5_20 numeric,
    svr_acceleration_20_60 numeric,
    persistence_above_median_20d numeric,
    persistence_above_median_60d numeric,
    persistence_above_75p_20d numeric,
    persistence_above_75p_60d numeric,
    persistence_above_90p_20d numeric,
    persistence_above_90p_60d numeric,
    short_activity_score numeric,
    data_quality_status text,
    calculated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_finra_svr_features_ticker_date
    ON public.finra_short_volume_daily_features (ticker, trade_date);

CREATE TABLE IF NOT EXISTS public.finra_short_interest (
    settlement_date date NOT NULL,
    publication_date date,
    publication_date_source text,
    ticker text NOT NULL,
    issue_name text,
    issuer_services_group_exchange_code text,
    market_class_code text,
    current_short_position_quantity numeric,
    previous_short_position_quantity numeric,
    change_previous_number numeric,
    change_percent numeric,
    average_daily_volume_quantity numeric,
    days_to_cover_quantity numeric,
    stock_split_flag text,
    revision_flag text,
    source_file text,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    data_quality_status text DEFAULT 'valid',
    PRIMARY KEY (settlement_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_finra_short_interest_ticker_settlement
    ON public.finra_short_interest (ticker, settlement_date);
CREATE INDEX IF NOT EXISTS idx_finra_short_interest_publication
    ON public.finra_short_interest (publication_date, ticker);

CREATE TABLE IF NOT EXISTS public.finra_short_interest_features (
    settlement_date date NOT NULL,
    publication_date date,
    ticker text NOT NULL,
    short_interest numeric,
    days_to_cover numeric,
    si_observation_count integer,
    si_change_1obs numeric,
    si_change_2obs numeric,
    si_change_4obs numeric,
    si_change_8obs numeric,
    si_change_12obs numeric,
    si_change_24obs numeric,
    si_change_48obs numeric,
    si_slope_3m numeric,
    si_slope_6m numeric,
    si_slope_12m numeric,
    si_slope_24m numeric,
    si_r2_3m numeric,
    si_r2_6m numeric,
    si_r2_12m numeric,
    si_r2_24m numeric,
    si_trend_quality numeric,
    si_acceleration numeric,
    own_si_percentile_1y numeric,
    own_si_percentile_3y numeric,
    own_si_percentile_5y numeric,
    market_si_percentile numeric,
    sector_si_percentile numeric,
    industry_si_percentile numeric,
    median_si_6m numeric,
    median_si_12m numeric,
    median_si_24m numeric,
    si_persistence_6m numeric,
    si_persistence_12m numeric,
    si_persistence_24m numeric,
    si_volatility_6m numeric,
    si_volatility_12m numeric,
    si_volatility_24m numeric,
    coefficient_variation_12m numeric,
    corporate_action_flag boolean DEFAULT false,
    calculated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (settlement_date, ticker)
);

ALTER TABLE IF EXISTS public.finra_short_interest_features
    ADD COLUMN IF NOT EXISTS industry_si_percentile numeric;
ALTER TABLE IF EXISTS public.finra_short_interest_features
    ADD COLUMN IF NOT EXISTS si_observation_count integer;
ALTER TABLE IF EXISTS public.finra_short_interest_features
    ADD COLUMN IF NOT EXISTS market_si_percentile numeric;
ALTER TABLE IF EXISTS public.finra_short_interest_features
    ADD COLUMN IF NOT EXISTS sector_si_percentile numeric;

CREATE INDEX IF NOT EXISTS idx_finra_si_features_ticker_settlement
    ON public.finra_short_interest_features (ticker, settlement_date);

CREATE TABLE IF NOT EXISTS public.finra_short_interest_intervals (
    ticker text NOT NULL,
    start_settlement_date date NOT NULL,
    end_settlement_date date NOT NULL,
    end_publication_date date,
    daily_observation_count integer,
    mean_svr numeric,
    median_svr numeric,
    max_svr numeric,
    mean_abnormal_svr numeric,
    interval_casv numeric,
    fraction_above_75p numeric,
    fraction_above_90p numeric,
    svr_acceleration numeric,
    stock_return numeric,
    benchmark_return numeric,
    residual_return numeric,
    volume_change numeric,
    realized_volatility numeric,
    actual_si_change numeric,
    expected_si_direction text,
    p_increase numeric,
    p_flat numeric,
    p_decrease numeric,
    flow_confirmation_signal text,
    signal_confidence numeric,
    component_json jsonb,
    calculated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, start_settlement_date, end_settlement_date)
);

CREATE INDEX IF NOT EXISTS idx_finra_si_intervals_end_date
    ON public.finra_short_interest_intervals (end_settlement_date, ticker);

CREATE TABLE IF NOT EXISTS public.us_equities_short_analytics (
    ticker text NOT NULL,
    analytics_date date NOT NULL,
    short_volume numeric,
    short_exempt_volume numeric,
    total_finra_volume numeric,
    short_volume_ratio numeric,
    short_exempt_ratio numeric,
    svr_5d numeric,
    svr_20d numeric,
    svr_60d numeric,
    svr_z20 numeric,
    svr_z60 numeric,
    svr_pctile_5d numeric,
    svr_pctile_20d numeric,
    svr_pctile_60d numeric,
    casv_5d numeric,
    casv_10d numeric,
    casv_20d numeric,
    svr_acceleration_5_20 numeric,
    svr_acceleration_20_60 numeric,
    persistence_above_median_20d numeric,
    persistence_above_median_60d numeric,
    persistence_above_75p_20d numeric,
    persistence_above_75p_60d numeric,
    persistence_above_90p_20d numeric,
    persistence_above_90p_60d numeric,
    si_settlement_date date,
    si_publication_date date,
    short_interest numeric,
    short_pct_float numeric,
    days_to_cover numeric,
    si_observation_count integer,
    si_change_1obs numeric,
    si_change_2obs numeric,
    si_change_4obs numeric,
    si_change_8obs numeric,
    si_change_12obs numeric,
    si_slope_6m numeric,
    si_slope_12m numeric,
    si_r2_6m numeric,
    si_r2_12m numeric,
    si_acceleration numeric,
    si_persistence_6m numeric,
    si_persistence_12m numeric,
    si_persistence_24m numeric,
    si_volatility_6m numeric,
    si_volatility_12m numeric,
    own_si_percentile_1y numeric,
    own_si_percentile_3y numeric,
    own_si_percentile_5y numeric,
    market_si_percentile numeric,
    sector_si_percentile numeric,
    industry_si_percentile numeric,
    close numeric,
    daily_return numeric,
    volume numeric,
    dollar_adv20 numeric,
    dollar_adv60 numeric,
    volatility_20d numeric,
    volatility_60d numeric,
    rel_return_1m numeric,
    rel_return_3m numeric,
    rel_return_6m numeric,
    rel_return_12m numeric,
    up_capture numeric,
    down_capture numeric,
    long_short_asymmetry numeric,
    short_pressure_effectiveness numeric,
    ma20 numeric,
    ma50 numeric,
    ma100 numeric,
    ma200 numeric,
    ma20_slope numeric,
    ma50_slope numeric,
    ma100_slope numeric,
    ma200_slope numeric,
    rsi14 numeric,
    macd numeric,
    macd_signal numeric,
    macd_histogram numeric,
    short_position_score numeric,
    short_activity_score numeric,
    short_flow_confirmation_score numeric,
    funding_short_score numeric,
    funding_short_quality_score numeric,
    unwind_risk_score numeric,
    short_regime text,
    regime_confidence numeric,
    regime_reason_json jsonb,
    data_quality_status text,
    calculated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, analytics_date)
);

CREATE INDEX IF NOT EXISTS idx_us_equities_short_analytics_date_scores
    ON public.us_equities_short_analytics
    (analytics_date, funding_short_score DESC, unwind_risk_score DESC);

ALTER TABLE IF EXISTS public.us_equities_short_analytics
    ADD COLUMN IF NOT EXISTS persistence_above_median_20d numeric;
ALTER TABLE IF EXISTS public.us_equities_short_analytics
    ADD COLUMN IF NOT EXISTS persistence_above_median_60d numeric;
ALTER TABLE IF EXISTS public.us_equities_short_analytics
    ADD COLUMN IF NOT EXISTS persistence_above_75p_20d numeric;
ALTER TABLE IF EXISTS public.us_equities_short_analytics
    ADD COLUMN IF NOT EXISTS persistence_above_75p_60d numeric;
ALTER TABLE IF EXISTS public.us_equities_short_analytics
    ADD COLUMN IF NOT EXISTS persistence_above_90p_20d numeric;
ALTER TABLE IF EXISTS public.us_equities_short_analytics
    ADD COLUMN IF NOT EXISTS persistence_above_90p_60d numeric;
ALTER TABLE IF EXISTS public.us_equities_short_analytics
    ADD COLUMN IF NOT EXISTS si_observation_count integer;
ALTER TABLE IF EXISTS public.us_equities_short_analytics
    ADD COLUMN IF NOT EXISTS market_si_percentile numeric;
ALTER TABLE IF EXISTS public.us_equities_short_analytics
    ADD COLUMN IF NOT EXISTS sector_si_percentile numeric;
ALTER TABLE IF EXISTS public.us_equities_short_analytics
    ADD COLUMN IF NOT EXISTS funding_short_quality_score numeric;
