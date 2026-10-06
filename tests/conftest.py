"""Fixtures compartilhadas para suite de testes pytest."""

import contextlib
import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

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
