-- ==============================================================================
-- 5. TRIGGERS DO BANCO DE DADOS
-- Responsabilidades limitadas estritamente a auditoria, updated_at e sinalização
-- (Sem downloads, scraping, chamadas HTTP ou inferência)
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- 1. Trigger para atualizar updated_at automaticamente
-- ------------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION audit.fn_update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_variable_catalog_updated_at ON features.variable_catalog;
CREATE TRIGGER trg_variable_catalog_updated_at
BEFORE UPDATE ON features.variable_catalog
FOR EACH ROW
EXECUTE FUNCTION audit.fn_update_timestamp();

-- ------------------------------------------------------------------------------
-- 2. Trigger para sinalizar novo dataset pronto em audit.pending_events
-- ------------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION features.fn_notify_dataset_ready()
RETURNS TRIGGER AS $$
BEGIN
    -- Só emite evento se o dataset for marcado como válido
    IF NEW.is_valid = TRUE THEN
        INSERT INTO audit.pending_events (event_type, payload, status)
        VALUES (
            'DATASET_READY',
            jsonb_build_object(
                'dataset_version_id', NEW.dataset_version_id,
                'version_tag', NEW.version_tag,
                'cutoff_date', NEW.cutoff_date,
                'start_date', NEW.start_date,
                'row_count', NEW.row_count,
                'feature_count', NEW.feature_count,
                'sha256_checksum', NEW.sha256_checksum,
                'created_at', NEW.created_at
            ),
            'PENDING'
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_dataset_version_ready ON features.dataset_versions;
CREATE TRIGGER trg_dataset_version_ready
AFTER INSERT OR UPDATE OF is_valid ON features.dataset_versions
FOR EACH ROW
EXECUTE FUNCTION features.fn_notify_dataset_ready();
