# Arquitetura do pipeline de dados

## 1. Escopo

O pipeline coleta, armazena, valida, transforma e versiona os dados usados para prever o Indicador CEPEA/ESALQ do café arábica (R$/saca de 60 kg). Ele não treina nem executa o modelo de previsão.

- **Dentro do escopo:** ingestão de dados de mercado e clima em PostgreSQL, validação de qualidade, cálculo das variáveis selecionadas (`KEEP`), separação temporal entre entradas e alvos ($X_t \to y_{t+h}$), catálogo de variáveis, versões imutáveis do dataset, controle da janela histórica e da poda (no máximo 2 anos), registro das previsões em `predictions.forecasts`, auditoria.
- **Fora do escopo:** o modelo de previsão (treino e inferência), painéis e visualizações, métricas de acurácia do modelo, fontes de dados pagas e exclusão física dos dados brutos (`raw`).

---

## 2. Schemas do PostgreSQL

O banco é dividido em seis schemas:

```
[Fontes: CEPEA, BCB PTAX, NASA POWER, B3, ICE (Yahoo Finance)]
                  │
                  ▼
         ┌───────────────────┐
         │     1. raw        │  <- dados brutos, imutáveis e auditáveis
         └─────────┬─────────┘
                   │
                   ▼
         ┌───────────────────┐
         │    2. staging     │  <- limpeza, tipagem e validação
         └─────────┬─────────┘
                   │
                   ▼
         ┌───────────────────┐
         │     3. core       │  <- séries diárias deduplicadas
         └─────────┬─────────┘
                   │
                   ▼
         ┌───────────────────┐
         │    4. features    │  <- catálogo, variáveis KEEP e versões
         └─────────┬─────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
┌──────────────────┐  ┌──────────────────┐
│  5. predictions  │  │    6. audit      │
│   (previsões)    │  │(execuções/checks)│
└──────────────────┘  └──────────────────┘
```

1. **`raw`**: os dados como chegaram da fonte, com os metadados da carga (URL, arquivo, hash SHA-256, horário da coleta e `run_id`).
2. **`staging`**: área transitória de tipagem, normalização de datas e validação, por carga.
3. **`core`**: séries diárias limpas e deduplicadas (`core.market_daily` e `core.weather_daily`), com o histórico completo.
4. **`features`**: catálogo de variáveis (`features.variable_catalog`), variáveis derivadas, alvos de 7, 15, 30 e 90 dias e versões imutáveis do dataset (`features.dataset_versions`).
5. **`predictions`**: onde o modelo de previsão grava suas inferências, com `dataset_version_id`, `model_version`, data de corte e horizonte.
6. **`audit`**: execuções (`pipeline_runs`), checagens de qualidade (`data_quality_checks`), execuções do modelo (`model_runs`) e eventos (`pending_events`).

---

## 3. Fluxo de uma execução

1. **Início:** gera um `run_id` (UUID v4) e registra a execução em `audit.pipeline_runs` com status `RUNNING`.
2. **Concorrência:** obtém o advisory lock (`pg_try_advisory_lock`); se outra execução estiver em andamento, termina como `BLOCKED`.
3. **Coleta:** obtém cotações e clima pelo cliente configurado em `AGROBR_MODE`.
4. **Metadados:** grava fonte, URL, arquivo, hash e horário em `raw.ingestion_files`.
5. **Mudança na fonte:** compara o hash com o da última carga bem sucedida.
6. **Brutos:** insere as observações em `raw.market_observations` e `raw.weather_observations`.
7. **Validação:** confere colunas, tipos, datas e limites físicos.
8. **Staging:** carrega as linhas aprovadas nas tabelas de `staging`.
9. **Upsert em `core`:** procedures fazem a mesclagem idempotente em `core.market_daily` e `core.weather_daily` (`ON CONFLICT DO UPDATE` com `COALESCE`).
10. **Variáveis derivadas:** janelas móveis fechadas no dia de corte $t$ e codificação cíclica do calendário.
11. **Versão do dataset:** registra a versão em `features.dataset_versions` e grava em `features.model_features` só as variáveis `KEEP` e os alvos `y_*`; `preco_arabica` não é gravado como entrada.
12. **Aviso de prontidão:** um trigger registra o evento em `audit.pending_events`.
13. **Consumo:** a consulta padrão e os metadados ficam disponíveis para o modelo.
14. **Previsões:** recebe e valida as inferências em `predictions.forecasts`.
15. **Fim:** atualiza `audit.pipeline_runs` para `SUCCESS` ou `FAILED`, com tempos e contagens, e libera o lock.

---

## 4. Idempotência e concorrência

