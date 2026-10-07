"""Testes Obrigatórios 18 e 20: versionamento e recuperação da última versão válida.

* 18 — a matriz de features vira uma versão imutável, assinada por checksum;
* 20 — depois de uma falha, a última versão válida continua sendo a entregue.

A primeira parte roda em memória. A segunda exige PostgreSQL: as versões são
criadas numa transação que nunca é confirmada, então nenhum outro consumidor do
banco chega a vê-las.
"""

import numpy as np
import pytest

from conftest import ESCALAS, JOB_NAME_TESTES, TAG_VERSAO_TESTE, matriz_sintetica
from src import dataset_versioning as dv
from src.feature_builder import build_feature_matrix, storage_frame


@pytest.fixture(scope="module")
def matriz():
    return matriz_sintetica()


def assinar(matriz):
    return dv.compute_checksum(storage_frame(matriz, ESCALAS), ESCALAS)


# ---------------------------------------------------------------------------
# Checksum (em memória)
# ---------------------------------------------------------------------------

def test_checksum_e_reproduzivel(matriz):
    assert assinar(matriz) == assinar(matriz_sintetica())
    assert len(assinar(matriz)) == 64


def test_checksum_muda_quando_um_valor_muda(matriz):
    alterada = matriz_sintetica()
    alterada.frame.iloc[10, alterada.frame.columns.get_loc("usd_brl")] += 0.0001

    assert assinar(alterada) != assinar(matriz)


def test_checksum_ignora_ruido_abaixo_da_precisao_gravada(matriz):
    ruidosa = matriz_sintetica()
    ruidosa.frame["usd_brl"] += 1e-9

    assert assinar(ruidosa) == assinar(matriz)


def test_checksum_nao_depende_da_ordem_das_colunas(matriz):
    gravada = storage_frame(matriz, ESCALAS)

    assert dv.compute_checksum(gravada[sorted(gravada.columns, reverse=True)], ESCALAS) \
        == dv.compute_checksum(gravada, ESCALAS)


def test_checksum_trata_coluna_ausente_como_nula(matriz):
    gravada = storage_frame(matriz, ESCALAS)
    escalas = {**ESCALAS, "coluna_futura": 2}

    com_nulos = gravada.assign(coluna_futura=np.nan)
    assert dv.compute_checksum(gravada, escalas) == dv.compute_checksum(com_nulos, escalas)


def test_zero_negativo_e_gravado_como_zero(matriz):
    com_zero = matriz_sintetica()
    com_zero.frame["sin_ano"] = -1e-9

    gravada = storage_frame(com_zero, ESCALAS)

    assert not np.signbit(gravada["sin_ano"]).any()
    assert dv._formatar(gravada["sin_ano"].iloc[0], 6) == "0.000000"


def test_tag_padrao_segue_a_data_de_corte(matriz):
    assert dv.default_version_tag(matriz.cutoff_date) == \
        f"v{dv.DATASET_SCHEMA_VERSION}-{matriz.cutoff_date:%Y%m%d}"


# ---------------------------------------------------------------------------
# Qualidade da matriz (em memória)
# ---------------------------------------------------------------------------

def test_matriz_sa_passa_nas_checagens_criticas(matriz):
    report = dv.check_matrix(matriz, "versao-x")

    assert report.passed
    assert all(r.details["dataset_version_id"] == "versao-x" for r in report.results)
    assert "linha_de_corte_completa" not in report.summary()["warning_failures"]


def test_coluna_de_feature_inteira_nula_e_falha_critica():
    quebrada = matriz_sintetica()
    quebrada.frame["precip_90d_sulmg"] = np.nan

    report = dv.check_matrix(quebrada)

    assert not report.passed
    assert report.summary()["critical_failures"] == ["features_sem_coluna_vazia"]


def test_alvo_entre_as_features_e_falha_critica():
    vazada = matriz_sintetica()
    vazada.feature_columns.append("y_7d")

    assert "alvo_fora_das_features" in dv.check_matrix(vazada).summary()["critical_failures"]


def test_linha_de_corte_incompleta_e_so_alerta():
    incompleta = matriz_sintetica()
    incompleta.frame.iloc[-1, incompleta.frame.columns.get_loc("umidade_rel_sulmg")] = np.nan

    report = dv.check_matrix(incompleta)

    assert report.passed, "Alerta não impede a versão"
    assert "linha_de_corte_completa" in report.summary()["warning_failures"]


def test_matriz_vazia_e_falha_critica():
    vazia = matriz_sintetica()
    vazia.frame = vazia.frame.iloc[0:0]

    assert "matriz_nao_vazia" in dv.check_matrix(vazia).summary()["critical_failures"]


