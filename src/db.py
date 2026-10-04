"""Módulo de conexão e utilitários de banco de dados PostgreSQL."""

import contextlib
from typing import Generator, Optional
import psycopg
from psycopg.rows import dict_row

from src.config import settings
from src.logging_config import logger

# Hash constante para o lock do pipeline
PIPELINE_ADVISORY_LOCK_ID = 84729103


def get_connection(
    host: Optional[str] = None,
    port: Optional[int] = None,
    dbname: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    autocommit: bool = False
) -> psycopg.Connection:
    """Cria e retorna uma conexão direta com o PostgreSQL."""
    conn = psycopg.connect(
        host=host or settings.DB_HOST,
        port=port or settings.DB_PORT,
        dbname=dbname or settings.DB_NAME,
        user=user or settings.DB_USER,
        password=password or settings.DB_PASSWORD,
        autocommit=autocommit,
        row_factory=dict_row
    )
    return conn


@contextlib.contextmanager
def db_cursor(conn: Optional[psycopg.Connection] = None, commit: bool = True) -> Generator[psycopg.Cursor, None, None]:
    """Context manager para gerenciar conexão e cursor com commit/rollback seguro."""
    should_close_conn = False
    if conn is None:
        conn = get_connection()
        should_close_conn = True

    try:
        with conn.cursor() as cur:
            yield cur
        if commit and not conn.autocommit:
            conn.commit()
    except Exception as e:
        if not conn.autocommit:
            conn.rollback()
        logger.error(f"Erro na transação de banco de dados: {e}")
        raise
    finally:
        if should_close_conn:
            conn.close()


@contextlib.contextmanager
def acquire_advisory_lock(conn: psycopg.Connection, lock_id: int = PIPELINE_ADVISORY_LOCK_ID):
    """Adquire um advisory lock no PostgreSQL para impedir concorrência.
    
    Lança RuntimeError se o lock já estiver ocupado por outra instância.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s) AS locked;", (lock_id,))
        row = cur.fetchone()
        locked = row["locked"] if row else False
        if not locked:
            raise RuntimeError(f"Bloqueio de concorrência: O pipeline já está em execução (lock_id={lock_id}).")
        logger.info(f"Advisory lock adquirido com sucesso (lock_id={lock_id}).")

    try:
        yield
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s);", (lock_id,))
            logger.info(f"Advisory lock liberado com sucesso (lock_id={lock_id}).")
        conn.commit()


def execute_sql_file(file_path: str, conn: psycopg.Connection) -> None:
    """Executa um arquivo SQL de forma transacional."""
    with open(file_path, "r", encoding="utf-8") as f:
        sql_content = f.read()

    with conn.cursor() as cur:
        cur.execute(sql_content)
    conn.commit()
    logger.info(f"Arquivo SQL executado com sucesso: {file_path}")
