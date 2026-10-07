# Arquitetura do Componente de Dados — Previsão do Preço do Café Arábica

## 1. Visão Geral e Limites de Escopo

Este componente tem como objetivo exclusivo a coleta, armazenamento, validação de qualidade, engenharia de features causais, versionamento e disponibilização de dados para o modelo de previsão do café arábica (CEPEA/ESALQ R$/saca 60kg).

### Limites Obrigatórios:
- **Dentro do escopo:** Ingestão de dados de mercado e clima, migração do SQLite local para PostgreSQL, validação rigorosa de qualidade, derivação de features selecionadas (KEEP), separação estrita sem vazamento temporal ($X_t \to y_{t+h}$), catálogo de variáveis, versionamento imutável de datasets, controle da janela histórica e contenção de poda (máximo 2 anos), gravação contratual de previsões em `predictions.forecasts`, auditoria e observabilidade.
- **Fora do escopo:** Redes neurais, treinamento ou inferência de machine learning, visualizações/dashboards/widgets, métricas de acurácia de modelo (POCID/POSID), serviços pagos de dados e exclusão física irreversível de dados brutos (`raw`).

---

## 2. Topologia de Schemas no PostgreSQL

O banco de dados PostgreSQL é dividido em 6 schemas lógicos com responsabilidades bem delimitadas:

```
[Fontes: Agro.br, CEPEA, BCB, NASA, B3]
                  │
                  ▼
         ┌───────────────────┐
         │     1. raw        │  <- Armazenamento bruto, imutável e auditável
         └─────────┬─────────┘
                   │
                   ▼
         ┌───────────────────┐
         │    2. staging     │  <- Limpeza inicial, parsing e validação de tipos
         └─────────┬─────────┘
                   │
                   ▼
         ┌───────────────────┐
         │     3. core       │  <- Entidades diárias normalizadas e consolidadas
         └─────────┬─────────┘
                   │
                   ▼
         ┌───────────────────┐
         │    4. features    │  <- Catálogo, variáveis KEEP e datasets versionados
         └─────────┬─────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
┌──────────────────┐  ┌──────────────────┐
│  5. predictions  │  │    6. audit      │
│ (Forecasts ext.) │  │(Execuções/Checks)│
└──────────────────┘  └──────────────────┘
```

1. **`raw`**: Recebe os dados exatamente como fornecidos pela fonte, sem transformações destrutivas, acompanhados de metadados de carga (URL, commit/arquivo, hash SHA-256, timestamp de coleta e `run_id`).
2. **`staging`**: Área de descompressão e validação preliminar (tipagem, normalização de datas, filtros de sintaxe). Tabelas truncadas ou particionadas por carga.
3. **`core`**: Séries históricas limpas, deduplicadas e normalizadas em granularidade diária (`core.market_daily` e `core.weather_daily`), mantendo histórico completo.
4. **`features`**: Catálogo de variáveis (`features.variable_catalog`), cálculo causal de variáveis derivadas autorizadas, geração de alvos para 7, 15, 30 e 90 dias, e snapshots versionados imutáveis (`features.dataset_versions`).
5. **`predictions`**: Tabela de destino onde o modelo externo de rede neural grava suas inferências, referenciando o `dataset_version_id`, `model_version`, data de corte e horizonte.
6. **`audit`**: Rastreabilidade completa de pipeline (`pipeline_runs`), verificações de qualidade (`data_quality_checks`), execuções de modelo (`model_runs`) e eventos assíncronos (`pending_events`).

---

## 3. Fluxo de Execução do Pipeline (15 Passos)

O pipeline implementa o seguinte ciclo de vida estritamente orquestrado:

1. **Iniciar execução:** Gera um `run_id` (UUID v4) único e registra o início em `audit.pipeline_runs` com status `RUNNING`.
2. **Verificar Concorrência:** Adquire PostgreSQL Advisory Lock (`pg_try_advisory_lock`) para impedir execuções simultâneas conflitantes.
3. **Baixar dados do Agro.br e fontes:** Obtém cotações de mercado e dados climáticos via adaptador configurável.
4. **Registrar metadados:** Salva fonte, URL, arquivo, hash e timestamp em `raw.ingestion_files`.
5. **Verificar se a fonte mudou:** Compara hash da carga anterior com a atual.
6. **Salvar dados brutos em `raw`:** Insere observações em `raw.market_observations` e `raw.weather_observations`.
7. **Validar colunas, tipos, datas e valores:** Validador em Python inspeciona esquema e limites plausíveis.
8. **Carregar `staging`:** Carga controlada nas tabelas transitórias de staging.
9. **Deduplicação e Upsert:** Stored procedures executam mesclagem idempotente em `core.market_daily` e `core.weather_daily` via `ON CONFLICT DO UPDATE` com `COALESCE`.
10. **Recalcular variáveis derivadas:** Calcula métricas causais autorizadas (janelas móveis até a data de corte $t$, codificação cíclica seno/cosseno).
11. **Criar versão imutável do dataset:** Registra snapshot em `features.dataset_versions` e popula `features.model_features` apenas com variáveis `KEEP` e `TARGET`.
12. **Sinalizar prontidão:** Dispara trigger/evento em `audit.pending_events` indicando dataset pronto para consumo.
13. **Disponibilizar contrato para previsão:** Expõe consulta e metadados para o componente externo.
14. **Registrar previsões:** Recebe e valida inferências externas em `predictions.forecasts`.
15. **Finalizar execução:** Atualiza `audit.pipeline_runs` para `SUCCESS` (ou `FAILED`), computando métricas de tempo, contagem de linhas e logs. Libera o lock.

