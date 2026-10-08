"""Adaptador Django: caixa preta da fronteira (ciclos exigem PostgreSQL).

Os testes puros cobrem precedência, validação e avisos de divergência de
configuração sem tocar o banco. Os ciclos rodam de verdade — ingestão com
stub, dataset com dublê — para provar que a fronteira preserva conexão
própria, auditoria, repetição e formato de retorno iguais aos dos jobs.
"""

import contextlib

import psycopg
import pytest

from conftest import JOB_NAME_TESTES, StubAgrobrClient, contar_do_teste, datas
from src import pipeline
from src.db import acquire_advisory_lock, get_connection
from src.integrations import django as ad

VERSAO_FALSA = {
    "dataset_version_id": "00000000-0000-0000-0000-000000000001",
    "version_tag": "teste-fronteira",
    "row_count": 42,
    "reused": False,
}


def rodar_ciclo(**kwargs):
    """Ciclo completo pela fronteira com a auditoria escopada aos testes.

    Sem os nomes de job de teste, as etapas internas seriam auditadas com os
    nomes de produção e escapariam da limpeza do ``ingest_conn``.
    """
    return ad.run_pipeline_job(
        JOB_NAME_TESTES, ingestion_job=JOB_NAME_TESTES, dataset_job=JOB_NAME_TESTES,
        **kwargs)


# ---------------------------------------------------------------------------
# Configuração: precedência, validação e divergência explícita
# ---------------------------------------------------------------------------

def test_precedencia_explicita_vence_django_e_ambiente():
    cfg = ad.resolve_config(
        explicit={"JOB_MAX_RETRIES": 7},
        django_config={"JOB_MAX_RETRIES": 9, "JOB_RETRY_WAIT_SECONDS": 5},
    )

    assert cfg.settings.JOB_MAX_RETRIES == 7
    assert cfg.sources["JOB_MAX_RETRIES"] == "explicit"
    assert cfg.settings.JOB_RETRY_WAIT_SECONDS == 5
    assert cfg.sources["JOB_RETRY_WAIT_SECONDS"] == "django"


def test_origem_de_cada_campo_registra_ambiente_e_padrao():
    cfg = ad.resolve_config()

    assert cfg.sources["DB_PORT"] == "env", "campos do .env têm origem registrada"
    assert cfg.sources["SCHEDULER_JOB_ID"] == "default"
    assert cfg.sources["MISFIRE_GRACE_SECONDS"] == "default"


def test_campo_injetado_divergente_avisa_qual_fonte_venceu(caplog):
    with caplog.at_level("WARNING"):
        cfg = ad.resolve_config(explicit={"JOB_MAX_RETRIES": 5})

    assert cfg.settings.JOB_MAX_RETRIES == 5, "a fonte injetada é honrada"
    assert "JOB_MAX_RETRIES" in caplog.text
    assert "explicit" in caplog.text


def test_repeticao_injetada_chega_ao_ciclo():
    """O valor de retry que a fronteira diz honrar é o que o ciclo de fato usa."""
    injetada = ad.resolve_config(
        explicit={"JOB_MAX_RETRIES": 5}, django_config={"JOB_RETRY_WAIT_SECONDS": 7})
    ambiente = ad.resolve_config()

    assert ad.retry_options(injetada) == {"max_retries": 5, "retry_wait_seconds": 7}
    assert ad.retry_options(injetada, max_retries=1, retry_wait_seconds=0) == {
        "max_retries": 1, "retry_wait_seconds": 0}, "o argumento da chamada vence a injeção"
    assert ad.retry_options(ambiente) == {"max_retries": None, "retry_wait_seconds": None}, \
        "sem injeção, run_job lê o ambiente como nos jobs agendados"


def test_ciclo_repete_o_numero_de_vezes_injetado(monkeypatch):
    @contextlib.contextmanager
    def conexao_falsa(**kwargs):
        yield object()

    tentativas, esperas = [], []

    def sempre_falha(job, **kwargs):
        tentativas.append(job)
        raise ConnectionError("banco indisponível")

    monkeypatch.setattr(ad, "pipeline_connection", conexao_falsa)
    monkeypatch.setattr(ad, "run_pipeline", sempre_falha)

    resultado = ad.run_pipeline_job(
        "teste", explicit={"JOB_MAX_RETRIES": 4, "JOB_RETRY_WAIT_SECONDS": 3},
        sleep=esperas.append)

    assert resultado["status"] == "FAILED"
    assert len(tentativas) == resultado["tentativas"] == 5, "1 tentativa + 4 repetições injetadas"
    assert esperas == [3, 3, 3, 3]


def test_campo_nao_injetavel_divergente_falha_explicitamente():
    atual = ad.Settings().LOG_LEVEL
    novo = "DEBUG" if atual != "DEBUG" else "WARNING"

    with pytest.raises(ad.PipelineConfigError, match="LOG_LEVEL"):
        ad.resolve_config(explicit={"LOG_LEVEL": novo})
    with pytest.raises(ad.PipelineConfigError, match="não é injetável"):
        ad.resolve_config(django_config={"UPDATE_TIME": "23:59"})


