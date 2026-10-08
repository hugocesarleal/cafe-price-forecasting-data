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
├── base/                        # Base legada SQLite + séries do CEPEA (pasta comum deste repositório)
│   ├── dados_manuais/           # CSVs das séries do CEPEA (versionados; atualizados por src.cepea_series)
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
│   ├── agrobr_client.py         # Contrato de coleta, fábrica e modo simulado
│   ├── agrobr_real.py           # Modo real: CEPEA, BCB, B3 e NASA via agrobr; ICE via Yahoo
│   ├── cepea_series.py          # Importa a planilha da série do CEPEA para os CSVs
│   ├── validation.py            # 12 checagens de qualidade e limites físicos
│   ├── ingestion.py             # Pipeline raw → staging → core (15 passos)
│   ├── transformations.py       # Transformações causais (janelas, defasagens, alvos)
│   ├── feature_builder.py       # core → candidatas → filtro KEEP + alvos y_7d…y_90d
│   ├── dataset_versioning.py    # Versões imutáveis do dataset, com checksum
│   ├── model_contract.py        # Contrato com o modelo: consumo e registro de previsões
│   ├── pipeline.py              # Orquestrador: ingestão → features → versão
│   ├── pruning.py               # Poda lógica da janela histórica (máx. 2 anos)
│   └── integrations/
│       └── django.py            # Adaptador de fronteira para Django/Celery (caixa preta)
│
├── tests/                       # Suite pytest (20 casos obrigatórios)
├── jobs/                        # Jobs agendáveis e manuais
│   ├── runner.py                # Linha de comando, corte e novas tentativas
│   ├── apscheduler_runner.py    # Agendador diário (APScheduler) do modo --schedule
│   ├── update_daily.py          # 00:00 — fecha o dia anterior
│   ├── update_after_close.py    # Pós-fechamento — fecha o próprio dia
│   ├── update_before_open.py    # Pré-abertura — revisa o dia anterior
│   ├── run_on_demand.py         # Execução sob demanda
│   └── reprocess_period.py      # Reprocessamento manual de um período
├── examples/                    # Exemplos de integração (Django: command, task Celery, Admin)
├── docs/                        # Arquitetura, dicionário de dados, contrato ML, runbook
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
| `DB_PORT` | `5433` | Porta (o `.env.example` e o `docker-compose.yml` usam `5432`) |
| `DB_NAME` | `cafe_previsao` | Nome do banco |
| `PIPELINE_DATABASE_URL` | (vazio) | URL completa do banco do pipeline; vence `DB_*` na conexão do pipeline |
| `ADVISORY_LOCK_KEY` | `84729103` | Chave do advisory lock que serializa os ciclos no PostgreSQL |
| `UPDATE_TIME` | `00:00` | Horário do job diário (fuso `TIMEZONE`) |
| `AFTER_CLOSE_TIME` | `19:00` | Horário do job de pós-fechamento |
| `BEFORE_OPEN_TIME` | `08:00` | Horário do job de pré-abertura |
| `JOB_MAX_RETRIES` | `2` | Novas tentativas de um job após erro inesperado |
| `SCHEDULER_ENABLED` | `true` | Liga/desliga o agendador embutido (`--schedule`) |
| `SCHEDULER_JOB_ID` | (nome do job) | Id estável do job no agendador; só com um job por processo |
| `MISFIRE_GRACE_SECONDS` | `3600` | Tolerância para um disparo atrasado ainda rodar uma vez |
| `HISTORICAL_YEARS` | `9` | Janela histórica em anos |
| `MAX_PRUNE_YEARS` | `2` | Teto de poda lógica |
| `AGROBR_MODE` | `simulated` | `real` (fontes de verdade) ou `simulated` (só para testar o pipeline) |
| `CEPEA_AUTO_DOWNLOAD` | `false` | Tenta baixar a série do CEPEA a cada coleta real; o site recusa hoje (403) |
| `COLLECTION_CACHE_DIR` | `.cache/coleta` | Cache em disco da coleta real (ajustes da B3) |
| `COLLECTION_WINDOW_DAYS` | `0` | Dias recoletados pelos jobs agendados; `0` = janela inteira |
| `CONFIRM_HISTORICAL_WINDOW` | `true` | Trava obrigatória em produção |

