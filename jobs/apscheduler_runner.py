"""Agendador diário baseado em APScheduler.

Substitui o laço de ``sleep`` que mantinha o processo em execução: aqui vive
apenas o "quando rodar". O corpo de cada disparo continua sendo o ``run_job``
de ``jobs.runner`` — manual e agendado passam pelo mesmo caminho, com as
mesmas novas tentativas, corte por dia e auditoria.

Decisões de operação (cobertas em ``tests/test_scheduler.py``):

* ``CronTrigger`` diário no fuso ``TIMEZONE``; o Brasil extinguiu o horário de
  verão em 2019, então não há transição de DST a tratar — o fuso IANA segue
  aplicado normalmente.
* ``coalesce=True`` e ``max_instances=1``: máquina dormindo ou processo lento
  nunca acumulam cargas atrasadas — no máximo uma rodada por disparo perdido.
* ``misfire_grace_time`` (``MISFIRE_GRACE_SECONDS``): disparo atrasado dentro
  da tolerância ainda roda uma vez; além dela vira misfire registrado, sem
  execução automática — o próximo horário normal assume.
* ``replace_existing=True`` com id estável (``SCHEDULER_JOB_ID`` ou o nome do
  job): reiniciar o processo não duplica job.
* O advisory lock do PostgreSQL segue como barreira final entre processos:
  quem perde registra BLOCKED e não roda nada.

O pacote ``apscheduler`` só é importado por quem agenda; execução manual
(``python -m jobs.update_daily``) e cron externo não dependem dele.
"""

from __future__ import annotations

import signal
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from jobs import update_after_close, update_before_open, update_daily
from jobs.runner import CutoffFn, collection_window, run_job, today_local, yesterday_local
from src.config import parse_hhmm, settings
from src.logging_config import logger
from src.pipeline import STATUS_BLOCKED


@dataclass(frozen=True)
class JobSpec:
    """Um job diário: nome auditado, horário ``HH:MM`` e o dia que ele fecha."""

    name: str
    horario: str
    descricao: str
    cutoff: CutoffFn


def _jobs_padrao() -> Tuple[JobSpec, ...]:
    """Os três jobs diários, em ordem de horário, lendo a configuração agora."""
    return (
        JobSpec(update_daily.JOB_NAME, settings.UPDATE_TIME,
                "Fecha o dia anterior na virada.", yesterday_local),
        JobSpec(update_before_open.JOB_NAME, settings.BEFORE_OPEN_TIME,
                "Revisa o dia anterior antes da abertura do mercado.", yesterday_local),
        JobSpec(update_after_close.JOB_NAME, settings.AFTER_CLOSE_TIME,
                "Fecha o dia corrente após o fechamento do mercado.", today_local),
    )


@dataclass(frozen=True)
class SchedulerConfig:
    """Fuso, tolerância de atraso, liga/desliga e os jobs a agendar."""

    timezone: str
    misfire_grace_seconds: int
    enabled: bool
    jobs: Tuple[JobSpec, ...]
    job_id: Optional[str] = None

    def __post_init__(self):
        if self.job_id and len(self.jobs) != 1:
            raise ValueError(
                "SCHEDULER_JOB_ID dá um id estável a um único job; com vários jobs o id "
                "estável é o próprio nome. Agende um job por processo ou remova "
                "SCHEDULER_JOB_ID do ambiente.")

    @classmethod
    def from_settings(cls, jobs: Optional[Sequence[JobSpec]] = None) -> "SchedulerConfig":
        """Configuração vinda do ambiente/``.env`` (``Settings`` valida cedo)."""
        return cls(
            timezone=settings.TIMEZONE,
            misfire_grace_seconds=settings.MISFIRE_GRACE_SECONDS,
            enabled=settings.SCHEDULER_ENABLED,
            jobs=tuple(jobs) if jobs is not None else _jobs_padrao(),
            job_id=settings.SCHEDULER_JOB_ID,
        )

    def job(self, name: str) -> JobSpec:
        for spec in self.jobs:
            if spec.name == name:
                return spec
        raise ValueError(
            f"Job desconhecido: {name!r}. Agendados: "
            f"{', '.join(j.name for j in self.jobs)}.")

    def identidade(self, job: JobSpec) -> str:
        """Id estável do job no agendador: reiniciar não duplica."""
        return self.job_id or job.name


# ---------------------------------------------------------------------------
# Construção do agendador
# ---------------------------------------------------------------------------

def create_job_trigger(job: JobSpec, config: SchedulerConfig) -> CronTrigger:
    """Disparo diário de ``job``, no horário e fuso de ``config``.

    Horário inválido levanta ``ValueError`` aqui — antes de o agendador ligar.
    """
    horario = parse_hhmm(job.horario)
    return CronTrigger(hour=horario.hour, minute=horario.minute, timezone=config.timezone)