- **Idempotência:** cada observação tem chave natural única, `(data_ref, variable_name)` no mercado e `(data_ref, regiao, variable_name)` no clima. Repetir a carga de uma data atualiza os mesmos registros, sem duplicar linhas.
- **Concorrência:** o advisory lock usa a chave inteira `ADVISORY_LOCK_KEY` (padrão `84729103`):
  `SELECT pg_try_advisory_lock(84729103);`
  Uma segunda instância que tente rodar ao mesmo tempo termina imediatamente, com registro em log e código de saída próprio.

---

## 5. Janela histórica e poda

O projeto combina duas exigências: uma janela de **9 anos**, da qual se pode podar no máximo **2 anos**, e um mínimo de **1.096 dias** (cerca de 3 anos) de dados ativos. Na versão de treino de 22/10/2026, `core` guarda 9 anos de histórico, a versão começa em **2018-01-02** (primeiro ajuste publicado pela B3) e nenhuma poda foi aplicada. As regras abaixo valem para as versões seguintes.

1. **Registro da janela:** ao iniciar, o sistema registra em log a janela configurada (`HISTORICAL_YEARS=9`), o mínimo (`DATASET_MIN_DAYS=1096`) e as datas encontradas na base.
2. **Confirmação:** a poda exige `CONFIRM_HISTORICAL_WINDOW=true`; sem ela, o pipeline se recusa a prosseguir.
3. **Teto:** a poda rejeita qualquer pedido que descarte mais de 730 dias (2 anos) da janela.
4. **Poda lógica:** a poda só marca linhas de `features.model_features` com `is_pruned = TRUE`. Nada em `raw` ou `core` é apagado.
5. **Piso da janela:** `DATASET_START_DATE` (`2018-01-02` na versão de treino) define o início das versões e prevalece sobre o recuo de `HISTORICAL_YEARS` quando o corte é posterior. Se o piso for posterior ao corte, o sistema registra um aviso e volta ao recuo histórico. A coleta e `core` continuam com o histórico completo.

---

## 6. Vazamento temporal

Para cada dia $t$ e horizonte $h \in \{7, 15, 30, 90\}$, as entradas de $t$ só usam informação disponível até $t$:
$$X_t \longrightarrow y_{t+h}$$

- **Alvo contemporâneo:** `preco_arabica` do dia $t$ nunca entra como entrada; só defasagens, retornos e médias passadas.
- **Atraso de publicação:** séries divulgadas com atraso são deslocadas antes de entrar nas features. O clima do NASA POWER entra com 3 dias de atraso (5 para radiação), conforme `PUBLICATION_LAG_DAYS`. Lacunas só são preenchidas para frente, com limite.
- **Janelas fechadas em $t$:** médias, desvios e somas de chuva de 30 e 90 dias são calculados em `src/transformations.py` com janelas que terminam em $t$.

---

## 7. Triggers e procedures

Triggers ficam restritos a tarefas leves e locais:
- atualização da coluna `updated_at`;
- registro de eventos em `audit.pending_events`.

Nenhum trigger faz chamada HTTP, coleta, processamento pesado ou inferência.

Procedures:
- `core.sp_upsert_market_observations(p_run_id UUID)`: move e deduplica o mercado de `staging` para `core`.
- `core.sp_upsert_weather_observations(p_run_id UUID)`: faz o mesmo para o clima.
- `audit.sp_register_pipeline_start(...)` / `audit.sp_register_pipeline_finish(...)`: registram início e fim das execuções.
- `audit.fn_emit_event(...)`: registra eventos em `audit.pending_events`.

O congelamento da versão do dataset não é uma procedure: fica em `src/dataset_versioning.py`, porque o checksum é calculado sobre a matriz antes da gravação e a versão só é validada depois que todas as linhas entram, na mesma transação.

---

## 8. Coleta

`AgrobrClient` (em `src/agrobr_client.py`) define o contrato da coleta e a política de novas tentativas; `AGROBR_MODE` escolhe a implementação.

- **Modo real** (`src/agrobr_real.py`): usa a biblioteca `agrobr` para CEPEA/ESALQ, BCB PTAX, B3 e NASA POWER, e o Yahoo Finance para o contrato Coffee C da ICE. O histórico do CEPEA vem de CSVs atualizados a partir da planilha do site (`src/cepea_series.py`, por importação do arquivo baixado no navegador, já que o site recusa o download automático); o `agrobr` completa os dias recentes. Os ajustes da B3 são baixados um pregão por vez, com cache em disco.
- **Modo simulado** (`SimulatedAgrobrClient`): lê os mesmos CSVs do CEPEA e gera o restante, com câmbio e futuros derivados das colunas de preço e clima por fórmula determinística. Serve para desenvolver e testar o pipeline sem rede; seus dados não servem para treino.

Os dois modos produzem o mesmo formato (`SourceFile` com linhas longas de mercado e clima), então nada depois da coleta sabe de qual deles os dados vieram. Por isso os dois não devem alimentar o mesmo banco.