# ---------------------------------------------------------------------------
# Com PostgreSQL
# ---------------------------------------------------------------------------

def tag(sufixo):
    return TAG_VERSAO_TESTE + sufixo


def construir(conn, core):
    return build_feature_matrix(conn, cutoff_date=core["corte"], start_date=core["inicio"])


def contar(conn, consulta, *params):
    with conn.cursor() as cur:
        cur.execute(consulta, params)
        return cur.fetchone()["n"]


def linhas_da_versao(conn, version_id):
    return contar(conn, "SELECT count(*) AS n FROM features.model_features "
                        "WHERE dataset_version_id = %s", version_id)


def eventos_ready(conn, version_id):
    return contar(conn, "SELECT count(*) AS n FROM audit.pending_events "
                        "WHERE event_type = 'DATASET_READY' "
                        "AND payload->>'dataset_version_id' = %s", version_id)


# -- Teste Obrigatório 18 ---------------------------------------------------

def test_versao_e_criada_valida_assinada_e_sinalizada(ingest_conn, core_no_banco):
    matriz = construir(ingest_conn, core_no_banco)

    versao = dv.create_dataset_version(ingest_conn, matriz, version_tag=tag("v1"))

    assert versao.is_valid and not versao.reused
    assert versao.version_tag == tag("v1")
    assert (versao.start_date, versao.cutoff_date) == (matriz.start_date, matriz.cutoff_date)
    assert versao.row_count == matriz.row_count == linhas_da_versao(
        ingest_conn, versao.dataset_version_id)
    assert versao.feature_count == len(matriz.feature_columns)
    assert dv.verify_dataset_version(ingest_conn, versao.dataset_version_id), \
        "O conteúdo gravado precisa bater com o checksum da versão"
    assert eventos_ready(ingest_conn, versao.dataset_version_id) == 1
    assert dv.get_latest_valid_version(ingest_conn).dataset_version_id == versao.dataset_version_id


def test_mesmo_conteudo_nao_gera_versao_nem_evento_novos(ingest_conn, core_no_banco):
    matriz = construir(ingest_conn, core_no_banco)
    primeira = dv.create_dataset_version(ingest_conn, matriz, version_tag=tag("v1"))

    segunda = dv.create_dataset_version(ingest_conn, matriz, version_tag=tag("v2"))

    assert segunda.reused
    assert segunda.dataset_version_id == primeira.dataset_version_id
    assert contar(ingest_conn, "SELECT count(*) AS n FROM features.dataset_versions "
                               "WHERE version_tag LIKE %s", tag("%")) == 1
    assert linhas_da_versao(ingest_conn, primeira.dataset_version_id) == matriz.row_count
    assert eventos_ready(ingest_conn, primeira.dataset_version_id) == 1


def test_conteudo_revisado_gera_versao_nova_e_preserva_a_anterior(ingest_conn, core_no_banco):
    primeira = dv.create_dataset_version(
        ingest_conn, construir(ingest_conn, core_no_banco), version_tag=tag("v1"))

    core_no_banco["recarregar"](seed=7)  # a fonte revisou os dados do mesmo período
    segunda = dv.create_dataset_version(
        ingest_conn, construir(ingest_conn, core_no_banco), version_tag=tag("v1"))

    assert not segunda.reused
    assert segunda.dataset_version_id != primeira.dataset_version_id
    assert segunda.sha256_checksum != primeira.sha256_checksum
    assert segunda.version_tag == f"{tag('v1')}-{segunda.sha256_checksum[:8]}"
    # A versão antiga continua exatamente como foi assinada.
    assert dv.verify_dataset_version(ingest_conn, primeira.dataset_version_id)
    assert dv.verify_dataset_version(ingest_conn, segunda.dataset_version_id)
    assert dv.get_latest_valid_version(ingest_conn).dataset_version_id == segunda.dataset_version_id


def test_adulteracao_das_linhas_e_detectada_pelo_checksum(ingest_conn, core_no_banco):
    versao = dv.create_dataset_version(
        ingest_conn, construir(ingest_conn, core_no_banco), version_tag=tag("v1"))

    with ingest_conn.cursor() as cur:
        cur.execute(
            "UPDATE features.model_features SET usd_brl = usd_brl + 1 "
            "WHERE dataset_version_id = %s AND data_ref = %s",
            (versao.dataset_version_id, core_no_banco["corte"]),
        )

    assert not dv.verify_dataset_version(ingest_conn, versao.dataset_version_id)


# -- Teste Obrigatório 20 ---------------------------------------------------

