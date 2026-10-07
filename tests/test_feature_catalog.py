"""Testes Obrigatórios 14, 15 e 17: catálogo de variáveis e alvos.

* 14 — a matriz final contém somente variáveis KEEP e os alvos;
* 15 — variáveis DROP, KEEP_WITH_CAVEAT e TEST_ONLY ficam fora da tabela final;
* 17 — os alvos de 7, 15, 30 e 90 dias são gerados e são parametrizáveis.

A primeira parte roda em memória. A última seção exige PostgreSQL e usa o job
``teste_ingestao`` e versões ``teste-*`` como escopo, apagados ao final.
"""

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from conftest import (
    DATA_BASE,
    KEEP_ESPERADAS,
    StubAgrobrClient,
    arquivo_stub,
    core_sintetico,
    hash_stub,
    linha_clima,
    linha_mercado,
)
from src.agrobr_client import UNITS
from src.feature_builder import (
    WARMUP_DAYS,
    assemble_matrix,
    build_candidates,
    build_feature_matrix,
    build_targets,
    load_keep_variables,
    publish_features,
)

DIAS = 400
INICIO = DATA_BASE + timedelta(days=WARMUP_DAYS)
CORTE = DATA_BASE + timedelta(days=DIAS - 1)
HORIZONTES = (7, 15, 30, 90)

COLUNAS_DE_CONTROLE = {"id", "dataset_version_id", "data_ref", "is_pruned", "created_at"}


@pytest.fixture(scope="module")
def core():
    return core_sintetico(DIAS)


@pytest.fixture(scope="module")
def matriz(core):
    return assemble_matrix(*core, KEEP_ESPERADAS, INICIO, CORTE, HORIZONTES)


# ---------------------------------------------------------------------------
# Teste Obrigatório 14 — somente KEEP e TARGET
# ---------------------------------------------------------------------------

def test_matriz_contem_exatamente_as_variaveis_keep_e_os_alvos(matriz):
    assert set(matriz.feature_columns) == set(KEEP_ESPERADAS)
    assert matriz.target_columns == ["y_7d", "y_15d", "y_30d", "y_90d"]
    assert set(matriz.frame.columns) == set(KEEP_ESPERADAS) | set(matriz.target_columns)


def test_matriz_cobre_a_grade_diaria_continua(matriz):
    assert matriz.row_count == (CORTE - INICIO).days + 1
    assert matriz.frame.index[0] == pd.Timestamp(INICIO)
    assert matriz.frame.index[-1] == pd.Timestamp(CORTE)
    assert (matriz.frame.index.to_series().diff().dropna() == pd.Timedelta(days=1)).all()


def test_features_keep_ficam_preenchidas_depois_do_aquecimento(matriz):
    # O histórico anterior à janela já completou as janelas de 90 dias.
    assert not matriz.frame[matriz.feature_columns].isna().any().any()


def test_sin_ano_e_cos_ano_entram_sempre_em_par(matriz):
    assert {"sin_ano", "cos_ano"} <= set(matriz.feature_columns)
    assert np.allclose(matriz.frame["sin_ano"] ** 2 + matriz.frame["cos_ano"] ** 2, 1.0)


def test_mercado_em_dia_sem_pregao_repete_o_ultimo_fechamento(core, matriz):
    mercado, _ = core
    sabado = next(d for d in matriz.frame.index if d.dayofweek == 5)
    sexta = sabado - pd.Timedelta(days=1)

    assert matriz.frame.loc[sabado, "usd_brl"] == mercado.loc[sexta, "usd_brl"]


# ---------------------------------------------------------------------------
# Teste Obrigatório 15 — DROP fora da tabela final
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("descartada", [
    "ret_1d", "vol_20d", "preco_media_20d",          # DROP no catálogo
    "vento_ms_sulmg", "umidade_rel_bambui",          # DROP no catálogo
    "precip_30d_bambui", "tmin_min_30d_bambui",      # DROP no catálogo
    "temp_media_sulmg", "preco_arabica_lag1",        # fora do catálogo
])
def test_variavel_nao_liberada_e_calculada_mas_nao_publicada(core, matriz, descartada):
    candidatas = build_candidates(*core, INICIO, CORTE)

    assert descartada in candidatas.columns, "Precisa existir para o filtro ter o que barrar"
    assert descartada not in matriz.frame.columns
    assert descartada in matriz.excluded_candidates


def test_variavel_keep_sem_calculo_interrompe_a_construcao(core):
    with pytest.raises(ValueError, match="variavel_inexistente"):
        assemble_matrix(*core, [*KEEP_ESPERADAS, "variavel_inexistente"],
                        INICIO, CORTE, HORIZONTES)


def test_regiao_ausente_em_core_nao_gera_coluna_vazia(core):
    mercado, clima = core
    with pytest.raises(ValueError, match="sulmg"):
        assemble_matrix(mercado, clima[clima["region"] != "sulmg"],
                        KEEP_ESPERADAS, INICIO, CORTE, HORIZONTES)


