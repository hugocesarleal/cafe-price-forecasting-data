# Dicionário de Dados e Catálogo de Variáveis

Este documento consolida a especificação do Catálogo de Variáveis (`features.variable_catalog`) e a modelagem detalhada das tabelas dos 6 schemas PostgreSQL.

---

## 1. Catálogo de Variáveis (`features.variable_catalog`)

A tabela `features.variable_catalog` define a taxonomia, procedência e permissão de entrada na matriz final de treinamento (`features.model_features`).

### Valores Válidos para `selection_status`:
- `KEEP`: Variável aprovada para a tabela final de features.
- `TARGET`: Variável-alvo principal (`preco_arabica`).
- `KEEP_WITH_CAVEAT`: Mantida em raw/core, mas **excluída** da matriz de features do modelo inicial.
- `TEST_ONLY`: Restrita para estudos pontuais; **excluída** da matriz de features de produção.
- `DROP`: Descartada das features do modelo; retida em raw/core para histórico ou auditoria.
- `INTERNAL_INPUT`: Usada exclusivamente para derivação de outras variáveis (ex.: mínimas diárias brutas para calcular `tmin_min_30d`), mas não publicada isoladamente.

> **Atraso de publicação do clima**: toda variável de clima, bruta ou calculada, entra na matriz de features com o atraso do NASA POWER — 3 dias (5 para `radiacao_mj_*`). "Últimos 30 dias" significa os 30 dias terminados em $t-3$. Ver `PUBLICATION_LAG_DAYS` em `src/feature_builder.py`.

### Tabela do Catálogo

