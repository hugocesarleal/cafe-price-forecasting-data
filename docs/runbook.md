# Runbook — Operação, Recuperação e Reprocessamento

Guia de quem opera o pipeline de dados no dia a dia. Para o desenho do sistema, veja [`architecture.md`](architecture.md); para o que o modelo consome e devolve, [`model_contract.md`](model_contract.md).

Todos os comandos são executados na raiz do repositório, com o `.env` configurado.

---

## 1. Rotina

| Horário (`America/Sao_Paulo`) | Job | O que faz |
|---|---|---|
| `UPDATE_TIME` (00:00) | `python -m jobs.update_daily` | Fecha o dia anterior |
| `BEFORE_OPEN_TIME` (08:00) | `python -m jobs.update_before_open` | Revisa o dia anterior; só grava se algo mudou |
| `AFTER_CLOSE_TIME` (19:00) | `python -m jobs.update_after_close` | Fecha o próprio dia |

Cada job faz o ciclo inteiro: coleta → `raw` → validação → `staging` → `core` → features → versão do dataset.

Duas formas de agendar:

- **Agendador externo** (cron, Agendador de Tarefas): chame o job sem opções. Ele roda uma vez e devolve o código de saída.
- **Agendador embutido**: `python -m jobs.update_daily --schedule` fica em execução e dispara todo dia no horário. É um processo por job; se ele cair, nada o reinicia.

| Código de saída | Significado | O que fazer |
|---|---|---|
| `0` | SUCCESS | Nada |
| `1` | FAILED | Ver a seção 3 |
| `2` | BLOCKED | Outro job está rodando; não é erro. Se persistir, ver 3.5 |

---

## 2. Verificar a saúde

### Últimas execuções dos jobs

```sql
SELECT job_name, status, started_at, finished_at - started_at AS duracao,
       records_ingested, records_features,
       metadata->>'version_tag'                     AS versao,
       metadata->>'failed_stage'                    AS etapa_reprovada,
       metadata->>'ultima_observacao_preco_arabica' AS ultima_observacao,
       metadata->>'ultima_ingestao_em'              AS ultima_ingestao,
       error_message
FROM audit.pipeline_runs
WHERE job_name IN ('update_daily', 'update_before_open', 'update_after_close',
                   'run_on_demand', 'reprocess_period')
ORDER BY started_at DESC
LIMIT 15;
```

`ultima_observacao` parada há vários dias úteis com os jobs em SUCCESS significa que a fonte não está publicando dado novo, não que o pipeline falhou.

### Execuções de cada etapa

Os jobs registram a execução-mãe; ingestão (`ingestao_agrobr`) e dataset (`dataset_features`) têm as suas, ligadas por `metadata->>'ingestion_run_id'` e `metadata->>'dataset_run_id'`.

```sql
SELECT job_name, status, started_at, records_ingested, records_features, error_message
FROM audit.pipeline_runs
ORDER BY started_at DESC
LIMIT 30;
```

### Checagens de qualidade reprovadas

```sql
SELECT c.checked_at, r.job_name, c.severity, c.check_name, c.table_name, c.details
FROM audit.data_quality_checks c
LEFT JOIN audit.pipeline_runs r ON r.run_id = c.pipeline_run_id
WHERE NOT c.passed
ORDER BY c.checked_at DESC
LIMIT 30;
```

`CRITICAL` reprovada bloqueia a publicação. `WARNING` só fica registrada.

### Versão que o modelo está lendo

```sql
SELECT version_tag, cutoff_date, start_date, row_count, feature_count, created_at
FROM features.dataset_versions
WHERE is_valid
ORDER BY created_at DESC
LIMIT 5;
```

A primeira linha é a versão entregue ao componente de previsão.

### Eventos pendentes

```sql
SELECT event_type, count(*), min(created_at), max(created_at)
FROM audit.pending_events
WHERE status = 'PENDING'
GROUP BY event_type;
```

Nenhum componente deste repositório consome esses eventos: eles ficam `PENDING` até que um consumidor externo os marque. `DATASET_READY` é o sinal para o componente de previsão.

---

## 3. Recuperação

### 3.1 Job FAILED por erro inesperado (banco fora do ar, falha de coleta)

O job já repete o ciclo sozinho (`JOB_MAX_RETRIES`, com `JOB_RETRY_WAIT_SECONDS` de espera). Se esgotou as tentativas, corrija a causa e rode de novo:

