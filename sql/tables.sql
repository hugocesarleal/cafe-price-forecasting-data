-- ==============================================================================
-- 2. TABELAS OBRIGATÓRIAS DO PIPELINE DE DADOS
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- SCHEMA AUDIT (Criado primeiro por conter chaves primárias de auditoria)
-- ------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit.pipeline_runs (
    run_id UUID PRIMARY KEY,
    job_name VARCHAR(60) NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    status VARCHAR(20) NOT NULL CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED', 'BLOCKED')),
    records_ingested INTEGER DEFAULT 0,
    records_features INTEGER DEFAULT 0,
    error_message TEXT,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS audit.data_quality_checks (
    check_id BIGSERIAL PRIMARY KEY,
    pipeline_run_id UUID REFERENCES audit.pipeline_runs(run_id) ON DELETE SET NULL,
    check_name VARCHAR(80) NOT NULL,
    table_name VARCHAR(80) NOT NULL,
    severity VARCHAR(20) NOT NULL CHECK (severity IN ('CRITICAL', 'WARNING')),
    passed BOOLEAN NOT NULL,
    details JSONB DEFAULT '{}'::jsonb,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS audit.model_runs (
    model_run_id UUID PRIMARY KEY,
    pipeline_run_id UUID REFERENCES audit.pipeline_runs(run_id) ON DELETE SET NULL,
    model_version VARCHAR(60) NOT NULL,
    executed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    input_dataset_version_id UUID,
    records_predicted INTEGER DEFAULT 0,
    status VARCHAR(20) NOT NULL CHECK (status IN ('COMPLETED', 'FAILED')),
    execution_notes TEXT
);

CREATE TABLE IF NOT EXISTS audit.pending_events (
    event_id BIGSERIAL PRIMARY KEY,
    event_type VARCHAR(60) NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    processed_at TIMESTAMPTZ,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'PROCESSED', 'FAILED'))
);

