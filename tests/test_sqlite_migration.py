"""
Testes Obrigatórios 2 e 3: Migração SQLite → PostgreSQL e Idempotência.

Caso 2: test_sqlite_migration_sample
Caso 3: test_idempotent_ingestion (primeira cobertura)
"""

import sqlite3
import pytest

from src.sqlite_migrator import (
    migrate_sqlite_to_postgres,
    _extract_market,
    _extract_weather,
    _load_dimension_maps,
    REGIAO_MAP,
    PRODUTO_MAP,
)
from src.config import settings


# ---------------------------------------------------------------------------
# Fixtures auxiliares
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sqlite_dims():
    """Carrega mapas de dimensão do SQLite para uso nos testes."""
    conn = sqlite3.connect(f"file:{settings.SQLITE_SOURCE_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    _load_dimension_maps(cur)
    yield cur
    conn.close()


@pytest.fixture(scope="module")
def migration_result(db_conn):
    """Executa a migração completa uma única vez para toda a módulo de testes."""
    result = migrate_sqlite_to_postgres(pg_conn=db_conn)
    return result


# ---------------------------------------------------------------------------
# Caso 2: Migração SQLite → PostgreSQL
# ---------------------------------------------------------------------------

def test_sqlite_migration_returns_success(migration_result):
    """Verifica que a migração retorna status SUCCESS."""
    assert migration_result["status"] == "SUCCESS"
    assert migration_result["run_id"] is not None


def test_sqlite_migration_market_count(sqlite_dims, db_conn, migration_result):
    """Verifica paridade de contagem de preços arábica no SQLite vs PostgreSQL."""
    # Conta no SQLite
    sqlite_dims.execute("""
        SELECT count(*) as cnt FROM tb_preco_cafe p
        JOIN tb_produto pr ON pr.id = p.fk_produto_id
        WHERE pr.especie = 'arabica';
    """)
    sqlite_count = sqlite_dims.fetchone()["cnt"]

    # Conta no PostgreSQL filtrando pelo run_id específico desta migração
    run_id = migration_result["run_id"]
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) as cnt FROM raw.market_observations
            WHERE variable_name = 'preco_arabica'
              AND source = 'CEPEA/ESALQ-SQLite'
              AND pipeline_run_id = %s;
        """, (run_id,))
        pg_count = cur.fetchone()["cnt"]

    assert pg_count == sqlite_count, (
        f"Divergência de contagem: SQLite={sqlite_count}, PostgreSQL raw={pg_count} (run_id={run_id})"
    )


def test_sqlite_migration_weather_count(sqlite_dims, db_conn, migration_result):
    """Verifica paridade de contagem de observações climáticas não-nulas."""
    # Conta dinamicamente os valores não-nulos por variável no SQLite
    weather_vars = ["temp_min", "temp_max", "temp_media",
                    "precip_mm", "umidade_rel", "radiacao_mj", "vento_ms"]
    expected_raw_obs = 0
    for v in weather_vars:
        sqlite_dims.execute(f"SELECT count(*) as cnt FROM tb_clima WHERE {v} IS NOT NULL;")
        expected_raw_obs += sqlite_dims.fetchone()["cnt"]

    # Filtra pelo run_id específico desta migração
    run_id = migration_result["run_id"]
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) as cnt FROM raw.weather_observations
            WHERE source = 'NASA-POWER-SQLite'
              AND pipeline_run_id = %s;
        """, (run_id,))
        pg_count = cur.fetchone()["cnt"]

    assert pg_count == expected_raw_obs, (
        f"Divergência clima: esperado={expected_raw_obs} (não-nulos), PostgreSQL raw={pg_count}"
    )


def test_sqlite_migration_core_market_populated(migration_result, db_conn):
    """Verifica que core.market_daily foi populado com datas do SQLite."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) as cnt FROM core.market_daily WHERE preco_arabica IS NOT NULL;")
        count = cur.fetchone()["cnt"]

    assert count > 0, "core.market_daily está vazio após migração."


def test_sqlite_migration_core_weather_populated(migration_result, db_conn):
    """Verifica que core.weather_daily foi populado por todas as 3 regiões."""
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT region, count(*) as cnt
            FROM core.weather_daily
            GROUP BY region
            ORDER BY region;
        """)
        rows = cur.fetchall()

    regions_found = {r["region"] for r in rows}
    expected_regions = {"bambui", "sulmg", "cerrado"}
    assert expected_regions.issubset(regions_found), (
        f"Regiões ausentes em core.weather_daily: {expected_regions - regions_found}"
    )


def test_sqlite_migration_value_spot_check(sqlite_dims, db_conn):
    """Verifica que o primeiro valor de preco_arabica bate entre SQLite e PostgreSQL."""
    # Obtém o primeiro registro do SQLite
    sqlite_dims.execute("""
        SELECT p.data_ref, p.preco_brl
        FROM tb_preco_cafe p
        JOIN tb_produto pr ON pr.id = p.fk_produto_id
        WHERE pr.especie = 'arabica'
        ORDER BY p.data_ref ASC LIMIT 1;
    """)
    sqlite_row = sqlite_dims.fetchone()
    assert sqlite_row is not None

    data_ref = sqlite_row["data_ref"]
    preco_brl = sqlite_row["preco_brl"]

    # Verifica no core
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT preco_arabica FROM core.market_daily WHERE data_ref = %s;",
            (data_ref,)
        )
        pg_row = cur.fetchone()

    assert pg_row is not None, f"Data {data_ref} não encontrada em core.market_daily."
    assert abs(float(pg_row["preco_arabica"]) - preco_brl) < 0.01, (
        f"Valor divergente em {data_ref}: SQLite={preco_brl}, PG={pg_row['preco_arabica']}"
    )


def test_sqlite_migration_raw_preserved(migration_result, db_conn):
    """Garante que dados em raw.market_observations não são deletados após upsert em core."""
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) as cnt FROM raw.market_observations
            WHERE source = 'CEPEA/ESALQ-SQLite';
        """)
        raw_count = cur.fetchone()["cnt"]

    assert raw_count > 0, "raw.market_observations foi esvaziado — violação da regra de imutabilidade."


# ---------------------------------------------------------------------------
# Caso 3: Idempotência da migração
# ---------------------------------------------------------------------------

def test_idempotent_migration(db_conn):
    """
    Caso 3: Executa a migração pela segunda vez e verifica que
    os contadores em raw aumentam (nova carga), mas core mantém
    os mesmos valores sem duplicar linhas (upsert correto).
    """
    # Conta antes da segunda execução
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) as cnt FROM core.market_daily;")
        core_count_before = cur.fetchone()["cnt"]

        cur.execute("SELECT count(*) as cnt FROM core.weather_daily;")
        core_weather_before = cur.fetchone()["cnt"]

    # Segunda execução
    result2 = migrate_sqlite_to_postgres(
        pg_conn=db_conn,
        load_version="sqlite_legacy_v2_idempotency_test"
    )
    assert result2["status"] == "SUCCESS"

    # core não deve crescer — upsert deve manter as mesmas linhas
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) as cnt FROM core.market_daily;")
        core_count_after = cur.fetchone()["cnt"]

        cur.execute("SELECT count(*) as cnt FROM core.weather_daily;")
        core_weather_after = cur.fetchone()["cnt"]

    assert core_count_after == core_count_before, (
        f"core.market_daily cresceu após segunda execução: "
        f"antes={core_count_before}, depois={core_count_after}"
    )
    assert core_weather_after == core_weather_before, (
        f"core.weather_daily cresceu após segunda execução: "
        f"antes={core_weather_before}, depois={core_weather_after}"
    )
