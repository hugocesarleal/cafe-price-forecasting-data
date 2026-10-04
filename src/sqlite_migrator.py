"""
Migrador SQLite → PostgreSQL.

Extrai dados do banco legado (cafe_centro_oeste_mg.db) e insere em
raw.market_observations e raw.weather_observations + core.market_daily
e core.weather_daily, respeitando a lógica de upsert com COALESCE.

O migrador é IDEMPOTENTE: pode ser executado várias vezes sem duplicar dados.
Os dados brutos em raw são preservados permanentemente.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row

from src.config import settings
from src.db import get_connection, db_cursor
from src.logging_config import logger

# Mapa: sigla da região SQLite → região canônica do PostgreSQL
REGIAO_MAP: Dict[int, str] = {}   # preenchido em runtime via tb_regiao

# Espécie do produto (id → string)
PRODUTO_MAP: Dict[int, str] = {}  # preenchido em runtime via tb_produto

# Código do contrato (id → codigo)
CONTRATO_MAP: Dict[int, str] = {}  # preenchido em runtime via tb_contrato


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_dimension_maps(sqlite_cur: sqlite3.Cursor) -> None:
    """Carrega os mapas de dimensão do SQLite para resolução de FKs."""
    global REGIAO_MAP, PRODUTO_MAP, CONTRATO_MAP

    sqlite_cur.execute("SELECT id, sigla FROM tb_regiao;")
    REGIAO_MAP = {r["id"]: r["sigla"] for r in sqlite_cur.fetchall()}

    sqlite_cur.execute("SELECT id, especie FROM tb_produto;")
    PRODUTO_MAP = {r["id"]: r["especie"] for r in sqlite_cur.fetchall()}

    sqlite_cur.execute("SELECT id, codigo FROM tb_contrato;")
    CONTRATO_MAP = {r["id"]: r["codigo"] for r in sqlite_cur.fetchall()}

    logger.info(f"Mapas carregados: {len(REGIAO_MAP)} regiões, "
                f"{len(PRODUTO_MAP)} produtos, {len(CONTRATO_MAP)} contratos.")


def _register_run(pg_conn: psycopg.Connection, run_id: uuid.UUID) -> None:
    """Registra execução de migração em audit.pipeline_runs."""
    with pg_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO audit.pipeline_runs (run_id, job_name, started_at, status)
            VALUES (%s, 'sqlite_migration', %s, 'RUNNING')
            ON CONFLICT (run_id) DO NOTHING;
        """, (str(run_id), datetime.now(timezone.utc)))
    pg_conn.commit()


def _finish_run(
    pg_conn: psycopg.Connection,
    run_id: uuid.UUID,
    ingested: int,
    status: str = "SUCCESS",
    error: Optional[str] = None,
) -> None:
    with pg_conn.cursor() as cur:
        cur.execute("""
            UPDATE audit.pipeline_runs
            SET finished_at = %s, status = %s,
                records_ingested = %s, error_message = %s
            WHERE run_id = %s;
        """, (datetime.now(timezone.utc), status, ingested, error, str(run_id)))
    pg_conn.commit()


# ---------------------------------------------------------------------------
# Extração SQLite
# ---------------------------------------------------------------------------

