"""Infraestrutura comum dos jobs: novas tentativas, agendamento e linha de comando.

Cada job é um módulo fino que diz qual dia está fechando e em que horário roda;
o resto vive aqui.

Códigos de saída: 0 = SUCCESS, 1 = FAILED, 2 = BLOCKED (outra execução em
andamento — não é erro, o trabalho está sendo feito por ela).
"""

from __future__ import annotations

import argparse
import json
import time as time_module
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, Optional, Sequence
from zoneinfo import ZoneInfo

from src.config import settings
from src.logging_config import logger
from src.pipeline import STATUS_BLOCKED, STATUS_FAILED, STATUS_SUCCESS, run_pipeline

EXIT_CODES = {STATUS_SUCCESS: 0, STATUS_FAILED: 1, STATUS_BLOCKED: 2}

# Maior intervalo dormido de uma vez no modo agendado; mantém o processo
# responsivo a mudanças de relógio.
MAX_SLEEP_SECONDS = 60.0

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


def parse_hhmm(valor: str) -> time:
    try:
        horas, minutos = valor.strip().split(":")
        return time(int(horas), int(minutos))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Horário inválido: {valor!r}. Use HH:MM.") from exc


def next_run_at(agora: datetime, horario: str) -> datetime:
    """Próxima ocorrência de ``horario`` (HH:MM) no fuso de ``agora``."""
    alvo = datetime.combine(agora.date(), parse_hhmm(horario), tzinfo=agora.tzinfo)
    if alvo <= agora:
        alvo = datetime.combine(
            agora.date() + timedelta(days=1), parse_hhmm(horario), tzinfo=agora.tzinfo)
    return alvo


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
# Agendamento
# ---------------------------------------------------------------------------

def run_scheduled(
    executar: Callable[[], Dict[str, Any]],
    horario: str,
    now: Callable[[], datetime] = now_local,
    sleep: Callable[[float], None] = time_module.sleep,
    max_runs: Optional[int] = None,
) -> int:
    """Chama ``executar`` todos os dias em ``horario``; devolve quantas vezes rodou.

    Uma execução com erro não derruba o agendador: fica registrada e a próxima
    acontece no dia seguinte. ``max_runs`` existe para testes.
    """
    execucoes = 0
    while max_runs is None or execucoes < max_runs:
        proxima = next_run_at(now(), horario)
        logger.info("Próxima execução agendada para %s.", proxima.isoformat())
        while (falta := (proxima - now()).total_seconds()) > 0:
            sleep(min(falta, MAX_SLEEP_SECONDS))
        try:
            executar()
        except Exception as exc:  # o agendador precisa sobreviver a qualquer job
            logger.error("Execução agendada falhou: %s", exc)
        execucoes += 1
    return execucoes


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
    """Ponto de entrada dos jobs diários: roda uma vez ou fica agendado."""
    parser = argparse.ArgumentParser(prog=f"python -m jobs.{job_name}", description=descricao)
    parser.add_argument(
        "--schedule", action="store_true",
        help=f"fica em execução e roda todos os dias às {horario} ({settings.TIMEZONE}); "
             "sem a opção, roda uma vez e termina (para cron ou Agendador de Tarefas)")
    parser.add_argument("--cutoff", type=parse_date, default=None,
                        help="dia a fechar (AAAA-MM-DD); por padrão é calculado pelo job")
    args = parser.parse_args(argv)

    def executar() -> Dict[str, Any]:
        # O corte é resolvido a cada execução: no modo agendado o dia muda.
        resultado = run_job(job_name, cutoff_date=args.cutoff or cutoff())
        print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
        return resultado

    if args.schedule:
        run_scheduled(executar, horario)
        return 0
    return EXIT_CODES.get(executar()["status"], 1)
