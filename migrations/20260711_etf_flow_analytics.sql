-- ETF flow analytics layer.
-- Idempotent schema migration. No destructive operations.

CREATE TABLE IF NOT EXISTS public.etf_daily_raw (
    date date NOT NULL,
    ticker text NOT NULL,
    issuer text,
    fund_name text,
    asset_class text,
    segment text,
    sector text,
    theme text,
    style text,
    region text,
    duration_bucket text,
    credit_quality text,
    shares_outstanding numeric CHECK (shares_outstanding IS NULL OR shares_outstanding >= 0),
    nav numeric CHECK (nav IS NULL OR nav >= 0),
    close numeric,
    adjusted_close numeric,
    aum numeric CHECK (aum IS NULL OR aum >= 0),
    volume numeric CHECK (volume IS NULL OR volume >= 0),
    currency text DEFAULT 'USD',
    is_active boolean DEFAULT true,
    source text NOT NULL,
    loaded_at timestamptz DEFAULT now(),
    PRIMARY KEY (date, ticker, source)
);

CREATE TABLE IF NOT EXISTS public.etf_master (
    ticker text PRIMARY KEY,
    fund_name text,
    issuer text,
    asset_class text,
    primary_segment text,
    secondary_segment text,
    sector text,
    theme text,
    style text,
    region text,
    duration_bucket text,
    credit_quality text,
    commodity_type text,
    currency text DEFAULT 'USD',
    benchmark text,
    inception_date date,
    is_leveraged boolean DEFAULT false,
    is_inverse boolean DEFAULT false,
    is_active boolean DEFAULT true,
    flow_eligible boolean DEFAULT true,
    classification_version text DEFAULT '2026-07-11',
    updated_at timestamptz DEFAULT now()
);

ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS exposure_id text;
ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS exposure_name text;
ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS exposure_type text;
ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS benchmark_family text;
ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS is_primary_proxy boolean DEFAULT false;
ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS allocation_weight_cap numeric DEFAULT 0.50;

CREATE TABLE IF NOT EXISTS public.etf_issuer_publication_schedule (
    issuer text NOT NULL,
    source text NOT NULL,
    expected_release_timezone text,
    expected_release_time text,
    typical_lag_days integer DEFAULT 0,
    allowed_lag_days integer DEFAULT 1,
    business_day_rule text DEFAULT 'issuer_business_day',
    weekend_rule text DEFAULT 'next_business_day',
    holiday_rule text DEFAULT 'next_business_day',
    last_successful_release_at timestamptz,
    median_release_delay_minutes numeric,
    release_delay_p90_minutes numeric,
    is_active boolean DEFAULT true,
    updated_at timestamptz DEFAULT now(),
    PRIMARY KEY (issuer, source)
);

CREATE TABLE IF NOT EXISTS public.etf_issuer_data_availability (
    analysis_timestamp timestamptz NOT NULL,
    effective_date date NOT NULL,
    issuer text NOT NULL,
    expected_release_at timestamptz,
    source_published_at timestamptz,
    ingested_at timestamptz,
    availability_status text NOT NULL,
    eligible_aum numeric,
    reported_aum numeric,
    freshness_hours numeric,
    data_quality_score numeric,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (analysis_timestamp, effective_date, issuer)
);

CREATE TABLE IF NOT EXISTS public.etf_flow_daily (
    date date NOT NULL,
    ticker text NOT NULL,
    issuer text,
    shares_outstanding numeric,
    shares_change numeric,
    nav numeric,
    nav_lag1 numeric,
    close numeric,
    return_1d numeric,
    aum numeric,
    aum_lag1 numeric,
    aum_avg_20d numeric,
    estimated_flow numeric,
    estimated_flow_lag_nav numeric,
    flow_pct_aum numeric,
    flow_pct_aum_lag numeric,
    flow_pct_aum_raw numeric,
    flow_pct_aum_winsorized numeric,
    volume numeric,
    data_quality_score numeric,
    flow_valid boolean,
    missing_data_flags text[],
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (date, ticker)
);

CREATE TABLE IF NOT EXISTS public.etf_flow_features (
    date date NOT NULL,
    ticker text NOT NULL,
    primary_segment text,
    issuer text,
    flow_1d numeric,
    flow_5d numeric,
    flow_20d numeric,
    flow_60d numeric,
    flow_5d_pct_aum numeric,
    flow_20d_pct_aum numeric,
    flow_60d_pct_aum numeric,
    flow_ema_5 numeric,
    flow_ema_20 numeric,
    flow_slope_5 numeric,
    flow_slope_20 numeric,
    flow_acceleration numeric,
    flow_zscore_20 numeric,
    flow_zscore_60 numeric,
    flow_percentile_252 numeric,
    positive_flow_days_5d integer,
    positive_flow_days_20d integer,
    flow_persistence_20d numeric,
    return_1d numeric,
    return_5d numeric,
    return_20d numeric,
    price_trend_score numeric,
    price_flow_state text,
    data_quality_score numeric,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (date, ticker)
);

