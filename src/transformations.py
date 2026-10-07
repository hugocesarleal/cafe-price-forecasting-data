"""Transformações causais usadas na construção das features.

Todas as funções operam sobre séries ordenadas por data (``DatetimeIndex``) e
obedecem à mesma regra: o valor calculado para o dia ``t`` depende apenas de
observações com data ``<= t``.

A única exceção deliberada é ``shift_target``, que olha para frente para montar
os alvos ``y_{t+h}`` — por isso alvos nunca podem ser usados como entrada.

As janelas (``rolling_*``, ``log_return``) são fechadas na observação corrente:
incluem ``t`` e nada depois. Isso basta para clima e para as demais séries de
mercado. Sobre ``preco_arabica`` não basta — o valor contemporâneo do alvo não
pode entrar em ``X_t`` — então qualquer estatística dele precisa passar por
``lag(..., 1)`` antes de virar feature.
"""

from __future__ import annotations

from datetime import date
from typing import Optional, Tuple

import numpy as np
import pandas as pd

DIAS_NO_ANO = 365.25
DIAS_UTEIS_ANO = 252


def daily_grid(start: date, end: date) -> pd.DatetimeIndex:
    """Grade diária contínua (dias corridos) de ``start`` a ``end``, inclusive."""
    if start > end:
        raise ValueError(f"Janela inválida: start={start} > end={end}")
    return pd.date_range(start, end, freq="D", name="data_ref")


def to_daily(series: pd.Series, grid: pd.DatetimeIndex) -> pd.Series:
    """Reposiciona a série na grade sem preencher lacunas."""
    series = series[~series.index.duplicated(keep="last")].sort_index()
    return series.reindex(grid)


def causal_ffill(series: pd.Series, limit: Optional[int] = None) -> pd.Series:
    """Propaga o último valor conhecido para frente, nunca para trás.

    ``limit`` é o número máximo de dias que um valor pode ser carregado; além
    disso a lacuna permanece NaN em vez de virar um dado velho disfarçado.
    """
    if limit is not None and limit <= 0:
        return series.copy()
    return series.ffill(limit=limit)


def apply_publication_lag(series: pd.Series, lag_days: int) -> pd.Series:
    """Desloca a série para a data em que o valor ficou disponível.

    Uma observação referente a ``t`` mas divulgada só em ``t + lag_days`` não
    pode aparecer em ``X_t``.
    """
    if lag_days < 0:
        raise ValueError(f"lag_days não pode ser negativo: {lag_days}")
    return series.shift(lag_days) if lag_days else series


# ---------------------------------------------------------------------------
# Janelas fechadas em t
# ---------------------------------------------------------------------------

def rolling_sum(series: pd.Series, window: int, min_periods: Optional[int] = None) -> pd.Series:
    """Soma dos últimos ``window`` dias, incluindo ``t``."""
    return series.rolling(window, min_periods=min_periods or window).sum()


def rolling_min(series: pd.Series, window: int, min_periods: Optional[int] = None) -> pd.Series:
    """Mínimo dos últimos ``window`` dias, incluindo ``t``."""
    return series.rolling(window, min_periods=min_periods or window).min()


def rolling_count_above(
    series: pd.Series,
    threshold: float,
    window: int,
    min_periods: Optional[int] = None,
) -> pd.Series:
    """Quantidade de dias com valor acima de ``threshold`` na janela até ``t``.

    Dias sem observação continuam ausentes (não contam como "abaixo"), para que
    ``min_periods`` reflita medições reais.
    """
    acima = (series > threshold).astype(float).where(series.notna())
    return acima.rolling(window, min_periods=min_periods or window).sum()


# ---------------------------------------------------------------------------
# Defasagens e estatísticas de preço
# ---------------------------------------------------------------------------

def lag(series: pd.Series, periods: int) -> pd.Series:
    """Valor de ``periods`` posições atrás. ``periods`` precisa ser >= 1."""
    if periods < 1:
        raise ValueError(f"Defasagem precisa ser >= 1 para ser causal: {periods}")
    return series.shift(periods)


def log_return(series: pd.Series, periods: int = 1) -> pd.Series:
    """Log-retorno entre a observação corrente e a de ``periods`` posições atrás."""
    return np.log(series / lag(series, periods))


def rolling_mean(series: pd.Series, window: int, min_periods: Optional[int] = None) -> pd.Series:
    """Média das últimas ``window`` observações, incluindo a corrente."""
    return series.rolling(window, min_periods=min_periods or window).mean()


def rolling_volatility(series: pd.Series, window: int, min_periods: Optional[int] = None) -> pd.Series:
    """Volatilidade anualizada dos log-retornos nas últimas ``window`` observações.

    Espera uma série de pregões (sem fins de semana preenchidos), senão os
    retornos nulos dos dias sem negociação achatam o desvio.
    """
    desvio = log_return(series).rolling(window, min_periods=min_periods or window).std()
    return desvio * np.sqrt(DIAS_UTEIS_ANO)


# ---------------------------------------------------------------------------
# Calendário e alvos
# ---------------------------------------------------------------------------

def cyclic_year(index: pd.DatetimeIndex) -> pd.DataFrame:
    """Codificação cíclica do dia do ano. Seno e cosseno saem sempre em par."""
    angulo = 2 * np.pi * index.dayofyear / DIAS_NO_ANO
    return pd.DataFrame({"sin_ano": np.sin(angulo), "cos_ano": np.cos(angulo)}, index=index)


def shift_target(series: pd.Series, horizon_days: int) -> pd.Series:
    """Alvo ``y_{t+h}``: o valor da série ``horizon_days`` dias corridos à frente.

    Fica NaN nos últimos ``horizon_days`` dias, onde o futuro ainda não existe.
    """
    if horizon_days < 1:
        raise ValueError(f"Horizonte precisa ser > 0: {horizon_days}")
    return series.shift(-horizon_days)


# ---------------------------------------------------------------------------
# Separação temporal
# ---------------------------------------------------------------------------

def temporal_split(
    frame: pd.DataFrame,
    val_fraction: float = 0.15,
    test_fraction: float = 0.15,
    embargo_days: int = 0,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Divide em treino, validação e teste em ordem cronológica, sem embaralhar.

    ``embargo_days`` descarta os últimos dias do treino e da validação. Deve ser
    o maior horizonte de previsão: o alvo de uma linha em ``t`` é o preço de
    ``t+h``, então sem o embargo o treino conheceria preços do período seguinte.
    """
    if not 0 <= val_fraction < 1 or not 0 <= test_fraction < 1:
        raise ValueError("Frações de validação e teste devem estar em [0, 1).")
    if val_fraction + test_fraction >= 1:
        raise ValueError("Validação + teste não podem consumir a série inteira.")
    if embargo_days < 0:
        raise ValueError(f"embargo_days não pode ser negativo: {embargo_days}")

    frame = frame.sort_index()
    total = len(frame)
    n_test = int(round(total * test_fraction))
    n_val = int(round(total * val_fraction))
    n_train = total - n_val - n_test

    train = frame.iloc[:n_train]
    val = frame.iloc[n_train:n_train + n_val]
    test = frame.iloc[n_train + n_val:]

    if embargo_days:
        corte = pd.Timedelta(days=embargo_days)
        if len(val):
            train = train[train.index <= val.index[0] - corte - pd.Timedelta(days=1)]
        elif len(test):
            train = train[train.index <= test.index[0] - corte - pd.Timedelta(days=1)]
        if len(val) and len(test):
            val = val[val.index <= test.index[0] - corte - pd.Timedelta(days=1)]

    return train, val, test