---

## 4. Idempotência e Bloqueio de Concorrência

- **Idempotência:** Cada observação possui chave natural única:
  - Mercado: `(data_ref, variable_name)`
  - Clima: `(data_ref, regiao, variable_name)`
  - Reexecutar uma carga para a mesma data sobrescreve os mesmos registros com os dados mais recentes sem duplicar linhas.
- **Bloqueio de Concorrência:**
  - Uso de **PostgreSQL Advisory Locks** vinculados a uma chave inteira constante de aplicação:
    `SELECT pg_try_advisory_lock(hashtext('pipeline_cafe_etl_lock'));`
  - Se outra instância tentar rodar ao mesmo tempo, a execução é abortada imediatamente com registro em log e saída segura com código de aviso, impedindo race conditions.

---

## 5. Diretrizes da Janela Histórica e Contenção de Poda

### Inconsistência Documentada:
O histórico do projeto referencia duas janelas temporais contraditórias:
- Janela de **9 anos** com poda máxima autorizada de **2 anos**.
- Janela legada do SQLite com aproximadamente **1.096 dias** (~3 anos).

### Regras de Implementação do Sistema:
1. **Transparência Absoluta:** O sistema inicializa imprimindo em nível WARNING/INFO o comparativo entre a janela configurada (`HISTORICAL_YEARS=9`), o mínimo de segurança (`DATASET_MIN_DAYS=1096`) e os limites de data encontrados na base.
2. **Trava em Produção:** Requer explicitamente `CONFIRM_HISTORICAL_WINDOW=true` nas variáveis de ambiente. Caso ausente em produção, o pipeline recusa prosseguir e instrui a confirmação.
3. **Teto de Poda Máxima de 2 Anos:** A função de poda rejeita formalmente qualquer tentativa de descartar mais de 730 dias (2 anos) da janela móvel histórica.
4. **Poda Estritamente Lógica:** A poda atua **apenas** nas visualizações e tabelas de features (`features.model_features`), aplicando flags de arquivamento (`is_pruned = TRUE`). Os dados em `raw` e `core` **nunca sofrem delete físico**.

---

## 6. Prevenção Rigorosa Contra Vazamento Temporal (Data Leakage)

O pipeline garante que para cada dia $t$ e horizonte preditivo $h \in \{7, 15, 30, 90\}$:
$$X_t \longrightarrow y_{t+h}$$

- **Nenhuma informação contemporânea ou futura do alvo entra nas features do dia $t$:**
  `preco_arabica` contemporâneo de $t$ **nunca** é utilizado como feature preditiva para prever $t$. Apenas defasagens (lags passados: $t-1, t-2, \dots$), retornos passados e médias passadas são permitidos.
- **Preenchimento Causal Estrito:** Séries que possuem divulgação defasada (como COT e ONI) só são propagadas a partir da sua `data_pub` real, impedindo que dados do fim do mês sejam projetados retrospectivamente para o início do mês antes da divulgação.
- **Janelas Móveis Fechadas em $t$:** Todas as funções agregadas (médias móveis, desvios, somas de precipitação de 30 e 90 dias) usam janelas do tipo `ROWS BETWEEN 29 PRECEDING AND CURRENT ROW`, sem extrapolação para o futuro.

---

## 7. Políticas de Triggers e Stored Procedures

### Triggers (Exclusivamente Leves e Locais):
- Atualização automática de coluna `updated_at`.
- Auditoria de alterações e inserção de eventos em `audit.pending_events`.
- **Proibido expressamente em triggers:** Chamadas HTTP, downloads do Agro.br, scrapers, processamento pesado ou inferência de redes neurais.

### Stored Procedures (Transações no Banco):
- `core.sp_upsert_market_observations(p_run_id UUID)`: Migra e deduplica de staging para core.
- `core.sp_upsert_weather_observations(p_run_id UUID)`: Consolida dados climáticos.
- `audit.sp_register_pipeline_start(...)` / `audit.sp_register_pipeline_finish(...)`: Registram início e término de execuções.
- `audit.fn_emit_event(...)`: Registra eventos em `audit.pending_events`.

O congelamento do snapshot de features não é uma procedure: fica em `src/dataset_versioning.py`, porque o checksum é calculado sobre a matriz antes da gravação e a versão só é validada depois que todas as linhas entram, na mesma transação.

---

## 8. Integração com o Agro.br

O módulo `src/agrobr_client.py` implementa um adaptador com padrão Strategy:
- **Modo Real:** Consome bibliotecas públicas do Agro.br e APIs conectadas (BCB, NASA POWER, B3).
- **Modo Simulado / Fallback:** Gera dados sintéticos consistentes ou consome arquivos locais (como os CSVs de histórico CEPEA presentes em `base/dados_manuais`) para execução em ambiente de testes ou quando serviços externos estiverem fora do ar.
