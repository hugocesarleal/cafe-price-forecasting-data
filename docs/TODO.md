# TODO — Roadmap do MVP de Dados

> Baseado no documento **prompt_desenvolvimento_mvp_cafe.pdf** (15 páginas), que define escopo, arquitetura, etapas de entrega e critérios de aceite. Este arquivo compara o estado atual do repositório com o que o documento exige.

O documento define **8 etapas de entrega**. As **8 etapas estão implementadas** e a suíte completa roda verde contra PostgreSQL 16 (334 passaram, 1 desligado por padrão — o teste que acessa as fontes reais). Depois do MVP, o repositório recebeu a **integração com Django** e a **migração do agendador para APScheduler** — ver a seção correspondente antes das observações operacionais.

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
- [x] Catálogo de variáveis semeado (`006`): 19 KEEP, 1 TARGET, 47 DROP, 3 INTERNAL_INPUT
- [x] Parâmetros obrigatórios em `src/config.py`: `HISTORICAL_YEARS=9`, `MAX_PRUNE_YEARS=2`, `DATASET_MIN_DAYS=1096`, `FORECAST_HORIZONS=7,15,30,90`, `TIMEZONE=America/Sao_Paulo`, `UPDATE_TIME=00:00`
- [x] Flag `CONFIRM_HISTORICAL_WINDOW` para a inconsistência 9 anos vs. ~1.096 dias exigida pelo documento

## Etapa 3 — Migração SQLite → PostgreSQL ✅ CONCLUÍDA

- [x] `src/sqlite_migrator.py` (migração do SQLite legado)
- [x] `tests/test_sqlite_migration.py`

---

## Etapa 4 — Ingestão e validação ✅ CONCLUÍDA

- [x] **`src/agrobr_client.py`** — cliente do Agro.br:
  - [x] baixar dados: `AGROBR_MODE=real` coleta CEPEA, BCB PTAX, B3 e NASA POWER pelo `agrobr` e ICE pelo Yahoo Finance (`src/agrobr_real.py`); `AGROBR_MODE=simulated` usa CSVs locais + séries sintéticas determinísticas
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
  - [ ] atualização de estatísticas — sem definição no documento além do nome; a contagem de features por execução já fica em `audit.pipeline_runs.records_features`

> **Armadilha conhecida**: `raw.ingestion_files.file_hash` é `CHAR(64)`. O PostgreSQL completa valores mais curtos com espaços, então qualquer comparação de hash precisa de `.strip()` — sem isso toda coleta pareceria "alterada" e o pipeline republicaria tudo, sempre. Já tratado em `src/ingestion.py`.

## Etapa 5 — Features selecionadas ✅ CONCLUÍDA

- [x] **`src/transformations.py`** — transformações causais puras (sem banco):
  - [x] alvos deslocados (`shift_target`)
  - [x] defasagens (`lag`), retornos (`log_return`), médias móveis (`rolling_mean`) e volatilidade (`rolling_volatility`)
  - [x] janelas fechadas em t para clima (`rolling_sum`, `rolling_min`, `rolling_count_above`)
  - [x] `sin_ano` e `cos_ano` sempre em par (`cyclic_year`)
  - [x] separação temporal entre treino, validação e teste com embargo (`temporal_split`)
- [x] **`src/feature_builder.py`** — lê `core`, calcula as candidatas, filtra pelo catálogo e grava em `features.model_features`:
  - [x] alvos `y_7d`, `y_15d`, `y_30d`, `y_90d` a partir de `FORECAST_HORIZONS`
  - [x] só variáveis `KEEP` do catálogo são publicadas; DROP e não catalogadas são calculadas e descartadas
  - [x] variável `KEEP` sem cálculo ou sem coluna na tabela interrompe a construção (não publica coluna vazia)
- [x] **Regras contra vazamento temporal** (`X_t → y_(t+h)`, h > 0):
  - [x] nenhum dado posterior ao corte é lido de `core`
  - [x] janelas móveis calculadas somente até t
  - [x] preenchimento causal: só para frente e com limite (6 dias em mercado, 3 em clima)
  - [x] dados publicados com atraso: deslocamento por variável em `PUBLICATION_LAG_DAYS`
  - [x] `preco_arabica` contemporâneo nunca é feature; estatísticas dele só entram defasadas em um dia
  - [x] alvo de t vem sempre de um pregão posterior a t (limite de preenchimento < menor horizonte)