def _extract_market(sqlite_cur: sqlite3.Cursor) -> List[Dict[str, Any]]:
    """Extrai preços, câmbio, futuros e macro do SQLite num formato normalizado."""
    rows = []
    run_date = datetime.now(timezone.utc).date().isoformat()

    # --- tb_preco_cafe → preco_arabica / preco_robusta ---
    sqlite_cur.execute("""
        SELECT p.data_ref, pr.especie, p.preco_brl, p.preco_usd
        FROM tb_preco_cafe p
        JOIN tb_produto pr ON pr.id = p.fk_produto_id
        WHERE pr.especie IN ('arabica', 'robusta')
        ORDER BY p.data_ref;
    """)
    for r in sqlite_cur.fetchall():
        especie = r["especie"]
        var_brl = "preco_arabica" if especie == "arabica" else "preco_robusta"
        if r["preco_brl"] is not None:
            rows.append({
                "observation_date": r["data_ref"],
                "variable_name": var_brl,
                "value": r["preco_brl"],
                "unit": "R$/sc 60kg",
                "source": "CEPEA/ESALQ-SQLite",
                "region": None,
            })

    # --- tb_cambio → usd_brl ---
    sqlite_cur.execute("SELECT data_ref, usd_brl_venda, usd_brl_compra FROM tb_cambio ORDER BY data_ref;")
    for r in sqlite_cur.fetchall():
        if r["usd_brl_venda"] is not None:
            rows.append({
                "observation_date": r["data_ref"],
                "variable_name": "usd_brl",
                "value": r["usd_brl_venda"],
                "unit": "BRL",
                "source": "BCB-PTAX-SQLite",
                "region": None,
            })

    # --- tb_futuros → ice_kc (contrato KC, 1ª ordem de vencimento) ---
    sqlite_cur.execute("""
        SELECT f.data_ref, f.ajuste, c.codigo
        FROM tb_futuros f
        JOIN tb_contrato c ON c.id = f.fk_contrato_id
        WHERE c.codigo = 'KC' AND f.ordem_vencimento = 1
        ORDER BY f.data_ref;
    """)
    for r in sqlite_cur.fetchall():
        if r["ajuste"] is not None:
            rows.append({
                "observation_date": r["data_ref"],
                "variable_name": "ice_kc",
                "value": round(r["ajuste"], 4),
                "unit": "cUSD/lb",
                "source": "ICE-KC-SQLite",
                "region": None,
            })

    # --- tb_futuros → b3_cafe_ajuste (contrato ICF) ---
    sqlite_cur.execute("""
        SELECT f.data_ref, f.ajuste
        FROM tb_futuros f
        JOIN tb_contrato c ON c.id = f.fk_contrato_id
        WHERE c.codigo = 'ICF' AND f.ordem_vencimento = 1
        ORDER BY f.data_ref;
    """)
    for r in sqlite_cur.fetchall():
        if r["ajuste"] is not None:
            rows.append({
                "observation_date": r["data_ref"],
                "variable_name": "b3_cafe_ajuste",
                "value": round(r["ajuste"], 4),
                "unit": "USD/sc",
                "source": "B3-ICF-SQLite",
                "region": None,
            })

    logger.info(f"Mercado extraído: {len(rows)} observações.")
    return rows


def _extract_weather(sqlite_cur: sqlite3.Cursor) -> List[Dict[str, Any]]:
    """Extrai dados climáticos normalizados (uma linha por variável por data/região)."""
    rows = []

    sqlite_cur.execute("""
        SELECT c.data_ref, c.fk_regiao_id,
               c.temp_min, c.temp_max, c.temp_media,
               c.precip_mm, c.umidade_rel, c.radiacao_mj, c.vento_ms
        FROM tb_clima c
        ORDER BY c.data_ref, c.fk_regiao_id;
    """)

    weather_vars = [
        ("temp_min", "°C"),
        ("temp_max", "°C"),
        ("temp_media", "°C"),
        ("precip_mm", "mm"),
        ("umidade_rel", "%"),
        ("radiacao_mj", "MJ/m²"),
        ("vento_ms", "m/s"),
    ]

    for r in sqlite_cur.fetchall():
        region = REGIAO_MAP.get(r["fk_regiao_id"], f"unknown_{r['fk_regiao_id']}")
        for var_name, unit in weather_vars:
            val = r[var_name]
            if val is not None:
                rows.append({
                    "observation_date": r["data_ref"],
                    "region": region,
                    "variable_name": var_name,
                    "value": round(float(val), 4),
                    "unit": unit,
                    "source": "NASA-POWER-SQLite",
                })

    logger.info(f"Clima extraído: {len(rows)} observações.")
    return rows


