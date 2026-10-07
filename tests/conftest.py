"""Fixtures compartilhadas para suite de testes pytest."""

import contextlib
import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

from src.agrobr_client import AgrobrClient, SourceFetchError, SourceFile
from src.db import get_connection
from src.migrator import apply_migrations


@pytest.fixture(scope="session")
def db_conn():
    """Conexão compartilhada de teste para a sessão."""
    conn = get_connection()
    # Garante que migrations estejam aplicadas
    apply_migrations(conn=conn)
    yield conn
    conn.close()


@pytest.fixture
def cursor(db_conn):
    """Cursor transacional para testes individuais com rollback garantido."""
    with db_conn.cursor() as cur:
        yield cur
    db_conn.rollback()


# ---------------------------------------------------------------------------
# Ingestão: dados de teste isolados do histórico real
# ---------------------------------------------------------------------------

# Nome de job exclusivo dos testes — é a chave usada para localizar e apagar o
# rastro deixado por eles, sem tocar em execuções reais do pipeline.
JOB_NAME_TESTES = "teste_ingestao"

# Janeiro de 1990: anterior a qualquer série real (os CSVs começam em 1996),
# então o upsert em core nunca sobrescreve dados pré-existentes.
DATA_BASE = date(1990, 1, 1)

# raw.ingestion_files.file_hash é CHAR(64): hashes mais curtos são completados
# com espaços e nunca coincidiriam com o valor recalculado na coleta seguinte.
HASH_STUB = "a" * 64


def hash_stub(variante: str) -> str:
    """Hash SHA-256 de 64 caracteres para simular conteúdo de origem."""
    return hashlib.sha256(variante.encode("utf-8")).hexdigest()


_TABELAS_COM_RUN = (
    "predictions.forecasts",
    "audit.model_runs",
    "raw.market_observations",
    "raw.weather_observations",
    "staging.market_observations",
    "staging.weather_observations",
    "core.market_daily",
    "core.weather_daily",
    "audit.data_quality_checks",
    "raw.ingestion_files",
)


def linha_mercado(
    obs_date: date,
    variable_name: str,
    value,
    unit: str,
    source: str = "STUB-TESTE",
) -> Dict:
    return {
        "observation_date": obs_date,
        "region": None,
        "variable_name": variable_name,
        "value": value,
        "unit": unit,
        "source": source,
    }


def linha_clima(
    obs_date: date,
    region: str,
    variable_name: str,
    value,
    unit: str,
    source: str = "STUB-TESTE",
) -> Dict:
    return {
        "observation_date": obs_date,
        "region": region,
        "variable_name": variable_name,
        "value": value,
        "unit": unit,
        "source": source,
    }


def datas(dias: int) -> List[date]:
    """``dias`` datas consecutivas a partir de DATA_BASE."""
    return [DATA_BASE + timedelta(days=d) for d in range(dias)]


def contar_do_teste(conn, tabela: str) -> int:
    """Conta linhas de ``tabela`` produzidas por execuções de teste.

    Todas as tabelas de dados carregam ``pipeline_run_id``, então o escopo vem da
    auditoria e não depende de datas — o banco pode conter cargas reais.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) AS n FROM {tabela} "
            "WHERE pipeline_run_id IN "
            "(SELECT run_id FROM audit.pipeline_runs WHERE job_name = %s)",
            (JOB_NAME_TESTES,),
        )
        return cur.fetchone()["n"]


def arquivo_stub(
    dias: int = 5,
    content_hash: str = HASH_STUB,
    market_rows: Optional[List[Dict]] = None,
    weather_rows: Optional[List[Dict]] = None,
    source_name: str = "CEPEA/ESALQ",
    file_name: str = "stub_mercado.csv",
) -> SourceFile:
    """Monta um SourceFile determinístico cobrindo ``dias`` dias a partir de DATA_BASE."""
    serie = datas(dias)

    if market_rows is None:
        market_rows = [
            linha_mercado(d, "preco_arabica", 1200.0 + i, "R$/sc 60kg")
            for i, d in enumerate(serie)
        ]
    if weather_rows is None:
        weather_rows = [
            linha_clima(d, "bambui", "temp_min", 15.0 + i, "°C") for i, d in enumerate(serie)
        ]

    return SourceFile(
        source_name=source_name,
        file_name=file_name,
        content_hash=content_hash,
        collected_at=datetime.now(timezone.utc),
        source_url="https://exemplo.invalido/stub",
        git_commit="0" * 40,
        market_rows=market_rows,
        weather_rows=weather_rows,
    )


class StubAgrobrClient(AgrobrClient):
    """Cliente de teste: devolve arquivos fixos e pode falhar nas primeiras tentativas."""

    def __init__(
        self,
        arquivos: Optional[List[SourceFile]] = None,
        falhas_iniciais: int = 0,
        falhar_sempre: bool = False,
    ) -> None:
        self.arquivos = arquivos if arquivos is not None else [arquivo_stub()]
        self.falhas_iniciais = falhas_iniciais
        self.falhar_sempre = falhar_sempre
        self.tentativas = 0

    def _fetch(self) -> List[SourceFile]:
        self.tentativas += 1
        if self.falhar_sempre or self.tentativas <= self.falhas_iniciais:
            raise SourceFetchError(
                f"Falha simulada de coleta (tentativa {self.tentativas})"
            )
        return self.arquivos


def purge_test_runs(conn) -> int:
    """Apaga todo o rastro deixado por execuções de teste. Devolve quantas removeu."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run_id::text AS run_id FROM audit.pipeline_runs WHERE job_name = %s",
            (JOB_NAME_TESTES,),
        )
        run_ids = [r["run_id"] for r in cur.fetchall()]

        if run_ids:
            for tabela in _TABELAS_COM_RUN:
                cur.execute(
                    f"DELETE FROM {tabela} WHERE pipeline_run_id = ANY(%s::uuid[])",
                    (run_ids,),
                )
            cur.execute(
                "DELETE FROM audit.pending_events WHERE payload->>'run_id' = ANY(%s::text[])",
                (run_ids,),
            )

        # Checagens sem run_id só podem vir de testes: em produção record_checks
        # é sempre chamado com o run_id da execução.
        cur.execute("DELETE FROM audit.data_quality_checks WHERE pipeline_run_id IS NULL")

        if run_ids:
            cur.execute(
                "DELETE FROM audit.pipeline_runs WHERE run_id = ANY(%s::uuid[])",
                (run_ids,),
            )
    conn.commit()
    return len(run_ids)