> **Decisões que valem revisão**:
> - As defasagens, retornos, médias e volatilidade de preço são calculados, mas **não são publicados**: o catálogo marca `ret_1d`, `vol_20d` e `preco_media_20d` como DROP, as defasagens não estão catalogadas e `features.model_features` não tem coluna para elas. Liberá-las exige mudar o catálogo e criar uma migration.
> - A grade é de dias corridos. Em dia sem pregão, as features de mercado repetem o último fechamento e o alvo vale o último indicador publicado até `t+h`.
> - `PUBLICATION_LAG_DAYS` desloca o clima pelo atraso de publicação do NASA POWER: 3 dias para as séries meteorológicas e 5 para a radiação, medidos em 07/10/2026 numa única coleta. `precip_30d` de t é, portanto, a chuva dos 30 dias terminados em t-3. Mercado não tem atraso. Revise os valores se a latência da fonte mudar.
> - `publish_features` exige uma versão já criada em `features.dataset_versions` e não faz commit: a criação da versão é a etapa 6.

## Etapa 6 — Versionamento e auditoria ✅ CONCLUÍDA

- [x] **`src/dataset_versioning.py`**:
  - [x] versão imutável do dataset em `features.dataset_versions`, assinada por SHA-256 do conteúdo gravado (`create_dataset_version`, `compute_checksum`)
  - [x] conteúdo idêntico reaproveita a versão existente; conteúdo revisado para o mesmo corte gera versão nova, com o checksum na tag
  - [x] carga com falha crítica **não substitui** a última versão válida: a versão nasce inválida, as linhas são gravadas e só então ela é validada, na mesma transação
  - [x] checagens de qualidade da matriz registradas em `audit.data_quality_checks` (`check_matrix`)
  - [x] sinalização de prontidão: o trigger emite `DATASET_READY` quando a versão vira válida
  - [x] recuperação da última versão válida após falha (`get_latest_valid_version`, `invalidate_dataset_version`)
  - [x] verificação de integridade de uma versão gravada (`verify_dataset_version`)
  - [x] execução completa auditada, com lock: `python -m src.dataset_versioning` (`run_dataset_build`)
- [x] **Integração com o componente de rede neural** — `src/model_contract.py`:
  - [x] consulta da última versão válida das features (`load_features`)
  - [x] identificador da versão, colunas e tipos, data de corte, horizontes, status de qualidade (`get_latest_dataset`)
  - [x] função de exemplo que simula o consumo das features (`mock_neural_model_predict`, `run_mock_forecast`)
  - [x] registro das previsões em `predictions.forecasts` por horizonte, com `model_version` e `pipeline_run_id`, sem duplicar (`register_forecasts`), e da execução em `audit.model_runs`

> **Decisões que valem revisão**:
> - A imutabilidade é garantida pelo código (nenhuma função reescreve uma versão) e conferível pelo checksum, mas **não há trigger no banco** impedindo um `UPDATE` manual em `features.model_features`.
> - Falhas CRÍTICAS da matriz: vazia, alvo entre as features, ou alguma feature inteira nula. Linha de corte incompleta, alvo sem nenhum valor e janela menor que `DATASET_MIN_DAYS` são só alerta e aparecem no status de qualidade.
> - `register_forecasts` rejeita o lote inteiro se uma previsão viola o contrato, e não aceita `reference_date` posterior ao corte da versão usada.
> - No conflito `(reference_date, horizon_days, model_version)` a previsão é atualizada. Além dos três campos do contrato original, o upsert atualiza `target_date`, `pipeline_run_id` e `forecast_status`.
> - `create_dataset_version` e `register_forecasts` não fazem commit; `run_dataset_build` e `run_mock_forecast` fazem.

## Etapa 7 — Agendamento ✅ CONCLUÍDA