CREATE TABLE IF NOT EXISTS public.etf_flow_segment_daily (
    date date NOT NULL,
    segment_type text NOT NULL,
    segment text NOT NULL,
    flow_1d numeric,
    flow_5d numeric,
    flow_20d numeric,
    flow_60d numeric,
    flow_pct_aum_5d numeric,
    flow_pct_aum_20d numeric,
    flow_zscore_20d numeric,
    flow_momentum numeric,
    flow_breadth_20d numeric,
    issuer_consensus numeric,
    concentration_penalty numeric,
    score numeric,
    signal text,
    confidence numeric,
    top_contributors jsonb,
    top_detractors jsonb,
    eligible_fund_count integer,
    issuer_count integer,
    data_quality_score numeric,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (date, segment_type, segment)
);

COMMENT ON COLUMN public.etf_flow_segment_daily.flow_breadth_20d IS 'Deprecated: ETF breadth is retained for compatibility only and is not used in active scoring/reporting.';

CREATE TABLE IF NOT EXISTS public.etf_flow_exposure_daily (
    date date NOT NULL,
    analysis_timestamp timestamptz NOT NULL,
    exposure_id text NOT NULL,
    exposure_name text NOT NULL,
    exposure_type text,
    known_flow_1d numeric,
    known_flow_5d numeric,
    known_flow_20d numeric,
    normalized_flow_1d numeric,
    normalized_flow_5d numeric,
    normalized_flow_20d numeric,
    flow_momentum numeric,
    flow_acceleration numeric,
    flow_persistence numeric,
    reported_etf_count integer,
    eligible_etf_count integer,
    reported_issuer_count integer,
    eligible_issuer_count integer,
    issuer_aum_coverage numeric,
    data_availability_status text,
    issuer_agreement_score numeric,
    signal_reliability numeric,
    raw_flow_score numeric,
    adjusted_flow_score numeric,
    flow_signal text,
    is_provisional boolean,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (date, analysis_timestamp, exposure_id)
);

CREATE TABLE IF NOT EXISTS public.etf_flow_consensus_daily (
    date date NOT NULL,
    segment text NOT NULL,
    positive_issuer_count integer,
    negative_issuer_count integer,
    neutral_issuer_count integer,
    positive_etf_count integer,
    negative_etf_count integer,
    issuer_weighted_consensus numeric,
    equal_weight_consensus numeric,
    aum_weighted_consensus numeric,
    flow_direction_agreement_ratio numeric,
    cross_issuer_dispersion numeric,
    dominant_issuer_share numeric,
    issuer_concentration_penalty numeric,
    direction text,
    confidence numeric,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (date, segment)
);

CREATE TABLE IF NOT EXISTS public.etf_flow_rotation_daily (
    date date NOT NULL,
    segment_type text NOT NULL,
    segment text NOT NULL,
    rank_today integer,
    rank_5d_ago integer,
    rank_20d_ago integer,
    rank_change_5d integer,
    rank_change_20d integer,
    flow_score numeric,
    flow_score_change_5d numeric,
    flow_score_change_20d numeric,
    flow_acceleration numeric,
    price_relative_strength_change numeric,
    rotation_status text,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (date, segment_type, segment)
);

