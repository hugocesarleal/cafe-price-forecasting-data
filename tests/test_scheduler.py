"""Agendador APScheduler: disparo diário, tolerância de atraso e ciclo de vida.

Substitui os testes acoplados ao laço de ``sleep``: aqui inspecionamos a
configuração do agendador e usamos esperas curtas e orientadas a evento (um
disparo atrasado executa já no ``start``). O horário de verão não tem teste de
transição porque o Brasil o extinguiu em 2019 — ver
``test_fuso_dos_jobs_sem_horario_de_verao_desde_2019``.

O disparo agendado roda o mesmo ``run_job`` da execução manual; a mecânica de
novas tentativas e corte continua coberta por ``test_jobs.py``. O teste do
advisory lock usa o PostgreSQL de teste, como o resto da suíte.
"""

import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.triggers.date import DateTrigger

from conftest import JOB_NAME_TESTES
from jobs import apscheduler_runner, update_daily
from jobs.apscheduler_runner import (
    JobSpec,
    SchedulerConfig,
    _ao_perder,
    _ao_terminar,
    build_scheduler,
    create_job_trigger,
    register_jobs,
    run_scheduled_job,
    serve,
    shutdown_scheduler,
    start_scheduler,
)
from jobs.runner import now_local, yesterday_local
from src.config import settings
from src.db import acquire_advisory_lock, get_connection

SP = ZoneInfo(settings.TIMEZONE)


def _config_minima(grace: int = 60, enabled: bool = True, jobs: tuple = ()) -> SchedulerConfig:
    return SchedulerConfig(
        timezone=settings.TIMEZONE, misfire_grace_seconds=grace, enabled=enabled, jobs=jobs)


class Evento:
    """Evento de agendador com os campos que os listeners leem."""

    def __init__(self, **campos):
        self.__dict__.update(campos)


# ---------------------------------------------------------------------------
# Configuração e disparo
# ---------------------------------------------------------------------------

def test_config_do_ambiente_traz_os_tres_jobs_do_dia():
    cfg = SchedulerConfig.from_settings()

    assert [j.name for j in cfg.jobs] == [
        "update_daily", "update_before_open", "update_after_close"]
    assert [j.horario for j in cfg.jobs] == [
        settings.UPDATE_TIME, settings.BEFORE_OPEN_TIME, settings.AFTER_CLOSE_TIME]
    assert cfg.timezone == settings.TIMEZONE
    assert cfg.misfire_grace_seconds == settings.MISFIRE_GRACE_SECONDS
    assert cfg.enabled == settings.SCHEDULER_ENABLED


def test_job_desconhecido_lista_os_agendados():
    cfg = SchedulerConfig.from_settings()

    with pytest.raises(ValueError, match="update_daily"):
        cfg.job("inexistente")


def test_horario_invalido_derruba_antes_de_agendar():
    cfg = _config_minima(jobs=(JobSpec("quebrado", "25:00", "teste", lambda: None),))

    with pytest.raises(ValueError, match="Horário inválido"):
        register_jobs(build_scheduler(cfg), cfg)


def test_trigger_dispara_no_horario_do_job_e_no_fuso_configurado():
    trigger = create_job_trigger(
        JobSpec("update_daily", "19:00", "teste", lambda: None), _config_minima())

    assert trigger.timezone == SP
    assert trigger.get_next_fire_time(None, datetime(2026, 10, 7, 18, 30, tzinfo=SP)) \
        == datetime(2026, 10, 7, 19, 0, tzinfo=SP), "antes do horário roda no mesmo dia"
    assert trigger.get_next_fire_time(None, datetime(2026, 10, 7, 19, 0, 1, tzinfo=SP)) \
        == datetime(2026, 10, 8, 19, 0, tzinfo=SP), "depois do horário roda no dia seguinte"


def test_trigger_de_meia_noite_vira_o_dia_e_o_ano():
    trigger = create_job_trigger(
        JobSpec("update_daily", "00:00", "teste", lambda: None), _config_minima())

    assert trigger.get_next_fire_time(None, datetime(2026, 12, 31, 23, 59, tzinfo=SP)) \
        == datetime(2027, 1, 1, 0, 0, tzinfo=SP)


def test_fuso_dos_jobs_sem_horario_de_verao_desde_2019():
    """O Brasil extinguiu o DST em 2019: nenhum disparo diário pula ou repete.

    Documenta por que não existe teste de transição de horário de verão.
    """
    tz = ZoneInfo(settings.TIMEZONE)

    assert datetime(2026, 7, 15, 12, tzinfo=tz).utcoffset() \
        == datetime(2026, 1, 15, 12, tzinfo=tz).utcoffset() == timedelta(hours=-3)