@pytest.fixture
def ingest_conn():
    """Conexão dedicada ao pipeline, limpa antes e depois de cada teste.

    A limpeza prévia garante que um teste abortado na execução anterior não
    contamine os seguintes.
    """
    conn = get_connection()
    purge_test_runs(conn)
    try:
        yield conn
    finally:
        with contextlib.suppress(Exception):
            conn.rollback()
            purge_test_runs(conn)
        conn.close()


@pytest.fixture
def executar_pipeline(ingest_conn):
    """Executa o pipeline com um StubAgrobrClient e devolve as métricas."""
    def _run(client: Optional[StubAgrobrClient] = None, **kwargs):
        from src.ingestion import IngestionPipeline

        pipeline = IngestionPipeline(
            client=client or StubAgrobrClient(),
            conn=ingest_conn,
            job_name=JOB_NAME_TESTES,
        )
        return pipeline.run(**kwargs)

    return _run


# ---------------------------------------------------------------------------
# Features: core sintético e versões de dataset descartáveis
# ---------------------------------------------------------------------------

# Variáveis que o catálogo semeado libera para features.model_features.
KEEP_ESPERADAS = (
    "preco_robusta", "usd_brl", "b3_cafe_ajuste", "ice_kc",
    "temp_media_cerrado", "umidade_rel_sulmg", "umidade_rel_cerrado",
    "radiacao_mj_sulmg", "radiacao_mj_cerrado",
    "precip_30d_sulmg", "precip_30d_cerrado", "precip_90d_sulmg", "precip_90d_cerrado",
    "tmin_min_30d_sulmg", "tmin_min_30d_cerrado",
    "dias_quente_30d_sulmg", "dias_quente_30d_cerrado",
    "sin_ano", "cos_ano",
)

REGIOES_TESTE = ("bambui", "sulmg", "cerrado")

# Prefixo exclusivo das versões criadas pelos testes, usado para apagá-las.
TAG_VERSAO_TESTE = "teste-"


def core_sintetico(dias: int = 400, inicio: date = DATA_BASE, seed: int = 42):
    """Mercado (só dias úteis) e clima diário no formato lido de ``core``."""
    rng = np.random.default_rng(seed)
    grade = pd.date_range(inicio, periods=dias, freq="D")
    uteis = grade[grade.dayofweek < 5]
    n = len(uteis)

    mercado = pd.DataFrame({
        "preco_arabica": 1000 + rng.normal(0, 5, n).cumsum(),
        "preco_robusta": 600 + rng.normal(0, 3, n).cumsum(),
        "usd_brl": 5 + rng.normal(0, 0.01, n).cumsum(),
        "b3_cafe_ajuste": 200 + rng.normal(0, 1, n).cumsum(),
        "ice_kc": 150 + rng.normal(0, 1, n).cumsum(),
    }, index=uteis).round(2)
    mercado.index.name = "data_ref"

    blocos = []
    for regiao in REGIOES_TESTE:
        temp_min = rng.uniform(8, 18, dias)
        temp_max = rng.uniform(22, 36, dias)
        bloco = pd.DataFrame({
            "temp_min": temp_min,
            "temp_max": temp_max,
            "temp_media": (temp_min + temp_max) / 2,
            "precip_mm": rng.uniform(0, 20, dias),
            "umidade_rel": rng.uniform(40, 90, dias),
            "radiacao_mj": rng.uniform(10, 25, dias),
            "vento_ms": rng.uniform(1, 5, dias),
        }).round(2)
        bloco.insert(0, "region", regiao)
        bloco.insert(0, "data_ref", grade)
        blocos.append(bloco)
    return mercado, pd.concat(blocos, ignore_index=True)


