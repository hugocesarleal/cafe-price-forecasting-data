"""Gerenciador de migrations SQL versionadas para o PostgreSQL."""

import os
import glob
from typing import List, Optional
import psycopg

from src.db import get_connection
from src.logging_config import logger


def init_migration_table(conn: psycopg.Connection) -> None:
    """Garante que a tabela de histórico de migrations exista."""
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS audit;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit.schema_migrations (
                version VARCHAR(100) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
            );
        """)
    conn.commit()


def get_applied_migrations(conn: psycopg.Connection) -> set[str]:
    """Retorna conjunto de versões de migrations já aplicadas."""
    init_migration_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM audit.schema_migrations;")
        rows = cur.fetchall()
        return {r["version"] for r in rows}


def apply_migrations(
    migrations_dir: str = "migrations",
    conn: Optional[psycopg.Connection] = None
) -> List[str]:
    """Aplica todas as migrations pendentes em ordem alfanumérica estrita."""
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    applied_now = []
    try:
        applied_set = get_applied_migrations(conn)
        sql_files = sorted(glob.glob(os.path.join(migrations_dir, "*.sql")))

        for file_path in sql_files:
            version_name = os.path.basename(file_path)
            if version_name not in applied_set:
                logger.info(f"Aplicando migration: {version_name}...")
                with open(file_path, "r", encoding="utf-8") as f:
                    sql_content = f.read()

                with conn.cursor() as cur:
                    cur.execute(sql_content)
                    cur.execute(
                        "INSERT INTO audit.schema_migrations (version) VALUES (%s);",
                        (version_name,)
                    )
                conn.commit()
                applied_now.append(version_name)
                logger.info(f"Migration {version_name} aplicada com sucesso.")
            else:
                logger.debug(f"Migration {version_name} já aplicada anteriormente.")

        logger.info(f"Total de migrations aplicadas nesta rodada: {len(applied_now)}")
        return applied_now
    finally:
        if should_close:
            conn.close()


if __name__ == "__main__":
    apply_migrations()