-- ------------------------------------------------------------------------------
-- SCHEMA RAW
-- ------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS raw.ingestion_files (
    file_id UUID PRIMARY KEY,
    pipeline_run_id UUID REFERENCES audit.pipeline_runs(run_id) ON DELETE SET NULL,
    source_name VARCHAR(60) NOT NULL,
    source_url VARCHAR(500),
    git_commit VARCHAR(64),
    file_name VARCHAR(255) NOT NULL,
    file_hash CHAR(64) NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    row_count INTEGER DEFAULT 0,
    has_changed BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS raw.market_observations (
    id BIGSERIAL PRIMARY KEY,
    file_id UUID REFERENCES raw.ingestion_files(file_id) ON DELETE SET NULL,
    pipeline_run_id UUID REFERENCES audit.pipeline_runs(run_id) ON DELETE SET NULL,
    source VARCHAR(60) NOT NULL,
    observation_date DATE NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    region VARCHAR(60),
    variable_name VARCHAR(80) NOT NULL,
    value NUMERIC(14, 4),
    unit VARCHAR(30) NOT NULL,
    load_version VARCHAR(50) NOT NULL,
    validation_status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS raw.weather_observations (
    id BIGSERIAL PRIMARY KEY,
    file_id UUID REFERENCES raw.ingestion_files(file_id) ON DELETE SET NULL,
    pipeline_run_id UUID REFERENCES audit.pipeline_runs(run_id) ON DELETE SET NULL,
    source VARCHAR(60) NOT NULL,
    observation_date DATE NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    region VARCHAR(30) NOT NULL,
    variable_name VARCHAR(80) NOT NULL,
    value NUMERIC(14, 4),
    unit VARCHAR(30) NOT NULL,
    load_version VARCHAR(50) NOT NULL,
    validation_status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- ------------------------------------------------------------------------------
-- SCHEMA STAGING
-- ------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS staging.market_observations (
    id BIGSERIAL PRIMARY KEY,
    pipeline_run_id UUID NOT NULL,
    source VARCHAR(60) NOT NULL,
    observation_date DATE NOT NULL,
    region VARCHAR(60),
    variable_name VARCHAR(80) NOT NULL,
    value NUMERIC(14, 4),
    unit VARCHAR(30) NOT NULL,
    load_version VARCHAR(50) NOT NULL
);

CREATE TABLE IF NOT EXISTS staging.weather_observations (
    id BIGSERIAL PRIMARY KEY,
    pipeline_run_id UUID NOT NULL,
    source VARCHAR(60) NOT NULL,
    observation_date DATE NOT NULL,
    region VARCHAR(30) NOT NULL,
    variable_name VARCHAR(80) NOT NULL,
    value NUMERIC(14, 4),
    unit VARCHAR(30) NOT NULL,
    load_version VARCHAR(50) NOT NULL
);

-- ------------------------------------------------------------------------------
-- SCHEMA CORE (Tabelas normalizadas e consolidadas em frequência diária)
-- ------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS core.market_daily (
    data_ref DATE PRIMARY KEY,
    preco_arabica NUMERIC(10, 2) CHECK (preco_arabica IS NULL OR preco_arabica > 0),
    preco_robusta NUMERIC(10, 2) CHECK (preco_robusta IS NULL OR preco_robusta > 0),
    usd_brl NUMERIC(10, 4) CHECK (usd_brl IS NULL OR usd_brl > 0),
    b3_cafe_ajuste NUMERIC(10, 2) CHECK (b3_cafe_ajuste IS NULL OR b3_cafe_ajuste > 0),
    ice_kc NUMERIC(10, 4) CHECK (ice_kc IS NULL OR ice_kc > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    pipeline_run_id UUID REFERENCES audit.pipeline_runs(run_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS core.weather_daily (
    data_ref DATE NOT NULL,
    region VARCHAR(30) NOT NULL,
    temp_min NUMERIC(6, 2),
    temp_max NUMERIC(6, 2),
    temp_media NUMERIC(6, 2),
    precip_mm NUMERIC(8, 2) CHECK (precip_mm IS NULL OR precip_mm >= 0),
    umidade_rel NUMERIC(6, 2) CHECK (umidade_rel IS NULL OR (umidade_rel >= 0 AND umidade_rel <= 100)),
    radiacao_mj NUMERIC(8, 2) CHECK (radiacao_mj IS NULL OR radiacao_mj >= 0),
    vento_ms NUMERIC(6, 2) CHECK (vento_ms IS NULL OR vento_ms >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    pipeline_run_id UUID REFERENCES audit.pipeline_runs(run_id) ON DELETE SET NULL,
    PRIMARY KEY (data_ref, region)
);

-- ------------------------------------------------------------------------------
-- SCHEMA FEATURES
-- ------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS features.variable_catalog (
    variable_name VARCHAR(80) PRIMARY KEY,
    source_group VARCHAR(40) NOT NULL,
    source_name VARCHAR(60) NOT NULL,
    unit VARCHAR(30) NOT NULL,
    is_raw_available BOOLEAN NOT NULL DEFAULT TRUE,
    is_required_for_feature_calculation BOOLEAN NOT NULL DEFAULT FALSE,
    is_model_feature BOOLEAN NOT NULL DEFAULT FALSE,
    selection_status VARCHAR(30) NOT NULL CHECK (selection_status IN (
        'KEEP', 'KEEP_WITH_CAVEAT', 'TEST_ONLY', 'DROP', 'TARGET', 'INTERNAL_INPUT'
    )),
    selection_reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS features.dataset_versions (
    dataset_version_id UUID PRIMARY KEY,
    version_tag VARCHAR(50) NOT NULL UNIQUE,
    cutoff_date DATE NOT NULL,
    start_date DATE NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    feature_count INTEGER NOT NULL DEFAULT 0,
    sha256_checksum CHAR(64) NOT NULL,
    is_valid BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS features.model_features (
    id BIGSERIAL PRIMARY KEY,
    dataset_version_id UUID NOT NULL REFERENCES features.dataset_versions(dataset_version_id) ON DELETE CASCADE,
    data_ref DATE NOT NULL,
    
    -- Grupo Climático Principal (KEEP)
    temp_media_cerrado NUMERIC(8, 3),
    umidade_rel_sulmg NUMERIC(8, 3),
    umidade_rel_cerrado NUMERIC(8, 3),
    radiacao_mj_sulmg NUMERIC(8, 3),
    radiacao_mj_cerrado NUMERIC(8, 3),
    precip_30d_sulmg NUMERIC(8, 3),
    precip_30d_cerrado NUMERIC(8, 3),
    precip_90d_sulmg NUMERIC(8, 3),
    precip_90d_cerrado NUMERIC(8, 3),
    tmin_min_30d_sulmg NUMERIC(8, 3),
    
    -- Grupo Climático Lote 3 e Calendário (KEEP)
    tmin_min_30d_cerrado NUMERIC(8, 3),
    dias_quente_30d_sulmg NUMERIC(8, 3),
    dias_quente_30d_cerrado NUMERIC(8, 3),
    sin_ano NUMERIC(8, 6),
    cos_ano NUMERIC(8, 6),
    
    -- Grupo Mercado (KEEP)
    preco_robusta NUMERIC(10, 2),
    usd_brl NUMERIC(10, 4),
    b3_cafe_ajuste NUMERIC(10, 2),
    ice_kc NUMERIC(10, 4),
    
    -- Alvos Futuros deslocados rigorosamente h dias a frente (TARGET)
    y_7d NUMERIC(10, 2),
    y_15d NUMERIC(10, 2),
    y_30d NUMERIC(10, 2),
    y_90d NUMERIC(10, 2),
    
    -- Flag de controle de poda lógica (sem delete fisico)
    is_pruned BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    
    CONSTRAINT uq_model_features_dataset_date UNIQUE (dataset_version_id, data_ref)
);

-- ------------------------------------------------------------------------------
-- SCHEMA PREDICTIONS
-- ------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS predictions.forecasts (
    forecast_id UUID PRIMARY KEY,
    reference_date DATE NOT NULL,
    target_date DATE NOT NULL,
    horizon_days INTEGER NOT NULL CHECK (horizon_days IN (7, 15, 30, 90)),
    predicted_value NUMERIC(10, 2) NOT NULL CHECK (predicted_value > 0),
    dataset_version_id UUID NOT NULL REFERENCES features.dataset_versions(dataset_version_id),
    model_version VARCHAR(60) NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    pipeline_run_id UUID NOT NULL REFERENCES audit.pipeline_runs(run_id),
    forecast_status VARCHAR(30) NOT NULL DEFAULT 'ACTIVE',
    CONSTRAINT uq_forecast_ref_horizon_model UNIQUE (reference_date, horizon_days, model_version)
);