# ---------------------------------------------------------------------------
# Registro: id estável, corpo do ciclo e padrões de execução
# ---------------------------------------------------------------------------

def test_jobs_registrados_usam_o_mesmo_corpo_da_execucao_manual():
    cfg = SchedulerConfig.from_settings()
    scheduler = build_scheduler(cfg)
    ids = register_jobs(scheduler, cfg)
    scheduler.start()
    try:
        assert ids == [j.name for j in cfg.jobs]
        registro = scheduler.get_job("update_after_close")
        assert registro.func is run_scheduled_job, "o disparo usa o mesmo run_job da execução manual"
        assert registro.args == ("update_after_close", cfg)
    finally:
        scheduler.shutdown()


def test_padroes_de_execucao_valem_para_todo_job():
    cfg = SchedulerConfig.from_settings()
    scheduler = build_scheduler(cfg)
    register_jobs(scheduler, cfg)
    scheduler.start()
    try:
        registro = scheduler.get_job("update_daily")
        assert registro.coalesce is True, "disparos atrasados colapsam num só"
        assert registro.max_instances == 1, "sem sobreposição dentro do processo"
        assert registro.misfire_grace_time == cfg.misfire_grace_seconds
    finally:
        scheduler.shutdown()


def test_reiniciar_o_processo_nao_duplica_jobs():
    cfg = SchedulerConfig.from_settings()
    scheduler = build_scheduler(cfg)
    register_jobs(scheduler, cfg)
    register_jobs(scheduler, cfg)  # segundo "reinício": replace_existing por id estável
    scheduler.start()
    try:
        assert sorted(j.id for j in scheduler.get_jobs()) == sorted(j.name for j in cfg.jobs)
    finally:
        scheduler.shutdown()


def test_scheduler_job_id_exige_um_unico_job():
    spec = JobSpec("update_daily", settings.UPDATE_TIME, "teste", lambda: None)
    unico = SchedulerConfig(
        timezone=settings.TIMEZONE, misfire_grace_seconds=60, enabled=True,
        jobs=(spec,), job_id="pipeline-cafe")

    assert unico.identidade(spec) == "pipeline-cafe"
    assert register_jobs(build_scheduler(unico), unico) == ["pipeline-cafe"]

    with pytest.raises(ValueError, match="SCHEDULER_JOB_ID"):
        SchedulerConfig(
            timezone=settings.TIMEZONE, misfire_grace_seconds=60, enabled=True,
            jobs=(spec, JobSpec("outro", "10:00", "teste", lambda: None)),
            job_id="pipeline-cafe")


# ---------------------------------------------------------------------------
# Disparos atrasados: dentro e além da tolerância
# ---------------------------------------------------------------------------

def test_disparo_atrasado_dentro_da_tolerancia_roda_uma_vez():
    scheduler = build_scheduler(_config_minima(grace=60))
    rodou, executou = threading.Event(), threading.Event()
    scheduler.add_job(rodou.set, trigger=DateTrigger(run_date=now_local() - timedelta(seconds=5)),
                      id="atrasado")
    scheduler.add_listener(lambda e: executou.set(), EVENT_JOB_EXECUTED)
    scheduler.start()
    try:
        assert executou.wait(10), "atraso dentro de MISFIRE_GRACE_SECONDS deveria rodar"
        assert rodou.is_set()
    finally:
        scheduler.shutdown()


def test_disparo_alem_da_tolerancia_vira_misfire_sem_execucao(caplog):
    scheduler = build_scheduler(_config_minima(grace=1))
    rodou, perdeu = threading.Event(), threading.Event()
    scheduler.add_job(rodou.set, trigger=DateTrigger(run_date=now_local() - timedelta(seconds=10)),
                      id="atrasado")
    scheduler.add_listener(lambda e: perdeu.set() if e.job_id == "atrasado" else None,
                           EVENT_JOB_MISSED)
    with caplog.at_level("WARNING"):
        scheduler.start()
        try:
            assert perdeu.wait(10), "atraso além da tolerância vira misfire registrado"
            assert not rodou.is_set(), "nada roda sozinho fora da tolerância"
            assert "MISFIRE_GRACE_SECONDS" in caplog.text, "o listener explica a política"
        finally:
            scheduler.shutdown()