- [x] **`src/pipeline.py`** — orquestrador do ciclo completo (ingestão → features → versão), com uma execução "mãe" auditada por job e um único advisory lock para o ciclo inteiro
- [x] Pasta **`jobs/`**:
  - [x] `update_daily.py` — job diário às 00:00 (`UPDATE_TIME`), fuso `America/Sao_Paulo`; fecha o dia anterior
  - [x] `update_after_close.py` — após o fechamento do mercado (`AFTER_CLOSE_TIME`); fecha o próprio dia
  - [x] `update_before_open.py` — antes da abertura (`BEFORE_OPEN_TIME`); revisa o dia anterior
  - [x] `reprocess_period.py` — reprocessamento manual de um período (`--start`/`--end`)
  - [x] `run_on_demand.py` — execução sob demanda (`--cutoff`, `--force`)
  - [x] retry seguro após falha — `jobs/runner.py` repete o ciclo após erro inesperado (`JOB_MAX_RETRIES`, `JOB_RETRY_WAIT_SECONDS`)
- [x] Sem duplicar dados/previsões entre jobs: origem inalterada não é republicada, conteúdo idêntico reaproveita a versão do dataset e previsões são gravadas por upsert
- [x] Hora da última observação disponível e da última ingestão registradas em `audit.pipeline_runs.metadata` da execução do job
- [x] Cada job roda uma vez (para cron / Agendador de Tarefas) ou fica agendado com `--schedule` (APScheduler desde a migração pós-MVP — ver seção ao final)

> **Mudanças na ingestão que esta etapa exigiu**:
> - Uma origem só conta como "inalterada" em relação à última carga que terminou em **SUCCESS**. Antes, o hash de uma carga que falhou no meio já valia: a tentativa seguinte via "nada mudou" e o dado nunca chegava a `core`; e repetir uma carga reprovada por qualidade virava um SUCCESS vazio.
> - Efeito colateral: enquanto uma origem continuar reprovada, cada execução grava de novo as linhas dela em `raw` (em quarentena). `raw` cresce até a origem ser corrigida.
> - `IngestionPipeline.run(force=True)` reprocessa origens mesmo sem mudança de hash; é o que o reprocessamento de período usa.
>
> **Decisões que valem revisão**:
> - Os horários `AFTER_CLOSE_TIME=19:00` e `BEFORE_OPEN_TIME=08:00` são padrões meus, não vieram do documento.
> - O corte do job vale também para o dataset: o job diário de segunda 00:00 gera uma versão com corte no domingo (features de mercado repetindo o fechamento de sexta).
> - Só erro inesperado é repetido. Ciclo reprovado por qualidade dos dados ou bloqueado por outro job termina na primeira tentativa.
> - Se a ingestão é reprovada, o dataset não é reconstruído naquele ciclo.
> - Os jobs não chamam o modelo: registrar previsões continua sendo iniciativa do componente de rede neural (`src/model_contract.py`).
> - `COLLECTION_WINDOW_DAYS` (padrão `0`, janela inteira) permite que os jobs agendados recoletem só os dias recentes. Com `0`, cada execução regrava o histórico todo em `raw`.
> - O agendador embutido (`--schedule`) usa APScheduler (`jobs/apscheduler_runner.py`): `CronTrigger` diário no fuso `TIMEZONE`, `coalesce=True`, `max_instances=1`, tolerância `MISFIRE_GRACE_SECONDS`, id estável (`SCHEDULER_JOB_ID`) e encerramento por SIGTERM/SIGINT. Continua sendo um processo por job, sem serviço do sistema.

## Etapa 8 — Testes e README ✅ CONCLUÍDA (20 de 20 obrigatórios implementados)

- [x] `test_schema.py` (1), `test_sqlite_migration.py` (2)
- [x] `test_idempotency.py` — carga idempotente (3), deduplicação (4), upsert (5)
- [x] `test_validation.py` — arquivo com coluna ausente (6), arquivo com tipo inválido (7), valor fora do limite (8)
- [x] `test_ingestion.py` — falha de conexão (9), retry (10), bloqueio de execução concorrente (11)
- [x] `test_pruning.py` — poda limitada a 2 anos (12), preservação de dados brutos (13)
- [x] `test_feature_catalog.py` — seleção somente KEEP/TARGET (14), exclusão de DROP/KEEP_WITH_CAVEAT/TEST_ONLY da tabela final (15), geração dos alvos de 7/15/30/90 dias (17)
- [x] `test_no_future_leakage.py` — ausência de vazamento temporal (16)
- [x] `test_dataset_versioning.py` — versionamento de features (18), recuperação da última versão válida após falha (20)
- [x] `test_model_contract.py` — registro da previsão por horizonte (19)
- [x] Além dos obrigatórios: `test_transformations.py`, `test_jobs.py`, `test_pipeline.py`
- [x] **`src/pruning.py`** — poda lógica por versão (`is_pruned`), teto de 2 anos cumulativo, mínimo de segurança `DATASET_MIN_DAYS`, trava `CONFIRM_HISTORICAL_WINDOW`, reversível (`--restore`)
- [x] `docs/runbook.md` — operação, recuperação e reprocessamento
- [x] README com configuração, execução de cada etapa, jobs, poda e testes

