# TODO — Roadmap do MVP de Dados

> Baseado no documento **prompt_desenvolvimento_mvp_cafe.pdf** (15 páginas), que define escopo, arquitetura, etapas de entrega e critérios de aceite. Este arquivo compara o estado atual do repositório com o que o documento exige.

O documento define **8 etapas de entrega**. As etapas **1–4 estão concluídas**; as etapas **5–8 estão pendentes** — o que resta é a camada de features, o versionamento do dataset, o agendamento dos jobs e os testes que dependem deles (a fundação de banco e o pipeline de ingestão já estão prontos).

---

## Etapa 1 — Arquitetura e dicionário de dados ✅ CONCLUÍDA

- [x] `docs/architecture.md`
- [x] `docs/data_dictionary.md`
- [x] `docs/model_contract.md` (contrato com o componente de rede neural)
- [x] README com configuração, migração e execução

## Etapa 2 — Migrations PostgreSQL ✅ CONCLUÍDA

- [x] 6 schemas criados: `raw`, `staging`, `core`, `features`, `predictions`, `audit`
- [x] Todas as 15 tabelas exigidas pelo documento:
  - `raw.ingestion_files`, `raw.market_observations`, `raw.weather_observations`
  - `staging.market_observations`, `staging.weather_observations`
  - `core.market_daily`, `core.weather_daily`
  - `features.variable_catalog`, `features.model_features`, `features.dataset_versions`
  - `predictions.forecasts`
  - `audit.pipeline_runs`, `audit.data_quality_checks`, `audit.model_runs`, `audit.pending_events`
- [x] Índices (`migrations/003`), funções (`004`), triggers (`005`)
- [x] Catálogo de variáveis semeado (`006`): 22 KEEP, 2 TARGET, 49 DROP, 4 INTERNAL_INPUT
- [x] Parâmetros obrigatórios em `src/config.py`: `HISTORICAL_YEARS=9`, `MAX_PRUNE_YEARS=2`, `DATASET_MIN_DAYS=1096`, `FORECAST_HORIZONS=7,15,30,90`, `TIMEZONE=America/Sao_Paulo`, `UPDATE_TIME=00:00`
- [x] Flag `CONFIRM_HISTORICAL_WINDOW` para a inconsistência 9 anos vs. ~1.096 dias exigida pelo documento

## Etapa 3 — Migração SQLite → PostgreSQL ✅ CONCLUÍDA

- [x] `src/sqlite_migrator.py` (migração do SQLite legado)
- [x] `tests/test_sqlite_migration.py`

---

## Etapa 4 — Ingestão e validação ✅ CONCLUÍDA

- [x] **`src/agrobr_client.py`** — cliente do Agro.br:
  - [x] baixar dados (modo real via HTTP; `AGROBR_MODE=simulated` usa CSVs locais + séries sintéticas determinísticas)
  - [x] registrar URL, commit, arquivo, hash e data da coleta em `raw.ingestion_files`
  - [x] detectar mudança na fonte (SHA-256 do conteúdo comparado com a coleta anterior)
  - [x] adaptador configurável + implementação simulada para testes
  - [x] política de novas tentativas com `tenacity` (`AGROBR_MAX_RETRIES`, só repete `SourceFetchError`)
- [x] **`src/ingestion.py`** — fluxo de 15 passos do pipeline:
  - [x] iniciar execução com `run_id` único em `audit.pipeline_runs`
  - [x] salvar brutos em `raw` (imutável; linhas reprovadas ficam com `validation_status='INVALID'`)
  - [x] carregar `staging` somente com as linhas aprovadas
  - [x] deduplicação e upsert
  - [x] publicar aprovados em `core`
  - [x] finalizar execução com status, métricas e logs
