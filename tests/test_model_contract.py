"""Teste Obrigatório 19 e contrato com o componente de rede neural.

* 19 — as previsões são registradas por horizonte, com versão do modelo, versão
  do dataset e execução de origem, sem duplicar.

Cobre também o lado de consumo: metadados da última versão válida e a consulta
padrão das features. A primeira parte roda em memória; a segunda exige
PostgreSQL e trabalha numa transação que nunca é confirmada.
"""

import json
import uuid
from datetime import date, timedelta

import pandas as pd
import pytest

from conftest import KEEP_ESPERADAS, TAG_VERSAO_TESTE, matriz_sintetica
from src import dataset_versioning as dv
from src import model_contract as mc
from src.feature_builder import build_feature_matrix

HORIZONTES = [7, 15, 30, 90]
CORTE = date(1991, 2, 4)

VERSAO = dv.DatasetVersion(
    dataset_version_id=str(uuid.uuid4()),
    version_tag=TAG_VERSAO_TESTE + "memoria",
    cutoff_date=CORTE,
    start_date=date(1990, 5, 1),
    row_count=280,
    feature_count=19,
    sha256_checksum="a" * 64,
    is_valid=True,
)
RUN_ID = str(uuid.uuid4())


def dataset_em_memoria():
    frame = matriz_sintetica().frame.reset_index()
    return frame.rename(columns={frame.columns[0]: "data_ref"})


def previsoes(**sobrescreve):
    lote = mc.mock_neural_model_predict(
        dataset_em_memoria(), VERSAO.dataset_version_id, RUN_ID, horizons=HORIZONTES)
    for campo, valor in sobrescreve.items():
        lote[0][campo] = valor
    return lote


# ---------------------------------------------------------------------------
# Consumidor de exemplo (em memória)
# ---------------------------------------------------------------------------

def test_consumidor_simulado_devolve_uma_previsao_por_horizonte():
    lote = previsoes()

    assert [p["horizon_days"] for p in lote] == HORIZONTES
    assert all(p["reference_date"] == CORTE for p in lote)
    assert all(p["target_date"] == CORTE + timedelta(days=p["horizon_days"]) for p in lote)
    assert all(p["predicted_value"] > 0 for p in lote)
    assert {p["dataset_version_id"] for p in lote} == {VERSAO.dataset_version_id}
    assert {p["pipeline_run_id"] for p in lote} == {RUN_ID}
    assert len({p["forecast_id"] for p in lote}) == 4


def test_consumidor_simulado_recusa_dataset_vazio():
    with pytest.raises(ValueError, match="vazio"):
        mc.mock_neural_model_predict(pd.DataFrame(), VERSAO.dataset_version_id, RUN_ID)


# ---------------------------------------------------------------------------
# Validação das previsões recebidas (em memória)
# ---------------------------------------------------------------------------

def test_lote_dentro_do_contrato_e_aceito():
    mc.validate_forecasts(previsoes(), VERSAO, HORIZONTES)


@pytest.mark.parametrize("campo, valor, trecho", [
    ("horizon_days", 10, "horizonte 10"),
    ("target_date", CORTE + timedelta(days=8), "target_date"),
    ("predicted_value", 0, "predicted_value"),
    ("predicted_value", -5.0, "predicted_value"),
    ("predicted_value", "1500", "predicted_value"),
    ("predicted_value", float("nan"), "predicted_value"),
    ("model_version", "", "campos ausentes"),
    ("pipeline_run_id", None, "campos ausentes"),
    ("dataset_version_id", str(uuid.uuid4()), "dataset_version_id"),
    ("reference_date", "1991-02-04", "precisam ser datas"),
])
def test_previsao_fora_do_contrato_rejeita_o_lote(campo, valor, trecho):
    with pytest.raises(ValueError, match=trecho):
        mc.validate_forecasts(previsoes(**{campo: valor}), VERSAO, HORIZONTES)


def test_previsao_apos_o_corte_da_versao_e_rejeitada():
    depois = CORTE + timedelta(days=1)
    lote = previsoes(reference_date=depois, target_date=depois + timedelta(days=7))

    with pytest.raises(ValueError, match="posterior ao corte"):
        mc.validate_forecasts(lote, VERSAO, HORIZONTES)


