# Pipeline de Dados — Previsão do Preço do Café Arábica

> **Variável-alvo:** Indicador CEPEA/ESALQ Café Arábica (R$/saca 60 kg)  
> **Recorte geográfico:** Região de Bambuí/MG — Centro-Oeste Mineiro  
> **Horizontes de previsão:** 7, 15, 30 e 90 dias  

---

## O que este repositório faz

Este componente é responsável exclusivamente por **coletar, armazenar, validar, transformar e versionar** os dados que alimentam um modelo de rede neural de previsão de preço do café. Ele **não treina nem executa o modelo** — apenas entrega a matriz de features pronta e registra as previsões geradas pelo time de ML.

---

## Arquitetura

```
Fontes Externas (CEPEA, BCB, NASA, B3, ICE, CFTC)
        │
        ▼
  ┌──────────────┐    ┌──────────────┐
  │  raw schema  │───▶│staging schema│  ← ingestão bruta imutável + limpeza
  └──────────────┘    └──────┬───────┘
                             │
                             ▼
                      ┌─────────────┐
                      │ core schema │  ← séries diárias deduplicadas (upsert)
                      └──────┬──────┘
                             │
                             ▼
                    ┌────────────────┐
                    │features schema │  ← catálogo + variáveis KEEP + alvos y_h
                    └───────┬────────┘
                            │
             ┌──────────────┴───────────────┐
             ▼                              ▼
   ┌──────────────────┐          ┌──────────────────┐
   │predictions schema│          │  audit schema    │
   │  (forecasts ML)  │          │  (rastreabilidade)│
   └──────────────────┘          └──────────────────┘
```

### Schemas do PostgreSQL

| Schema | Responsabilidade |
|---|---|
| `raw` | Armazenamento bruto imutável de cada coleta, com hash SHA-256 e `pipeline_run_id` |
| `staging` | Área transitória de limpeza e tipagem por execução |
| `core` | Séries históricas deduplicadas em granularidade diária |
| `features` | Catálogo de variáveis, dataset versionado e alvos $y_{t+h}$ |
| `predictions` | Registro formal das previsões produzidas pela rede neural |
| `audit` | Execuções de pipeline, checagens de qualidade e eventos assíncronos |

---

## Estrutura do Projeto

```
.
├── base/                        # Base legada SQLite + dados manuais CEPEA
│   ├── dados_manuais/           # CSVs históricos CEPEA (versionados)
│   ├── cafe_centro_oeste_mg.db  # Banco SQLite legado (não versionado)
│   ├── build_base_cafe.py       # Script original de ETL do SQLite
│   └── schema.sql               # Schema original SQLite
│
├── migrations/                  # Scripts SQL versionados (001 → 006)
├── sql/                         # Fontes SQL (schemas, tabelas, índices, funções, triggers, seed)
│
├── src/                         # Código Python do pipeline
│   ├── config.py                # Configurações via Pydantic (lê .env)
│   ├── db.py                    # Conexão PostgreSQL + advisory locks
│   ├── logging_config.py        # Logging estruturado
│   ├── migrator.py              # Executor de migrations versionadas
│   ├── sqlite_migrator.py       # Migração SQLite legado → PostgreSQL
│   ├── agrobr_client.py         # Adaptador Agro.br (modo real/simulado)
│   ├── validation.py            # 12 checagens de qualidade e limites físicos
│   ├── ingestion.py             # Pipeline raw → staging → core (15 passos)
│   ├── transformations.py       # Transformações causais (janelas, defasagens, alvos)
│   ├── feature_builder.py       # core → candidatas → filtro KEEP + alvos y_7d…y_90d
│   │
│   │   — Em implementação —
│   ├── dataset_versioning.py    # Snapshots imutáveis com checksum
│   ├── pruning.py               # Janela histórica e poda lógica (máx. 2 anos)
│   └── pipeline.py              # Orquestrador completo (15 passos)
│
├── tests/                       # Suite pytest (20 casos obrigatórios)
├── jobs/                        # Jobs agendados (daily, after_close, before_open)
├── docs/                        # Arquitetura, dicionário de dados, contrato ML
│
├── .env.example                 # Template de variáveis de ambiente
├── requirements.txt             # Dependências Python
└── docker-compose.yml           # PostgreSQL 16 conteinerizado
```

---

## Configuração e Execução

### Pré-requisitos

- Python 3.11+
- PostgreSQL 16 (local ou via Docker)

### 1. Instalar dependências

```bash
pip install -r requirements.txt
```

### 2. Configurar variáveis de ambiente

```bash
cp .env.example .env
# Edite .env com suas credenciais PostgreSQL
```

Parâmetros críticos do `.env`:

| Variável | Padrão | Descrição |
|---|---|---|
| `DB_HOST` | `127.0.0.1` | Host do PostgreSQL |
| `DB_PORT` | `5433` | Porta |
| `DB_NAME` | `cafe_previsao` | Nome do banco |
| `HISTORICAL_YEARS` | `9` | Janela histórica em anos |
| `MAX_PRUNE_YEARS` | `2` | Teto de poda lógica |
| `AGROBR_MODE` | `simulated` | `real` ou `simulated` |
| `CONFIRM_HISTORICAL_WINDOW` | `true` | Trava obrigatória em produção |

