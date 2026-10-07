"""Jobs: horários, dia de corte, novas tentativas, agendamento e linha de comando.

Tudo roda em memória: o ciclo de dados é substituído por um dublê, então estes
testes cobrem só o que é responsabilidade dos jobs.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jobs import reprocess_period, run_on_demand, runner, update_after_close
from jobs import update_before_open, update_daily
from src import pipeline
from src.config import settings

SP = ZoneInfo("America/Sao_Paulo")


class PipelineDuble:
    """Substitui ``run_pipeline``: falha N vezes e depois devolve um status."""

    def __init__(self, falhas=0, status="SUCCESS"):
        self.falhas = falhas
        self.status = status
        self.chamadas = []

    def __call__(self, job_name, **kwargs):
        self.chamadas.append((job_name, kwargs))
        if len(self.chamadas) <= self.falhas:
            raise ConnectionError(f"banco indisponível ({len(self.chamadas)})")
        return {"job_name": job_name, "status": self.status}


class Relogio:
    """Relógio falso: ``sleep`` avança o tempo em vez de esperar."""

    def __init__(self, inicio):
        self.agora = inicio
        self.dormidas = []

    def now(self):
        return self.agora

    def sleep(self, segundos):
        self.dormidas.append(segundos)
        self.agora += timedelta(seconds=segundos)


# ---------------------------------------------------------------------------
# Horários e dia de corte
# ---------------------------------------------------------------------------

def test_proxima_execucao_e_hoje_se_o_horario_ainda_nao_passou():
    agora = datetime(2026, 10, 7, 18, 30, tzinfo=SP)

    assert runner.next_run_at(agora, "19:00") == datetime(2026, 10, 7, 19, 0, tzinfo=SP)


def test_proxima_execucao_e_amanha_se_o_horario_ja_passou():
    agora = datetime(2026, 10, 7, 19, 0, tzinfo=SP)  # exatamente na hora: já rodou

    assert runner.next_run_at(agora, "19:00") == datetime(2026, 10, 8, 19, 0, tzinfo=SP)


def test_meia_noite_cai_na_virada_do_dia_no_fuso_configurado():
    agora = datetime(2026, 12, 31, 23, 59, tzinfo=SP)

    assert runner.next_run_at(agora, "00:00") == datetime(2027, 1, 1, 0, 0, tzinfo=SP)


@pytest.mark.parametrize("invalido", ["", "25:00", "12h30", "12", None])
def test_horario_invalido_e_rejeitado(invalido):
    with pytest.raises(ValueError, match="Horário inválido"):
        runner.parse_hhmm(invalido)


def test_dia_de_corte_segue_o_fuso_configurado_e_nao_o_utc(monkeypatch):
    # 01:30 UTC do dia 8 ainda é dia 7 em São Paulo.
    instante = datetime(2026, 10, 8, 1, 30, tzinfo=ZoneInfo("UTC"))
    monkeypatch.setattr(runner, "now_local", lambda: instante.astimezone(SP))

    assert runner.today_local() == date(2026, 10, 7)
    assert runner.yesterday_local() == date(2026, 10, 6)


def test_horarios_padrao_dos_jobs():
    assert settings.TIMEZONE == "America/Sao_Paulo"
    assert settings.UPDATE_TIME == "00:00"
    assert runner.parse_hhmm(settings.AFTER_CLOSE_TIME) > runner.parse_hhmm(settings.BEFORE_OPEN_TIME)


# ---------------------------------------------------------------------------
# Novas tentativas
# ---------------------------------------------------------------------------

def test_job_bem_sucedido_roda_uma_vez_e_repassa_os_parametros():
    duble = PipelineDuble()

    resultado = runner.run_job("update_daily", cutoff_date=date(2026, 10, 6),
                               pipeline=duble, sleep=lambda s: None)

    assert resultado == {"job_name": "update_daily", "status": "SUCCESS", "tentativas": 1}
    assert duble.chamadas == [("update_daily", {
        "cutoff_date": date(2026, 10, 6), "start_date": None, "end_date": None, "force": False})]


def test_erro_inesperado_e_repetido_ate_dar_certo():
    duble, esperas = PipelineDuble(falhas=2), []

    resultado = runner.run_job("update_daily", max_retries=2, retry_wait_seconds=30,
                               pipeline=duble, sleep=esperas.append)

    assert resultado["status"] == "SUCCESS"
    assert resultado["tentativas"] == 3
    assert esperas == [30, 30]


def test_erro_persistente_esgota_as_tentativas_e_termina_failed():
    duble, esperas = PipelineDuble(falhas=99), []

    resultado = runner.run_job("update_daily", max_retries=2, retry_wait_seconds=5,
                               pipeline=duble, sleep=esperas.append)

    assert resultado["status"] == "FAILED"
    assert resultado["tentativas"] == 3 == len(duble.chamadas)
    assert "banco indisponível" in resultado["error"]
    assert esperas == [5, 5], "Não espera depois da última tentativa"


@pytest.mark.parametrize("status", ["FAILED", "BLOCKED"])
def test_ciclo_reprovado_ou_bloqueado_nao_e_repetido(status):
    duble = PipelineDuble(status=status)

    resultado = runner.run_job("update_daily", max_retries=5, pipeline=duble,
                               sleep=lambda s: None)

    assert resultado["status"] == status
    assert len(duble.chamadas) == 1


def test_sem_novas_tentativas_configuradas_falha_na_primeira():
    duble = PipelineDuble(falhas=1)

    resultado = runner.run_job("update_daily", max_retries=0, pipeline=duble,
                               sleep=lambda s: None)

    assert resultado["status"] == "FAILED" and resultado["tentativas"] == 1


# ---------------------------------------------------------------------------
# Agendamento
# ---------------------------------------------------------------------------

def test_agendador_espera_ate_o_horario_e_roda_uma_vez_por_dia():
    relogio = Relogio(datetime(2026, 10, 7, 23, 58, 30, tzinfo=SP))
    disparos = []

    execucoes = runner.run_scheduled(lambda: disparos.append(relogio.agora), "00:00",
                                     now=relogio.now, sleep=relogio.sleep, max_runs=3)

    assert execucoes == 3
    assert disparos == [datetime(2026, 10, d, 0, 0, tzinfo=SP) for d in (8, 9, 10)]
    assert max(relogio.dormidas) <= runner.MAX_SLEEP_SECONDS


def test_agendador_sobrevive_a_uma_execucao_com_erro():
    relogio = Relogio(datetime(2026, 10, 7, 12, 0, tzinfo=SP))
    disparos = []

    def executar():
        disparos.append(relogio.agora.date())
        if len(disparos) == 1:
            raise RuntimeError("falha do primeiro dia")

    assert runner.run_scheduled(executar, "19:00", now=relogio.now,
                                sleep=relogio.sleep, max_runs=2) == 2
    assert disparos == [date(2026, 10, 7), date(2026, 10, 8)]


# ---------------------------------------------------------------------------
# Linha de comando
# ---------------------------------------------------------------------------

@pytest.fixture
def run_job_falso(monkeypatch):
    """Captura as chamadas a ``run_job`` feitas pelos pontos de entrada."""
    chamadas = []
    retorno = {"status": "SUCCESS"}

    def falso(job_name, **kwargs):
        chamadas.append((job_name, kwargs))
        return {"job_name": job_name, **retorno}

    for modulo in (runner, run_on_demand, reprocess_period):
        monkeypatch.setattr(modulo, "run_job", falso)
    monkeypatch.setattr(runner, "now_local", lambda: datetime(2026, 10, 7, 0, 0, 5, tzinfo=SP))
    return chamadas, retorno


@pytest.mark.parametrize("modulo, corte", [
    (update_daily, date(2026, 10, 6)),         # fecha o dia que acabou
    (update_before_open, date(2026, 10, 6)),   # revisa o dia anterior
    (update_after_close, date(2026, 10, 7)),   # fecha o próprio dia
])
def test_cada_job_fecha_o_dia_certo(run_job_falso, capsys, modulo, corte):
    chamadas, _ = run_job_falso

    assert modulo.main([]) == 0
    assert chamadas == [(modulo.JOB_NAME, {"cutoff_date": corte})]
    assert '"status": "SUCCESS"' in capsys.readouterr().out


def test_corte_pode_ser_informado_na_linha_de_comando(run_job_falso):
    chamadas, _ = run_job_falso

    update_daily.main(["--cutoff", "2026-09-30"])

    assert chamadas[0][1] == {"cutoff_date": date(2026, 9, 30)}


@pytest.mark.parametrize("status, codigo", [("SUCCESS", 0), ("FAILED", 1), ("BLOCKED", 2)])
def test_codigo_de_saida_reflete_o_status(run_job_falso, status, codigo):
    _, retorno = run_job_falso
    retorno["status"] = status

    assert update_daily.main([]) == codigo
    assert run_on_demand.main([]) == codigo


def test_execucao_sob_demanda_nao_fixa_corte_e_aceita_force(run_job_falso):
    chamadas, _ = run_job_falso

    run_on_demand.main([])
    run_on_demand.main(["--cutoff", "2026-09-30", "--force"])

    assert chamadas == [
        ("run_on_demand", {"cutoff_date": None, "force": False}),
        ("run_on_demand", {"cutoff_date": date(2026, 9, 30), "force": True}),
    ]


def test_reprocessamento_forca_a_janela_pedida(run_job_falso):
    chamadas, _ = run_job_falso

    assert reprocess_period.main(["--start", "2025-01-01", "--end", "2025-12-31"]) == 0
    assert chamadas == [("reprocess_period", {
        "start_date": date(2025, 1, 1), "end_date": date(2025, 12, 31), "force": True})]


@pytest.mark.parametrize("argumentos", [
    [],                                              # janela é obrigatória
    ["--start", "2025-01-01"],
    ["--start", "2025-12-31", "--end", "2025-01-01"],  # invertida
    ["--start", "31/12/2025", "--end", "2026-01-01"],  # formato errado
])
def test_reprocessamento_rejeita_janela_invalida(run_job_falso, argumentos):
    chamadas, _ = run_job_falso

    with pytest.raises(SystemExit) as saida:
        reprocess_period.main(argumentos)

    assert saida.value.code == 2
    assert chamadas == []


# ---------------------------------------------------------------------------
# Cliente de coleta
# ---------------------------------------------------------------------------

def test_janela_de_coleta_no_modo_real_e_erro_e_nao_coleta_completa(monkeypatch):
    monkeypatch.setattr(settings, "AGROBR_MODE", "real")

    with pytest.raises(ValueError, match="AGROBR_MODE=simulated"):
        pipeline.build_client(start_date=date(2025, 1, 1), end_date=date(2025, 12, 31))


def test_modo_simulado_recebe_a_janela_pedida(monkeypatch):
    monkeypatch.setattr(settings, "AGROBR_MODE", "simulated")

    cliente = pipeline.build_client(date(2025, 1, 1), date(2025, 12, 31))

    assert (cliente.start_date, cliente.end_date) == (date(2025, 1, 1), date(2025, 12, 31))
