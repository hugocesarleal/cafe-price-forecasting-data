"""Transformações causais: janelas, preenchimento, calendário e separação temporal."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src import transformations as tf


def serie(valores, inicio="2020-01-01"):
    return pd.Series(
        [float(v) if v is not None else np.nan for v in valores],
        index=pd.date_range(inicio, periods=len(valores), freq="D"),
    )


def test_grade_diaria_e_continua_e_inclui_as_pontas():
    grade = tf.daily_grid(date(2020, 2, 27), date(2020, 3, 2))

    assert len(grade) == 5  # 2020 é bissexto
    assert grade[0] == pd.Timestamp("2020-02-27") and grade[-1] == pd.Timestamp("2020-03-02")


def test_grade_rejeita_janela_invertida():
    with pytest.raises(ValueError):
        tf.daily_grid(date(2020, 1, 2), date(2020, 1, 1))


def test_ffill_so_propaga_para_frente_e_respeita_o_limite():
    preenchida = tf.causal_ffill(serie([None, 10, None, None, None, 20]), limit=2)

    assert np.isnan(preenchida.iloc[0]), "Nunca preenche para trás"
    assert preenchida.iloc[1:4].tolist() == [10.0, 10.0, 10.0]
    assert np.isnan(preenchida.iloc[4]), "Além do limite a lacuna permanece"
    assert preenchida.iloc[5] == 20.0


def test_soma_movel_inclui_o_dia_corrente_e_nada_depois():
    soma = tf.rolling_sum(serie(range(1, 11)), window=3)

    assert np.isnan(soma.iloc[1])
    assert soma.iloc[2] == 1 + 2 + 3
    assert soma.iloc[9] == 8 + 9 + 10


def test_soma_movel_aceita_janela_parcial_com_min_periods():
    soma = tf.rolling_sum(serie([1, None, 3, 4]), window=3, min_periods=2)

    assert np.isnan(soma.iloc[1])
    assert soma.iloc[2] == 4.0
    assert soma.iloc[3] == 7.0


def test_minimo_movel():
    minimo = tf.rolling_min(serie([5, 3, 8, 9, 7]), window=3)

    assert minimo.iloc[2:].tolist() == [3.0, 3.0, 7.0]


def test_contagem_acima_do_limiar_nao_conta_dia_sem_medicao():
    contagem = tf.rolling_count_above(serie([33, None, 35, 30]), 32.0, window=4, min_periods=3)

    assert np.isnan(contagem.iloc[2]), "Só duas medições reais até aqui"
    assert contagem.iloc[3] == 2.0


def test_defasagem_precisa_ser_positiva():
    with pytest.raises(ValueError):
        tf.lag(serie([1, 2, 3]), 0)
    assert tf.lag(serie([1, 2, 3]), 1).tolist()[1:] == [1.0, 2.0]


def test_log_retorno_e_media_movel():
    precos = serie([100, 110, 121])

    assert tf.log_return(precos).iloc[1] == pytest.approx(np.log(1.1))
    assert tf.rolling_mean(precos, 2).iloc[2] == pytest.approx(115.5)


def test_volatilidade_de_serie_com_retorno_constante_e_zero():
    precos = serie([100 * 1.01 ** i for i in range(30)])

    assert tf.rolling_volatility(precos, 20).iloc[-1] == pytest.approx(0.0, abs=1e-9)


def test_atraso_de_publicacao_desloca_a_serie():
    deslocada = tf.apply_publication_lag(serie([1, 2, 3, 4]), 2)

    assert np.isnan(deslocada.iloc[1])
    assert deslocada.iloc[2:].tolist() == [1.0, 2.0]
    with pytest.raises(ValueError):
        tf.apply_publication_lag(serie([1]), -1)


def test_seno_e_cosseno_saem_juntos_e_formam_um_circulo():
    ciclo = tf.cyclic_year(pd.date_range("2021-01-01", "2021-12-31", freq="D"))

    assert list(ciclo.columns) == ["sin_ano", "cos_ano"]
    assert np.allclose(ciclo["sin_ano"] ** 2 + ciclo["cos_ano"] ** 2, 1.0)
    # Fim e começo do ano ficam vizinhos, o que mês ou dia do ano não garantem.
    assert abs(ciclo["cos_ano"].iloc[0] - ciclo["cos_ano"].iloc[-1]) < 0.01


def test_alvo_e_o_valor_h_dias_a_frente():
    alvo = tf.shift_target(serie(range(10)), 3)

    assert alvo.iloc[0] == 3.0
    assert alvo.iloc[6] == 9.0
    assert alvo.iloc[7:].isna().all(), "Sem futuro conhecido não há alvo"
    with pytest.raises(ValueError):
        tf.shift_target(serie([1, 2]), 0)


# ---------------------------------------------------------------------------
# Separação temporal entre treino, validação e teste
# ---------------------------------------------------------------------------

def test_separacao_e_cronologica_e_sem_sobreposicao():
    quadro = pd.DataFrame({"x": range(100)},
                          index=pd.date_range("2020-01-01", periods=100, freq="D"))

    treino, validacao, teste = tf.temporal_split(quadro, 0.2, 0.1)

    assert (len(treino), len(validacao), len(teste)) == (70, 20, 10)
    assert treino.index.max() < validacao.index.min()
    assert validacao.index.max() < teste.index.min()


def test_embargo_impede_que_o_alvo_do_treino_caia_no_periodo_seguinte():
    quadro = pd.DataFrame({"x": range(400)},
                          index=pd.date_range("2020-01-01", periods=400, freq="D"))
    horizonte = pd.Timedelta(days=90)

    treino, validacao, teste = tf.temporal_split(quadro, 0.25, 0.25, embargo_days=90)

    assert treino.index.max() + horizonte < validacao.index.min()
    assert validacao.index.max() + horizonte < teste.index.min()
    assert len(teste) == 100, "O teste não perde linhas para o embargo"


def test_separacao_rejeita_fracoes_invalidas():
    quadro = pd.DataFrame({"x": range(10)},
                          index=pd.date_range("2020-01-01", periods=10, freq="D"))

    with pytest.raises(ValueError):
        tf.temporal_split(quadro, 0.6, 0.4)
    with pytest.raises(ValueError):
        tf.temporal_split(quadro, embargo_days=-1)
