"""Fixtures compartilhadas para suite de testes pytest."""

import pytest
import psycopg
from psycopg.rows import dict_row

from src.config import settings
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