def test_campo_desconhecido_e_erro_de_configuracao():
    with pytest.raises(ad.PipelineConfigError, match="JVOB_MAX_RETRIES"):
        ad.resolve_config(explicit={"JVOB_MAX_RETRIES": 3})


def test_valor_invalido_vira_erro_de_configuracao():
    with pytest.raises(ad.PipelineConfigError, match="UPDATE_TIME"):
        ad.resolve_config(explicit={"UPDATE_TIME": "99:99"})
    with pytest.raises(ad.PipelineConfigError, match="TIMEZONE"):
        ad.resolve_config(django_config={"TIMEZONE": "Marte/Base"})


def test_senha_nunca_aparece_em_log_ou_erro(caplog, monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "senha-do-ambiente")

    with caplog.at_level("WARNING"):
        cfg = ad.resolve_config(explicit={"DB_PASSWORD": "senha-injetada"})

    assert cfg.settings.DB_PASSWORD == "senha-injetada"
    assert "DB_PASSWORD" in caplog.text
    assert "senha-do-ambiente" not in caplog.text
    assert "senha-injetada" not in caplog.text


def test_url_sem_partes_injetadas_vira_dsn(monkeypatch):
    url = "postgresql://usuario:segredo@127.0.0.1:5432/cafe"
    monkeypatch.setenv("PIPELINE_DATABASE_URL", url)

    cfg = ad.resolve_config()

    assert ad.connection_options(cfg) == {"dsn": url}


def test_parte_de_conexao_injetada_tem_precedencia_sobre_a_url(monkeypatch):
    monkeypatch.setenv(
        "PIPELINE_DATABASE_URL", "postgresql://usuario:segredo@127.0.0.1:5432/cafe")

    cfg = ad.resolve_config(explicit={"DB_PASSWORD": "outra-senha"})
    opcoes = ad.connection_options(cfg)

    assert "dsn" not in opcoes
    assert opcoes["password"] == "outra-senha"
    assert opcoes["host"] == cfg.settings.DB_HOST


# ---------------------------------------------------------------------------
# Ciclos pela fronteira (ingestão real com stub, dataset com dublê)
# ---------------------------------------------------------------------------

@pytest.fixture
def dataset_falso_pipeline(monkeypatch):
    """Dublê de ``src.pipeline.run_dataset_build`` (o ciclo completo passa lá)."""
    chamadas = []

    def falso(conn, **kwargs):
        chamadas.append(kwargs)
        return {"run_id": None, "status": "SUCCESS", "versao": dict(VERSAO_FALSA)}

    monkeypatch.setattr(pipeline, "run_dataset_build", falso)
    return chamadas


@pytest.fixture
def conexoes_do_ciclo(monkeypatch):
    """Registra as conexões abertas pela fronteira, na ordem."""
    abertas = []
    real = ad.pipeline_connection

    @contextlib.contextmanager
    def espiao(**kwargs):
        with real(**kwargs) as conn:
            abertas.append(conn)
            yield conn

    monkeypatch.setattr(ad, "pipeline_connection", espiao)
    return abertas


def test_run_pipeline_job_executa_o_ciclo_com_conexao_propria(
    ingest_conn, monkeypatch, dataset_falso_pipeline, conexoes_do_ciclo
):
    monkeypatch.setattr(pipeline, "build_client", lambda *a, **k: StubAgrobrClient())

    resultado = rodar_ciclo()

    assert resultado["status"] == "SUCCESS"
    assert resultado["tentativas"] == 1
    assert resultado["run_id"]
    assert len(conexoes_do_ciclo) == 1, "o ciclo usa uma única conexão, aberta pela fronteira"
    assert conexoes_do_ciclo[0].closed, "quem abre a conexão é quem fecha"
    assert contar_do_teste(ingest_conn, "raw.market_observations") == 5

    with ingest_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM audit.pipeline_runs WHERE run_id = %s",
            (resultado["run_id"],),
        )
        assert cur.fetchone()["status"] == "SUCCESS", "a auditoria do ciclo é preservada"


def test_run_pipeline_job_repete_apos_erro_e_fecha_cada_conexao(
    ingest_conn, monkeypatch, dataset_falso_pipeline, conexoes_do_ciclo
):
    chamadas = {"n": 0}

    def coleta_instavel(*a, **k):
        chamadas["n"] += 1
        if chamadas["n"] == 1:
            raise RuntimeError("coleta indisponível")
        return StubAgrobrClient()

    monkeypatch.setattr(pipeline, "build_client", coleta_instavel)

    resultado = rodar_ciclo(
        max_retries=1, retry_wait_seconds=0, sleep=lambda s: None)

    assert resultado["status"] == "SUCCESS"
    assert resultado["tentativas"] == 2
    assert len(conexoes_do_ciclo) == 2, "cada tentativa tem a própria conexão"
    assert all(c.closed for c in conexoes_do_ciclo)