- [x] **`src/validation.py`** — 12 validações de qualidade:
  - [x] colunas obrigatórias
  - [x] tipos numéricos
  - [x] datas válidas e fora do futuro indevido
  - [x] duplicidades
  - [x] valores nulos
  - [x] limites físicos plausíveis
  - [x] unidades
  - [x] continuidade temporal
  - [x] quantidade mínima de registros
  - [x] disponibilidade de dados para os horizontes
  - [x] ausência de features calculadas com informação futura
  - [x] registro de falhas em `audit.data_quality_checks`
- [x] **Idempotência** — origem com hash inalterado é ignorada; a publicação em `core` usa `ON CONFLICT DO UPDATE` com `COALESCE`, então a mesma carga 2× não duplica nem apaga colunas ausentes
- [x] **Bloqueio de execução concorrente** — `pg_try_advisory_lock` em `src/db.py` (`acquire_advisory_lock`, lock `84729103`); uma segunda execução simultânea termina como `BLOCKED` sem tocar nos dados
- [x] **Severidade** — falha CRÍTICA interrompe a publicação (`core` permanece intacto, execução `FAILED`); falha WARNING nunca bloqueia
- [x] **Stored procedures** (em `sql/functions.sql`, criadas pela migration `004`):
  - [x] upsert transacional + publicação de staging para core — `core.sp_upsert_market_observations`, `core.sp_upsert_weather_observations`
  - [x] abertura e fechamento de uma carga — `audit.sp_register_pipeline_start`, `audit.sp_register_pipeline_finish`
  - [x] registro de eventos — `audit.fn_emit_event`
  - [ ] deduplicação como procedure — hoje é feita em Python (`validation.deduplicate`) antes do `staging`, o que mantém a regra testável; migrar para SQL só se houver outro consumidor
  - [ ] atualização de estatísticas — depende da etapa 5 (features)

> **Armadilha conhecida**: `raw.ingestion_files.file_hash` é `CHAR(64)`. O PostgreSQL completa valores mais curtos com espaços, então qualquer comparação de hash precisa de `.strip()` — sem isso toda coleta pareceria "alterada" e o pipeline republicaria tudo, sempre. Já tratado em `src/ingestion.py`.

## Etapa 5 — Features selecionadas ❌ PENDENTE

- [ ] **`src/feature_builder.py`** / **`src/transformations.py`**:
  - [ ] alvos deslocados: `y_7d`, `y_15d`, `y_30d`, `y_90d` (parâmetros já existem no config)
  - [ ] defasagens de `preco_arabica` e `preco_robusta`
  - [ ] retornos passados, médias móveis passadas, volatilidade passada
  - [ ] `sin_ano` e `cos_ano` mantidos juntos (codificação cíclica)
  - [ ] consultas padrão só selecionam variáveis KEEP/TARGET do catálogo; DROP nunca entra em `features.model_features`
- [ ] **Regras contra vazamento temporal** (`X_t → y_(t+h)`, h > 0):
  - [ ] nenhum dado publicado depois de t entra em `X_t`
  - [ ] janelas móveis calculadas somente até t
  - [ ] preenchimento causal
  - [ ] tratamento de dados de mercado publicados após o fechamento
  - [ ] separação temporal entre treino, validação e teste
  - [ ] `preco_arabica` contemporâneo nunca usado como feature para prever a si mesmo

## Etapa 6 — Versionamento e auditoria ❌ PENDENTE

- [ ] **`src/dataset_versioning.py`**:
  - [ ] criar versão imutável do dataset de features em `features.dataset_versions`
  - [ ] carga com falha crítica **não substitui** a última versão válida (mantém a anterior disponível)
  - [ ] sinalizar que os dados estão prontos para o componente de previsão
  - [ ] recuperação da última versão válida após falha
- [ ] **Integração com o componente de rede neural** (implementar o que `model_contract.md` documenta):
  - [ ] consulta/endpoint da última versão válida das features
  - [ ] identificador da versão, colunas e tipos, data de corte, horizonte, status de qualidade
  - [ ] função de exemplo que simula o consumo das features (sem implementar o modelo)
  - [ ] registro das previsões recebidas em `predictions.forecasts` (por horizonte, com `model_version` e `pipeline_run_id`)