```bash
python -m jobs.run_on_demand
```

Repetir é seguro. Uma carga que caiu no meio é reprocessada desde o início, porque a origem só conta como "já carregada" depois de uma execução em SUCCESS. `raw` fica com as linhas da tentativa que falhou (ele nunca é limpo); `core` não duplica.

### 3.2 Job FAILED na etapa `ingestion` (qualidade dos dados)

Uma checagem crítica reprovou a coleta. `core` não foi alterado e a última versão do dataset continua valendo.

1. Veja qual checagem e quais linhas, na consulta de checagens reprovadas (seção 2). O campo `details` traz exemplos.
2. As linhas reprovadas estão em `raw`, em quarentena:

   ```sql
   SELECT source, observation_date, region, variable_name, value, unit
   FROM raw.market_observations
   WHERE validation_status = 'INVALID'
   ORDER BY ingested_at DESC
   LIMIT 50;
   ```

3. Corrija na origem (por exemplo, o CSV em `base/dados_manuais/`) e rode `python -m jobs.run_on_demand`.

Repetir sem corrigir não adianta: a mesma carga reprova de novo, e cada tentativa grava outra cópia das linhas em `raw`.

Se a reprovação for `datas_futuras` ou `ausencia_informacao_futura`, a coleta trouxe dado posterior ao dia que o job estava fechando. Rode com o corte certo: `python -m jobs.run_on_demand --cutoff AAAA-MM-DD`.

### 3.3 Job FAILED na etapa `dataset`

A ingestão foi publicada em `core`, mas a matriz de features foi reprovada e nenhuma versão foi criada. Motivos críticos:

| Checagem | Causa provável |
|---|---|
| `features_sem_coluna_vazia` | Uma série inteira não chegou a `core` na janela |
| `matriz_nao_vazia` | `core` não tem dados no período |
| `alvo_fora_das_features` | Catálogo liberou o alvo como feature |

Se o job terminou com exceção em vez de reprovação, a mensagem diz o que falta. As mais comuns:

- *"Variáveis KEEP do catálogo sem cálculo disponível"*: falta uma região (`sulmg` ou `cerrado`) em `core.weather_daily`, ou o catálogo marca como KEEP algo que não é calculado.
- *"core.market_daily não tem preco_arabica até a data de corte"*: a ingestão ainda não publicou preço.

Para inspecionar a matriz sem gravar nada:

```bash
python -m src.feature_builder
```

### 3.4 Uma versão publicada está errada

Retire-a de circulação. As linhas ficam preservadas; a versão válida anterior volta a ser a entregue.

```python
from src.db import get_connection
from src.dataset_versioning import invalidate_dataset_version

with get_connection() as conn:
    anterior = invalidate_dataset_version(conn, "<dataset_version_id>", "motivo")
    conn.commit()
    print("Versão em uso agora:", anterior.version_tag if anterior else None)
```

Para saber se as linhas de uma versão foram alteradas depois de publicadas:

```python
from src.db import get_connection
from src.dataset_versioning import verify_dataset_version

with get_connection() as conn:
    print(verify_dataset_version(conn, "<dataset_version_id>"))   # False = adulterada
```

Depois de corrigir os dados, `python -m jobs.run_on_demand` gera uma versão nova. Não existe "editar" uma versão.

### 3.5 Job sempre BLOCKED

O lock é de sessão do PostgreSQL: some quando a conexão que o segura fecha. Se nenhum job está rodando e o bloqueio continua, há uma conexão presa.

```sql
SELECT a.pid, a.state, a.query_start, a.application_name, left(a.query, 80) AS consulta
FROM pg_locks l
JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.locktype = 'advisory' AND l.objid = 84729103;
```

Confirme que o processo não é um job legítimo em andamento e só então encerre a conexão com `SELECT pg_terminate_backend(<pid>);`.

Execuções que ficaram como `RUNNING` depois de um processo morto não bloqueiam nada: são só registro. O status não é corrigido automaticamente.

### 3.6 Previsões registradas com erro

Reenviar as previsões com a mesma `reference_date`, `horizon_days` e `model_version` substitui as anteriores. Um lote com qualquer previsão fora do contrato é rejeitado inteiro; a mensagem lista cada problema.

---

## 4. Reprocessamento

### Um período