| variable_name | source_group | source_name | unit | is_raw_available | is_required_for_feature_calculation | is_model_feature | selection_status | selection_reason |
|---|---|---|---|---|---|---|---|---|
| `preco_arabica` | mercado | CEPEA/ESALQ | R$/sc 60kg | TRUE | TRUE | FALSE | `TARGET` | Variável-alvo principal do MVP. Base das projeções y_7d, y_15d, y_30d, y_90d. Não entra contemporânea como feature. |
| `preco_robusta` | mercado | CEPEA/ESALQ | R$/sc 60kg | TRUE | TRUE | TRUE | `KEEP` | Indicador CEPEA Conilon/Robusta. Substituição e correlação de mercado. |
| `usd_brl` | mercado | BCB PTAX | BRL | TRUE | FALSE | TRUE | `KEEP` | Cotação PTAX Venda. Impacto direto na formação de preço para exportação. |
| `b3_cafe_ajuste` | mercado | B3 | USD/sc | TRUE | FALSE | TRUE | `KEEP` | Preço de ajuste do contrato futuro ICF B3. |
| `ice_kc` | mercado | ICE US | cUSD/lb | TRUE | FALSE | TRUE | `KEEP` | Cotação internacional de referência do café arábica Coffee C. |
| `temp_media_cerrado` | clima | NASA POWER | °C | TRUE | FALSE | TRUE | `KEEP` | Temperatura média no polo produtor de Patrocínio/MG (Cerrado). |
| `umidade_rel_sulmg` | clima | NASA POWER | % | TRUE | FALSE | TRUE | `KEEP` | Umidade relativa do ar em Varginha/MG (Sul de Minas). |
| `umidade_rel_cerrado`| clima | NASA POWER | % | TRUE | FALSE | TRUE | `KEEP` | Umidade relativa do ar em Patrocínio/MG (Cerrado). |
| `radiacao_mj_sulmg` | clima | NASA POWER | MJ/m² | TRUE | FALSE | TRUE | `KEEP` | Radiação solar incidente no Sul de Minas. |
| `radiacao_mj_cerrado`| clima | NASA POWER | MJ/m² | TRUE | FALSE | TRUE | `KEEP` | Radiação solar incidente no Cerrado Mineiro. |
| `precip_30d_sulmg` | clima | Calculada | mm | FALSE | FALSE | TRUE | `KEEP` | Acumulado móvel de precipitação de 30 dias no Sul de Minas. |
| `precip_30d_cerrado`| clima | Calculada | mm | FALSE | FALSE | TRUE | `KEEP` | Acumulado móvel de precipitação de 30 dias no Cerrado. |
| `precip_90d_sulmg` | clima | Calculada | mm | FALSE | FALSE | TRUE | `KEEP` | Acumulado móvel de precipitação de 90 dias no Sul de Minas. |
| `precip_90d_cerrado`| clima | Calculada | mm | FALSE | FALSE | TRUE | `KEEP` | Acumulado móvel de precipitação de 90 dias no Cerrado. |
| `tmin_min_30d_sulmg`| clima | Calculada | °C | FALSE | FALSE | TRUE | `KEEP` | Mínima absoluta dos últimos 30 dias no Sul de Minas. Indicador de frio/geada. |
| `tmin_min_30d_cerrado`| clima | Calculada | °C | FALSE | FALSE | TRUE | `KEEP` | Mínima absoluta dos últimos 30 dias no Cerrado Mineiro. |
| `dias_quente_30d_sulmg`| clima | Calculada | dias | FALSE | FALSE | TRUE | `KEEP` | Contagem de dias com Tmax > 32°C nos últimos 30 dias no Sul de Minas. |
| `dias_quente_30d_cerrado`| clima | Calculada | dias | FALSE | FALSE | TRUE | `KEEP` | Contagem de dias com Tmax > 32°C nos últimos 30 dias no Cerrado. |
| `sin_ano` | calendario | Calculada | - | FALSE | FALSE | TRUE | `KEEP` | Codificação cíclica da sazonalidade: sin(2*pi*dia_ano/365.25). |
| `cos_ano` | calendario | Calculada | - | FALSE | FALSE | TRUE | `KEEP` | Codificação cíclica da sazonalidade: cos(2*pi*dia_ano/365.25). |
| `precip_mm_bambui` | clima | NASA POWER | mm | TRUE | FALSE | FALSE | `DROP` | Ponto hiperlocal com menor relevância nacional agregada. Mantido em raw/core. |
| `precip_30d_bambui` | clima | Calculada | mm | FALSE | FALSE | FALSE | `DROP` | Excluída por auditoria. |
| `precip_90d_bambui` | clima | Calculada | mm | FALSE | FALSE | FALSE | `DROP` | Excluída por auditoria. |
| `precip_90d_anom_bambui` | clima | Calculada | z-score | FALSE | FALSE | FALSE | `DROP` | Variável de anomalia excluída por auditoria. |
| `precip_90d_anom_cerrado`| clima | Calculada | z-score | FALSE | FALSE | FALSE | `DROP` | Variável de anomalia excluída por auditoria. |
| `dias_frio_30d_bambui` | clima | Calculada | dias | FALSE | FALSE | FALSE | `DROP` | Excluída por auditoria. |
| `dias_frio_30d_cerrado`| clima | Calculada | dias | FALSE | FALSE | FALSE | `DROP` | Excluída por auditoria. |
| `umidade_rel_bambui` | clima | NASA POWER | % | TRUE | FALSE | FALSE | `DROP` | Excluída por auditoria. |
| `radiacao_mj_bambui` | clima | NASA POWER | MJ/m² | TRUE | FALSE | FALSE | `DROP` | Excluída por auditoria. |
| `vento_ms_bambui` | clima | NASA POWER | m/s | TRUE | FALSE | FALSE | `DROP` | Excluída por auditoria. |
| `vento_ms_sulmg` | clima | NASA POWER | m/s | TRUE | FALSE | FALSE | `DROP` | Excluída por auditoria. |
| `vento_ms_cerrado` | clima | NASA POWER | m/s | TRUE | FALSE | FALSE | `DROP` | Excluída por auditoria. |
| `tmin_min_30d_bambui`| clima | Calculada | °C | FALSE | FALSE | FALSE | `DROP` | Excluída por auditoria. |
| `dias_quente_30d_bambui`| clima | Calculada | dias | FALSE | FALSE | FALSE | `DROP` | Excluída por auditoria. |
| `dias_secos_seq_bambui` | clima | Calculada | dias | FALSE | FALSE | FALSE | `DROP` | Lote 3 descarte por ressalva de estabilidade temporal. |
| `dias_secos_seq_sulmg` | clima | Calculada | dias | FALSE | FALSE | FALSE | `DROP` | Lote 3 descarte por ressalva de estabilidade temporal. |
| `dias_secos_seq_cerrado`| clima | Calculada | dias | FALSE | FALSE | FALSE | `DROP` | Lote 3 descarte por ressalva de estabilidade temporal. |
| `deficit_hidrico_60d_bambui`| clima | Calculada | mm | FALSE | FALSE | FALSE | `DROP` | Dependência de aproximação de evapotranspiração não validada. |
| `deficit_hidrico_60d_sulmg` | clima | Calculada | mm | FALSE | FALSE | FALSE | `DROP` | Dependência de aproximação de evapotranspiração não validada. |
| `deficit_hidrico_60d_cerrado`| clima | Calculada | mm | FALSE | FALSE | FALSE | `DROP` | Dependência de aproximação de evapotranspiração não validada. |
| `preco_arabica_usd` | mercado | Calculada | USD/sc | TRUE | FALSE | FALSE | `DROP` | Redundância com preco_arabica e usd_brl. |
| `preco_robusta_usd` | mercado | Calculada | USD/sc | TRUE | FALSE | FALSE | `DROP` | Redundância com preco_robusta e usd_brl. |
| `usd_brl_compra` | mercado | BCB PTAX | BRL | TRUE | FALSE | FALSE | `DROP` | PTAX Venda já é mantida. |
| `selic` | macro | BCB SGS | % a.a. | TRUE | FALSE | FALSE | `DROP` | Descartada da matriz de features iniciais. |
| `ipca` | macro | BCB SGS | % m.m. | TRUE | FALSE | FALSE | `DROP` | Frequência mensal com baixa reatividade em prazos curtos. |
| `cot_mm_long` | mercado | CFTC | contratos | TRUE | FALSE | FALSE | `DROP` | Descartada inicialmente por atraso de publicação. |
| `cot_mm_short` | mercado | CFTC | contratos | TRUE | FALSE | FALSE | `DROP` | Descartada inicialmente por atraso de publicação. |
| `cot_mm_net` | mercado | CFTC | contratos | TRUE | FALSE | FALSE | `DROP` | Descartada inicialmente por atraso de publicação. |
| `cot_open_interest` | mercado | CFTC | contratos | TRUE | FALSE | FALSE | `DROP` | Descartada inicialmente por atraso de publicação. |
| `b3_open_interest` | mercado | B3 | contratos | TRUE | FALSE | FALSE | `DROP` | Descartada inicialmente por auditoria. |
| `dxy` | mercado | Yahoo Finance | pontos | TRUE | FALSE | FALSE | `DROP` | Descartada da matriz de features iniciais. |
| `brent` | mercado | Yahoo Finance | USD/bbl | TRUE | FALSE | FALSE | `DROP` | Descartada da matriz de features iniciais. |
| `oni` | clima | NOAA CPC | °C | TRUE | FALSE | FALSE | `DROP` | Descartada por frequência mensal e atraso de consolidação. |
| `oni_fase` | clima | NOAA CPC | categorico | TRUE | FALSE | FALSE | `DROP` | Descartada inicialmente. |
| `ret_1d` | mercado | Calculada | ratio | FALSE | FALSE | FALSE | `DROP` | Transformação de preço não aprovada no contrato inicial. |
| `vol_20d` | mercado | Calculada | ratio | FALSE | FALSE | FALSE | `DROP` | Transformação de preço não aprovada no contrato inicial. |
| `preco_media_20d` | mercado | Calculada | R$/sc | FALSE | FALSE | FALSE | `DROP` | Transformação de preço não aprovada no contrato inicial. |
| `spread_arab_rob` | mercado | Calculada | R$/sc | FALSE | FALSE | FALSE | `DROP` | Transformação não autorizada no contrato do modelo. |
| `base_local` | mercado | Calculada | R$/sc | FALSE | FALSE | FALSE | `DROP` | Descartada inicialmente. |
| `base_local_pct` | mercado | Calculada | % | FALSE | FALSE | FALSE | `DROP` | Descartada inicialmente. |
| `ice_kc_brl_saca` | mercado | Calculada | R$/sc | FALSE | FALSE | FALSE | `DROP` | Descartada inicialmente. |
| `mes` | calendario | Calculada | inteiro | FALSE | FALSE | FALSE | `DROP` | Sazonalidade tratada via sin_ano/cos_ano. |
| `semana_ano` | calendario | Calculada | inteiro | FALSE | FALSE | FALSE | `DROP` | Sazonalidade tratada via sin_ano/cos_ano. |
| `dia_util` | calendario | Calculada | booleano | FALSE | FALSE | FALSE | `DROP` | Descartada inicialmente. |
| `ano_carga_alta` | safra | CONAB | booleano | FALSE | FALSE | FALSE | `DROP` | Descartada inicialmente. |
| `fase_fenologica` | safra | Manual | categorico | FALSE | FALSE | FALSE | `DROP` | Descartada inicialmente. |
| `risco_geada` | clima | Calculada | booleano | FALSE | FALSE | FALSE | `DROP` | Descartada inicialmente. |