def purge_test_versions(conn) -> None:
    """Apaga as versões de dataset criadas por testes (as features vão em cascata)."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM predictions.forecasts WHERE dataset_version_id IN "
            "(SELECT dataset_version_id FROM features.dataset_versions "
            " WHERE version_tag LIKE %s)",
            (TAG_VERSAO_TESTE + "%",),
        )
        cur.execute(
            "DELETE FROM audit.pending_events WHERE payload->>'version_tag' LIKE %s",
            (TAG_VERSAO_TESTE + "%",),
        )
        cur.execute(
            "DELETE FROM features.dataset_versions WHERE version_tag LIKE %s",
            (TAG_VERSAO_TESTE + "%",),
        )
    conn.commit()


@pytest.fixture
def versao_teste(ingest_conn):
    """Versão de dataset descartável; devolve o ``dataset_version_id``.

    Nasce com ``is_valid = FALSE`` para não emitir DATASET_READY nem ser lida
    por um consumidor real do banco.
    """
    purge_test_versions(ingest_conn)
    version_id = str(uuid.uuid4())
    with ingest_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO features.dataset_versions "
            "(dataset_version_id, version_tag, cutoff_date, start_date, "
            " sha256_checksum, is_valid) VALUES (%s, %s, %s, %s, %s, FALSE)",
            (version_id, TAG_VERSAO_TESTE + version_id[:8], DATA_BASE, DATA_BASE, HASH_STUB),
        )
    ingest_conn.commit()
    try:
        yield version_id
    finally:
        with contextlib.suppress(Exception):
            ingest_conn.rollback()
            purge_test_versions(ingest_conn)

# Casas decimais das colunas de features.model_features (sql/tables.sql), para
# os testes que arredondam e assinam a matriz sem consultar o banco.
ESCALAS = {
    **{c: 3 for c in KEEP_ESPERADAS},
    "sin_ano": 6, "cos_ano": 6,
    "usd_brl": 4, "ice_kc": 4, "preco_robusta": 2, "b3_cafe_ajuste": 2,
    "y_7d": 2, "y_15d": 2, "y_30d": 2, "y_90d": 2,
}

# Janela usada pelos testes que passam o core sintético pela ingestão real.
DIAS_CORE_BANCO = 260


def matriz_sintetica(dias: int = 400, seed: int = 42):
    """Matriz de features montada em memória a partir do core sintético."""
    from src.feature_builder import WARMUP_DAYS, assemble_matrix

    mercado, clima = core_sintetico(dias, seed=seed)
    return assemble_matrix(
        mercado, clima, KEEP_ESPERADAS,
        DATA_BASE + timedelta(days=WARMUP_DAYS), DATA_BASE + timedelta(days=dias - 1),
        (7, 15, 30, 90),
    )


def arquivos_do_core_sintetico(dias: int, seed: int = 42) -> List[SourceFile]:
    """O core sintético no formato de coleta, para passar pela ingestão real."""
    from src.agrobr_client import UNITS

    mercado, clima = core_sintetico(dias, seed=seed)
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
    return [arquivo_stub(content_hash=hash_stub(f"core-sintetico-{seed}"),
                         market_rows=linhas_mercado, weather_rows=linhas_clima)]


@pytest.fixture
def core_no_banco(ingest_conn, executar_pipeline):
    """Publica o core sintético em ``core`` e devolve a janela e o run da carga.

    As versões que os testes criarem sobre ele não são confirmadas: somem no
    rollback e nunca ficam visíveis para outro consumidor do banco. Por isso
    ``recarregar`` — que simula uma revisão da fonte — roda a ingestão em outra
    conexão, para que o commit dela não confirme a transação do teste.
    """
    from src.feature_builder import WARMUP_DAYS
    from src.ingestion import IngestionPipeline

    def recarregar(seed: int):
        with get_connection() as outra:
            return IngestionPipeline(
                client=StubAgrobrClient(arquivos_do_core_sintetico(DIAS_CORE_BANCO, seed=seed)),
                conn=outra,
                job_name=JOB_NAME_TESTES,
            ).run()

    purge_test_versions(ingest_conn)
    carga = executar_pipeline(StubAgrobrClient(arquivos_do_core_sintetico(DIAS_CORE_BANCO)))
    assert carga["status"] == "SUCCESS"
    try:
        yield {
            "inicio": DATA_BASE + timedelta(days=WARMUP_DAYS),
            "corte": DATA_BASE + timedelta(days=DIAS_CORE_BANCO - 1),
            "run_id": carga["run_id"],
            "recarregar": recarregar,
        }
    finally:
        with contextlib.suppress(Exception):
            ingest_conn.rollback()
            purge_test_versions(ingest_conn)