# ---------------------------------------------------------------------------
# Teste Obrigatório 17 — alvos de 7, 15, 30 e 90 dias
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("horizonte", HORIZONTES)
def test_alvo_e_o_preco_do_arabica_h_dias_depois(core, matriz, horizonte):
    mercado, _ = core
    coluna = f"y_{horizonte}d"
    # Um dia cujo t+h é dia útil, para comparar com o pregão exato.
    dia = next(d for d in matriz.frame.index
               if (d + pd.Timedelta(days=horizonte)) in mercado.index)

    assert matriz.frame.loc[dia, coluna] == mercado.loc[
        dia + pd.Timedelta(days=horizonte), "preco_arabica"]


@pytest.mark.parametrize("horizonte", HORIZONTES)
def test_alvo_fica_nulo_quando_o_futuro_ainda_nao_existe(matriz, horizonte):
    coluna = matriz.frame[f"y_{horizonte}d"]

    assert coluna.iloc[-horizonte:].isna().all()
    assert coluna.iloc[:-horizonte].notna().all()


def test_alvo_em_dia_sem_pregao_usa_o_ultimo_indicador_publicado(core):
    mercado, _ = core
    alvos = build_targets(mercado, INICIO, CORTE, [7])
    sabado = next(d for d in alvos.index if d.dayofweek == 5)
    sexta_seguinte = sabado + pd.Timedelta(days=6)

    assert alvos.loc[sabado, "y_7d"] == mercado.loc[sexta_seguinte, "preco_arabica"]


def test_horizontes_sao_parametrizaveis(core):
    alvos = build_targets(core[0], INICIO, CORTE, [10, 3])

    assert list(alvos.columns) == ["y_3d", "y_10d"]
    with pytest.raises(ValueError):
        build_targets(core[0], INICIO, CORTE, [])
    with pytest.raises(ValueError):
        build_targets(core[0], INICIO, CORTE, [0, 7])


# ---------------------------------------------------------------------------
# Com PostgreSQL: catálogo real, leitura de core e publicação
# ---------------------------------------------------------------------------

def colunas_model_features(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'features' AND table_name = 'model_features'"
        )
        return {r["column_name"] for r in cur.fetchall()}


def test_catalogo_semeado_libera_as_variaveis_esperadas(db_conn):
    assert set(load_keep_variables(db_conn)) == set(KEEP_ESPERADAS)


def test_tabela_final_so_tem_colunas_keep_ou_alvo(db_conn):
    colunas = colunas_model_features(db_conn) - COLUNAS_DE_CONTROLE
    alvos = {c for c in colunas if c.startswith("y_")}

    assert colunas - alvos == set(load_keep_variables(db_conn))
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT variable_name FROM features.variable_catalog "
            "WHERE selection_status <> 'KEEP'"
        )
        nao_liberadas = {r["variable_name"] for r in cur.fetchall()}
    assert not colunas & nao_liberadas


def arquivos_do_core_sintetico(dias):
    """O core sintético no formato de coleta, para passar pela ingestão real."""
    mercado, clima = core_sintetico(dias)
    linhas_mercado = [
        linha_mercado(data.date(), variavel, float(valor), UNITS[variavel])
        for variavel in mercado.columns
        for data, valor in mercado[variavel].items()
    ]
    variaveis_clima = [c for c in clima.columns if c not in ("data_ref", "region")]
    linhas_clima = [
        linha_clima(linha.data_ref.date(), linha.region, variavel,
                    float(getattr(linha, variavel)), UNITS[variavel])
        for linha in clima.itertuples(index=False)
        for variavel in variaveis_clima
    ]
    return [arquivo_stub(content_hash=hash_stub("core-sintetico"),
                         market_rows=linhas_mercado, weather_rows=linhas_clima)]


def test_features_construidas_de_core_sao_publicadas_sem_duplicar(
    ingest_conn, executar_pipeline, versao_teste
):
    dias = 260
    inicio = DATA_BASE + timedelta(days=WARMUP_DAYS)
    corte = DATA_BASE + timedelta(days=dias - 1)
    carga = executar_pipeline(StubAgrobrClient(arquivos_do_core_sintetico(dias)))
    assert carga["status"] == "SUCCESS"

    matriz = build_feature_matrix(ingest_conn, cutoff_date=corte, start_date=inicio)
    primeira = publish_features(ingest_conn, versao_teste, matriz)
    segunda = publish_features(ingest_conn, versao_teste, matriz)
    ingest_conn.commit()

    assert set(matriz.feature_columns) == set(KEEP_ESPERADAS)
    assert primeira == matriz.row_count == dias - WARMUP_DAYS
    assert segunda == 0, "Republicar a mesma versão não pode duplicar linhas"

    with ingest_conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM features.model_features "
            "WHERE dataset_version_id = %s ORDER BY data_ref",
            (versao_teste,),
        )
        gravadas = cur.fetchall()

    assert len(gravadas) == matriz.row_count
    assert all(linha[c] is not None for linha in gravadas for c in KEEP_ESPERADAS)
    assert all(not linha["is_pruned"] for linha in gravadas)

    ultima = matriz.frame.iloc[-1]
    assert float(gravadas[-1]["precip_90d_cerrado"]) == pytest.approx(
        ultima["precip_90d_cerrado"], abs=1e-3)
    assert gravadas[-1]["y_7d"] is None
    assert float(gravadas[0]["y_7d"]) == pytest.approx(matriz.frame["y_7d"].iloc[0], abs=1e-2)