CREATE TABLE IF NOT EXISTS public.etf_flow_regime_daily (
    date date PRIMARY KEY,
    existing_regime_score numeric,
    flow_regime_score numeric,
    combined_regime_score numeric,
    flow_regime_label text,
    flow_regime_confidence numeric,
    regime_conflict_flag boolean,
    dominant_allocation_direction text,
    largest_flow_contradiction text,
    strongest_cross_issuer_theme text,
    largest_deterioration text,
    components jsonb,
    created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.etf_flow_forward_signals (
    date date NOT NULL,
    segment_type text NOT NULL,
    segment text NOT NULL,
    outperformance_score numeric,
    probability_bucket text,
    flow_contribution numeric,
    price_contribution numeric,
    breadth_contribution numeric,
    regime_contribution numeric,
    confidence numeric,
    catalysts text[],
    invalidators text[],
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (date, segment_type, segment)
);

CREATE TABLE IF NOT EXISTS public.etf_flow_audit_flags (
    date date NOT NULL,
    flag_type text NOT NULL,
    severity text NOT NULL,
    segment text,
    description text NOT NULL,
    suggested_fix text,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (date, flag_type, segment, description)
);

CREATE TABLE IF NOT EXISTS public.etf_representative_map (
    exposure_id text PRIMARY KEY,
    exposure_name text NOT NULL,
    exposure_type text,
    primary_ticker text NOT NULL,
    secondary_ticker text,
    tertiary_ticker text,
    primary_issuer text,
    benchmark_family text,
    selection_priority integer DEFAULT 100,
    aggregation_allowed boolean DEFAULT false,
    divergence_threshold_z numeric DEFAULT 1.5,
    minimum_history_days integer DEFAULT 40,
    is_active boolean DEFAULT true,
    notes text,
    updated_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.etf_flow_signal_daily (
    date date NOT NULL,
    ticker text NOT NULL,
    exposure_id text,
    exposure_type text,
    flow_1d numeric,
    flow_5d numeric,
    flow_20d numeric,
    flow_60d numeric,
    flow_pct_aum_1d numeric,
    flow_pct_aum_5d numeric,
    flow_pct_aum_20d numeric,
    flow_pct_aum_60d numeric,
    flow_zscore_1d numeric,
    flow_zscore_5d numeric,
    flow_zscore_20d numeric,
    flow_zscore_60d numeric,
    flow_percentile_20d numeric,
    flow_percentile_60d numeric,
    positive_flow_days_20d integer,
    positive_flow_days_60d integer,
    flow_persistence_20d numeric,
    flow_persistence_60d numeric,
    consecutive_inflow_days integer,
    consecutive_outflow_days integer,
    flow_momentum numeric,
    flow_acceleration numeric,
    flow_rotation_state text,
    volume_ratio_20d numeric,
    volume_ratio_60d numeric,
    volume_zscore_20d numeric,
    volume_zscore_60d numeric,
    dollar_volume numeric,
    dollar_volume_ratio_20d numeric,
    dollar_volume_zscore_60d numeric,
    price_state text,
    flow_state text,
    volume_state text,
    price_flow_volume_state text,
    state_strength numeric,
    state_confidence numeric,
    interpretation text,
    data_quality_score numeric,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (date, ticker)
);

CREATE TABLE IF NOT EXISTS public.etf_market_flow_daily (
    date date PRIMARY KEY,
    equity_risk_flow_score numeric,
    credit_risk_flow_score numeric,
    sector_cyclicality_flow_score numeric,
    cash_preference_score numeric,
    duration_demand_score numeric,
    duration_liquidity_score numeric,
    gold_signal numeric,
    bitcoin_signal numeric,
    alternative_asset_score numeric,
    alternative_asset_interpretation text,
    market_flow_score numeric,
    market_flow_regime text,
    market_flow_reliability numeric,
    created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.etf_flow_divergence_flags (
    date date NOT NULL,
    flag_type text NOT NULL,
    severity text NOT NULL,
    exposure_id text,
    primary_ticker text,
    comparison_ticker text,
    description text NOT NULL,
    interpretation text,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (date, flag_type, exposure_id, primary_ticker, comparison_ticker)
);

CREATE INDEX IF NOT EXISTS idx_etf_daily_raw_date ON public.etf_daily_raw (date);
CREATE INDEX IF NOT EXISTS idx_etf_daily_raw_ticker_date ON public.etf_daily_raw (ticker, date);
CREATE INDEX IF NOT EXISTS idx_etf_flow_daily_date ON public.etf_flow_daily (date);
CREATE INDEX IF NOT EXISTS idx_etf_flow_daily_ticker_date ON public.etf_flow_daily (ticker, date);
CREATE INDEX IF NOT EXISTS idx_etf_flow_features_ticker_date ON public.etf_flow_features (ticker, date);
CREATE INDEX IF NOT EXISTS idx_etf_flow_segment_daily_segment_date ON public.etf_flow_segment_daily (segment, date);
CREATE INDEX IF NOT EXISTS idx_etf_flow_segment_daily_type_date ON public.etf_flow_segment_daily (segment_type, date);
CREATE INDEX IF NOT EXISTS idx_etf_flow_consensus_daily_segment_date ON public.etf_flow_consensus_daily (segment, date);
CREATE INDEX IF NOT EXISTS idx_etf_flow_rotation_daily_segment_date ON public.etf_flow_rotation_daily (segment, date);
CREATE INDEX IF NOT EXISTS idx_etf_flow_exposure_daily_exposure_date ON public.etf_flow_exposure_daily (exposure_id, date);
CREATE INDEX IF NOT EXISTS idx_etf_flow_exposure_daily_date_type ON public.etf_flow_exposure_daily (date, exposure_type);
CREATE INDEX IF NOT EXISTS idx_etf_issuer_data_availability_issuer_date ON public.etf_issuer_data_availability (issuer, effective_date);
CREATE INDEX IF NOT EXISTS idx_etf_issuer_data_availability_analysis ON public.etf_issuer_data_availability (analysis_timestamp);
CREATE INDEX IF NOT EXISTS idx_etf_representative_map_primary ON public.etf_representative_map (primary_ticker);
CREATE INDEX IF NOT EXISTS idx_etf_flow_signal_daily_ticker_date ON public.etf_flow_signal_daily (ticker, date);
CREATE INDEX IF NOT EXISTS idx_etf_flow_signal_daily_exposure_date ON public.etf_flow_signal_daily (exposure_id, date);
CREATE INDEX IF NOT EXISTS idx_etf_flow_divergence_flags_date ON public.etf_flow_divergence_flags (date);