## Etapa 7 — Agendamento ❌ PENDENTE

- [ ] Criar a pasta **`jobs/`** (não existe):
  - [ ] `update_daily.py` — job diário às 00:00, fuso `America/Sao_Paulo`
  - [ ] `update_after_close.py` — preparado para execução após fechamento do mercado
  - [ ] `update_before_open.py` — preparado para execução antes da abertura
  - [ ] reprocessamento manual de um período
  - [ ] execução sob demanda
  - [ ] retry seguro após falha
- [ ] Sem duplicar dados/previsões entre jobs; registrar hora da última observação disponível e da última ingestão

## Etapa 8 — Testes e README 🔄 EM ANDAMENTO (11 de 20 obrigatórios prontos)

Prontos: `test_schema.py` (1), `test_sqlite_migration.py` (2), `test_idempotency.py` (3, 4, 5), `test_validation.py` (6, 7, 8 + as 12 checagens), `test_ingestion.py` (9, 10, 11). Faltam 9:

- [x] `test_idempotency.py` — carga idempotente (3), deduplicação (4), upsert (5)
- [x] `test_validation.py` — arquivo com coluna ausente (6), arquivo com tipo inválido (7), valor fora do limite (8)
- [x] `test_ingestion.py` — falha de conexão (9), retry (10), bloqueio de execução concorrente (11)
- [ ] `test_pruning.py` — poda limitada a 2 anos (12), preservação de dados brutos (13)
- [ ] `test_feature_catalog.py` — seleção somente KEEP/TARGET (14), exclusão de DROP/KEEP_WITH_CAVEAT/TEST_ONLY da tabela final (15), geração dos alvos de 7/15/30/90 dias (17), versionamento de features (18), registro da previsão por horizonte (19), recuperação da última versão válida após falha (20)
- [ ] `test_no_future_leakage.py` — ausência de vazamento temporal (16)
- [ ] `docs/runbook.md` — operação, recuperação e reprocessamento

> Os testes da etapa 4 exigem um PostgreSQL acessível (`.env` com `DB_*`). Eles usam o job `teste_ingestao` como escopo: `tests/conftest.py` apaga todo o rastro antes e depois de cada teste, então é seguro rodar contra um banco que já contenha cargas reais.

---

## Observações operacionais

1. **Ordem recomendada**: seguir a sequência do documento (5 → 6 → 7 → 8). A etapa 8 já foi adiantada nos itens que não dependem de features (3 a 11); os testes 12 a 20 só saem depois das etapas 5 e 6. Dá para paralelizar `docs/runbook.md` com a etapa 5, já que o pipeline de ingestão está estável.
2. **Submódulo `base/`**: o clone ainda não traz a pasta `base/` (gitlink sem `.gitmodules`). Quem clonar precisa clonar `hugocesarleal/base` manualmente para dentro dela, ou adicionarmos o `.gitmodules` + `--recurse-submodules`.
3. **Decisão de negócio pendente**: o documento proíbe assumir silenciosamente a janela histórica (9 anos vs. ~1.096 dias). A confirmação da janela **deve ser validada pela equipe antes da execução em produção**.
4. **Fora do escopo** (não implementar aqui): telas, widgets, rede neural, treinamento, POCID/POSID, serviços pagos.

## Critérios de aceite do documento (resumo)

Concluído somente quando: PostgreSQL subir localmente; migrations criarem tudo; SQLite migrar para base de teste; pipeline baixar ou simular Agro.br; mesma carga rodar 2× sem duplicidade; variáveis geradas com nomes/tipos documentados; excluídas fora da consulta padrão; brutos rastreáveis; falha não destruir última versão válida; poda ≤ 2 anos; 4 horizontes parametrizados; testes de vazamento temporal passando; triggers/procedures nos limites definidos; logs/status/hash/versão/contagens registrados; README completo; contrato com o modelo documentado; nenhum widget/tela/modelo neste escopo.