# ---------------------------------------------------------------------------
# Carga no PostgreSQL (raw + core)
# ---------------------------------------------------------------------------

def _load_market_to_raw(
    pg_conn: psycopg.Connection,
    rows: List[Dict],
    run_id: uuid.UUID,
    load_version: str,
) -> int:
    """Insere observações de mercado em raw.market_observations."""
    inserted = 0
    with pg_conn.cursor() as cur:
        for r in rows:
            cur.execute("""
                INSERT INTO raw.market_observations
                    (pipeline_run_id, source, observation_date, region,
                     variable_name, value, unit, load_version, validation_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'VALID')
            """, (
                str(run_id),
                r["source"],
                r["observation_date"],
                r.get("region"),
                r["variable_name"],
                r["value"],
                r["unit"],
                load_version,
            ))
            inserted += 1
    pg_conn.commit()
    return inserted


def _load_weather_to_raw(
    pg_conn: psycopg.Connection,
    rows: List[Dict],
    run_id: uuid.UUID,
    load_version: str,
) -> int:
    """Insere observações climáticas em raw.weather_observations."""
    inserted = 0
    with pg_conn.cursor() as cur:
        for r in rows:
            cur.execute("""
                INSERT INTO raw.weather_observations
                    (pipeline_run_id, source, observation_date, region,
                     variable_name, value, unit, load_version, validation_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'VALID')
            """, (
                str(run_id),
                r["source"],
                r["observation_date"],
                r["region"],
                r["variable_name"],
                r["value"],
                r["unit"],
                load_version,
            ))
            inserted += 1
    pg_conn.commit()
    return inserted


def _upsert_core_market(pg_conn: psycopg.Connection, rows: List[Dict]) -> None:
    """Consolida observações de mercado em core.market_daily via upsert com COALESCE."""
    # Agrupa por data
    by_date: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        d = r["observation_date"]
        if d not in by_date:
            by_date[d] = {}
        by_date[d][r["variable_name"]] = r["value"]

    with pg_conn.cursor() as cur:
        for data_ref, vals in by_date.items():
            cur.execute("""
                INSERT INTO core.market_daily
                    (data_ref, preco_arabica, preco_robusta, usd_brl,
                     b3_cafe_ajuste, ice_kc, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (data_ref) DO UPDATE SET
                    preco_arabica  = COALESCE(EXCLUDED.preco_arabica,  core.market_daily.preco_arabica),
                    preco_robusta  = COALESCE(EXCLUDED.preco_robusta,  core.market_daily.preco_robusta),
                    usd_brl        = COALESCE(EXCLUDED.usd_brl,        core.market_daily.usd_brl),
                    b3_cafe_ajuste = COALESCE(EXCLUDED.b3_cafe_ajuste, core.market_daily.b3_cafe_ajuste),
                    ice_kc         = COALESCE(EXCLUDED.ice_kc,         core.market_daily.ice_kc),
                    updated_at     = now();
            """, (
                data_ref,
                vals.get("preco_arabica"),
                vals.get("preco_robusta"),
                vals.get("usd_brl"),
                vals.get("b3_cafe_ajuste"),
                vals.get("ice_kc"),
            ))
    pg_conn.commit()
    logger.info(f"core.market_daily populado: {len(by_date)} datas.")


