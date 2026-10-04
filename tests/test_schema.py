"""Teste Obrigatório 1: Criação dos Schemas e Estrutura de Tabelas."""

import pytest


def test_schema_creation(db_conn):
    """Verifica se os 6 schemas obrigatórios foram criados com sucesso."""
    expected_schemas = {"raw", "staging", "core", "features", "predictions", "audit"}
    
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT schema_name 
            FROM information_schema.schemata 
            WHERE schema_name IN ('raw', 'staging', 'core', 'features', 'predictions', 'audit');
        """)
        rows = cur.fetchall()
        found_schemas = {r["schema_name"] for r in rows}
        
    assert expected_schemas.issubset(found_schemas), f"Schemas ausentes: {expected_schemas - found_schemas}"


def test_required_tables_exist(db_conn):
    """Verifica se todas as tabelas mínimas obrigatórias existem no PostgreSQL."""
    expected_tables = {
        ("raw", "ingestion_files"),
        ("raw", "market_observations"),
        ("raw", "weather_observations"),
        ("staging", "market_observations"),
        ("staging", "weather_observations"),
        ("core", "market_daily"),
        ("core", "weather_daily"),
        ("features", "variable_catalog"),
        ("features", "model_features"),
        ("features", "dataset_versions"),
        ("predictions", "forecasts"),
        ("audit", "pipeline_runs"),
        ("audit", "data_quality_checks"),
        ("audit", "model_runs"),
        ("audit", "pending_events"),
    }
    
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT table_schema, table_name 
            FROM information_schema.tables 
            WHERE table_type = 'BASE TABLE';
        """)
        rows = cur.fetchall()
        found_tables = {(r["table_schema"], r["table_name"]) for r in rows}
        
    for table_tuple in expected_tables:
        assert table_tuple in found_tables, f"Tabela obrigatória ausente: {table_tuple[0]}.{table_tuple[1]}"


def test_variable_catalog_seeded(db_conn):
    """Verifica se o catálogo de variáveis foi devidamente populado com KEEP e TARGET."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT selection_status, count(*) as cnt FROM features.variable_catalog GROUP BY selection_status;")
        rows = cur.fetchall()
        status_counts = {r["selection_status"]: r["cnt"] for r in rows}
        
    assert "TARGET" in status_counts, "Catálogo não possui variável TARGET cadastrada."
    assert "KEEP" in status_counts, "Catálogo não possui variáveis KEEP cadastradas."
    assert status_counts["KEEP"] >= 18, f"Esperado ao menos 18 variáveis KEEP, obtido: {status_counts.get('KEEP')}"