---

## 2. Modelagem Detalhada das Tabelas PostgreSQL

### Schema: `raw`
- **`raw.ingestion_files`**:
  - `file_id` (UUID, PK)
  - `pipeline_run_id` (UUID, FK -> audit.pipeline_runs)
  - `source_name` (VARCHAR(50), ex: 'agrobr_cepea', 'agrobr_bcb', 'nasa_power')
  - `source_url` (VARCHAR(500))
  - `git_commit` (VARCHAR(64), quando aplicável)
  - `file_name` (VARCHAR(255))
  - `file_hash` (CHAR(64) SHA-256)
  - `collected_at` (TIMESTAMPTZ)
  - `row_count` (INTEGER)
  - `has_changed` (BOOLEAN)
- **`raw.market_observations`**:
  - `id` (BIGSERIAL, PK)
  - `file_id` (UUID, FK -> raw.ingestion_files)
  - `pipeline_run_id` (UUID, FK -> audit.pipeline_runs)
  - `source` (VARCHAR(60))
  - `observation_date` (DATE)
  - `ingested_at` (TIMESTAMPTZ DEFAULT clock_timestamp())
  - `region` (VARCHAR(60), NULL quando praca geral)
  - `variable_name` (VARCHAR(80))
  - `value` (NUMERIC(14, 4))
  - `unit` (VARCHAR(30))
  - `load_version` (VARCHAR(50))
  - `validation_status` (VARCHAR(20) DEFAULT 'PENDING')