### 3. Criar o banco e aplicar migrations

```bash
# Criar banco (se ainda não existir); use a porta do seu DB_PORT
createdb -h 127.0.0.1 -p 5432 -U postgres cafe_previsao

# Aplicar todas as migrations
python -m src.migrator
```

### 4. Migrar dados do SQLite legado

```bash
python -m src.sqlite_migrator
```

### 5. Executar a ingestão

Com `AGROBR_MODE=real`, a primeira execução baixa todo o histórico da B3 e leva horas (veja [Fontes de Dados](#fontes-de-dados)). Para começar com uma janela menor, use `python -m jobs.reprocess_period --start AAAA-MM-DD --end AAAA-MM-DD`.

```bash
python -m src.ingestion
```

O comando imprime as métricas da execução em JSON. O fluxo é: coleta (`AGROBR_MODE`) → registro do hash em `raw.ingestion_files` → brutos em `raw` → 12 validações → `staging` → upsert em `core`.

Comportamentos que valem atenção:

| Situação | Resultado |
|---|---|
| Hash da origem não mudou desde a última carga bem sucedida | Nada é republicado; execução termina `SUCCESS` com 0 registros |
| Carga anterior falhou depois de registrar a origem | A origem é processada de novo, mesmo com hash igual |
| `force=True` | Origens inalteradas são reprocessadas; `raw` acumula, `core` não duplica |
| Linha inválida (tipo, nulo, fora do limite) | Vai para `raw` com `validation_status='INVALID'` e não chega a `staging` |
| Checagem **CRÍTICA** reprovada | Publicação interrompida, `core` permanece intacto, execução `FAILED` |
| Checagem de **ALERTA** reprovada | Registrada em `audit.data_quality_checks`, mas não bloqueia |
| Outra execução em andamento | `pg_try_advisory_lock` nega; execução termina `BLOCKED` sem tocar nos dados |
| Falha de coleta | Novas tentativas com espera exponencial até `AGROBR_MAX_RETRIES`; só `SourceFetchError` é repetida |
| Fonte sem nenhum dado na janela (modo real) | Coleta falha em vez de publicar uma variável vazia |

Para reprocessar uma janela específica, use a API Python:

```python
from datetime import date
from src.ingestion import run_ingestion

# start_date/end_date restringem a janela coletada, nos dois modos.
run_ingestion(cutoff_date=date(2026, 1, 1), start_date=date(2025, 1, 1))
```

`cutoff_date` é a data-limite da validação "ausência de informação futura" e vale nos dois modos. Os demais argumentos são repassados ao cliente de coleta.

Toda execução fica auditada em `audit.pipeline_runs` (status, contagens, erro) e em `audit.data_quality_checks` (uma linha por checagem).

### 6. Construir as features

```bash
python -m src.feature_builder
```

Lê `core` até o último dia com `preco_arabica`, calcula as variáveis na grade diária e imprime um resumo em JSON (janela, colunas, alvos preenchidos, candidatas excluídas pelo catálogo). O comando **não grava nada** — serve para inspecionar a matriz antes de publicar.

Só entram as variáveis que o catálogo marca como `KEEP`. Uma variável `KEEP` sem dado em `core` (uma região ausente, por exemplo) interrompe a construção em vez de publicar uma coluna vazia.

### 7. Publicar uma versão do dataset

```bash
python -m src.dataset_versioning
```

Constrói as features, roda as checagens de qualidade e congela o resultado numa versão de `features.dataset_versions`, assinada por SHA-256. O trigger emite `DATASET_READY` em `audit.pending_events` quando a versão fica válida.

| Situação | Resultado |
|---|---|
| Conteúdo idêntico ao de uma versão válida | Nenhuma versão nova; a existente é devolvida (`reused`) |
| Mesmo corte, conteúdo revisado | Versão nova, com o início do checksum na tag |
| Checagem **CRÍTICA** reprovada | Nenhuma versão criada, execução `FAILED`, a última versão válida continua sendo a entregue |
| Erro durante a gravação | Transação desfeita: nem versão, nem linhas parciais |
| Outra execução em andamento | Execução `BLOCKED` |

Para retirar de circulação uma versão já publicada (as linhas ficam preservadas):

```python
from src.db import get_connection
from src.dataset_versioning import invalidate_dataset_version, verify_dataset_version

with get_connection() as conn:
    verify_dataset_version(conn, dataset_version_id)   # o gravado ainda bate com o checksum?
    invalidate_dataset_version(conn, dataset_version_id, "motivo")
    conn.commit()
```

### 8. Simular o consumo pelo modelo

```bash
python -m src.model_contract
```

Lê a última versão válida, gera previsões fictícias para os horizontes configurados e as registra em `predictions.forecasts`. Não é o modelo: é um consumidor de exemplo que exercita o contrato de ponta a ponta.

### 9. Agendar e operar os jobs

Cada job executa o ciclo completo — ingestão, features e versão do dataset — numa execução auditada, com um único lock para o ciclo inteiro.

| Job | Quando | Dia que fecha |
|---|---|---|
| `python -m jobs.update_daily` | `UPDATE_TIME` (00:00) | o dia anterior |
| `python -m jobs.update_after_close` | `AFTER_CLOSE_TIME` | o próprio dia |
| `python -m jobs.update_before_open` | `BEFORE_OPEN_TIME` | o dia anterior (revisão) |
| `python -m jobs.run_on_demand [--cutoff AAAA-MM-DD] [--force]` | manual | o último dia com preço, ou o informado |
| `python -m jobs.reprocess_period --start AAAA-MM-DD --end AAAA-MM-DD` | manual | recoleta a janela e reconstrói o dataset |

Por padrão os jobs agendados recoletam a janela histórica inteira a cada execução, e `raw` (que só acumula) recebe todas essas linhas de novo. Depois da primeira carga, defina `COLLECTION_WINDOW_DAYS` (por exemplo `45`) para que eles recoletem só os dias recentes.

Sem opções, um job roda uma vez e termina — é a forma de usar com cron ou com o Agendador de Tarefas do Windows. Com `--schedule` ele fica em execução e dispara todos os dias no horário configurado, no fuso `TIMEZONE`:

```bash
python -m jobs.update_daily --schedule                 # agendador embutido (APScheduler)
python -m jobs.update_daily --schedule --cutoff 2026-09-30   # fixa o dia de todas as execuções
```

O agendador embutido é o APScheduler (`jobs/apscheduler_runner.py`): um `CronTrigger` diário no fuso `TIMEZONE`, com `coalesce=True` e `max_instances=1` (nada se acumula nem se sobrepõe), tolerância a atraso `MISFIRE_GRACE_SECONDS` (dentro dela o disparo atrasado ainda roda uma vez; além dela fica registrado como perdido e o próximo horário normal assume), id estável (`SCHEDULER_JOB_ID` ou o nome do job — reiniciar não duplica) e encerramento controlado por SIGTERM/SIGINT, esperando a execução em curso. `SCHEDULER_ENABLED=false` desliga o agendamento sem erro. O pacote `apscheduler` só é exigido pelo modo `--schedule`; execução única e cron não dependem dele.

O código de saída é `0` (SUCCESS), `1` (FAILED) ou `2` (BLOCKED: outro job está rodando).

Rodar jobs em sequência não duplica nada: origem inalterada não é republicada, conteúdo idêntico reaproveita a versão do dataset e previsões são gravadas por upsert. Um erro inesperado (banco fora do ar, falha de coleta) faz o job repetir o ciclo até `JOB_MAX_RETRIES` vezes, esperando `JOB_RETRY_WAIT_SECONDS` entre elas; um ciclo reprovado por qualidade dos dados não é repetido.

Cada execução de job fica em `audit.pipeline_runs` com, em `metadata`, os `run_id` das etapas, a versão produzida, a data da última observação disponível e a hora da última ingestão:

```sql
SELECT job_name, status, started_at,
       metadata->>'version_tag'                     AS versao,
       metadata->>'ultima_observacao_preco_arabica' AS ultima_observacao,
       metadata->>'ultima_ingestao_em'              AS ultima_ingestao
FROM audit.pipeline_runs
WHERE job_name LIKE 'update_%'
ORDER BY started_at DESC
LIMIT 10;
```

### 10. Integrar com Django (opcional)

O pipeline é uma **biblioteca externa**: o Django não ganha regra de negócio, não reimplementa o banco em ORM e não duplica o esquema. A fronteira é `src/integrations/django.py`, que monta a configuração canônica, abre a **própria** conexão psycopg do ciclo (o advisory lock nunca usa a conexão gerenciada pelo ciclo de requests do Django) e devolve o dicionário do ciclo (`run_id`, `status`, métricas):

```python
from src.integrations.django import run_pipeline_job, run_ingestion_job, run_dataset_job

resultado = run_pipeline_job("pipeline_django")     # ciclo completo
resultado = run_ingestion_job("pipeline_django")    # só ingestão
resultado = run_dataset_job("pipeline_django")      # só construção do dataset
```

Precedência de configuração, por campo: **explícita do chamador > Django > ambiente/`.env`**. Divergência nunca passa em silêncio: campos de conexão e de retry são honrados com aviso registrando qual fonte venceu; qualquer outro campo divergente derruba a chamada com `PipelineConfigError`.

Exemplos prontos em `examples/django_integration/`: management command fino, task Celery e Admin somente leitura sobre `audit.pipeline_runs`. O esquema continua sendo criado **somente** pelas migrations SQL de `migrations/` (`python -m src.migrator`) — não crie migrations Django duplicadas para as mesmas tabelas. Não agende o pipeline em views, signals ou middlewares.

### 11. Podar a janela histórica

```bash
python -m src.pruning --days 365          # descarta o primeiro ano da última versão válida
python -m src.pruning --start 2019-01-01  # primeiro dia que continua ativo
python -m src.pruning --restore           # desfaz a poda
```

A poda é lógica: marca `is_pruned = TRUE` nas linhas mais antigas de `features.model_features` e o consumidor deixa de recebê-las. Nada é apagado, e `raw` e `core` não são tocados. É recusada, sem alterar nada, se descartar mais de `MAX_PRUNE_YEARS` (730 dias, contando o que já foi podado na versão), se deixar menos de `DATASET_MIN_DAYS` dias ativos, ou sem `CONFIRM_HISTORICAL_WINDOW=true`.

### 12. Executar os testes

```bash
python -m pytest tests/ -v
```

Os testes de ingestão exigem um PostgreSQL acessível e usam o job `teste_ingestao` como escopo: `tests/conftest.py` apaga o próprio rastro antes e depois de cada caso, então é seguro rodar contra um banco com cargas reais. `test_transformations.py`, `test_no_future_leakage.py`, `test_jobs.py`, `test_agrobr_real.py`, `test_cepea_series.py` e parte de `test_feature_catalog.py`, `test_dataset_versioning.py`, `test_model_contract.py`, `test_scheduler.py` e `test_django_integration.py` rodam em memória, sem banco. Os testes de versionamento e de previsões criam suas versões numa transação que nunca é confirmada, então nenhum outro consumidor do banco chega a vê-las.

---

## Fontes de Dados

O modo de coleta é escolhido por `AGROBR_MODE`.

### Modo real (`AGROBR_MODE=real`)

| Variável | Fonte | Como é coletada |
|---|---|---|
| `preco_arabica` (alvo), `preco_robusta` | CEPEA/ESALQ | CSVs em `base/dados_manuais/`, atualizados a partir da planilha do site, + `agrobr` para os dias recentes |
| `usd_brl` | BCB PTAX (venda) | `agrobr` |
| `b3_cafe_ajuste` | B3, contrato ICF, 1º vencimento | `agrobr`, um arquivo por pregão, com cache em disco |
| `ice_kc` | ICE US Coffee C (`KC=F`), fechamento | Yahoo Finance (`yfinance`) |
| Clima, 7 séries × 3 regiões | NASA POWER | `agrobr`, um ponto por região (Bambuí, Varginha, Patrocínio) |

O que é preciso saber para operar:

- **O CEPEA exige um passo manual periódico.** O histórico vem dos CSVs de `base/dados_manuais/`; o `agrobr` só enxerga as cotações recentes, então um CSV velho deixa um buraco entre os dois, avisado no log. Para atualizar: baixe pelo navegador a planilha "Série histórica" de cada indicador e rode `python -m src.cepea_series --importar arquivo1.xls arquivo2.xls`. O comando reconhece a série pelo título, confere que é a mesma do CSV atual (cobertura e preços coincidentes) e guarda o anterior como `.bak`. O download automático existe (`CEPEA_AUTO_DOWNLOAD=true`, ou `python -m src.cepea_series`), mas o site o recusa com HTTP 403, então vem desligado.
- **A primeira coleta da B3 é lenta.** Cada pregão é um arquivo de ~11 MB com o mercado inteiro: cerca de 4 segundos por dia, ou seja, **mais de 2 horas para 9 anos**. Os dias baixados ficam em `COLLECTION_CACHE_DIR`, então isso acontece uma vez; uma coleta interrompida continua de onde parou, e a rotina diária baixa só o pregão novo.
- **O clima chega com atraso.** O NASA POWER publica cerca de 3 dias depois (5 para radiação). As features usam o clima com esse mesmo atraso, para que o treino veja o que a previsão terá.
- **Licenças.** CEPEA/ESALQ é CC BY-NC 4.0 (uso não comercial, com citação). B3 e Yahoo Finance não têm termos claros para acesso programático; para uso comercial, confirme antes.

### Modo simulado (`AGROBR_MODE=simulated`)

Serve para desenvolver e testar o pipeline sem rede. **Não serve para treinar modelo**: só os preços do CEPEA (lidos dos mesmos CSVs) são reais. Câmbio, ICE e B3 são derivados das duas colunas de preço do CSV do arábica, e o clima é gerado por fórmula.

> **Não misture os dois modos no mesmo banco.** A publicação em `core` é por upsert: uma ingestão simulada grava por cima de câmbio, futuros e clima reais das mesmas datas, e um banco que já recebeu dados simulados fica com eles onde a coleta real não tiver valor. Use bancos separados.

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
| **Sem data leakage** | Nada posterior ao corte é lido; janelas fechadas em $t$; preenchimento só para frente e com limite; `preco_arabica` do dia nunca é feature; clima deslocado pelo atraso de publicação do NASA POWER (`PUBLICATION_LAG_DAYS`: 3 dias, 5 para radiação) |
| **Imutabilidade do raw** | `raw.market_observations` e `raw.weather_observations` nunca têm linhas deletadas |
| **Idempotência** | Upsert com `ON CONFLICT … DO UPDATE SET col = COALESCE(excluded.col, atual)` |
| **Sem concorrência** | `pg_try_advisory_lock` impede duas instâncias simultâneas |
| **Teto de poda** | Máximo 2 anos removidos da janela ativa, mesmo em podas sucessivas; poda é lógica (`is_pruned = TRUE`), reversível e nunca física |
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
| `test_cepea_series.py` | 30 | Leitura da planilha do CEPEA (datas dd/mm, números pt-BR), conferência contra o CSV existente, gravação com `.bak`, falhas que não tocam no arquivo |
| `test_agrobr_real.py` | 31 | Coleta real com downloads substituídos: junção CEPEA manual + recente, PTAX, 1º vencimento da B3, cache e retomada, pregão em andamento da ICE, clima não publicado, falhas de rede (1 teste contra as fontes de verdade, desligado por padrão) |
| `test_ingestion.py` | 19 | Pipeline ponta a ponta, metadados da coleta, quarentena, bloqueio por falha CRÍTICA, falha de conexão (9), retry (10), lock concorrente (11), retomada após falha no meio da carga e `force` |
| `test_idempotency.py` | 9 | Carga repetida não duplica (3), deduplicação dentro e entre origens (4), upsert sem duplicar datas e com `COALESCE` (5) |
| `test_transformations.py` | 16 | Janelas móveis, preenchimento causal, calendário cíclico, alvos e separação treino/validação/teste com embargo |
| `test_no_future_leakage.py` | 10 | Anti-leakage por perturbação (16): alterar o futuro não muda o passado, corte, alvo fora das features, atraso de publicação |
| `test_feature_catalog.py` | 29 | Somente KEEP e alvos (14), DROP fora da tabela final (15), alvos de 7/15/30/90 dias (17), publicação sem duplicar |
| `test_dataset_versioning.py` | 20 | Checksum, checagens da matriz, versão imutável (18), falha não substitui nem apaga a última versão válida (20) |
| `test_model_contract.py` | 25 | Metadados e consulta padrão do contrato, validação e registro das previsões por horizonte (19) |
| `test_pipeline.py` | 11 | Orquestrador: etapas e contagens, frescor registrado, corte, falha por etapa, erro inesperado, lock e conexão única do ciclo |
| `test_jobs.py` | 29 | Horários e fuso, dia de corte de cada job, novas tentativas, códigos de saída e argumentos |
| `test_scheduler.py` | 20 | Agendador APScheduler: gatilhos e fuso, tolerância de atraso (misfire), `coalesce`/`max_instances`, reinício sem duplicar, lock como barreira, falha que não derruba o processo, encerramento controlado e ponte do modo `--schedule` |
| `test_django_integration.py` | 19 | Adaptador Django: precedência de configuração (explícita > Django > ambiente), validação de campos, conexão própria do ciclo, tradução de erros e leitura do Admin |
| `test_pruning.py` | 22 | Teto de 2 anos e mínimo de segurança (12), nenhum dado apagado e `raw`/`core` intactos (13), poda reversível |

Os números entre parênteses são os **20 testes obrigatórios** do documento de especificação; os 20 estão implementados.

---

## Contrato com o Time de ML

O componente de rede neural **consome** a última versão válida:

```python
from src.db import get_connection
from src.model_contract import get_latest_dataset, load_features, register_forecasts

with get_connection() as conn:
    dataset = get_latest_dataset(conn)    # versão, corte, colunas e tipos, horizontes, qualidade
    features = load_features(conn, dataset["dataset_version_id"])
    ...
```

E **devolve** as previsões, uma por horizonte:

```python
    register_forecasts(conn, previsoes)   # valida o lote e faz upsert em predictions.forecasts
    conn.commit()
```

Uma previsão repetida para a mesma `(reference_date, horizon_days, model_version)` é atualizada, não duplicada.

Ver [`docs/model_contract.md`](docs/model_contract.md) para o contrato completo.

---

## Documentação

| Documento | Conteúdo |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Fluxo dos 6 schemas, pipeline de 15 passos, locks e janela histórica |
| [`docs/data_dictionary.md`](docs/data_dictionary.md) | Catálogo completo de variáveis (KEEP, TARGET, DROP) e modelagem das tabelas |
| [`docs/model_contract.md`](docs/model_contract.md) | Contrato formal de integração com o time de ML, consulta SQL padrão e mock consumer |
| [`docs/runbook.md`](docs/runbook.md) | Operação diária, verificação de saúde, recuperação de falhas, reprocessamento e poda |
| [`docs/TODO.md`](docs/TODO.md) | Estado de cada etapa, decisões em aberto e critérios de aceite |