def test_falha_persistente_vira_failed_sem_levantar(
    ingest_conn, monkeypatch, conexoes_do_ciclo
):
    def falhar_sempre(*a, **k):
        raise RuntimeError("agrobr fora do ar")

    monkeypatch.setattr(pipeline, "build_client", falhar_sempre)

    resultado = rodar_ciclo(
        max_retries=1, retry_wait_seconds=0, sleep=lambda s: None)

    assert resultado["status"] == "FAILED"
    assert resultado["tentativas"] == 2
    assert "agrobr fora do ar" in resultado["error"]
    assert all(c.closed for c in conexoes_do_ciclo)


def test_ciclo_bloqueado_quando_outro_job_esta_rodando(
    ingest_conn, monkeypatch, dataset_falso_pipeline, conexoes_do_ciclo
):
    monkeypatch.setattr(pipeline, "build_client", lambda *a, **k: StubAgrobrClient())

    detentor = get_connection()
    try:
        with acquire_advisory_lock(detentor):
            resultado = rodar_ciclo()
    finally:
        detentor.close()

    assert resultado["status"] == "BLOCKED"
    assert resultado["tentativas"] == 1
    assert "blocked_reason" in resultado
    assert conexoes_do_ciclo[0].closed


def test_run_ingestion_job_publica_no_core(
    ingest_conn, monkeypatch, conexoes_do_ciclo
):
    monkeypatch.setattr(ad, "build_client", lambda *a, **k: StubAgrobrClient())

    resultado = ad.run_ingestion_job(JOB_NAME_TESTES, cutoff_date=datas(5)[-1])

    assert resultado["status"] == "SUCCESS"
    assert resultado["tentativas"] == 1
    assert contar_do_teste(ingest_conn, "core.market_daily") == 5
    assert len(conexoes_do_ciclo) == 1
    assert conexoes_do_ciclo[0].closed


def test_run_dataset_job_usa_conexao_propria_e_repassa_argumentos(
    ingest_conn, monkeypatch, conexoes_do_ciclo
):
    chamadas = []

    def falso(conn, **kwargs):
        chamadas.append((conn, kwargs))
        return {"run_id": None, "status": "SUCCESS", "versao": dict(VERSAO_FALSA)}

    monkeypatch.setattr(ad, "run_dataset_build", falso)

    resultado = ad.run_dataset_job(
        JOB_NAME_TESTES, cutoff_date=datas(5)[-1], version_tag="teste-fronteira")

    assert resultado["status"] == "SUCCESS"
    assert resultado["tentativas"] == 1
    conn, kwargs = chamadas[0]
    assert conn is conexoes_do_ciclo[0], "a etapa roda na conexão aberta pela fronteira"
    assert kwargs["job_name"] == JOB_NAME_TESTES
    assert kwargs["cutoff_date"] == datas(5)[-1]
    assert kwargs["start_date"] is None
    assert kwargs["version_tag"] == "teste-fronteira"
    assert conexoes_do_ciclo[0].closed


# ---------------------------------------------------------------------------
# Leitura para o Admin (somente consulta)
# ---------------------------------------------------------------------------

def test_leitura_do_admin_lista_recentes_e_ultima_bem_sucedida(
    ingest_conn, monkeypatch, dataset_falso_pipeline, conexoes_do_ciclo
):
    monkeypatch.setattr(pipeline, "build_client", lambda *a, **k: StubAgrobrClient())
    executado = rodar_ciclo()

    recentes = ad.read_recent_runs(job_name=JOB_NAME_TESTES)
    ultima = ad.latest_successful_run(job_name=JOB_NAME_TESTES)

    # O mesmo nome de job audita o ciclo-mãe e a sub-execução da ingestão.
    registro = next(r for r in recentes if r["run_id"] == executado["run_id"])
    assert registro["status"] == "SUCCESS"
    assert registro["version_tag"] == "teste-fronteira"
    assert set(registro) == {
        "run_id", "job_name", "status", "started_at", "finished_at",
        "records_ingested", "records_features", "version_tag", "failed_stage",
        "error_message",
    }
    inicio = [r["started_at"] for r in recentes]
    assert inicio == sorted(inicio, reverse=True), "mais recentes primeiro"
    assert ultima["run_id"] == executado["run_id"]


def test_erro_de_conexao_na_leitura_vira_erro_de_integracao(monkeypatch):
    def conexao_recusada(**kwargs):
        raise psycopg.OperationalError("conexão recusada")

    monkeypatch.setattr(ad, "pipeline_connection", conexao_recusada)

    with pytest.raises(ad.PipelineIntegrationError, match="inacessível"):
        ad.read_recent_runs()
