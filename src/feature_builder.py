"""Construção da matriz de features ``X_t -> y_{t+h}`` a partir de ``core``.

Corresponde ao passo 10 do fluxo descrito em ``docs/architecture.md``:

1. lê ``core.market_daily`` e ``core.weather_daily`` até a data de corte;
2. calcula, numa grade diária contínua, todas as variáveis candidatas;
3. mantém apenas as que o catálogo (``features.variable_catalog``) marca como
   ``KEEP`` — o resto é calculado e descartado, nunca publicado;
4. gera os alvos ``y_{h}d`` para os horizontes de ``FORECAST_HORIZONS``;
5. grava em ``features.model_features`` sob uma versão de dataset já existente.

Garantias contra vazamento temporal:

* nada posterior à data de corte é lido de ``core``;
* toda feature de ``t`` usa apenas observações com data ``<= t`` (janelas
  fechadas em ``t``, preenchimento só para frente e com limite);
* ``preco_arabica`` contemporâneo nunca é feature: só entram estatísticas dele
  defasadas em pelo menos um dia, e ainda assim somente se o catálogo liberar;
* séries divulgadas com atraso são deslocadas por ``PUBLICATION_LAG_DAYS``;
* o alvo de ``t`` vem sempre de um pregão posterior a ``t``.

A criação da versão em ``features.dataset_versions`` fica em
``src/dataset_versioning.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
from psycopg import sql

from src import transformations as tf
from src.config import settings
from src.db import get_connection
from src.logging_config import logger
from src.validation import TARGET_VARIABLE

MARKET_VARIABLES: Sequence[str] = (
    "preco_arabica", "preco_robusta", "usd_brl", "b3_cafe_ajuste", "ice_kc",
)
WEATHER_VARIABLES: Sequence[str] = (
    "temp_min", "temp_max", "temp_media", "precip_mm",
    "umidade_rel", "radiacao_mj", "vento_ms",
)

# Por quantos dias um valor conhecido pode ser carregado para frente. Mercado
# cobre fim de semana e feriado prolongado; clima cobre falhas curtas da fonte.
MARKET_FILL_LIMIT_DAYS = 6
WEATHER_FILL_LIMIT_DAYS = 3

# Atraso, em dias, entre a data de referência e a disponibilidade do dado, por
# variável de core. Uma série com atraso N só entra nas features de t com o
# valor de t-N, que é o que de fato existe quando a linha de t é montada em
# produção; sem isso o treino veria um clima que a previsão nunca terá.
#
# Mercado não tem atraso: no job diário das 00:00 tudo do dia anterior já saiu.
# Clima vem do NASA POWER, que publica dias depois — medido em 07/10/2026, o
# último dia disponível era t-3 para as séries meteorológicas e t-5 para a
# radiação. Revise se a fonte ou a latência mudarem.
PUBLICATION_LAG_DAYS: Dict[str, int] = {
    "temp_min": 3, "temp_max": 3, "temp_media": 3, "precip_mm": 3,
    "umidade_rel": 3, "vento_ms": 3,
    "radiacao_mj": 5,
}

HOT_DAY_THRESHOLD_C = 32.0
PRICE_LAGS_DAYS: Sequence[int] = (1, 7)
PRICE_STAT_WINDOW = 20

# Histórico lido antes do início da janela para que as janelas móveis mais
# longas (90 dias) já estejam completas na primeira linha publicada.
WARMUP_DAYS = 120

SELECAO_KEEP = "KEEP"


@dataclass
class FeatureMatrix:
    """Features e alvos de uma janela, prontos para publicação."""

    frame: pd.DataFrame
    feature_columns: List[str]
    target_columns: List[str]
    start_date: date
    cutoff_date: date
    excluded_candidates: List[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.frame)

    def summary(self) -> Dict:
        return {
            "start_date": self.start_date.isoformat(),
            "cutoff_date": self.cutoff_date.isoformat(),
            "row_count": self.row_count,
            "feature_count": len(self.feature_columns),
            "feature_columns": self.feature_columns,
            "target_columns": self.target_columns,
            "linhas_com_alvo": {c: int(self.frame[c].notna().sum()) for c in self.target_columns},
            "nulos_por_feature": {
                c: int(n) for c, n in self.frame[self.feature_columns].isna().sum().items() if n
            },
            "candidatas_excluidas": self.excluded_candidates,
        }


def target_column(horizon_days: int) -> str:
    return f"y_{horizon_days}d"


# ---------------------------------------------------------------------------
# Cálculo (puro: não toca no banco)
# ---------------------------------------------------------------------------

def _strictly_past(stat: pd.Series, grid: pd.DatetimeIndex) -> pd.Series:
    """Leva uma estatística de pregão para a grade usando só pregões até ``t-1``."""
    na_grade = tf.causal_ffill(tf.to_daily(stat, grid), MARKET_FILL_LIMIT_DAYS)
    return tf.lag(na_grade, 1)


def build_candidates(
    market: pd.DataFrame,
    weather: pd.DataFrame,
    start_date: date,
    cutoff_date: date,
    publication_lag: Optional[Mapping[str, int]] = None,
) -> pd.DataFrame:
    """Calcula todas as variáveis candidatas a feature na grade diária.

    ``market`` é indexado por data com as colunas de ``core.market_daily``;
    ``weather`` traz ``data_ref``, ``region`` e as colunas de
    ``core.weather_daily``. O resultado inclui variáveis que o catálogo descarta:
    a seleção é responsabilidade de ``select_features``.
    """
    atrasos = PUBLICATION_LAG_DAYS if publication_lag is None else publication_lag
    corte = pd.Timestamp(cutoff_date)

    # Nada posterior ao corte participa de cálculo algum.
    market = market[market.index <= corte]
    weather = weather[weather["data_ref"] <= corte]

    grid = tf.daily_grid(start_date - timedelta(days=WARMUP_DAYS), cutoff_date)
    colunas: Dict[str, pd.Series] = {}

    # Mercado: nível do dia, com o último pregão carregado nos dias sem negociação.
    for variavel in MARKET_VARIABLES:
        if variavel == TARGET_VARIABLE or variavel not in market.columns:
            continue
        serie = tf.apply_publication_lag(tf.to_daily(market[variavel], grid), atrasos.get(variavel, 0))
        colunas[variavel] = tf.causal_ffill(serie, MARKET_FILL_LIMIT_DAYS)

    if "preco_robusta" in colunas:
        for dias in PRICE_LAGS_DAYS:
            colunas[f"preco_robusta_lag{dias}"] = tf.lag(colunas["preco_robusta"], dias)

    # Alvo: só entra defasado. As estatísticas são calculadas sobre os pregões e
    # só então deslocadas, para que a linha de t enxergue no máximo t-1.
    pregoes = market[TARGET_VARIABLE].dropna().sort_index()
    janela, minimo = PRICE_STAT_WINDOW, PRICE_STAT_WINDOW // 2
    colunas["ret_1d"] = _strictly_past(tf.log_return(pregoes), grid)
    colunas["vol_20d"] = _strictly_past(tf.rolling_volatility(pregoes, janela, minimo), grid)
    colunas["preco_media_20d"] = _strictly_past(tf.rolling_mean(pregoes, janela, minimo), grid)
    arabica_na_grade = tf.causal_ffill(tf.to_daily(pregoes, grid), MARKET_FILL_LIMIT_DAYS)
    for dias in PRICE_LAGS_DAYS:
        colunas[f"{TARGET_VARIABLE}_lag{dias}"] = tf.lag(arabica_na_grade, dias)

    # Clima, por região: níveis diários e acumulados em janelas fechadas em t.
    for regiao, bloco in weather.groupby("region"):
        bloco = bloco.set_index("data_ref")
        diarias = {
            v: tf.apply_publication_lag(tf.to_daily(bloco[v], grid), atrasos.get(v, 0))
            for v in WEATHER_VARIABLES if v in bloco.columns
        }
        for variavel, serie in diarias.items():
            colunas[f"{variavel}_{regiao}"] = tf.causal_ffill(serie, WEATHER_FILL_LIMIT_DAYS)

        if "precip_mm" in diarias:
            colunas[f"precip_30d_{regiao}"] = tf.rolling_sum(diarias["precip_mm"], 30, 15)
            colunas[f"precip_90d_{regiao}"] = tf.rolling_sum(diarias["precip_mm"], 90, 45)
        if "temp_min" in diarias:
            colunas[f"tmin_min_30d_{regiao}"] = tf.rolling_min(diarias["temp_min"], 30, 15)
        if "temp_max" in diarias:
            colunas[f"dias_quente_30d_{regiao}"] = tf.rolling_count_above(
                diarias["temp_max"], HOT_DAY_THRESHOLD_C, 30, 15)

    candidatas = pd.DataFrame(colunas, index=grid).join(tf.cyclic_year(grid))
    return candidatas.loc[pd.Timestamp(start_date):corte]


def build_targets(
    market: pd.DataFrame,
    start_date: date,
    cutoff_date: date,
    horizons: Sequence[int],
) -> pd.DataFrame:
    """Alvos ``y_{h}d``: preço do arábica ``h`` dias corridos depois de ``t``.

    Quando ``t+h`` cai em dia sem pregão vale o último indicador publicado até
    ``t+h``. O preenchimento é limitado a menos dias que o menor horizonte, o que
    garante que o alvo venha sempre de um pregão posterior a ``t``.
    """
    if not horizons:
        raise ValueError("Nenhum horizonte de previsão configurado.")
    if min(horizons) < 1:
        raise ValueError(f"Horizontes precisam ser > 0: {list(horizons)}")

    corte = pd.Timestamp(cutoff_date)
    grid = tf.daily_grid(start_date, cutoff_date)
    pregoes = market.loc[market.index <= corte, TARGET_VARIABLE].dropna()
    limite = min(MARKET_FILL_LIMIT_DAYS, min(horizons) - 1)
    preco = tf.causal_ffill(tf.to_daily(pregoes, grid), limite)

    return pd.DataFrame(
        {target_column(h): tf.shift_target(preco, h) for h in sorted(horizons)},
        index=grid,
    )


def select_features(candidates: pd.DataFrame, keep_names: Sequence[str]) -> pd.DataFrame:
    """Reduz as candidatas às variáveis liberadas pelo catálogo."""
    proibidas = [n for n in keep_names if n == TARGET_VARIABLE or n.startswith("y_")]
    if proibidas:
        raise ValueError(
            f"Catálogo libera o alvo como feature, o que é vazamento: {proibidas}"
        )
    faltantes = sorted(set(keep_names) - set(candidates.columns))
    if faltantes:
        raise ValueError(
            "Variáveis KEEP do catálogo sem cálculo disponível (região ou série "
            f"ausente em core, ou transformação não implementada): {faltantes}"
        )
    return candidates[sorted(keep_names)]


def assemble_matrix(
    market: pd.DataFrame,
    weather: pd.DataFrame,
    keep_names: Sequence[str],
    start_date: date,
    cutoff_date: date,
    horizons: Optional[Sequence[int]] = None,
    publication_lag: Optional[Mapping[str, int]] = None,
) -> FeatureMatrix:
    """Monta a matriz final (features KEEP + alvos) a partir dos dados de core."""
    horizons = list(horizons if horizons is not None else settings.horizons_list)
    candidatas = build_candidates(market, weather, start_date, cutoff_date, publication_lag)
    features = select_features(candidatas, keep_names)
    alvos = build_targets(market, start_date, cutoff_date, horizons)

    return FeatureMatrix(
        frame=features.join(alvos),
        feature_columns=list(features.columns),
        target_columns=list(alvos.columns),
        start_date=start_date,
        cutoff_date=cutoff_date,
        excluded_candidates=sorted(set(candidatas.columns) - set(features.columns)),
    )


# ---------------------------------------------------------------------------
# Leitura de core e do catálogo
# ---------------------------------------------------------------------------

def load_keep_variables(conn: psycopg.Connection) -> List[str]:
    """Variáveis que o catálogo autoriza na tabela final de features."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT variable_name FROM features.variable_catalog "
            "WHERE selection_status = %s AND is_model_feature ORDER BY variable_name",
            (SELECAO_KEEP,),
        )
        return [r["variable_name"] for r in cur.fetchall()]


