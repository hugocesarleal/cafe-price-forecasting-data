-- ==============================================================================
-- 1. SCHEMAS OBRIGATÓRIOS DO PIPELINE DE DADOS
-- ==============================================================================

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS features;
CREATE SCHEMA IF NOT EXISTS predictions;
CREATE SCHEMA IF NOT EXISTS audit;

COMMENT ON SCHEMA raw IS 'Armazenamento bruto imutável de coletas e arquivos de fontes';
COMMENT ON SCHEMA staging IS 'Área transitória para limpeza inicial e tipagem';
COMMENT ON SCHEMA core IS 'Tabelas consolidadas e deduplicadas em granularidade diária';
COMMENT ON SCHEMA features IS 'Catálogo de variáveis, datasets imutáveis e variáveis de modelo';
COMMENT ON SCHEMA predictions IS 'Registro formal das previsões produzidas pela rede neural externa';
COMMENT ON SCHEMA audit IS 'Rastreabilidade de execuções, checagens de qualidade e eventos';