def _upsert_core_weather(pg_conn: psycopg.Connection, rows: List[Dict]) -> None:
    """Consolida observações climáticas em core.weather_daily via upsert."""
    # Agrupa por (data, região)
    by_key: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        key = (r["observation_date"], r["region"])
        if key not in by_key:
            by_key[key] = {}
        by_key[key][r["variable_name"]] = r["value"]

    with pg_conn.cursor() as cur:
        for (data_ref, region), vals in by_key.items():
            cur.execute("""
                INSERT INTO core.weather_daily
                    (data_ref, region, temp_min, temp_max, temp_media,
                     precip_mm, umidade_rel, radiacao_mj, vento_ms, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (data_ref, region) DO UPDATE SET
                    temp_min    = COALESCE(EXCLUDED.temp_min,    core.weather_daily.temp_min),
                    temp_max    = COALESCE(EXCLUDED.temp_max,    core.weather_daily.temp_max),
                    temp_media  = COALESCE(EXCLUDED.temp_media,  core.weather_daily.temp_media),
                    precip_mm   = COALESCE(EXCLUDED.precip_mm,   core.weather_daily.precip_mm),
                    umidade_rel = COALESCE(EXCLUDED.umidade_rel, core.weather_daily.umidade_rel),
                    radiacao_mj = COALESCE(EXCLUDED.radiacao_mj, core.weather_daily.radiacao_mj),
                    vento_ms    = COALESCE(EXCLUDED.vento_ms,    core.weather_daily.vento_ms),
                    updated_at  = now();
            """, (
                data_ref, region,
                vals.get("temp_min"),
                vals.get("temp_max"),
                vals.get("temp_media"),
                vals.get("precip_mm"),
                vals.get("umidade_rel"),
                vals.get("radiacao_mj"),
                vals.get("vento_ms"),
            ))
    pg_conn.commit()
    logger.info(f"core.weather_daily populado: {len(by_key)} combinações data/região.")


# ---------------------------------------------------------------------------
# Ponto de entrada principal
# ---------------------------------------------------------------------------

def migrate_sqlite_to_postgres(
    sqlite_path: Optional[str] = None,
    pg_conn: Optional[psycopg.Connection] = None,
    load_version: str = "sqlite_legacy_v1",
) -> Dict[str, Any]:
    """
    Executa a migração completa SQLite → PostgreSQL.

    Retorna um dict com os contadores de registros migrados e o run_id.
    Operação idempotente: pode ser chamada repetidamente sem duplicar dados.
    """
    sqlite_path = sqlite_path or settings.SQLITE_SOURCE_PATH
    run_id = uuid.uuid4()

    should_close_pg = False
    if pg_conn is None:
        pg_conn = get_connection()
        should_close_pg = True

    try:
        # Registra início da execução
        _register_run(pg_conn, run_id)
        logger.info(f"Iniciando migração SQLite → PostgreSQL. run_id={run_id}")

        # Abre SQLite em modo leitura
        sqlite_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        sqlite_conn.row_factory = sqlite3.Row
        sqlite_cur = sqlite_conn.cursor()

        # Carrega mapas de dimensão
        _load_dimension_maps(sqlite_cur)

        # Extrai
        market_rows = _extract_market(sqlite_cur)
        weather_rows = _extract_weather(sqlite_cur)
        sqlite_conn.close()

        # Carga raw
        market_ingested = _load_market_to_raw(pg_conn, market_rows, run_id, load_version)
        weather_ingested = _load_weather_to_raw(pg_conn, weather_rows, run_id, load_version)
        total_raw = market_ingested + weather_ingested

        # Upsert core
        _upsert_core_market(pg_conn, market_rows)
        _upsert_core_weather(pg_conn, weather_rows)

        _finish_run(pg_conn, run_id, total_raw, "SUCCESS")
        logger.info(
            f"Migração concluída. raw={total_raw} obs "
            f"(mercado={market_ingested}, clima={weather_ingested})."
        )

        return {
            "run_id": str(run_id),
            "market_rows_raw": market_ingested,
            "weather_rows_raw": weather_ingested,
            "total_raw": total_raw,
            "status": "SUCCESS",
        }

    except Exception as e:
        _finish_run(pg_conn, run_id, 0, "FAILED", str(e))
        logger.error(f"Migração falhou: {e}")
        raise
    finally:
        if should_close_pg:
            pg_conn.close()


if __name__ == "__main__":
    result = migrate_sqlite_to_postgres()
    print(result)
