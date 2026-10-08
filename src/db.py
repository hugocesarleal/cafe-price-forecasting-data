"""Módulo de conexão e utilitários de banco de dados PostgreSQL."""

import contextlib
from typing import Generator, Optional
import psycopg
from psycopg.rows import dict_row

from src.config import settings
from src.logging_config import logger


def get_connection(
    host: Optional[str] = None,
    port: Optional[int] = None,
    dbname: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    autocommit: bool = False,
    dsn: Optional[str] = None
) -> psycopg.Connection:
    """Cria e retorna uma conexão direta com o PostgreSQL.

    Precedência da conexão: ``dsn`` explícito > ``PIPELINE_DATABASE_URL``
    (quando nenhuma parte explícita foi informada) > campos ``DB_*``.
    A URL e a senha nunca são registradas em log.
    """
    url = dsn or settings.PIPELINE_DATABASE_URL
    partes_explicitas = any(v is not None for v in (host, port, dbname, user, password))

    if url and not partes_explicitas:
        conn = psycopg.connect(url, autocommit=autocommit, row_factory=dict_row)
        logger.debug("Conexão PostgreSQL aberta via URL configurada.")
        return conn

    if url:
        logger.debug(
            "Partes explícitas de conexão têm precedência sobre a URL configurada."
        )
    conn = psycopg.connect(
        host=host or settings.DB_HOST,
        port=port or settings.DB_PORT,
        dbname=dbname or settings.DB_NAME,
        user=user or settings.DB_USER,
        password=password or settings.DB_PASSWORD,
        autocommit=autocommit,
        row_factory=dict_row
    )
    logger.debug(
        "Conexão PostgreSQL aberta em %s:%s/%s.",
        host or settings.DB_HOST, port or settings.DB_PORT, dbname or settings.DB_NAME,
    )
    return conn


@contextlib.contextmanager
def pipeline_connection(
    dsn: Optional[str] = None, **kwargs
) -> Generator[psycopg.Connection, None, None]:
    """Conexão dedicada a um ciclo do pipeline, com abertura/fechamento registrados.

    O ciclo inteiro (advisory lock e todas as etapas) roda nesta única conexão:
    o lock é de sessão, então precisa ser adquirido, usado e liberado na mesma
    conexão — e é quem abre que fecha, inclusive quando o ciclo falha.
    """
    conn = get_connection(dsn=dsn, **kwargs)
    logger.info("Conexão do pipeline aberta.")
    try:
        yield conn
    finally:
        conn.close()
        logger.info("Conexão do pipeline fechada.")


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
def acquire_advisory_lock(conn: psycopg.Connection, lock_id: Optional[int] = None):
    """Adquire um advisory lock no PostgreSQL para impedir concorrência.

    ``lock_id`` explícito tem precedência; sem ele, vale ``ADVISORY_LOCK_KEY``
    da configuração. Lança RuntimeError se o lock já estiver ocupado por
    outra instância.
    """
    lock_id = settings.ADVISORY_LOCK_KEY if lock_id is None else lock_id
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