def build_scheduler(config: SchedulerConfig) -> BackgroundScheduler:
    """Agendador parado, com fuso, padrões de execução e listeners prontos.

    Os padrões valem para todo job registrado: ``coalesce=True`` colapsa
    disparos atrasados num só, ``max_instances=1`` impede sobreposição dentro
    do processo e ``misfire_grace_time`` decide se um atraso ainda roda.
    """
    scheduler = BackgroundScheduler(
        timezone=config.timezone,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": config.misfire_grace_seconds,
        },
    )
    scheduler.add_listener(_ao_terminar, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    scheduler.add_listener(_ao_perder, EVENT_JOB_MISSED)
    return scheduler


def register_jobs(scheduler: BackgroundScheduler, config: SchedulerConfig) -> Sequence[str]:
    """Registra os jobs de ``config`` e devolve os ids.

    Reexecutar não duplica: o id é estável e ``replace_existing`` substitui a
    definição anterior em vez de acumular. O corpo registrado é
    ``run_scheduled_job`` — o mesmo ciclo da execução manual.
    """
    return [
        scheduler.add_job(
            run_scheduled_job,
            trigger=create_job_trigger(job, config),
            args=(job.name, config),
            id=config.identidade(job),
            name=job.descricao,
            replace_existing=True,
        ).id
        for job in config.jobs
    ]


# ---------------------------------------------------------------------------
# Execução do disparo
# ---------------------------------------------------------------------------

def run_scheduled_job(job_name: str, config: SchedulerConfig) -> Dict[str, Any]:
    """Um ciclo do job agendado, com o corte resolvido na hora do disparo.

    É o que o agendador chama: mesmas novas tentativas, mesma janela de coleta
    e mesma auditoria da execução manual (``run_job``). Um ciclo FAILED ou
    BLOCKED não levanta — fica registrado e o agendador segue vivo.
    """
    job = config.job(job_name)
    dia = job.cutoff()
    logger.info("Disparo agendado: %s fechando %s.", job_name, dia)
    resultado = run_job(job_name, cutoff_date=dia, **collection_window(dia))
    if resultado["status"] == STATUS_BLOCKED:
        logger.warning(
            "Execução agendada de %s pulada: o advisory lock está com outra execução.",
            job_name)
    return resultado


# ---------------------------------------------------------------------------
# Ciclo de vida
# ---------------------------------------------------------------------------

def start_scheduler(config: SchedulerConfig) -> Optional[BackgroundScheduler]:
    """Constrói, registra e inicia o agendador; ``None`` se desabilitado.

    ``SCHEDULER_ENABLED=false`` é desligamento deliberado: registra aviso e
    encerra sem erro. Configuração inválida (horário ou fuso) derruba aqui,
    antes do primeiro disparo.
    """
    if not config.enabled:
        logger.warning("Agendador desabilitado (SCHEDULER_ENABLED=false): nada foi iniciado.")
        return None

    scheduler = build_scheduler(config)
    register_jobs(scheduler, config)
    scheduler.start()
    logger.info(
        "Agendador iniciado (%s), fuso %s, tolerância de atraso %ds.",
        "; ".join(f"{j.name} às {j.horario}" for j in config.jobs),
        config.timezone, config.misfire_grace_seconds)
    return scheduler


def shutdown_scheduler(scheduler: Optional[BackgroundScheduler], aguardar: bool = True) -> None:
    """Encerra o agendador; com ``aguardar``, deixa as execuções em curso terminarem."""
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=aguardar)
    except SchedulerNotRunningError:
        pass
    logger.info("Agendador encerrado.")


def _pedido_de_encerramento(parar: threading.Event) -> Callable[[int, Any], None]:
    def handler(signum, frame):
        logger.info("Sinal %s recebido: encerrando o agendador.", signum)
        parar.set()

    return handler


def _instalar_sinais(parar: threading.Event) -> Dict[int, Any]:
    """SIGTERM/SIGINT pedem encerramento; fora da thread principal, é no-op.

    ``signal.signal`` só funciona na thread principal; embutido em worker ou
    servidor, o encerramento fica com o dono do processo.
    """
    anteriores: Dict[int, Any] = {}
    handler = _pedido_de_encerramento(parar)
    for sinal in (signal.SIGTERM, signal.SIGINT):
        try:
            anteriores[sinal] = signal.signal(sinal, handler)
        except ValueError:
            logger.info(
                "Sinais indisponíveis fora da thread principal; encerre o agendador "
                "pelo dono do processo.")
            break
    return anteriores


def _restaurar_sinais(anteriores: Dict[int, Any]) -> None:
    for sinal, anterior in anteriores.items():
        signal.signal(sinal, anterior)


def serve(config: SchedulerConfig, parar: Optional[threading.Event] = None) -> int:
    """Mantém o agendador em execução até SIGTERM/SIGINT (ou ``parar`` acionado).

    É o que o modo ``--schedule`` dos jobs chama: encerrar espera as execuções
    em curso e devolve 0.
    """
    scheduler = start_scheduler(config)
    if scheduler is None:
        return 0

    parar = parar or threading.Event()
    anteriores = _instalar_sinais(parar)
    try:
        parar.wait()
    finally:
        _restaurar_sinais(anteriores)
        shutdown_scheduler(scheduler)
    return 0


# ---------------------------------------------------------------------------
# Listeners: observabilidade sem derrubar o agendador
# ---------------------------------------------------------------------------

def _ao_terminar(evento) -> None:
    """Sucesso/erro de uma execução agendada, sempre com o job auditado."""
    if evento.exception is not None:
        logger.error("Execução agendada de %s falhou: %s", evento.job_id, evento.exception)
        return
    resultado = evento.retval if isinstance(evento.retval, dict) else {}
    nome = resultado.get("job_name", evento.job_id)
    if resultado.get("status") == STATUS_BLOCKED:
        logger.warning(
            "Execução agendada de %s pulada: outra execução segura o advisory lock.", nome)
    else:
        logger.info("Execução agendada de %s terminou %s.",
                    nome, resultado.get("status", "?"))


def _ao_perder(evento) -> None:
    logger.warning(
        "Disparo de %s perdido (previsto para %s): atraso além de "
        "MISFIRE_GRACE_SECONDS. Nada roda sozinho; o próximo horário normal assume.",
        evento.job_id, getattr(evento, "scheduled_run_time", "?"))