def test_falha_na_gravacao_nao_deixa_versao_e_mantem_a_anterior(
    ingest_conn, core_no_banco, monkeypatch
):
    anterior = dv.create_dataset_version(
        ingest_conn, construir(ingest_conn, core_no_banco), version_tag=tag("v1"))
    core_no_banco["recarregar"](seed=7)
    nova = construir(ingest_conn, core_no_banco)

    def falhar(*args, **kwargs):
        raise RuntimeError("falha simulada na gravação")

    monkeypatch.setattr(dv, "publish_features", falhar)
    with pytest.raises(RuntimeError, match="falha simulada"):
        with ingest_conn.transaction():  # savepoint: desfaz só a tentativa
            dv.create_dataset_version(ingest_conn, nova, version_tag=tag("v2"))

    assert contar(ingest_conn, "SELECT count(*) AS n FROM features.dataset_versions "
                               "WHERE version_tag LIKE %s", tag("v2%")) == 0
    recuperada = dv.get_latest_valid_version(ingest_conn)
    assert recuperada.dataset_version_id == anterior.dataset_version_id
    assert dv.verify_dataset_version(ingest_conn, recuperada.dataset_version_id)


def test_falha_critica_de_qualidade_nao_cria_versao(ingest_conn, core_no_banco, monkeypatch):
    antes = dv.get_latest_valid_version(ingest_conn)
    quebrada = construir(ingest_conn, core_no_banco)
    quebrada.frame["precip_90d_sulmg"] = np.nan
    monkeypatch.setattr(dv, "build_feature_matrix", lambda *a, **k: quebrada)

    metricas = dv.run_dataset_build(ingest_conn, job_name=JOB_NAME_TESTES, version_tag=tag("v1"))

    assert metricas["status"] == "FAILED"
    assert metricas["blocked_reason"] == "features_sem_coluna_vazia"
    assert contar(ingest_conn, "SELECT count(*) AS n FROM features.dataset_versions "
                               "WHERE version_tag LIKE %s", tag("%")) == 0
    depois = dv.get_latest_valid_version(ingest_conn)
    assert (depois and depois.dataset_version_id) == (antes and antes.dataset_version_id)
    assert (metricas["ultima_versao_valida"] or {}).get("dataset_version_id") == \
        (antes and antes.dataset_version_id)

    with ingest_conn.cursor() as cur:
        cur.execute("SELECT status, error_message FROM audit.pipeline_runs WHERE run_id = %s",
                    (metricas["run_id"],))
        execucao = cur.fetchone()
        cur.execute("SELECT count(*) AS n FROM audit.data_quality_checks "
                    "WHERE pipeline_run_id = %s AND NOT passed AND severity = 'CRITICAL'",
                    (metricas["run_id"],))
        criticas = cur.fetchone()["n"]
    assert execucao["status"] == "FAILED"
    assert "features_sem_coluna_vazia" in execucao["error_message"]
    assert criticas == 1


def test_erro_na_construcao_registra_falha_e_propaga(ingest_conn, core_no_banco, monkeypatch):
    def falhar(*args, **kwargs):
        raise ValueError("core sem dados")

    monkeypatch.setattr(dv, "build_feature_matrix", falhar)

    with pytest.raises(ValueError, match="core sem dados"):
        dv.run_dataset_build(ingest_conn, job_name=JOB_NAME_TESTES)

    with ingest_conn.cursor() as cur:
        cur.execute(
            "SELECT status, error_message FROM audit.pipeline_runs "
            "WHERE job_name = %s AND error_message = 'core sem dados'",
            (JOB_NAME_TESTES,),
        )
        assert cur.fetchone()["status"] == "FAILED"


def test_invalidar_versao_devolve_a_anterior_e_preserva_as_linhas(ingest_conn, core_no_banco):
    primeira = dv.create_dataset_version(
        ingest_conn, construir(ingest_conn, core_no_banco), version_tag=tag("v1"))
    core_no_banco["recarregar"](seed=7)
    segunda = dv.create_dataset_version(
        ingest_conn, construir(ingest_conn, core_no_banco), version_tag=tag("v2"))

    recuperada = dv.invalidate_dataset_version(
        ingest_conn, segunda.dataset_version_id, "dado de origem corrompido")

    assert recuperada.dataset_version_id == primeira.dataset_version_id
    assert not dv.get_dataset_version(ingest_conn, segunda.dataset_version_id).is_valid
    assert linhas_da_versao(ingest_conn, segunda.dataset_version_id) == segunda.row_count
    with pytest.raises(ValueError):
        dv.invalidate_dataset_version(ingest_conn, segunda.dataset_version_id, "de novo")
