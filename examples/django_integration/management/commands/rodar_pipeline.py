"""Management command de exemplo: roda o ciclo completo do pipeline.

Copie para ``<app>/management/commands/rodar_pipeline.py`` do seu projeto
Django (junto com os ``__init__.py`` de ``management/`` e
``management/commands/``) e mantenha ``src/`` do pipeline no ``PYTHONPATH``.

Fronteira pura: nenhuma regra de negócio aqui. O command lê as opções, chama
``run_pipeline_job`` (que já cuida de repetição, auditoria e advisory lock na
própria conexão) e traduz o status em código de saída:

    0 = SUCCESS, 1 = FAILED, 2 = BLOCKED (outra execução em andamento).

Uso:
    python manage.py rodar_pipeline
    python manage.py rodar_pipeline --cutoff 2026-10-07 --force
    python manage.py rodar_pipeline --somente-dataset --version-tag v2026-10-07
"""

import json
import sys
from datetime import date

from django.core.management.base import BaseCommand

from jobs.runner import yesterday_local
from src.integrations.django import run_dataset_job, run_pipeline_job
from src.pipeline import STATUS_BLOCKED, STATUS_FAILED, STATUS_SUCCESS

EXIT_CODES = {STATUS_SUCCESS: 0, STATUS_FAILED: 1, STATUS_BLOCKED: 2}


class Command(BaseCommand):
    help = "Executa o ciclo do pipeline (ingestão + dataset) fora do request/response."

    def add_arguments(self, parser):
        parser.add_argument(
            "--job", default="pipeline_django",
            help="nome do job registrado em audit.pipeline_runs")
        parser.add_argument(
            "--cutoff", type=date.fromisoformat, default=None,
            help="dia a fechar (AAAA-MM-DD); padrão: ontem no fuso do pipeline")
        parser.add_argument(
            "--somente-dataset", action="store_true",
            help="pula a coleta e reconstrói só a versão do dataset")
        parser.add_argument(
            "--version-tag", default=None,
            help="rótulo explícito da versão do dataset (só com --somente-dataset)")
        parser.add_argument("--force", action="store_true",
                            help="reprocessa origens mesmo sem mudança de hash")
        parser.add_argument(
            "--max-retries", type=int, default=None,
            help="repetições após erro inesperado (padrão: JOB_MAX_RETRIES do ambiente)")

    def handle(self, *args, **options):
        # "Ontem" no fuso do pipeline (TIMEZONE), não no relógio do servidor: num
        # servidor em UTC os dois divergem entre 21h e meia-noite de Brasília.
        cutoff = options["cutoff"] or yesterday_local()

        if options["somente_dataset"]:
            resultado = run_dataset_job(
                options["job"], cutoff_date=cutoff,
                version_tag=options["version_tag"],
                max_retries=options["max_retries"])
        else:
            resultado = run_pipeline_job(
                options["job"], cutoff_date=cutoff, force=options["force"],
                max_retries=options["max_retries"])

        self.stdout.write(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
        if resultado["status"] != STATUS_SUCCESS:
            self.stderr.write(
                f"Pipeline terminou {resultado['status']}: "
                f"{resultado.get('error') or resultado.get('blocked_reason') or ''}")
        sys.exit(EXIT_CODES.get(resultado["status"], 1))