> **Estado da verificação**: a suíte completa já foi executada contra PostgreSQL 16 e **passa** — 334 testes passaram e 1 ficou desligado por padrão (o que acessa as fontes reais; ligue com `RUN_LIVE_TESTS=1`). Isso cobriu ingestão, idempotência, schema e migração, features, versionamento, contrato, orquestrador, poda, jobs, agendador e a fronteira Django.
>
> Os testes 9 a 11 e a suíte da etapa 4 também foram executados nessa rodada; a mudança da etapa 7 na detecção de origem inalterada está coberta.

> **Decisões da poda que valem revisão**:
> - A poda vale para uma versão; versões novas nascem com a janela inteira.
> - O mínimo `DATASET_MIN_DAYS` como condição para podar é interpretação minha de "mínimo de segurança": com a base legada de ~1.096 dias, qualquer poda é recusada.
> - `CONFIRM_HISTORICAL_WINDOW` trava a poda, não o pipeline inteiro. O documento fala em recusar a execução "em produção", mas não há hoje uma configuração que diga qual ambiente é produção, e o padrão da flag em `src/config.py` é `true`.

## Modo real de coleta ✅ IMPLEMENTADO (fora das 8 etapas)

- [x] **`src/agrobr_real.py`** — `RealAgrobrClient`, com as mesmas chamadas do script legado `base/build_base_cafe.py`:
  - [x] CEPEA: CSV da série + complemento recente pelo `agrobr`, com aviso de buraco entre os dois; `src/cepea_series.py` atualiza o CSV a partir da planilha do site (`--importar` para a baixada pelo navegador), com conferência antes de substituir
  - [x] BCB PTAX (venda, último boletim do dia)
  - [x] B3 ICF, 1º vencimento — corrigido o `MemoryError` do legado
  - [x] ICE Coffee C pelo Yahoo Finance, ignorando o pregão em andamento
  - [x] NASA POWER nas 3 regiões, descartando dias ainda não publicados
- [x] Janela de coleta (`start_date`/`end_date`) nos dois modos
- [x] `tests/test_agrobr_real.py` e `tests/test_cepea_series.py`, com os downloads substituídos por dados fixos

> **O que causava o `MemoryError` da B3**: `agrobr.b3.historico` dispara o download de todos os pregões da janela ao mesmo tempo, e cada arquivo ocupa centenas de MB ao ser processado (medi ~1,2 GB de pico com 4 simultâneos). O cliente baixa em lotes de 20 dias, 2 por vez (pico de memória em torno de 600 MB), e grava o resultado em cache a cada lote.
>
> **Decisões que valem revisão**:
> - 1º vencimento = o contrato de vencimento mais próximo em cada pregão, como no script legado. Perto do vencimento esse contrato perde liquidez; uma regra de rolagem pode ser melhor.
> - O arquivo de ajustes do dia D traz uma linha datada do pregão seguinte que só repete o ajuste de D; ela é descartada.
> - Um dia útil sem arquivo só é marcado como feriado no cache depois de 5 dias; antes disso é tentado de novo a cada coleta.
> - Se o complemento recente do CEPEA falhar, a coleta segue só com o CSV. Qualquer outra fonte indisponível ou vazia derruba a coleta.
> - O download automático da série do CEPEA (`CEPEA_AUTO_DOWNLOAD`) vem desligado: o site o recusou com HTTP 403 em 07/10/2026. O caminho suportado é baixar a planilha pelo navegador e usar `--importar`.
> - O `id` da série do robusta (`24`) em `src/cepea_series.py` nunca foi confirmado, e só importa para o download automático. A importação reconhece a série pelo título.
> - O leitor da planilha foi escrito a partir do script legado e conferido com as duas planilhas reais do CEPEA em 07/10/2026: os preços das datas em comum com os CSVs anteriores coincidiram exatamente.
> - `usd_brl_compra` e as variáveis DROP do legado (Selic, IPCA, COT, DXY, Brent, ONI) não são coletadas.

