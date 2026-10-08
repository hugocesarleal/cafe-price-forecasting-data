# Integração com Django — exemplos de fronteira

O pipeline continua sendo uma **biblioteca externa**: o Django não ganha regra
de negócio, não reimplementa o banco em ORM e não duplica o esquema. A fronteira
é o adaptador `src/integrations/django.py`, que:

- monta a configuração canônica (`src.config.Settings`) com precedência
  **explícita > Django > ambiente/.env**;
- abre a própria conexão psycopg do ciclo (`src.db.pipeline_connection`) e
  mantém o advisory lock nela até o fim — **nunca** use a conexão gerenciada
  pelo ciclo de requests do Django para isso;
- chama a função pública do pipeline (`run_pipeline` / `run_ingestion` /
  `run_dataset_build`) e devolve o dicionário com `run_id`, `status` e métricas;
- preserva logs do pipeline (`cafe_pipeline`) e a auditoria em
  `audit.pipeline_runs`.

## Arquivos deste diretório

| Arquivo | O que é |
| --- | --- |
| `management/commands/rodar_pipeline.py` | Management command fino: roda o ciclo completo e traduz o status em código de saída (0 SUCCESS, 1 FAILED, 2 BLOCKED). |
| `celery_task.py` | Task Celery que executa o pipeline como biblioteca e devolve um resumo serializável. |
| `admin_readonly.py` | Modelo **não gerenciado** (`managed = False`) e ModelAdmin somente leitura para `audit.pipeline_runs`. |

Para usar no seu projeto, copie cada arquivo para o lugar equivalente (o
command precisa dos `__init__.py` de `management/` e `management/commands/`)
e mantenha `src/` do pipeline no `PYTHONPATH`.

## Configuração

As variáveis de ambiente continuam sendo a fonte padrão (veja `.env.example`).
O adaptador aceita duas fontes adicionais por chamada, com precedência:

```python
run_pipeline_job(
    "pipeline_django",
    cutoff_date=date(2026, 10, 7),
    explicit={"PIPELINE_DATABASE_URL": settings.PIPELINE_DATABASE_URL},
)
```

- **Campos injetáveis** (a fronteira os leva fisicamente até o ciclo):
  `PIPELINE_DATABASE_URL`, `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`,
  `DB_PASSWORD`, `JOB_MAX_RETRIES`, `JOB_RETRY_WAIT_SECONDS`. Divergência do
  ambiente é honrada, com aviso registrando qual fonte venceu.
- **Qualquer outro campo** diverge do ambiente → `PipelineConfigError`. O ciclo
  lê esses campos direto do ambiente; silenciar a divergência seria mentir
  sobre o que roda. Ajuste o ambiente ou remova o campo da configuração
  injetada.
- `ADVISORY_LOCK_KEY` não é injetável de propósito: o lock é o mecanismo de
  exclusão mútua entre processos e precisa valer o mesmo para todos.

## Banco e migrações

O esquema é criado **somente** pelas migrações SQL de `migrations/` (aplicadas pelo
próprio pipeline: `python -m src.migrator`). Não crie migrations Django
duplicadas para as mesmas tabelas: como `PipelineRun` é `managed = False`, o
Django nem tenta criar nada — o modelo existe só para o Admin.

O campo "versão do pipeline" do painel usa `metadata->>'version_tag'`: o
esquema de auditoria não tem coluna própria de versão — limitação documentada.

## Agendamento

**Não** agende o pipeline em views, signals ou middlewares, e **não** deixe
triggers de agendamento baixarem dados ou rodarem inferência: o disparo só
chama o ciclo. Para o "quando rodar" já existe o agendador do próprio pipeline
(`jobs/apscheduler_runner.py`, modo `--schedule`); quem preferir Celery Beat
aponta a task de `celery_task.py` no beat e pronto — o corpo do job é o mesmo.