### 3. Criar o banco e aplicar migrations

```bash
# Criar banco (se ainda não existir)
createdb -h 127.0.0.1 -p 5432 -U postgres cafe_previsao

# Aplicar todas as migrations
python -m src.migrator
```

### 4. Migrar dados do SQLite legado

```bash
python -m src.sqlite_migrator
```

### 5. Executar a ingestão

```bash
python -m src.ingestion
```

O comando imprime as métricas da execução em JSON. O fluxo é: coleta (`AGROBR_MODE`) → registro do hash em `raw.ingestion_files` → brutos em `raw` → 12 validações → `staging` → upsert em `core`.

Comportamentos que valem atenção:

| Situação | Resultado |
|---|---|
| Hash da origem não mudou | Nada é republicado; execução termina `SUCCESS` com 0 registros |
| Linha inválida (tipo, nulo, fora do limite) | Vai para `raw` com `validation_status='INVALID'` e não chega a `staging` |
| Checagem **CRÍTICA** reprovada | Publicação interrompida, `core` permanece intacto, execução `FAILED` |
| Checagem de **ALERTA** reprovada | Registrada em `audit.data_quality_checks`, mas não bloqueia |
| Outra execução em andamento | `pg_try_advisory_lock` nega; execução termina `BLOCKED` sem tocar nos dados |
| Falha de coleta | Novas tentativas com espera exponencial até `AGROBR_MAX_RETRIES`; só `SourceFetchError` é repetida |

Para reprocessar uma janela específica, use a API Python:

```python
from datetime import date
from src.ingestion import run_ingestion

# start_date/end_date valem para AGROBR_MODE=simulated, que é quem aceita janela.
run_ingestion(cutoff_date=date(2026, 1, 1), start_date=date(2025, 1, 1))
```

`cutoff_date` é a data-limite da validação "ausência de informação futura" e vale nos dois modos. Os demais argumentos são repassados ao cliente de coleta.

Toda execução fica auditada em `audit.pipeline_runs` (status, contagens, erro) e em `audit.data_quality_checks` (uma linha por checagem).

### 6. Construir as features

```bash
python -m src.feature_builder
```

Lê `core` até o último dia com `preco_arabica`, calcula as variáveis na grade diária e imprime um resumo em JSON (janela, colunas, alvos preenchidos, candidatas excluídas pelo catálogo). O comando **não grava nada**: a gravação em `features.model_features` é feita por `publish_features`, que precisa de uma versão em `features.dataset_versions` (etapa de versionamento, ainda pendente).

```python
from src.db import get_connection
from src.feature_builder import build_feature_matrix, publish_features

with get_connection() as conn:
    matriz = build_feature_matrix(conn)            # ou cutoff_date=..., start_date=...
    publish_features(conn, dataset_version_id, matriz)
    conn.commit()
```

Só entram as variáveis que o catálogo marca como `KEEP`. Uma variável `KEEP` sem dado em `core` (uma região ausente, por exemplo) interrompe a construção em vez de publicar uma coluna vazia.

### 7. Executar os testes

```bash
python -m pytest tests/ -v
```

Os testes de ingestão exigem um PostgreSQL acessível e usam o job `teste_ingestao` como escopo: `tests/conftest.py` apaga o próprio rastro antes e depois de cada caso, então é seguro rodar contra um banco com cargas reais. `test_transformations.py`, `test_no_future_leakage.py` e a maior parte de `test_feature_catalog.py` rodam em memória, sem banco.

---

## Fontes de Dados

| Variável | Fonte | Frequência |
|---|---|---|
| Preço arábica (alvo) | CEPEA/ESALQ — arquivo manual | Diária (pregão) |
| Preço robusta | CEPEA/ESALQ | Diária |
| Câmbio USD/BRL | BCB PTAX — API | Diária útil |
| Selic | BCB SGS — API | Diária |
| Futuros café ICE KC | Yahoo Finance / B3 | Diária |
| Clima 3 regiões | NASA POWER — API | Diária |
| El Niño (ONI) | NOAA CPC — arquivo | Mensal |
| Posição de fundos | CFTC — API | Semanal |

### Por que o preço do café não é coletado automaticamente

O CEPEA não oferece API com histórico. A coleta automática via `agrobr` só retorna os últimos ~60 dias (página estática). O histórico completo vem do **download manual** do CEPEA e os arquivos ficam em `base/dados_manuais/`. O `agrobr` complementa apenas os dias mais recentes.

---

## Variáveis do Modelo

### Entradas (KEEP)

| Grupo | Variáveis |
|---|---|
| Mercado | `preco_robusta`, `usd_brl`, `b3_cafe_ajuste`, `ice_kc` |
| Clima Sul de Minas | `umidade_rel_sulmg`, `radiacao_mj_sulmg`, `precip_30d_sulmg`, `precip_90d_sulmg`, `tmin_min_30d_sulmg`, `dias_quente_30d_sulmg` |
| Clima Cerrado | `temp_media_cerrado`, `umidade_rel_cerrado`, `radiacao_mj_cerrado`, `precip_30d_cerrado`, `precip_90d_cerrado`, `tmin_min_30d_cerrado`, `dias_quente_30d_cerrado` |
| Calendário | `sin_ano`, `cos_ano` (sempre em par) |

