"""Teste Obrigatório 16: ausência de vazamento temporal.

Para todo dia ``t`` e horizonte ``h``, ``X_t`` só pode depender de dados com
data ``<= t`` e ``y_{t+h}`` só de preços posteriores a ``t``. A prova é por
perturbação: altera-se o futuro e verifica-se que o passado não se mexe.

Os testes rodam sobre um core sintético em memória; não precisam de banco.
"""

from datetime import timedelta

import pandas as pd
import pytest

from conftest import DATA_BASE, KEEP_ESPERADAS, core_sintetico
from src.feature_builder import (
    MARKET_FILL_LIMIT_DAYS,
    WARMUP_DAYS,
    assemble_matrix,
    build_candidates,
    build_targets,
    select_features,
)

DIAS = 400
INICIO = DATA_BASE + timedelta(days=WARMUP_DAYS)
CORTE = DATA_BASE + timedelta(days=DIAS - 1)
MEIO = DATA_BASE + timedelta(days=260)  # uma quarta-feira
HORIZONTES = (7, 15, 30, 90)


@pytest.fixture(scope="module")
def core():
    return core_sintetico(DIAS)


def perturbar_depois_de(mercado, clima, data):
    """Devolve cópias com todo valor posterior a ``data`` trocado."""
    limite = pd.Timestamp(data)
    mercado, clima = mercado.copy(), clima.copy()
    mercado.loc[mercado.index > limite] = mercado.loc[mercado.index > limite] * 3 + 7
    numericas = clima.columns.difference(["data_ref", "region"])
    futuro = clima["data_ref"] > limite
    clima.loc[futuro, numericas] = clima.loc[futuro, numericas] * 0.5 + 1
    return mercado, clima


def test_alterar_o_futuro_nao_muda_nenhuma_feature_do_passado(core):
    mercado, clima = core
    original = build_candidates(mercado, clima, INICIO, CORTE)
    alterado = build_candidates(*perturbar_depois_de(mercado, clima, MEIO), INICIO, CORTE)

    ate_meio = slice(None, pd.Timestamp(MEIO))
    pd.testing.assert_frame_equal(original.loc[ate_meio], alterado.loc[ate_meio])
    # O teste só prova algo se a perturbação de fato alcançar as features.
    assert not original.loc[pd.Timestamp(MEIO) + pd.Timedelta(days=1):].equals(
        alterado.loc[pd.Timestamp(MEIO) + pd.Timedelta(days=1):])


def test_construir_com_corte_anterior_reproduz_as_mesmas_features(core):
    mercado, clima = core
    completo = build_candidates(mercado, clima, INICIO, CORTE)
    truncado = build_candidates(mercado, clima, INICIO, MEIO)

    pd.testing.assert_frame_equal(completo.loc[:pd.Timestamp(MEIO)], truncado)


def test_dados_posteriores_ao_corte_sao_ignorados(core):
    mercado, clima = core
    limpo = assemble_matrix(mercado, clima, KEEP_ESPERADAS, INICIO, MEIO, HORIZONTES)
    sujo = assemble_matrix(*perturbar_depois_de(mercado, clima, MEIO),
                           KEEP_ESPERADAS, INICIO, MEIO, HORIZONTES)

    pd.testing.assert_frame_equal(limpo.frame, sujo.frame)
    assert limpo.frame.index.max() == pd.Timestamp(MEIO)


def test_preco_arabica_do_dia_nao_entra_nas_features_do_proprio_dia(core):
    mercado, clima = core
    dia = pd.Timestamp(MEIO)
    assert dia in mercado.index
    alterado = mercado.copy()
    alterado.loc[dia, "preco_arabica"] += 500

    original = build_candidates(mercado, clima, INICIO, CORTE)
    com_choque = build_candidates(alterado, clima, INICIO, CORTE)

    pd.testing.assert_series_equal(original.loc[dia], com_choque.loc[dia])
    # No dia seguinte o choque já é passado e pode aparecer como defasagem.
    seguinte = dia + pd.Timedelta(days=1)
    assert com_choque.loc[seguinte, "preco_arabica_lag1"] == pytest.approx(
        original.loc[seguinte, "preco_arabica_lag1"] + 500)


def test_alvo_nunca_e_coluna_de_feature(core):
    mercado, clima = core
    candidatas = build_candidates(mercado, clima, INICIO, CORTE)
    matriz = assemble_matrix(mercado, clima, KEEP_ESPERADAS, INICIO, CORTE, HORIZONTES)

    assert "preco_arabica" not in candidatas.columns
    assert "preco_arabica" not in matriz.feature_columns
    assert not set(matriz.feature_columns) & set(matriz.target_columns)
    with pytest.raises(ValueError, match="vazamento"):
        select_features(candidatas, [*KEEP_ESPERADAS, "preco_arabica"])
    with pytest.raises(ValueError, match="vazamento"):
        select_features(candidatas, [*KEEP_ESPERADAS, "y_7d"])


def test_alvo_depende_apenas_de_precos_posteriores_a_t(core):
    mercado, clima = core
    alterado = mercado.copy()
    passado = alterado.index <= pd.Timestamp(MEIO)
    alterado.loc[passado, "preco_arabica"] += 500

    original = build_targets(mercado, INICIO, CORTE, HORIZONTES)
    com_choque = build_targets(alterado, INICIO, CORTE, HORIZONTES)

    desde_meio = slice(pd.Timestamp(MEIO), None)
    pd.testing.assert_frame_equal(original.loc[desde_meio], com_choque.loc[desde_meio])


def test_limite_de_preenchimento_e_menor_que_o_menor_horizonte():
    # É o que garante que um alvo preenchido nunca recue até o próprio dia t.
    assert MARKET_FILL_LIMIT_DAYS < min(HORIZONTES)


def test_fonte_publicada_com_atraso_so_aparece_depois(core):
    mercado, clima = core
    sem_atraso = build_candidates(mercado, clima, INICIO, CORTE)
    com_atraso = build_candidates(mercado, clima, INICIO, CORTE,
                                  publication_lag={"usd_brl": 2, "precip_mm": 2})

    dia = pd.Timestamp(MEIO)
    antes = dia - pd.Timedelta(days=2)
    assert com_atraso.loc[dia, "usd_brl"] == sem_atraso.loc[antes, "usd_brl"]
    assert com_atraso.loc[dia, "precip_30d_sulmg"] == pytest.approx(
        sem_atraso.loc[antes, "precip_30d_sulmg"])
    # Variáveis sem atraso declarado não são afetadas.
    assert com_atraso.loc[dia, "ice_kc"] == sem_atraso.loc[dia, "ice_kc"]


def test_janelas_moveis_fecham_em_t(core):
    mercado, clima = core
    candidatas = build_candidates(mercado, clima, INICIO, CORTE)
    dia = pd.Timestamp(MEIO)

    sulmg = clima[clima["region"] == "sulmg"].set_index("data_ref")
    janela_30 = sulmg.loc[dia - pd.Timedelta(days=29):dia]
    janela_90 = sulmg.loc[dia - pd.Timedelta(days=89):dia]

    assert len(janela_30) == 30 and len(janela_90) == 90
    assert candidatas.loc[dia, "precip_30d_sulmg"] == pytest.approx(janela_30["precip_mm"].sum())
    assert candidatas.loc[dia, "precip_90d_sulmg"] == pytest.approx(janela_90["precip_mm"].sum())
    assert candidatas.loc[dia, "tmin_min_30d_sulmg"] == janela_30["temp_min"].min()
    assert candidatas.loc[dia, "dias_quente_30d_sulmg"] == (janela_30["temp_max"] > 32).sum()