```bash
python -m jobs.reprocess_period --start 2025-01-01 --end 2025-12-31
```

Recoleta a janela ignorando a checagem de "origem inalterada" e reconstrói o dataset.

- `raw` ganha as linhas da nova coleta, além das antigas.
- `core` é atualizado por upsert: uma linha por data, e uma coluna que a nova coleta não trouxer mantém o valor que tinha.
- O dataset só ganha versão nova se o conteúdo mudar.

Um valor que precisa ser **apagado** de `core` (e não substituído) não sai por reprocessamento, por causa do `COALESCE` do upsert. Isso exige intervenção manual no banco.

### Tudo, forçando

```bash
python -m jobs.run_on_demand --force
```

### Fechar um dia específico

```bash
python -m jobs.run_on_demand --cutoff 2026-09-30
```

Gera uma versão cujo último dia é o informado. A coleta é limitada a esse dia; se ainda assim vier dado posterior a ele, a ingestão é reprovada.

---

## 5. Poda da janela histórica

A poda é lógica: marca as linhas mais antigas de uma versão como `is_pruned`, e o consumidor deixa de recebê-las. Nada é apagado e `raw`/`core` não são tocados.

```bash
python -m src.pruning --days 365                 # descarta o primeiro ano da última versão
python -m src.pruning --start 2019-01-01         # primeiro dia que continua ativo
python -m src.pruning --version <id> --days 90   # outra versão
python -m src.pruning --restore                  # desfaz a poda
```

A poda é recusada, sem alterar nada, quando:

- descartaria mais de `MAX_PRUNE_YEARS` (730 dias), contando o que já foi podado na versão;
- deixaria menos de `DATASET_MIN_DAYS` (1.096) dias ativos;
- `CONFIRM_HISTORICAL_WINDOW` não é `true`.

A poda vale para a versão em que foi aplicada. Uma versão nova nasce com a janela inteira e, se a poda deve continuar, precisa ser aplicada de novo.

> **Janela histórica**: o projeto traz duas referências que não fecham entre si — 9 anos de janela com poda de até 2, e a base legada de ~1.096 dias. Com a base legada, qualquer poda é recusada pelo mínimo de segurança. A equipe precisa decidir qual janela vale antes de podar em produção.

---

## 6. Implantação e manutenção

### Banco novo, com dados reais

```bash
pip install -r requirements.txt   # inclui agrobr e yfinance
python -m src.migrator            # schemas, tabelas, índices, funções, triggers, catálogo
```

No `.env`, defina `AGROBR_MODE=real`. Então:

```bash
python -m jobs.run_on_demand      # primeira carga e primeira versão
```

A primeira carga baixa um arquivo da B3 por pregão: cerca de 4 segundos cada, **mais de 2 horas para os 9 anos**. O log mostra o progresso a cada 20 pregões. Pode interromper: o que já foi baixado está em `COLLECTION_CACHE_DIR` (`.cache/coleta/b3_icf_ajustes.csv`) e a execução seguinte continua dali. As outras fontes levam cerca de um minuto.

Para começar com menos histórico e completar depois:

```bash
python -m jobs.reprocess_period --start 2024-01-01 --end 2026-10-06
```

Depois da primeira carga, defina `COLLECTION_WINDOW_DAYS=45` no `.env`. Sem isso, cada job recoleta os 9 anos e grava tudo de novo em `raw`. Com a janela curta, uma revisão da fonte mais antiga que 45 dias só entra por `jobs.reprocess_period`.

> **Use bancos separados para os modos simulado e real.** A publicação em `core` é por upsert, então uma ingestão simulada grava por cima dos dados reais das mesmas datas, e um banco que já recebeu dados simulados continua com eles onde a coleta real não tiver valor (os últimos dias de clima, por exemplo). Nada no código impede a mistura.

`src.sqlite_migrator` (base legada do SQLite) não é necessário no modo real: a coleta cobre o mesmo período e mais. Se usá-lo, rode uma vez só — ele grava em `raw` a cada execução e duplica as linhas legadas.

### Histórico do CEPEA

Os preços do arábica e do robusta vêm dos CSVs de `base/dados_manuais/`, e o `agrobr` completa só as cotações mais recentes. Quando o log da coleta avisar de um *buraco entre o CSV manual e o agrobr*, os CSVs precisam ser atualizados:

1. Na página do indicador de café do CEPEA, baixe pelo navegador a planilha "Série histórica" do arábica e a do robusta.
2. Importe as duas (o nome dos arquivos não importa; a série é reconhecida pelo título da planilha):

   ```bash
   python -m src.cepea_series --importar caminho/arabica.xls caminho/robusta.xls
   ```

3. Rode `python -m jobs.run_on_demand`.

Acrescente `--check` para só conferir, sem gravar. Um CSV só é substituído se a planilha for claramente a mesma série, mais atual: o título cita o indicador certo, ela não é menor que o arquivo existente, não termina antes dele e os preços das datas em comum coincidem. O arquivo anterior fica ao lado, como `.csv.bak`. Se alguma conferência falhar, nada é gravado e a mensagem diz qual.

Enquanto o buraco existir, os dias sem preço ficam sem alvo e, passados 6 dias, sem `preco_robusta` nas features.

**Por que não é automático.** O código para baixar a planilha existe (`python -m src.cepea_series`, ou `CEPEA_AUTO_DOWNLOAD=true` para a coleta tentar sozinha), mas em 07/10/2026 o site respondeu HTTP 403 ao download feito pelo programa. O `robots.txt` do CEPEA, de 03/09/2026, declara a intenção de barrar acesso automatizado e de reforçar isso no firewall. Não contorne o bloqueio trocando a identificação do programa: se o acesso automático for necessário, o caminho é pedir autorização ao CEPEA. Com a opção ligada e o site recusando, a coleta registra um aviso e segue com o CSV existente.

Os dados do CEPEA são CC BY-NC 4.0 (uso não comercial, com citação).

### Uma fonte está fora do ar

A coleta falha inteira (exceto pelo complemento recente do CEPEA, que cai para o CSV manual com um aviso). O job repete sozinho; se a fonte continuar indisponível, termina `FAILED` e a última versão do dataset segue valendo. Não há modo degradado que publique sem uma das fontes, porque isso entregaria ao modelo uma variável vazia.

O cache da B3 pode ser apagado a qualquer momento (`.cache/coleta/`); o custo é baixar tudo de novo.

### Mudar o catálogo de variáveis

O catálogo (`features.variable_catalog`) decide o que entra em `features.model_features`. Liberar uma variável nova como `KEEP` exige também:

1. que `src/feature_builder.py` a calcule;
2. uma migration criando a coluna em `features.model_features`.

Sem isso, a construção do dataset para com erro explícito em vez de publicar uma coluna vazia.

### Atraso de publicação das fontes

`PUBLICATION_LAG_DAYS`, em `src/feature_builder.py`, diz quantos dias cada série leva para ser publicada. Hoje: 3 dias para o clima e 5 para a radiação (NASA POWER); mercado sem atraso. A linha de `t` usa o clima de `t-3`, que é o que existe quando ela é montada em produção.

Se a latência da fonte mudar, ajuste ali. Atraso configurado menor que o real faz a última linha de cada versão sair com clima mais velho do que o usado no treino; maior que o real só desperdiça informação.

### Testes

```bash
python -m pytest tests/ -v
```

Exigem PostgreSQL acessível. Os dados de teste ficam em 1990, sob o job `teste_ingestao` e versões `teste-*`, e são apagados ao final; as versões de dataset criadas pelos testes nunca são confirmadas.

---

## 7. Limitações conhecidas

- **Modo simulado não serve para treino.** Nele só os preços do CEPEA são reais; câmbio e futuros são derivados deles e o clima é sintético.
- **Nada impede misturar simulado e real no mesmo banco** (ver seção 6).
- **O histórico inteiro é regravado em `raw` a cada job**, a menos que `COLLECTION_WINDOW_DAYS` esteja definido.
- **O job de pós-fechamento pode rodar antes de a B3 publicar os ajustes do dia.** Nesse caso a versão repete, para aquele dia, o ajuste da véspera. O job diário das 00:00 não tem esse problema.
- **1º vencimento da B3 sem regra de rolagem.** Usa-se o contrato mais próximo até ele vencer.
- **Imutabilidade das versões não é imposta pelo banco.** Nada no código reescreve uma versão e `verify_dataset_version` detecta alteração, mas um `UPDATE` manual não é barrado.
- **`raw` só cresce.** Não há rotina de expurgo, por definição do escopo.
- **`audit.pending_events` não tem consumidor** neste repositório.