- **`raw.weather_observations`**:
  - `id` (BIGSERIAL, PK)
  - `file_id` (UUID, FK -> raw.ingestion_files)
  - `pipeline_run_id` (UUID, FK -> audit.pipeline_runs)
  - `source` (VARCHAR(60))
  - `observation_date` (DATE)
  - `ingested_at` (TIMESTAMPTZ DEFAULT clock_timestamp())
  - `region` (VARCHAR(30) NOT NULL, ex: 'sulmg', 'cerrado', 'bambui')
  - `variable_name` (VARCHAR(80))
  - `value` (NUMERIC(14, 4))
  - `unit` (VARCHAR(30))
  - `load_version` (VARCHAR(50))
  - `validation_status` (VARCHAR(20) DEFAULT 'PENDING')

### Schema: `staging`
- **`staging.market_observations`**: Estrutura espelho com índices de carga rápida e tipagem normalizada. Truncada ou isolada por `pipeline_run_id`.
- **`staging.weather_observations`**: Estrutura espelho para observações climáticas limpas antes do upsert no core.

### Schema: `core`
- **`core.market_daily`**:
  - `data_ref` (DATE, PK)
  - `preco_arabica` (NUMERIC(10, 2))
  - `preco_robusta` (NUMERIC(10, 2))
  - `usd_brl` (NUMERIC(10, 4))
  - `b3_cafe_ajuste` (NUMERIC(10, 2))
  - `ice_kc` (NUMERIC(10, 4))
  - `updated_at` (TIMESTAMPTZ)
  - `pipeline_run_id` (UUID)