## Integração com Django e agendador APScheduler ✅ IMPLEMENTADO (pós-MVP)

Trabalho executado depois das 8 etapas, a pedido da equipe: chamar o pipeline a partir do Django como biblioteca externa e trocar o laço de `sleep` do agendador embutido por APScheduler.

- [x] **`src/integrations/django.py`** — adaptador de fronteira (caixa preta):
  - [x] precedência de configuração por campo: explícita do chamador > Django > ambiente/`.env`; divergência nunca passa em silêncio (campo injetável é honrado com aviso registrando a fonte; qualquer outro campo divergente derruba com `PipelineConfigError`)
  - [x] abre a **própria** conexão psycopg do ciclo (`src.db.pipeline_connection`) e mantém o advisory lock nela até o fim — nunca a conexão do ciclo de requests do Django
  - [x] `run_pipeline_job`, `run_ingestion_job`, `run_dataset_job`: mesmas repetições, idempotência, auditoria e formato de retorno dos jobs
  - [x] leitura somente consulta para painel: `read_recent_runs`, `latest_successful_run`
  - [x] nenhum import de Django no `src/`; acoplamento confinado em `examples/django_integration/`
- [x] **`examples/django_integration/`** — management command fino (código de saída 0/1/2), task Celery e ModelAdmin somente leitura sobre `audit.pipeline_runs` (`managed = False`)
- [x] **`jobs/apscheduler_runner.py`** — substitui o laço de `sleep` do runner, preservando `run_job`, retries, corte por dia, idempotência, lock, logs e formato de retorno:
  - [x] `CronTrigger` diário no fuso `TIMEZONE`; horário inválido derruba antes de agendar
  - [x] `coalesce=True`, `max_instances=1`, `misfire_grace_time` (`MISFIRE_GRACE_SECONDS`, padrão 1h): atraso dentro da tolerância roda uma vez; além dela, misfire registrado sem execução automática
  - [x] `replace_existing=True` com id estável (`SCHEDULER_JOB_ID` ou o nome do job): reiniciar não duplica
  - [x] listeners de sucesso/erro/misfire; falha de um disparo não derruba o agendador
  - [x] encerramento controlado por SIGTERM/SIGINT, esperando a execução em curso; `SCHEDULER_ENABLED=false` desliga sem erro
  - [x] `--schedule` dos jobs delega ao agendador; `--cutoff` fixa o dia de todas as execuções agendadas
- [x] Config central (`src/config.py`): `PIPELINE_DATABASE_URL`, `ADVISORY_LOCK_KEY`, `SCHEDULER_ENABLED`, `SCHEDULER_JOB_ID`, `MISFIRE_GRACE_SECONDS` — sem nomes paralelos; validação no carregamento
- [x] Testes: `test_scheduler.py` (20) substitui os testes presos ao `sleep`; `test_django_integration.py` (19); `test_jobs.py` migrado (34 → 29, sem os casos do laço antigo)

> **Decisões que valem revisão**:
> - Horário de verão não existe no Brasil desde 2019; um teste documenta o deslocamento constante UTC-3 em vez de simular DST.
> - `SCHEDULER_JOB_ID` só vale com um job por processo (`python -m jobs.X --schedule`); com vários jobs o id estável é o próprio nome (erro explícito se configurado).
> - O agendador embutido continua sendo um processo por job; para os três horários, rode três processos ou use um agendador externo (cron) que não depende do pacote `apscheduler`.
> - A fronteira Django não expõe `ADVISORY_LOCK_KEY` como injetável de propósito: o lock é o mecanismo de exclusão mútua entre processos e precisa valer o mesmo para todos.
> - Não há modelagem Django do banco: o modelo do Admin é `managed = False` e o esquema continua sendo criado somente pelas migrations SQL de `migrations/`.

## Observações operacionais

1. **O que falta para fechar**: executar `python -m pytest tests/ -v` com PostgreSQL disponível e resolver o que aparecer.
2. **Pasta `base/`**: faz parte deste repositório desde 07/10/2026 (antes era um vínculo de submódulo sem `.gitmodules`, e o conteúdo não chegava a quem clonava). São versionados o script legado, o schema do SQLite, o README e os CSVs do CEPEA em `base/dados_manuais/`. O SQLite (`*.db`), `base_modelo.csv`, as auditorias e as planilhas `.xls` ficam fora, pelo `.gitignore`.
3. **Decisão de negócio pendente**: o documento proíbe assumir silenciosamente a janela histórica (9 anos vs. ~1.096 dias). A confirmação da janela **deve ser validada pela equipe antes da execução em produção**.
4. **Fora do escopo** (não implementar aqui): telas, widgets, rede neural, treinamento, POCID/POSID, serviços pagos.

