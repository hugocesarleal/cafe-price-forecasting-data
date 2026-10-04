-- ==============================================================================
-- 3. ÍNDICES OBRIGATÓRIOS DO PIPELINE DE DADOS
-- ==============================================================================

-- Raw Observations
CREATE INDEX IF NOT EXISTS idx_raw_market_obs_date ON raw.market_observations (observation_date);
CREATE INDEX IF NOT EXISTS idx_raw_market_obs_var ON raw.market_observations (variable_name);
CREATE INDEX IF NOT EXISTS idx_raw_market_obs_run ON raw.market_observations (pipeline_run_id);

CREATE INDEX IF NOT EXISTS idx_raw_weather_obs_date ON raw.weather_observations (observation_date);
CREATE INDEX IF NOT EXISTS idx_raw_weather_obs_reg_var ON raw.weather_observations (region, variable_name);
CREATE INDEX IF NOT EXISTS idx_raw_weather_obs_run ON raw.weather_observations (pipeline_run_id);

CREATE INDEX IF NOT EXISTS idx_raw_files_hash ON raw.ingestion_files (file_hash);
CREATE INDEX IF NOT EXISTS idx_raw_files_run ON raw.ingestion_files (pipeline_run_id);

-- Staging Observations
CREATE INDEX IF NOT EXISTS idx_staging_market_run ON staging.market_observations (pipeline_run_id);
CREATE INDEX IF NOT EXISTS idx_staging_weather_run ON staging.weather_observations (pipeline_run_id);

-- Core
CREATE INDEX IF NOT EXISTS idx_core_market_data ON core.market_daily (data_ref);
CREATE INDEX IF NOT EXISTS idx_core_weather_data ON core.weather_daily (data_ref);
CREATE INDEX IF NOT EXISTS idx_core_weather_reg_data ON core.weather_daily (region, data_ref);

-- Features
CREATE INDEX IF NOT EXISTS idx_features_model_dataset ON features.model_features (dataset_version_id);
CREATE INDEX IF NOT EXISTS idx_features_model_date ON features.model_features (data_ref);
CREATE INDEX IF NOT EXISTS idx_features_model_pruned ON features.model_features (dataset_version_id, is_pruned);
CREATE INDEX IF NOT EXISTS idx_features_versions_cutoff ON features.dataset_versions (cutoff_date);

-- Predictions
CREATE INDEX IF NOT EXISTS idx_predictions_ref_date ON predictions.forecasts (reference_date);
CREATE INDEX IF NOT EXISTS idx_predictions_target_date ON predictions.forecasts (target_date);
CREATE INDEX IF NOT EXISTS idx_predictions_dataset_ver ON predictions.forecasts (dataset_version_id);

-- Audit
CREATE INDEX IF NOT EXISTS idx_audit_runs_status ON audit.pipeline_runs (status);
CREATE INDEX IF NOT EXISTS idx_audit_checks_run ON audit.data_quality_checks (pipeline_run_id, passed);
CREATE INDEX IF NOT EXISTS idx_audit_events_status ON audit.pending_events (status, created_at);