- **`core.weather_daily`**:
  - `data_ref` (DATE)
  - `region` (VARCHAR(30))
  - `temp_min` (NUMERIC(6, 2))
  - `temp_max` (NUMERIC(6, 2))
  - `temp_media` (NUMERIC(6, 2))
  - `precip_mm` (NUMERIC(8, 2))
  - `umidade_rel` (NUMERIC(6, 2))
  - `radiacao_mj` (NUMERIC(8, 2))
  - `vento_ms` (NUMERIC(6, 2))
  - `updated_at` (TIMESTAMPTZ)
  - `pipeline_run_id` (UUID)
  - *PK: `(data_ref, region)`*

### Schema: `features`
- **`features.variable_catalog`**: Conforme seção 1 deste documento.
- **`features.dataset_versions`**:
  - `dataset_version_id` (UUID, PK)
  - `version_tag` (VARCHAR(50) UNIQUE, ex: 'v1.0.0-20261003')
  - `cutoff_date` (DATE)
  - `start_date` (DATE)
  - `row_count` (INTEGER)
  - `feature_count` (INTEGER)
  - `sha256_checksum` (CHAR(64))
  - `is_valid` (BOOLEAN DEFAULT TRUE)
  - `created_at` (TIMESTAMPTZ DEFAULT clock_timestamp())
- **`features.model_features`**:
  - `id` (BIGSERIAL, PK)
  - `dataset_version_id` (UUID, FK -> features.dataset_versions)
  - `data_ref` (DATE)
  - Variáveis KEEP e TARGET (somente as autorizadas pelo catálogo)
  - `y_7d` (NUMERIC(10, 2))
  - `y_15d` (NUMERIC(10, 2))
  - `y_30d` (NUMERIC(10, 2))
  - `y_90d` (NUMERIC(10, 2))
  - `is_pruned` (BOOLEAN DEFAULT FALSE)
  - `created_at` (TIMESTAMPTZ DEFAULT clock_timestamp())
  - *UNIQUE: `(dataset_version_id, data_ref)`*

### Schema: `predictions`
- **`predictions.forecasts`**:
  - `forecast_id` (UUID, PK)
  - `reference_date` (DATE NOT NULL)
  - `target_date` (DATE NOT NULL)
  - `horizon_days` (INTEGER NOT NULL CHECK (horizon_days IN (7, 15, 30, 90)))
  - `predicted_value` (NUMERIC(10, 2) NOT NULL)
  - `dataset_version_id` (UUID NOT NULL, FK -> features.dataset_versions)
  - `model_version` (VARCHAR(50) NOT NULL)
  - `generated_at` (TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())
  - `pipeline_run_id` (UUID NOT NULL, FK -> audit.pipeline_runs)
  - `forecast_status` (VARCHAR(30) DEFAULT 'ACTIVE')
  - *UNIQUE: `(reference_date, horizon_days, model_version)`*

### Schema: `audit`
- **`audit.pipeline_runs`**:
  - `run_id` (UUID, PK)
  - `job_name` (VARCHAR(60))
  - `started_at` (TIMESTAMPTZ)
  - `finished_at` (TIMESTAMPTZ)
  - `status` (VARCHAR(20) CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED', 'BLOCKED')))
  - `records_ingested` (INTEGER)
  - `records_features` (INTEGER)
  - `error_message` (TEXT)
  - `metadata` (JSONB)
- **`audit.data_quality_checks`**:
  - `check_id` (BIGSERIAL, PK)
  - `pipeline_run_id` (UUID, FK -> audit.pipeline_runs)
  - `check_name` (VARCHAR(80))
  - `table_name` (VARCHAR(80))
  - `severity` (VARCHAR(20) CHECK (severity IN ('CRITICAL', 'WARNING')))
  - `passed` (BOOLEAN)
  - `details` (JSONB)
  - `checked_at` (TIMESTAMPTZ DEFAULT clock_timestamp())
- **`audit.model_runs`**: Registra execuções de treinamento e inferência externa para fins de linhagem.
- **`audit.pending_events`**: Eventos desacoplados sinalizando prontidão de novos dados (`DATASET_READY`) para workers downstream.