def test_previsao_repetida_no_lote_e_rejeitada():
    lote = previsoes()

    with pytest.raises(ValueError, match="repetida"):
        mc.validate_forecasts(lote + [dict(lote[0])], VERSAO, HORIZONTES)


def test_lote_vazio_ou_de_versao_invalida_e_rejeitado():
    invalida = dv.DatasetVersion(**{**VERSAO.as_dict(), "is_valid": False})

    with pytest.raises(ValueError, match="vazio"):
        mc.validate_forecasts([], VERSAO, HORIZONTES)
    with pytest.raises(ValueError, match="inválida"):
        mc.validate_forecasts(previsoes(), invalida, HORIZONTES)


# ---------------------------------------------------------------------------
# Com PostgreSQL
# ---------------------------------------------------------------------------

@pytest.fixture
def versao_no_banco(ingest_conn, core_no_banco):
    matriz = build_feature_matrix(
        ingest_conn, cutoff_date=core_no_banco["corte"], start_date=core_no_banco["inicio"])
    return dv.create_dataset_version(ingest_conn, matriz, version_tag=TAG_VERSAO_TESTE + "v1")


def previsoes_gravadas(conn, version_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM predictions.forecasts WHERE dataset_version_id = %s "
            "ORDER BY horizon_days",
            (version_id,),
        )
        return cur.fetchall()


def test_metadados_da_ultima_versao_valida(ingest_conn, versao_no_banco):
    dataset = mc.get_latest_dataset(ingest_conn)

    assert dataset["dataset_version_id"] == versao_no_banco.dataset_version_id
    assert dataset["version_tag"] == versao_no_banco.version_tag
    assert dataset["cutoff_date"] == versao_no_banco.cutoff_date
    assert dataset["row_count"] == versao_no_banco.row_count
    assert dataset["sha256_checksum"] == versao_no_banco.sha256_checksum
    assert dataset["horizons"] == HORIZONTES
    assert set(dataset["feature_columns"]) == set(KEEP_ESPERADAS)
    assert dataset["target_columns"] == ["y_7d", "y_15d", "y_30d", "y_90d"]
    assert dataset["columns"]["data_ref"] == "date"
    assert dataset["columns"]["usd_brl"] == "numeric(10,4)"
    assert dataset["quality"] == {"status": "OK", "alertas": []}


def test_status_de_qualidade_reflete_os_alertas_da_construcao(
    ingest_conn, core_no_banco, versao_no_banco
):
    with ingest_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit.data_quality_checks "
            "(pipeline_run_id, check_name, table_name, severity, passed, details) "
            "VALUES (%s, 'janela_minima', 'features.model_features', 'WARNING', FALSE, %s::jsonb)",
            (core_no_banco["run_id"],
             json.dumps({"dataset_version_id": versao_no_banco.dataset_version_id})),
        )

    assert mc.get_latest_dataset(ingest_conn)["quality"] == {
        "status": "WARNING", "alertas": ["janela_minima"]}


def test_consulta_padrao_devolve_as_features_da_versao_em_ordem(ingest_conn, versao_no_banco):
    features = mc.load_features(ingest_conn)

    assert len(features) == versao_no_banco.row_count
    assert set(features.columns) == {"data_ref", *KEEP_ESPERADAS, "y_7d", "y_15d", "y_30d", "y_90d"}
    assert features["data_ref"].is_monotonic_increasing
    assert features["data_ref"].iloc[-1] == versao_no_banco.cutoff_date
    assert features.attrs["dataset_version_id"] == versao_no_banco.dataset_version_id


def test_consulta_padrao_ignora_linhas_podadas_e_versoes_invalidas(ingest_conn, versao_no_banco):
    with ingest_conn.cursor() as cur:
        cur.execute(
            "UPDATE features.model_features SET is_pruned = TRUE "
            "WHERE dataset_version_id = %s AND data_ref = %s",
            (versao_no_banco.dataset_version_id, versao_no_banco.start_date),
        )
    assert len(mc.load_features(ingest_conn)) == versao_no_banco.row_count - 1

    dv.invalidate_dataset_version(ingest_conn, versao_no_banco.dataset_version_id, "teste")
    with pytest.raises(LookupError):
        mc.load_features(ingest_conn, versao_no_banco.dataset_version_id)


