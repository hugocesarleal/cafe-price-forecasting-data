"""Orquestrador do ciclo completo (exige PostgreSQL).

A ingestão roda de verdade, com dados de teste em 1990. A construção do dataset
é substituída por um dublê: ela já é coberta em ``test_dataset_versioning.py`` e,
executada aqui, confirmaria uma versão válida de teste visível a qualquer
consumidor do banco.
"""

import pytest

from conftest import (
    JOB_NAME_TESTES,
    StubAgrobrClient,
    arquivo_stub,
    contar_do_teste,
    datas,
    hash_stub,
    linha_mercado,
)
from src import pipeline
from src.db import PIPELINE_ADVISORY_LOCK_ID, acquire_advisory_lock, get_connection

VERSAO_FALSA = {
    "dataset_version_id": "00000000-0000-0000-0000-000000000001",
    "version_tag": "teste-orquestrador",
    "row_count": 42,
    "reused": False,
}


@pytest.fixture
def dataset_falso(monkeypatch):
    """Dublê de ``run_dataset_build``; ``resposta`` pode ser trocada pelo teste."""
    estado = {"chamadas": [], "resposta": {"run_id": None, "status": "SUCCESS",
                                           "versao": dict(VERSAO_FALSA)}}

    def falso(conn, **kwargs):
        estado["chamadas"].append(kwargs)
        if isinstance(estado["resposta"], Exception):
            raise estado["resposta"]
        return estado["resposta"]

    monkeypatch.setattr(pipeline, "run_dataset_build", falso)
    return estado


@pytest.fixture
def rodar(ingest_conn):
    def _rodar(client=None, **kwargs):
        return pipeline.run_pipeline(
            JOB_NAME_TESTES, conn=ingest_conn, client=client or StubAgrobrClient(),
            ingestion_job=JOB_NAME_TESTES, dataset_job=JOB_NAME_TESTES, **kwargs)
    return _rodar


def execucao(conn, run_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, records_ingested, records_features, error_message, metadata "
            "FROM audit.pipeline_runs WHERE run_id = %s",
            (run_id,),
        )
        return cur.fetchone()


def test_ciclo_completo_registra_etapas_frescor_e_contagens(ingest_conn, rodar, dataset_falso):
    resultado = rodar()

    assert resultado["status"] == "SUCCESS"
    assert resultado["ingestion"]["status"] == "SUCCESS"
    assert resultado["dataset"]["versao"]["version_tag"] == "teste-orquestrador"
    assert len(dataset_falso["chamadas"]) == 1

    registro = execucao(ingest_conn, resultado["run_id"])
    assert registro["status"] == "SUCCESS"
    assert registro["records_ingested"] == 10
    assert registro["records_features"] == 42
    meta = registro["metadata"]
    assert meta["ingestion_run_id"] == resultado["ingestion"]["run_id"]
    assert meta["version_tag"] == "teste-orquestrador"
    assert meta["failed_stage"] is None
    # Última observação disponível e última ingestão ficam registradas.
    assert meta["ultima_observacao_preco_arabica"] is not None
    assert meta["ultima_ingestao_em"] is not None
    assert resultado["freshness"]["ultima_observacao_preco_arabica"] >= datas(5)[-1]


def test_corte_do_job_vale_para_a_ingestao_e_para_o_dataset(rodar, dataset_falso):
    corte = datas(5)[-1]

    resultado = rodar(cutoff_date=corte)

    assert resultado["status"] == "SUCCESS"
    assert dataset_falso["chamadas"][0]["cutoff_date"] == corte


def test_dado_posterior_ao_corte_reprova_o_ciclo(rodar, dataset_falso):
    resultado = rodar(cutoff_date=datas(5)[2])  # a carga traz dois dias além do corte

    assert resultado["status"] == "FAILED"
    assert resultado["failed_stage"] == "ingestion"
    assert dataset_falso["chamadas"] == []


def test_dois_jobs_seguidos_sem_dado_novo_nao_duplicam_nada(ingest_conn, rodar, dataset_falso):
    primeiro = rodar()
    dataset_falso["resposta"]["versao"]["reused"] = True
    segundo = rodar()

    assert primeiro["status"] == segundo["status"] == "SUCCESS"
    assert segundo["ingestion"]["arquivos_alterados"] == 0
    assert contar_do_teste(ingest_conn, "raw.market_observations") == 5
    registro = execucao(ingest_conn, segundo["run_id"])
    assert registro["records_ingested"] == 0
    assert registro["records_features"] == 0
    assert registro["metadata"]["version_reused"] is True


def test_ingestao_reprovada_nao_reconstroi_o_dataset(ingest_conn, rodar, dataset_falso):
    invalido = [linha_mercado(d, "preco_arabica", None, "R$/sc 60kg") for d in datas(5)]

    resultado = rodar(StubAgrobrClient([arquivo_stub(market_rows=invalido, weather_rows=[])]))

    assert resultado["status"] == "FAILED"
    assert resultado["failed_stage"] == "ingestion"
    assert dataset_falso["chamadas"] == [], "A última versão válida fica como está"
    registro = execucao(ingest_conn, resultado["run_id"])
    assert registro["status"] == "FAILED"
    assert "valores_nulos" in registro["error_message"]
    assert registro["metadata"]["failed_stage"] == "ingestion"


def test_dataset_reprovado_encerra_o_ciclo_como_failed(ingest_conn, rodar, dataset_falso):
    dataset_falso["resposta"] = {"run_id": None, "status": "FAILED",
                                 "blocked_reason": "features_sem_coluna_vazia"}

    resultado = rodar()

    assert resultado["status"] == "FAILED"
    assert resultado["failed_stage"] == "dataset"
    registro = execucao(ingest_conn, resultado["run_id"])
    assert "features_sem_coluna_vazia" in registro["error_message"]
    assert registro["records_ingested"] == 10, "A ingestão em si foi publicada"


def test_erro_inesperado_e_auditado_propagado_e_libera_o_lock(ingest_conn, rodar, dataset_falso):
    dataset_falso["resposta"] = RuntimeError("falha na construção")

    with pytest.raises(RuntimeError, match="falha na construção"):
        rodar()

    with ingest_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM audit.pipeline_runs "
            "WHERE job_name = %s AND status = 'FAILED' AND error_message = %s",
            (JOB_NAME_TESTES, "falha na construção"),
        )
        assert cur.fetchone()["n"] == 1

    # Outra conexão consegue o lock: ele não ficou preso na sessão que falhou.
    outra = get_connection()
    try:
        with acquire_advisory_lock(outra, PIPELINE_ADVISORY_LOCK_ID):
            pass
    finally:
        outra.close()

    dataset_falso["resposta"] = {"run_id": None, "status": "SUCCESS",
                                 "versao": dict(VERSAO_FALSA)}
    retomada = rodar(StubAgrobrClient([arquivo_stub(content_hash=hash_stub("retomada"))]))
    assert retomada["status"] == "SUCCESS"


def test_ciclo_e_bloqueado_enquanto_outro_job_roda(ingest_conn, rodar, dataset_falso):
    detentor = get_connection()
    try:
        with acquire_advisory_lock(detentor, PIPELINE_ADVISORY_LOCK_ID):
            resultado = rodar()
    finally:
        detentor.close()

    assert resultado["status"] == "BLOCKED"
    assert "ingestion" not in resultado
    assert dataset_falso["chamadas"] == []
    assert execucao(ingest_conn, resultado["run_id"])["status"] == "BLOCKED"
    assert contar_do_teste(ingest_conn, "raw.ingestion_files") == 0
