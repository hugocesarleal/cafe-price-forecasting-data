"""Testes Obrigatórios 9, 10 e 11: ingestão ponta a ponta, falhas e concorrência.

Cobrem o caminho feliz (coleta → raw → validação → staging → core), a quarentena
de linhas inválidas, o bloqueio de publicação em falha CRÍTICA, a política de
novas tentativas do cliente e o advisory lock contra execução concorrente.
"""

import pytest

from conftest import (
    HASH_STUB,
    JOB_NAME_TESTES,
    StubAgrobrClient,
    arquivo_stub,
    contar_do_teste,
    datas,
    hash_stub,
    linha_clima,
    linha_mercado,
)
from src.agrobr_client import SourceFetchError
from src.config import settings
from src.db import acquire_advisory_lock, get_connection
from src.ingestion import (
    EVENTO_BLOQUEIO_QUALIDADE,
    EVENTO_CONCLUSAO,
    IngestionPipeline,
)


def contar(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()["n"]


def run_status(conn, run_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, records_ingested, error_message "
            "FROM audit.pipeline_runs WHERE run_id = %s",
            (run_id,),
        )
        return cur.fetchone()


def eventos(conn, run_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_type FROM audit.pending_events WHERE payload->>'run_id' = %s",
            (run_id,),
        )
        return [r["event_type"] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Caminho feliz
# ---------------------------------------------------------------------------

def test_caminho_feliz_publica_de_raw_ate_core(ingest_conn, executar_pipeline):
    metricas = executar_pipeline()

    assert metricas["status"] == "SUCCESS"
    assert metricas["arquivos_coletados"] == 1
    assert metricas["arquivos_alterados"] == 1
    assert metricas["raw_market"] == 5
    assert metricas["raw_weather"] == 5
    assert metricas["raw_invalidos"] == 0
    assert metricas["staging_market"] == 5
    assert metricas["staging_weather"] == 5
    assert metricas["core_market_dates"] == 5
    assert metricas["core_weather_rows"] == 5
    assert metricas["checks"]["critical_failures"] == []


def test_core_recebe_os_valores_pivotados(ingest_conn, executar_pipeline):
    serie = datas(5)
    metricas = executar_pipeline()

    with ingest_conn.cursor() as cur:
        cur.execute(
            "SELECT data_ref, preco_arabica, pipeline_run_id "
            "FROM core.market_daily WHERE data_ref = ANY(%s) ORDER BY data_ref",
            (serie,),
        )
        linhas = cur.fetchall()
        cur.execute(
            "SELECT data_ref, region, temp_min FROM core.weather_daily "
            "WHERE data_ref = ANY(%s) ORDER BY data_ref",
            (serie,),
        )
        clima = cur.fetchall()

    assert len(linhas) == 5
    assert [l["preco_arabica"] for l in linhas] == [1200.0, 1201.0, 1202.0, 1203.0, 1204.0]
    assert all(str(l["pipeline_run_id"]) == metricas["run_id"] for l in linhas)
    assert len(clima) == 5
    assert all(l["region"] == "bambui" for l in clima)


def test_metadados_da_coleta_sao_registrados(ingest_conn, executar_pipeline):
    metricas = executar_pipeline()

    with ingest_conn.cursor() as cur:
        cur.execute(
            "SELECT source_name, file_name, file_hash, source_url, git_commit, "
            "       row_count, has_changed "
            "FROM raw.ingestion_files WHERE pipeline_run_id = %s",
            (metricas["run_id"],),
        )
        arquivo = cur.fetchone()

    assert arquivo["source_name"] == "CEPEA/ESALQ"
    assert arquivo["file_name"] == "stub_mercado.csv"
    assert arquivo["file_hash"] == HASH_STUB
    assert arquivo["source_url"].startswith("https://")
    assert len(arquivo["git_commit"]) == 40
    assert arquivo["row_count"] == 10
    assert arquivo["has_changed"] is True


def test_execucao_e_finalizada_com_metricas(ingest_conn, executar_pipeline):
    metricas = executar_pipeline()
    registro = run_status(ingest_conn, metricas["run_id"])

    assert registro["status"] == "SUCCESS"
    assert registro["records_ingested"] == 10
    assert registro["error_message"] is None


def test_as_doze_checagens_sao_gravadas_por_lote(ingest_conn, executar_pipeline):
    metricas = executar_pipeline()
    gravadas = contar(
        ingest_conn,
        "SELECT count(*) AS n FROM audit.data_quality_checks WHERE pipeline_run_id = %s",
        (metricas["run_id"],),
    )

    assert metricas["checks"]["batches"] == 2
    assert gravadas == 24


def test_evento_de_conclusao_e_emitido(ingest_conn, executar_pipeline):
    metricas = executar_pipeline()

    assert metricas["event_id"] > 0
    assert eventos(ingest_conn, metricas["run_id"]) == [EVENTO_CONCLUSAO]


# ---------------------------------------------------------------------------
# Quarentena e bloqueio por qualidade
# ---------------------------------------------------------------------------

def test_linhas_invalidas_ficam_em_quarentena_no_raw(ingest_conn, executar_pipeline):
    """Ruído tolerado não bloqueia a carga, mas nunca chega a staging/core."""
    serie = datas(100)
    clima = [
        linha_clima(d, "bambui", "precip_mm", -1.0 if i < 5 else 10.0, "mm")
        for i, d in enumerate(serie)
    ]
    metricas = executar_pipeline(
        StubAgrobrClient([arquivo_stub(market_rows=[], weather_rows=clima)])
    )

    assert metricas["status"] == "SUCCESS"
    assert metricas["raw_invalidos"] == 5
    assert metricas["raw_weather"] == 100
    assert metricas["staging_weather"] == 95

    with ingest_conn.cursor() as cur:
        cur.execute(
            "SELECT validation_status, count(*) AS n FROM raw.weather_observations "
            "WHERE pipeline_run_id = %s GROUP BY validation_status",
            (metricas["run_id"],),
        )
        por_status = {r["validation_status"]: r["n"] for r in cur.fetchall()}
        cur.execute(
            "SELECT count(*) AS n FROM raw.weather_observations "
            "WHERE pipeline_run_id = %s AND validation_status = 'INVALID' AND value >= 0",
            (metricas["run_id"],),
        )
        falso_positivo = cur.fetchone()["n"]

    assert por_status == {"VALID": 95, "INVALID": 5}
    assert falso_positivo == 0
    assert metricas["core_weather_rows"] == 95


def test_falha_critica_impede_a_publicacao(ingest_conn, executar_pipeline):
    mercado = [
        linha_mercado(d, "preco_arabica", None, "R$/sc 60kg") for d in datas(5)
    ]
    metricas = executar_pipeline(
        StubAgrobrClient([arquivo_stub(market_rows=mercado, weather_rows=[])])
    )

    assert metricas["status"] == "FAILED"
    assert "valores_nulos" in metricas["checks"]["critical_failures"]
    assert metricas["staging_market"] == 0
    assert metricas["core_market_dates"] == 0

    # Os brutos coletados são preservados, em quarentena.
    assert metricas["raw_invalidos"] == 5
    assert contar(
        ingest_conn,
        "SELECT count(*) AS n FROM staging.market_observations WHERE pipeline_run_id = %s",
        (metricas["run_id"],),
    ) == 0
    assert contar(
        ingest_conn,
        "SELECT count(*) AS n FROM core.market_daily WHERE data_ref = ANY(%s)",
        (datas(5),),
    ) == 0
    assert run_status(ingest_conn, metricas["run_id"])["status"] == "FAILED"
    assert EVENTO_BLOQUEIO_QUALIDADE in eventos(ingest_conn, metricas["run_id"])


def test_aviso_nao_bloqueia_a_publicacao(ingest_conn, executar_pipeline):
    """Janela curta reprova disponibilidade_horizontes, que é WARNING."""
    metricas = executar_pipeline()

    assert "disponibilidade_horizontes" in metricas["checks"]["warning_failures"]
    assert metricas["status"] == "SUCCESS"
    assert metricas["core_market_dates"] == 5


# ---------------------------------------------------------------------------
# Teste Obrigatório 9 — falha de conexão com a fonte
# ---------------------------------------------------------------------------

def test_falha_de_conexao_encerra_a_execucao_como_failed(ingest_conn, executar_pipeline):
    client = StubAgrobrClient(falhar_sempre=True)

    with pytest.raises(SourceFetchError):
        executar_pipeline(client)

    with ingest_conn.cursor() as cur:
        cur.execute(
            "SELECT status, error_message FROM audit.pipeline_runs WHERE job_name = %s",
            (JOB_NAME_TESTES,),
        )
        registro = cur.fetchone()

    assert registro["status"] == "FAILED"
    assert "Falha simulada de coleta" in registro["error_message"]
    assert contar_do_teste(ingest_conn, "raw.market_observations") == 0, \
        "Nenhum bruto pode ser gravado quando a coleta falha"
    assert contar_do_teste(ingest_conn, "core.market_daily") == 0
    assert contar_do_teste(ingest_conn, "raw.ingestion_files") == 0


def test_falha_inesperada_tambem_audita_antes_de_propagar(ingest_conn):
    class ClientQuebrado:
        def fetch(self):
            raise RuntimeError("boom inesperado")

    pipeline = IngestionPipeline(client=ClientQuebrado(), conn=ingest_conn,
                                 job_name=JOB_NAME_TESTES)

    with pytest.raises(RuntimeError, match="boom inesperado"):
        pipeline.run()

    with ingest_conn.cursor() as cur:
        cur.execute(
            "SELECT status, error_message FROM audit.pipeline_runs WHERE job_name = %s",
            (JOB_NAME_TESTES,),
        )
        registro = cur.fetchone()

    assert registro["status"] == "FAILED"
    assert "boom inesperado" in registro["error_message"]


# ---------------------------------------------------------------------------
# Teste Obrigatório 10 — novas tentativas
# ---------------------------------------------------------------------------

def test_falha_persistente_esgota_as_tentativas_configuradas(ingest_conn, executar_pipeline):
    client = StubAgrobrClient(falhar_sempre=True)

    with pytest.raises(SourceFetchError):
        executar_pipeline(client)

    assert client.tentativas == max(1, settings.AGROBR_MAX_RETRIES)


def test_falha_transitoria_e_recuperada_por_nova_tentativa(ingest_conn, executar_pipeline):
    client = StubAgrobrClient(falhas_iniciais=settings.AGROBR_MAX_RETRIES - 1)
    metricas = executar_pipeline(client)

    assert client.tentativas == settings.AGROBR_MAX_RETRIES
    assert metricas["status"] == "SUCCESS"
    assert metricas["core_market_dates"] == 5


def test_erro_que_nao_e_de_coleta_nao_tenta_de_novo(ingest_conn, executar_pipeline):
    """A política de tentativas vale só para SourceFetchError."""
    class FalhaGrave(StubAgrobrClient):
        def _fetch(self):
            self.tentativas += 1
            raise ValueError("erro de programação")

    client = FalhaGrave()
    with pytest.raises(ValueError, match="erro de programação"):
        executar_pipeline(client)

    assert client.tentativas == 1


# ---------------------------------------------------------------------------
# Teste Obrigatório 11 — execução concorrente bloqueada
# ---------------------------------------------------------------------------

def test_execucao_concorrente_e_bloqueada_pelo_advisory_lock(ingest_conn, executar_pipeline):
    detentor = get_connection()
    try:
        with acquire_advisory_lock(detentor):
            metricas = executar_pipeline()
    finally:
        detentor.close()

    assert metricas["status"] == "BLOCKED"
    assert str(settings.ADVISORY_LOCK_KEY) in metricas["blocked_reason"]

    registro = run_status(ingest_conn, metricas["run_id"])
    assert registro["status"] == "BLOCKED"
    assert "concorrência" in registro["error_message"].lower()

    assert contar_do_teste(ingest_conn, "raw.market_observations") == 0
    assert contar_do_teste(ingest_conn, "staging.market_observations") == 0
    assert contar_do_teste(ingest_conn, "core.market_daily") == 0
    assert contar_do_teste(ingest_conn, "raw.ingestion_files") == 0
    assert eventos(ingest_conn, metricas["run_id"]) == ["INGESTAO_BLOQUEADA_CONCORRENCIA"]


def test_lock_e_liberado_ao_final_e_nova_execucao_prosseguir(ingest_conn, executar_pipeline):
    detentor = get_connection()
    try:
        with acquire_advisory_lock(detentor):
            bloqueada = executar_pipeline()
    finally:
        detentor.close()

    assert bloqueada["status"] == "BLOCKED"

    # Hash diferente: a origem "mudou", então há o que publicar de fato.
    liberada = executar_pipeline(
        StubAgrobrClient([arquivo_stub(content_hash=hash_stub("apos-lock"))])
    )
    assert liberada["status"] == "SUCCESS"
    assert liberada["core_market_dates"] == 5


# ---------------------------------------------------------------------------
# Retry seguro e reprocessamento
# ---------------------------------------------------------------------------

def test_carga_que_falhou_no_meio_e_reprocessada_na_tentativa_seguinte(
    ingest_conn, executar_pipeline, monkeypatch
):
    """A origem só conta como "já carregada" se a carga anterior terminou bem."""
    original = IngestionPipeline._insert_staging

    def falhar(self, *args, **kwargs):
        raise RuntimeError("queda no meio da carga")

    monkeypatch.setattr(IngestionPipeline, "_insert_staging", falhar)
    with pytest.raises(RuntimeError, match="queda no meio"):
        executar_pipeline()
    assert contar_do_teste(ingest_conn, "raw.ingestion_files") == 1
    assert contar_do_teste(ingest_conn, "core.market_daily") == 0

    monkeypatch.setattr(IngestionPipeline, "_insert_staging", original)
    retomada = executar_pipeline()  # mesmo conteúdo, mesmo hash

    assert retomada["status"] == "SUCCESS"
    assert retomada["arquivos_alterados"] == 1,         "Hash igual ao de uma carga que falhou não pode ser tratado como inalterado"
    assert retomada["core_market_dates"] == 5
    # Depois de publicada, a mesma origem volta a ser ignorada.
    assert executar_pipeline()["arquivos_alterados"] == 0


def test_carga_reprovada_continua_reprovada_ao_repetir(ingest_conn, executar_pipeline):
    """Repetir uma carga com falha crítica não pode virar um SUCCESS vazio."""
    mercado = [linha_mercado(d, "preco_arabica", None, "R$/sc 60kg") for d in datas(5)]
    arquivos = [arquivo_stub(market_rows=mercado, weather_rows=[])]

    primeira = executar_pipeline(StubAgrobrClient(arquivos))
    segunda = executar_pipeline(StubAgrobrClient(arquivos))

    assert primeira["status"] == segunda["status"] == "FAILED"
    assert contar_do_teste(ingest_conn, "core.market_daily") == 0


def test_force_reprocessa_origem_inalterada_sem_duplicar_core(ingest_conn, executar_pipeline):
    executar_pipeline()
    forcada = executar_pipeline(force=True)

    assert forcada["status"] == "SUCCESS"
    assert forcada["forcado"] is True
    assert forcada["arquivos_alterados"] == 1
    assert forcada["core_market_dates"] == 5
    # raw acumula a nova coleta; core continua com uma linha por data.
    assert contar_do_teste(ingest_conn, "raw.market_observations") == 10
    assert contar(
        ingest_conn,
        "SELECT count(*) AS n FROM core.market_daily WHERE data_ref = ANY(%s)",
        (datas(5),),
    ) == 5
    with ingest_conn.cursor() as cur:
        cur.execute(
            "SELECT has_changed FROM raw.ingestion_files WHERE pipeline_run_id = %s",
            (forcada["run_id"],),
        )
        assert cur.fetchone()["has_changed"] is False,             "O registro da coleta continua dizendo a verdade: o conteúdo não mudou"
