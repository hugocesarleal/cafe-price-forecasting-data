"""Exemplo de task Celery que executa o pipeline como biblioteca externa.

Copie para uma ``tasks.py`` do seu projeto Django e aponte o worker/beat para
``pipeline.ciclo_completo``. A task é só fronteira: sanitiza as datas para
serialização e devolve um resumo serializável — nenhuma regra de negócio.

``FAILED`` é resultado auditado, não exceção: repetições inesperadas já
acontecem dentro de ``run_job``; deixe o retry do Celery desligado para o
mesmo ciclo não rodar duas vezes com políticas diferentes.
"""

import json
from datetime import date

from celery import shared_task

from src.integrations.django import run_ingestion_job, run_pipeline_job


def _data(valor):
    return date.fromisoformat(valor) if valor else None


def _resumo(resultado):
    return json.loads(json.dumps(resultado, ensure_ascii=False, default=str))


@shared_task(name="pipeline.ciclo_completo")
def ciclo_completo(job="pipeline_django", cutoff=None, force=False, max_retries=None):
    resultado = run_pipeline_job(
        job, cutoff_date=_data(cutoff), force=force, max_retries=max_retries)
    return _resumo(resultado)


@shared_task(name="pipeline.ingestao")
def ingestao(job="pipeline_django_ingestao", cutoff=None, force=False, max_retries=None):
    resultado = run_ingestion_job(
        job, cutoff_date=_data(cutoff), force=force, max_retries=max_retries)
    return _resumo(resultado)