## Critérios de aceite do documento

Legenda: ✅ implementado e verificado pela suíte contra PostgreSQL · ❌ não atendido.

| Critério | Estado | Onde |
|---|---|---|
| PostgreSQL sobe localmente | ✅ | `docker-compose.yml`; suíte executada contra PostgreSQL 16 |
| Migrations criam tudo | ✅ | `src/migrator.py`, `test_schema.py` |
| SQLite migra para base de teste | ✅ | `src/sqlite_migrator.py`, `test_sqlite_migration.py` |
| Pipeline baixa ou simula Agro.br | ✅ | os dois modos; a coleta real foi executada contra as seis fontes e é coberta por `test_agrobr_real.py` |
| Mesma carga roda 2× sem duplicidade | ✅ | `test_idempotency.py`; ressalva: `src.sqlite_migrator` duplica `raw` se rodar 2× |
| Variáveis geradas com nomes/tipos documentados | ✅ | `docs/data_dictionary.md`, `test_feature_catalog.py` |
| Excluídas fora da consulta padrão | ✅ | `test_feature_catalog.py` (15) |
| Brutos rastreáveis | ✅ | `raw.*` com `file_id`, `pipeline_run_id`, hash |
| Falha não destrói a última versão válida | ✅ | `test_dataset_versioning.py` (20) |
| Poda ≤ 2 anos | ✅ | `test_pruning.py` (12) |
| 4 horizontes parametrizados | ✅ | `FORECAST_HORIZONS`, `test_feature_catalog.py` (17) |
| Testes de vazamento temporal passando | ✅ | `test_no_future_leakage.py` (16) |
| Triggers/procedures nos limites definidos | ✅ | `sql/triggers.sql`, `sql/functions.sql`: só `updated_at`, eventos e upsert |
| Logs/status/hash/versão/contagens registrados | ✅ | `audit.pipeline_runs`, `raw.ingestion_files`, `features.dataset_versions` |
| README completo | ✅ | `README.md`, `docs/runbook.md` |
| Contrato com o modelo documentado | ✅ | `docs/model_contract.md`, `src/model_contract.py` |
| Nenhum widget/tela/modelo neste escopo | ✅ | só o consumidor simulado, que não é modelo |

### Pendências fora das etapas

- **Histórico do CEPEA**: atualizado em 07/10/2026 pelas planilhas do site, importadas com `python -m src.cepea_series --importar` — arábica de 02/09/1996 a 06/10/2026 (7.495 cotações) e robusta de 08/11/2001 a 06/10/2026 (6.161). Precisa ser repetido periodicamente; a coleta avisa quando abrir um buraco.
- **Acesso automático ao CEPEA**: recusado pelo site (HTTP 403 em 07/10/2026; o `robots.txt` de 03/09/2026 declara o bloqueio de acesso automatizado). Se for necessário, depende de autorização do CEPEA. O complemento recente via `agrobr` também depende do site e pode passar a falhar; nesse caso a coleta segue só com o CSV.
- **Primeira carga real**: 9 anos de ajustes da B3 levam mais de 2 horas; ainda não foi feita.
- **Bancos separados para simulado e real**: nada impede hoje uma ingestão simulada de gravar por cima de dados reais.
- **Job de pós-fechamento e a B3**: às 19:00 o arquivo de ajustes do dia pode ainda não estar publicado; a versão gerada repete o ajuste da véspera para aquele dia.
- **Janela histórica**: 9 anos contra ~1.096 dias; decisão da equipe.
- **SQLite legado fora do repositório**: `base/cafe_centro_oeste_mg.db` não é versionado. `src.sqlite_migrator` e `test_sqlite_migration.py` dependem dele; no modo real ele não é necessário.
- **Imutabilidade das versões no banco**: hoje garantida pelo código e conferível por checksum, sem trigger.
- **Liberação do lock na ingestão após erro SQL**: a liberação roda antes do rollback; visto na leitura do código, não reproduzido.