def load_core(conn: psycopg.Connection, start_date: date, cutoff_date: date):
    """Lê mercado e clima de ``core`` entre ``start_date`` e ``cutoff_date``."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT data_ref, {} FROM core.market_daily "
                    "WHERE data_ref BETWEEN %s AND %s ORDER BY data_ref")
            .format(sql.SQL(", ").join(map(sql.Identifier, MARKET_VARIABLES))),
            (start_date, cutoff_date),
        )
        mercado = pd.DataFrame(cur.fetchall(), columns=["data_ref", *MARKET_VARIABLES])
        cur.execute(
            sql.SQL("SELECT data_ref, region, {} FROM core.weather_daily "
                    "WHERE data_ref BETWEEN %s AND %s ORDER BY data_ref, region")
            .format(sql.SQL(", ").join(map(sql.Identifier, WEATHER_VARIABLES))),
            (start_date, cutoff_date),
        )
        clima = pd.DataFrame(cur.fetchall(), columns=["data_ref", "region", *WEATHER_VARIABLES])

    mercado["data_ref"] = pd.to_datetime(mercado["data_ref"])
    mercado = mercado.set_index("data_ref").astype(float)
    clima["data_ref"] = pd.to_datetime(clima["data_ref"])
    clima[list(WEATHER_VARIABLES)] = clima[list(WEATHER_VARIABLES)].astype(float)
    return mercado, clima


def _resolve_window(
    conn: psycopg.Connection,
    cutoff_date: Optional[date],
    start_date: Optional[date],
) -> tuple[date, date]:
    hoje = datetime.now(ZoneInfo(settings.TIMEZONE)).date()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT min(data_ref) AS primeiro, max(data_ref) AS ultimo "
            "FROM core.market_daily WHERE preco_arabica IS NOT NULL AND data_ref <= %s",
            (cutoff_date or hoje,),
        )
        limites = cur.fetchone()

    if not limites or limites["ultimo"] is None:
        raise ValueError(
            "core.market_daily não tem preco_arabica até a data de corte; "
            "execute a ingestão antes de construir as features."
        )

    corte = cutoff_date or limites["ultimo"]
    if start_date is None:
        configurado = (pd.Timestamp(corte) - pd.DateOffset(years=settings.HISTORICAL_YEARS)).date()
        start_date = max(configurado, limites["primeiro"])
    if start_date > corte:
        raise ValueError(f"Janela inválida: start_date={start_date} > cutoff_date={corte}")

    dias = (corte - start_date).days + 1
    logger.info(
        "Janela de features: %s a %s (%d dias). Configurado: HISTORICAL_YEARS=%d, "
        "DATASET_MIN_DAYS=%d. Histórico de preco_arabica em core: %s a %s.",
        start_date, corte, dias, settings.HISTORICAL_YEARS, settings.DATASET_MIN_DAYS,
        limites["primeiro"], limites["ultimo"],
    )
    if dias < settings.DATASET_MIN_DAYS:
        logger.warning(
            "Janela de %d dias é menor que o mínimo de segurança DATASET_MIN_DAYS=%d.",
            dias, settings.DATASET_MIN_DAYS,
        )
    return start_date, corte


def build_feature_matrix(
    conn: psycopg.Connection,
    cutoff_date: Optional[date] = None,
    start_date: Optional[date] = None,
    horizons: Optional[Sequence[int]] = None,
) -> FeatureMatrix:
    """Constrói a matriz de features a partir do que está publicado em ``core``.

    Sem ``cutoff_date``, o corte é o último dia com ``preco_arabica``. Sem
    ``start_date``, a janela recua ``HISTORICAL_YEARS`` anos ou até o primeiro
    preço disponível, o que vier depois.
    """
    start_date, cutoff_date = _resolve_window(conn, cutoff_date, start_date)
    mercado, clima = load_core(
        conn, start_date - timedelta(days=WARMUP_DAYS), cutoff_date)
    matriz = assemble_matrix(
        mercado, clima, load_keep_variables(conn), start_date, cutoff_date, horizons)

    logger.info(
        "Matriz de features: %d linhas, %d features, alvos %s; %d candidatas excluídas "
        "pelo catálogo.",
        matriz.row_count, len(matriz.feature_columns), matriz.target_columns,
        len(matriz.excluded_candidates),
    )
    return matriz


# ---------------------------------------------------------------------------
# Publicação
# ---------------------------------------------------------------------------

CONTROL_COLUMNS = frozenset({"id", "dataset_version_id", "data_ref", "is_pruned", "created_at"})


def column_scales(conn: psycopg.Connection, matrix: Optional[FeatureMatrix] = None) -> Dict[str, int]:
    """Casas decimais de cada coluna de dados de ``features.model_features``.

    Com ``matrix``, confere antes que toda coluna dela exista na tabela.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, numeric_scale FROM information_schema.columns "
            "WHERE table_schema = 'features' AND table_name = 'model_features'"
        )
        escalas = {
            r["column_name"]: r["numeric_scale"] for r in cur.fetchall()
            if r["column_name"] not in CONTROL_COLUMNS
        }

    if matrix is not None:
        sem_coluna = [c for c in matrix.feature_columns + matrix.target_columns
                      if c not in escalas]
        if sem_coluna:
            raise ValueError(
                "features.model_features não tem coluna para "
                f"{sem_coluna}; crie uma migration antes de liberá-las no catálogo."
            )
    return escalas


