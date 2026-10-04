-- ==============================================================================
-- 4. STORED PROCEDURES E FUNÇÕES DO PIPELINE DE DADOS
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- Auditoria de Execução
-- ------------------------------------------------------------------------------

CREATE OR REPLACE PROCEDURE audit.sp_register_pipeline_start(
    IN p_run_id UUID,
    IN p_job_name VARCHAR(60),
    IN p_metadata JSONB DEFAULT '{}'::jsonb
)
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO audit.pipeline_runs (run_id, job_name, started_at, status, metadata)
    VALUES (p_run_id, p_job_name, clock_timestamp(), 'RUNNING', p_metadata)
    ON CONFLICT (run_id) DO UPDATE SET
        job_name = EXCLUDED.job_name,
        started_at = EXCLUDED.started_at,
        status = 'RUNNING';
END;
$$;

CREATE OR REPLACE PROCEDURE audit.sp_register_pipeline_finish(
    IN p_run_id UUID,
    IN p_status VARCHAR(20),
    IN p_records_ingested INTEGER DEFAULT 0,
    IN p_records_features INTEGER DEFAULT 0,
    IN p_error_message TEXT DEFAULT NULL
)
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE audit.pipeline_runs
    SET finished_at = clock_timestamp(),
        status = p_status,
        records_ingested = p_records_ingested,
        records_features = p_records_features,
        error_message = p_error_message
    WHERE run_id = p_run_id;
END;
$$;

CREATE OR REPLACE FUNCTION audit.fn_emit_event(
    p_event_type VARCHAR(60),
    p_payload JSONB
)
RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    v_event_id BIGINT;
BEGIN
    INSERT INTO audit.pending_events (event_type, payload, status)
    VALUES (p_event_type, p_payload, 'PENDING')
    RETURNING event_id INTO v_event_id;
    RETURN v_event_id;
END;
$$;

-- ------------------------------------------------------------------------------
-- Upsert e Deduplicação Transacional: Staging -> Core
-- ------------------------------------------------------------------------------

CREATE OR REPLACE PROCEDURE core.sp_upsert_market_observations(IN p_run_id UUID)
LANGUAGE plpgsql
AS $$
BEGIN
    -- Agrega os dados da carga presentes em staging por data_ref
    INSERT INTO core.market_daily (
        data_ref,
        preco_arabica,
        preco_robusta,
        usd_brl,
        b3_cafe_ajuste,
        ice_kc,
        updated_at,
        pipeline_run_id
    )
    SELECT
        s.observation_date AS data_ref,
        MAX(CASE WHEN s.variable_name = 'preco_arabica' THEN s.value END) AS preco_arabica,
        MAX(CASE WHEN s.variable_name = 'preco_robusta' THEN s.value END) AS preco_robusta,
        MAX(CASE WHEN s.variable_name = 'usd_brl' THEN s.value END) AS usd_brl,
        MAX(CASE WHEN s.variable_name = 'b3_cafe_ajuste' THEN s.value END) AS b3_cafe_ajuste,
        MAX(CASE WHEN s.variable_name = 'ice_kc' THEN s.value END) AS ice_kc,
        clock_timestamp(),
        p_run_id
    FROM staging.market_observations s
    WHERE s.pipeline_run_id = p_run_id
    GROUP BY s.observation_date
    ON CONFLICT (data_ref) DO UPDATE SET
        preco_arabica   = COALESCE(EXCLUDED.preco_arabica, core.market_daily.preco_arabica),
        preco_robusta   = COALESCE(EXCLUDED.preco_robusta, core.market_daily.preco_robusta),
        usd_brl         = COALESCE(EXCLUDED.usd_brl, core.market_daily.usd_brl),
        b3_cafe_ajuste  = COALESCE(EXCLUDED.b3_cafe_ajuste, core.market_daily.b3_cafe_ajuste),
        ice_kc          = COALESCE(EXCLUDED.ice_kc, core.market_daily.ice_kc),
        updated_at      = clock_timestamp(),
        pipeline_run_id = EXCLUDED.pipeline_run_id;
END;
$$;

CREATE OR REPLACE PROCEDURE core.sp_upsert_weather_observations(IN p_run_id UUID)
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO core.weather_daily (
        data_ref,
        region,
        temp_min,
        temp_max,
        temp_media,
        precip_mm,
        umidade_rel,
        radiacao_mj,
        vento_ms,
        updated_at,
        pipeline_run_id
    )
    SELECT
        s.observation_date AS data_ref,
        s.region,
        MAX(CASE WHEN s.variable_name = 'temp_min' THEN s.value END) AS temp_min,
        MAX(CASE WHEN s.variable_name = 'temp_max' THEN s.value END) AS temp_max,
        MAX(CASE WHEN s.variable_name = 'temp_media' THEN s.value END) AS temp_media,
        MAX(CASE WHEN s.variable_name = 'precip_mm' THEN s.value END) AS precip_mm,
        MAX(CASE WHEN s.variable_name = 'umidade_rel' THEN s.value END) AS umidade_rel,
        MAX(CASE WHEN s.variable_name = 'radiacao_mj' THEN s.value END) AS radiacao_mj,
        MAX(CASE WHEN s.variable_name = 'vento_ms' THEN s.value END) AS vento_ms,
        clock_timestamp(),
        p_run_id
    FROM staging.weather_observations s
    WHERE s.pipeline_run_id = p_run_id
    GROUP BY s.observation_date, s.region
    ON CONFLICT (data_ref, region) DO UPDATE SET
        temp_min        = COALESCE(EXCLUDED.temp_min, core.weather_daily.temp_min),
        temp_max        = COALESCE(EXCLUDED.temp_max, core.weather_daily.temp_max),
        temp_media      = COALESCE(EXCLUDED.temp_media, core.weather_daily.temp_media),
        precip_mm       = COALESCE(EXCLUDED.precip_mm, core.weather_daily.precip_mm),
        umidade_rel     = COALESCE(EXCLUDED.umidade_rel, core.weather_daily.umidade_rel),
        radiacao_mj     = COALESCE(EXCLUDED.radiacao_mj, core.weather_daily.radiacao_mj),
        vento_ms        = COALESCE(EXCLUDED.vento_ms, core.weather_daily.vento_ms),
        updated_at      = clock_timestamp(),
        pipeline_run_id = EXCLUDED.pipeline_run_id;
END;
$$;