# ---------------------------------------------------------------------------
# Ciclo de vida: falhas, desligamento e encerramento
# ---------------------------------------------------------------------------

def test_falha_de_um_disparo_nao_derruba_o_agendador(caplog):
    scheduler = build_scheduler(_config_minima(grace=60))

    def explodir():
        raise RuntimeError("coleta indisponível")

    seguinte = threading.Event()
    scheduler.add_job(explodir, trigger=DateTrigger(run_date=now_local()), id="explosivo")
    scheduler.add_job(seguinte.set,
                      trigger=DateTrigger(run_date=now_local() + timedelta(seconds=0.3)),
                      id="seguinte")
    with caplog.at_level("ERROR"):
        scheduler.start()
        try:
            assert seguinte.wait(10), "o agendador continua vivo e roda o próximo job"
            assert scheduler.running
            assert "coleta indisponível" in caplog.text
        finally:
            scheduler.shutdown()


def test_agendador_desabilitado_nao_inicia_nem_espera():
    cfg = _config_minima(enabled=False)

    assert start_scheduler(cfg) is None
    assert serve(cfg) == 0
    shutdown_scheduler(None)  # encerrar um agendador que nunca subiu é inofensivo


def test_serve_encerra_por_evento_de_parada():
    cfg = _config_minima(
        jobs=(JobSpec("update_daily", settings.UPDATE_TIME, "teste", lambda: None),))
    parar = threading.Event()
    resultado = []

    thread = threading.Thread(target=lambda: resultado.append(serve(cfg, parar=parar)), daemon=True)
    thread.start()
    parar.set()
    thread.join(timeout=15)

    assert not thread.is_alive(), "o agendador deveria encerrar ao receber o pedido de parada"
    assert resultado == [0]


def test_listeners_registram_status_sem_derrubar_o_agendador(caplog):
    with caplog.at_level("INFO"):
        _ao_terminar(Evento(job_id="update_daily", exception=None,
                            retval={"job_name": "update_daily", "status": "SUCCESS"}))
    assert "SUCCESS" in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING"):
        _ao_terminar(Evento(job_id="update_daily", exception=None,
                            retval={"job_name": "update_daily", "status": "BLOCKED"}))
    assert "pulada" in caplog.text

    caplog.clear()
    with caplog.at_level("ERROR"):
        _ao_terminar(Evento(job_id="update_daily", exception=RuntimeError("banco fora"),
                            retval=None))
    assert "banco fora" in caplog.text


def test_listener_de_misfire_explica_a_politica(caplog):
    with caplog.at_level("WARNING"):
        _ao_perder(Evento(job_id="update_daily", scheduled_run_time="2026-10-07 00:00:00"))

    assert "MISFIRE_GRACE_SECONDS" in caplog.text


# ---------------------------------------------------------------------------
# Barreira entre processos e ponte com a linha de comando
# ---------------------------------------------------------------------------

def test_disparo_agendado_respeita_o_advisory_lock(ingest_conn, caplog):
    cfg = _config_minima(
        jobs=(JobSpec(JOB_NAME_TESTES, "19:00", "teste", lambda: date(2026, 10, 7)),))
    detentor = get_connection()
    try:
        with acquire_advisory_lock(detentor):
            with caplog.at_level("WARNING"):
                resultado = run_scheduled_job(JOB_NAME_TESTES, cfg)
    finally:
        detentor.close()

    assert resultado["status"] == "BLOCKED"
    assert resultado["tentativas"] == 1
    assert "pulada" in caplog.text


def test_modo_schedule_entrega_o_job_ao_agendador(monkeypatch):
    configs = []
    monkeypatch.setattr(apscheduler_runner, "serve", lambda cfg, **kw: configs.append(cfg) or 0)

    assert update_daily.main(["--schedule"]) == 0

    cfg = configs[0]
    assert len(cfg.jobs) == 1
    spec = cfg.jobs[0]
    assert spec.name == "update_daily"
    assert spec.horario == settings.UPDATE_TIME
    assert spec.cutoff() == yesterday_local()


def test_modo_schedule_aceita_cutoff_fixo(monkeypatch):
    configs = []
    monkeypatch.setattr(apscheduler_runner, "serve", lambda cfg, **kw: configs.append(cfg) or 0)

    update_daily.main(["--schedule", "--cutoff", "2026-09-30"])

    assert configs[0].jobs[0].cutoff() == date(2026, 9, 30)