# -- Teste Obrigatório 19 ---------------------------------------------------

def test_previsoes_sao_registradas_por_horizonte(ingest_conn, core_no_banco, versao_no_banco):
    features = mc.load_features(ingest_conn)
    lote = mc.mock_neural_model_predict(
        features, versao_no_banco.dataset_version_id, core_no_banco["run_id"])

    assert mc.register_forecasts(ingest_conn, lote) == 4

    gravadas = previsoes_gravadas(ingest_conn, versao_no_banco.dataset_version_id)
    assert [g["horizon_days"] for g in gravadas] == HORIZONTES
    for g in gravadas:
        assert g["reference_date"] == versao_no_banco.cutoff_date
        assert g["target_date"] == g["reference_date"] + timedelta(days=g["horizon_days"])
        assert g["model_version"] == mc.MOCK_MODEL_VERSION
        assert str(g["pipeline_run_id"]) == core_no_banco["run_id"]
        assert g["forecast_status"] == "ACTIVE"
        assert g["predicted_value"] > 0

    with ingest_conn.cursor() as cur:
        cur.execute(
            "SELECT model_version, records_predicted, status FROM audit.model_runs "
            "WHERE input_dataset_version_id = %s",
            (versao_no_banco.dataset_version_id,),
        )
        execucoes = cur.fetchall()
    assert [(e["model_version"], e["records_predicted"], e["status"]) for e in execucoes] == [
        (mc.MOCK_MODEL_VERSION, 4, "COMPLETED")]


def test_previsao_repetida_atualiza_em_vez_de_duplicar(
    ingest_conn, core_no_banco, versao_no_banco
):
    features = mc.load_features(ingest_conn)
    lote = mc.mock_neural_model_predict(
        features, versao_no_banco.dataset_version_id, core_no_banco["run_id"])
    mc.register_forecasts(ingest_conn, lote)

    revisado = [{**p, "forecast_id": str(uuid.uuid4()), "predicted_value": 999.99} for p in lote]
    mc.register_forecasts(ingest_conn, revisado)

    gravadas = previsoes_gravadas(ingest_conn, versao_no_banco.dataset_version_id)
    assert len(gravadas) == 4, "Mesma data, horizonte e modelo: uma linha só"
    assert all(float(g["predicted_value"]) == 999.99 for g in gravadas)


def test_outra_versao_de_modelo_convive_com_a_primeira(
    ingest_conn, core_no_banco, versao_no_banco
):
    features = mc.load_features(ingest_conn)
    for modelo in ("modelo_a", "modelo_b"):
        mc.register_forecasts(ingest_conn, mc.mock_neural_model_predict(
            features, versao_no_banco.dataset_version_id, core_no_banco["run_id"], modelo))

    gravadas = previsoes_gravadas(ingest_conn, versao_no_banco.dataset_version_id)
    assert len(gravadas) == 8
    assert {g["model_version"] for g in gravadas} == {"modelo_a", "modelo_b"}


def test_lote_invalido_nao_grava_nenhuma_previsao(ingest_conn, core_no_banco, versao_no_banco):
    features = mc.load_features(ingest_conn)
    lote = mc.mock_neural_model_predict(
        features, versao_no_banco.dataset_version_id, core_no_banco["run_id"])
    lote[2]["predicted_value"] = -1

    with pytest.raises(ValueError, match="predicted_value"):
        mc.register_forecasts(ingest_conn, lote)

    assert previsoes_gravadas(ingest_conn, versao_no_banco.dataset_version_id) == []


def test_previsao_para_versao_inexistente_e_rejeitada(ingest_conn, core_no_banco, versao_no_banco):
    features = mc.load_features(ingest_conn)
    lote = mc.mock_neural_model_predict(features, str(uuid.uuid4()), core_no_banco["run_id"])

    with pytest.raises(ValueError, match="inexistente"):
        mc.register_forecasts(ingest_conn, lote)
