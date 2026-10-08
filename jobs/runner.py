"""Infraestrutura comum dos jobs: novas tentativas, linha de comando e corte.

Cada job é um módulo fino que diz qual dia está fechando e em que horário roda;
o resto vive aqui. Quem agenda de verdade é o APScheduler
(``jobs/apscheduler_runner.py``), chamado pelo modo ``--schedule``; este módulo
não guarda mais nenhuma regra de "quando rodar".

Códigos de saída: 0 = SUCCESS, 1 = FAILED, 2 = BLOCKED (outra execução em
andamento — não é erro, o trabalho está sendo feito por ela).
"""

from __future__ import annotations

import argparse
import json
import time as time_module
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Optional, Sequence
from zoneinfo import ZoneInfo

from src.config import parse_hhmm, settings
from src.logging_config import logger
from src.pipeline import STATUS_BLOCKED, STATUS_FAILED, STATUS_SUCCESS, run_pipeline

EXIT_CODES = {STATUS_SUCCESS: 0, STATUS_FAILED: 1, STATUS_BLOCKED: 2}

CutoffFn = Callable[[], Optional[date]]


# ---------------------------------------------------------------------------
# Relógio
# ---------------------------------------------------------------------------

def now_local() -> datetime:
    return datetime.now(ZoneInfo(settings.TIMEZONE))


def today_local() -> date:
    return now_local().date()


def yesterday_local() -> date:
    return today_local() - timedelta(days=1)


def collection_window(cutoff_date: Optional[date]) -> Dict[str, date]:
    """Janela de coleta dos jobs agendados, conforme ``COLLECTION_WINDOW_DAYS``.

    Vazio (coleta a janela histórica inteira) quando a configuração é 0.
    """
    dias = settings.COLLECTION_WINDOW_DAYS
    if dias <= 0 or cutoff_date is None:
        return {}
    return {"start_date": cutoff_date - timedelta(days=dias)}


# ---------------------------------------------------------------------------
# Execução com novas tentativas
# ---------------------------------------------------------------------------

def run_job(
    job_name: str,
    cutoff_date: Optional[date] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    force: bool = False,
    max_retries: Optional[int] = None,
    retry_wait_seconds: Optional[float] = None,
    pipeline: Callable[..., Dict[str, Any]] = run_pipeline,
    sleep: Callable[[float], None] = time_module.sleep,
) -> Dict[str, Any]:
    """Executa o ciclo completo, repetindo-o apenas após erro inesperado.

    Repetir é seguro: cada tentativa é uma execução nova e auditada, origem já
    publicada não é republicada e o que falhou no meio é processado de novo.
    Um ciclo que termina FAILED por qualidade dos dados ou BLOCKED não é
    repetido — tentar de novo na hora daria o mesmo resultado.
    """
    tentativas = 1 + max(0, settings.JOB_MAX_RETRIES if max_retries is None else max_retries)
    espera = settings.JOB_RETRY_WAIT_SECONDS if retry_wait_seconds is None else retry_wait_seconds

    for tentativa in range(1, tentativas + 1):
        try:
            resultado = pipeline(
                job_name, cutoff_date=cutoff_date, start_date=start_date,
                end_date=end_date, force=force)
            resultado["tentativas"] = tentativa
            return resultado
        except Exception as exc:
            if tentativa == tentativas:
                logger.error("Job %s falhou após %d tentativa(s): %s", job_name, tentativa, exc)
                return {"job_name": job_name, "status": STATUS_FAILED,
                        "error": str(exc), "tentativas": tentativa}
            logger.warning("Job %s: tentativa %d de %d falhou (%s); nova tentativa em %ss.",
                           job_name, tentativa, tentativas, exc, espera)
            sleep(espera)

    raise AssertionError("inalcançável")


# ---------------------------------------------------------------------------
# Linha de comando
# ---------------------------------------------------------------------------

def parse_date(valor: str) -> date:
    try:
        return date.fromisoformat(valor)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Data inválida: {valor!r}. Use AAAA-MM-DD.") from exc


def scheduled_job_main(
    job_name: str,
    descricao: str,
    cutoff: CutoffFn,
    horario: str,
    argv: Optional[Sequence[str]] = None,
) -> int:
    """Ponto de entrada dos jobs diários: roda uma vez, ou fica agendado.

    Sem ``--schedule``, executa o ciclo uma única vez e termina (para cron ou
    Agendador de Tarefas). Com ``--schedule``, entrega a tarefa ao APScheduler
    (``jobs.apscheduler_runner``), que a dispara todos os dias no horário.
    """
    parser = argparse.ArgumentParser(prog=f"python -m jobs.{job_name}", description=descricao)
    parser.add_argument(
        "--schedule", action="store_true",
        help=f"fica em execução (APScheduler) e roda todos os dias às {horario} "
             f"({settings.TIMEZONE}); sem a opção, roda uma vez e termina "
             "(para cron ou Agendador de Tarefas)")
    parser.add_argument("--cutoff", type=parse_date, default=None,
                        help="dia a fechar (AAAA-MM-DD); por padrão é calculado pelo job")
    args = parser.parse_args(argv)

    def executar() -> Dict[str, Any]:
        dia = args.cutoff or cutoff()
        resultado = run_job(job_name, cutoff_date=dia, **collection_window(dia))
        print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
        return resultado

    if args.schedule:
        # Import tardio: APScheduler é exigido só por quem agenda; execução
        # única (cron, Agendador de Tarefas) não depende dele.
        from jobs.apscheduler_runner import JobSpec, SchedulerConfig, serve

        spec = JobSpec(job_name, horario, descricao, cutoff)
        if args.cutoff:
            # --cutoff fixa o dia de todas as execuções agendadas.
            spec = replace(spec, cutoff=lambda: args.cutoff)
        return serve(SchedulerConfig.from_settings([spec]))
    return EXIT_CODES.get(executar()["status"], 1)