### Alvos (TARGET)

| Variável | Horizonte |
|---|---|
| `y_7d` | Preço arábica em $t+7$ dias |
| `y_15d` | Preço arábica em $t+15$ dias |
| `y_30d` | Preço arábica em $t+30$ dias |
| `y_90d` | Preço arábica em $t+90$ dias |

### Variáveis excluídas

Todas as variáveis com sufixo `_bambui`, anomalias padronizadas, SELIC, IPCA, COT, DXY, ONI e Brent estão **preservadas em `raw` e `core`** mas **excluídas de `features.model_features`** conforme catálogo em `features.variable_catalog`.

---

## Garantias de Qualidade

| Garantia | Como é implementada |
|---|---|
| **Sem data leakage** | Nada posterior ao corte é lido; janelas fechadas em $t$; preenchimento só para frente e com limite; `preco_arabica` do dia nunca é feature; fontes com atraso deslocadas por `PUBLICATION_LAG_DAYS` |
| **Imutabilidade do raw** | `raw.market_observations` e `raw.weather_observations` nunca têm linhas deletadas |
| **Idempotência** | Upsert com `ON CONFLICT … DO UPDATE SET col = COALESCE(excluded.col, atual)` |
| **Sem concorrência** | `pg_try_advisory_lock` impede duas instâncias simultâneas |
| **Teto de poda** | Máximo 2 anos removidos da janela ativa; poda é lógica (`is_pruned = TRUE`), nunca física |
| **Rastreabilidade** | Toda linha carregada carrega `pipeline_run_id` e `load_version` |

---

## Testes

```bash
python -m pytest tests/ -v --tb=short
```

| Arquivo de Teste | Casos | Cobertura |
|---|---|---|
| `test_schema.py` | 3 | Schemas, tabelas obrigatórias e seed do catálogo |
| `test_sqlite_migration.py` | 8 | Migração legado, paridade de contagem, spot-check de valor, idempotência |
| `test_validation.py` | 36 | As 12 checagens: colunas obrigatórias (6), tipos (7), limites e tolerância de 5% (8), nulos, datas, duplicidades, unidades, continuidade, horizontes e persistência em `audit.data_quality_checks` |
| `test_ingestion.py` | 16 | Pipeline ponta a ponta, metadados da coleta, quarentena, bloqueio por falha CRÍTICA, falha de conexão (9), retry (10) e lock concorrente (11) |
| `test_idempotency.py` | 9 | Carga repetida não duplica (3), deduplicação dentro e entre origens (4), upsert sem duplicar datas e com `COALESCE` (5) |
| `test_transformations.py` | 16 | Janelas móveis, preenchimento causal, calendário cíclico, alvos e separação treino/validação/teste com embargo |
| `test_no_future_leakage.py` | 9 | Anti-leakage por perturbação (16): alterar o futuro não muda o passado, corte, alvo fora das features, atraso de publicação |
| `test_feature_catalog.py` | 29 | Somente KEEP e alvos (14), DROP fora da tabela final (15), alvos de 7/15/30/90 dias (17), publicação sem duplicar |
| `test_dataset_versioning.py` | — | *(em desenvolvimento)* Versionamento, previsão por horizonte e recuperação (18–20) |
| `test_pruning.py` | — | *(em desenvolvimento)* Teto de 2 anos, imutabilidade do raw (12, 13) |
| `test_model_contract.py` | — | *(em desenvolvimento)* Consumidor simulado e registro de previsões |

Os números entre parênteses são os **20 testes obrigatórios** do documento de especificação; 15 deles já estão implementados.

---

## Contrato com o Time de ML

O componente de rede neural **consome** a view:

```sql
SELECT * FROM features.model_features
JOIN features.dataset_versions dv USING (dataset_version_id)
WHERE dv.is_valid = TRUE
ORDER BY (SELECT dataset_version_id FROM features.dataset_versions
          WHERE is_valid = TRUE ORDER BY created_at DESC LIMIT 1),
         data_ref ASC;
```

E **grava** previsões em:

```sql
INSERT INTO predictions.forecasts
  (forecast_id, reference_date, target_date, horizon_days,
   predicted_value, dataset_version_id, model_version, pipeline_run_id)
VALUES (…)
ON CONFLICT (reference_date, horizon_days, model_version) DO UPDATE SET …;
```

Ver [`docs/model_contract.md`](docs/model_contract.md) para o contrato completo.

---

## Documentação

| Documento | Conteúdo |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Fluxo dos 6 schemas, pipeline de 15 passos, locks e janela histórica |
| [`docs/data_dictionary.md`](docs/data_dictionary.md) | Catálogo completo de variáveis (KEEP, TARGET, DROP) e modelagem das tabelas |
| [`docs/model_contract.md`](docs/model_contract.md) | Contrato formal de integração com o time de ML, consulta SQL padrão e mock consumer |