def storage_frame(matrix: FeatureMatrix, scales: Mapping[str, int]) -> pd.DataFrame:
    """A matriz como será gravada: arredondada na escala de cada coluna.

    É a única representação usada para gravar e para calcular o checksum da
    versão, de modo que o que está no banco seja sempre o que foi assinado.
    """
    colunas = matrix.feature_columns + matrix.target_columns
    arredondada = matrix.frame[colunas].round(
        {c: scales[c] for c in colunas if scales.get(c) is not None})
    # Soma zero para que -0.0 vire 0.0, como o NUMERIC do banco devolve.
    return arredondada + 0.0


def publish_features(
    conn: psycopg.Connection,
    dataset_version_id: str,
    matrix: FeatureMatrix,
    scales: Optional[Mapping[str, int]] = None,
) -> int:
    """Grava a matriz em ``features.model_features`` sob uma versão existente.

    Devolve quantas linhas foram inseridas. Linhas já gravadas para a mesma
    versão e data são mantidas como estão (a versão é imutável), então repetir a
    chamada não duplica nada. Não faz commit: quem cria a versão decide a
    transação.
    """
    valores = storage_frame(matrix, scales or column_scales(conn, matrix))
    colunas = list(valores.columns)
    valores = valores.astype(object).where(valores.notna(), None)

    insert = sql.SQL(
        "INSERT INTO features.model_features (dataset_version_id, data_ref, {}) "
        "VALUES ({}) ON CONFLICT (dataset_version_id, data_ref) DO NOTHING"
    ).format(
        sql.SQL(", ").join(map(sql.Identifier, colunas)),
        sql.SQL(", ").join(sql.Placeholder() * (len(colunas) + 2)),
    )
    params = [
        (dataset_version_id, data_ref.date(), *linha)
        for data_ref, linha in zip(valores.index, valores.itertuples(index=False, name=None))
    ]
    with conn.cursor() as cur:
        cur.executemany(insert, params)
        inseridas = cur.rowcount

    logger.info("features.model_features: %d linhas gravadas na versão %s.",
                inseridas, dataset_version_id)
    return inseridas


if __name__ == "__main__":
    with get_connection() as conexao:
        resumo = build_feature_matrix(conexao).summary()
    print(json.dumps(resumo, ensure_ascii=False, indent=2))
