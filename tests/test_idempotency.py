"""Testes Obrigatórios 3, 4 e 5: idempotência, deduplicação e upsert.

A mesma carga executada duas vezes não pode duplicar observações, features ou
previsões. Verifica também que o upsert em ``core`` atualiza sem duplicar datas
e sem apagar colunas que a carga seguinte não trouxe.
"""

from conftest import (
    HASH_STUB,
    JOB_NAME_TESTES,
    StubAgrobrClient,
    arquivo_stub,
    contar_do_teste,
    datas,
    hash_stub,
    linha_mercado,
)


def linhas_core(conn, serie, *colunas):
    colunas = ", ".join(colunas) or "preco_arabica"
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT data_ref, {colunas}, pipeline_run_id FROM core.market_daily "
            "WHERE data_ref = ANY(%s) ORDER BY data_ref",
            (serie,),
        )
        return cur.fetchall()


def preco_core(conn, serie):
    return linhas_core(conn, serie, "preco_arabica")


def contar_raw(conn, run_id=None, tabela="raw.market_observations"):
    """Conta linhas de ``raw``/``staging``; sem ``run_id``, soma todas as do teste."""
    if run_id:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) AS n FROM {tabela} WHERE pipeline_run_id = %s",
                (run_id,),
            )
            return cur.fetchone()["n"]
    return contar_do_teste(conn, tabela)


def mercado(valores, variavel="preco_arabica", unit="R$/sc 60kg", source="STUB-TESTE"):
    return [
        linha_mercado(d, variavel, v, unit, source)
        for d, v in zip(datas(len(valores)), valores)
    ]


# ---------------------------------------------------------------------------
# Teste Obrigatório 3 — carga idempotente
# ---------------------------------------------------------------------------

def test_mesma_carga_duas_vezes_nao_duplica_nada(ingest_conn, executar_pipeline):
    serie = datas(5)
    primeira = executar_pipeline()
    segunda = executar_pipeline()

    assert primeira["status"] == segunda["status"] == "SUCCESS"
    assert primeira["run_id"] != segunda["run_id"]
    assert primeira["arquivos_alterados"] == 1
    # Hash idêntico: a origem não mudou, nada é republicado.
    assert segunda["arquivos_alterados"] == 0
    assert segunda["arquivos_inalterados"] == 1
    assert segunda["raw_market"] == segunda["raw_weather"] == 0
    assert segunda["staging_market"] == segunda["staging_weather"] == 0

    assert contar_raw(ingest_conn) == 5, "raw não pode ganhar linhas repetidas"
    assert contar_raw(ingest_conn, tabela="raw.weather_observations") == 5
    assert contar_raw(ingest_conn, tabela="staging.market_observations") == 5
    assert len(preco_core(ingest_conn, serie)) == 5


def test_segunda_carga_preserva_os_valores_ja_publicados(ingest_conn, executar_pipeline):
    serie = datas(5)
    executar_pipeline()
    executar_pipeline()

    linhas = preco_core(ingest_conn, serie)

    assert [l["preco_arabica"] for l in linhas] == [1200.0, 1201.0, 1202.0, 1203.0, 1204.0]


def test_cargas_repetidas_registram_ambas_as_execucoes(ingest_conn, executar_pipeline):
    primeira = executar_pipeline()
    segunda = executar_pipeline()

    with ingest_conn.cursor() as cur:
        cur.execute(
            "SELECT run_id::text AS run_id, status, records_ingested "
            "FROM audit.pipeline_runs WHERE job_name = %s ORDER BY started_at",
            (JOB_NAME_TESTES,),
        )
        execucoes = cur.fetchall()
        cur.execute(
            "SELECT has_changed, count(*) AS n FROM raw.ingestion_files "
            "WHERE pipeline_run_id IN "
            "(SELECT run_id FROM audit.pipeline_runs WHERE job_name = %s) "
            "GROUP BY has_changed ORDER BY has_changed DESC",
            (JOB_NAME_TESTES,),
        )
        arquivos = {r["has_changed"]: r["n"] for r in cur.fetchall()}

    assert [e["status"] for e in execucoes] == ["SUCCESS", "SUCCESS"]
    assert execucoes[0]["records_ingested"] == 10
    assert execucoes[1]["records_ingested"] == 0, \
        "A trilha de auditoria deve mostrar que a segunda carga não ingeriu nada"
    assert arquivos == {True: 1, False: 1}
    assert primeira["run_id"] != segunda["run_id"]


# ---------------------------------------------------------------------------
# Teste Obrigatório 5 — upsert sem duplicar
# ---------------------------------------------------------------------------

def test_origem_alterada_atualiza_core_sem_duplicar_datas(ingest_conn, executar_pipeline):
    serie = datas(5)
    executar_pipeline()
    revisada = executar_pipeline(StubAgrobrClient([
        arquivo_stub(content_hash=hash_stub("revisao"),
                     market_rows=mercado([2000.0, 2001.0, 2002.0, 2003.0, 2004.0]),
                     weather_rows=[])
    ]))

    assert revisada["arquivos_alterados"] == 1
    assert revisada["core_market_dates"] == 5

    linhas = preco_core(ingest_conn, serie)
    assert len(linhas) == 5, "Upsert não pode criar uma segunda linha para a mesma data"
    assert [l["preco_arabica"] for l in linhas] == [2000.0, 2001.0, 2002.0, 2003.0, 2004.0]
    assert all(str(l["pipeline_run_id"]) == revisada["run_id"] for l in linhas)

    # raw é imutável: as duas cargas continuam lá.
    assert contar_raw(ingest_conn) == 10


def test_upsert_preserva_coluna_ausente_na_carga_seguinte(ingest_conn, executar_pipeline):
    """COALESCE impede que uma origem missing apague o que outra já publicou."""
    serie = datas(5)
    executar_pipeline(StubAgrobrClient([
        arquivo_stub(content_hash=hash_stub("completa"),
                     market_rows=mercado([1200.0] * 5) + mercado([5.25] * 5, "usd_brl", "BRL"),
                     weather_rows=[])
    ]))
    parcial = executar_pipeline(StubAgrobrClient([
        arquivo_stub(content_hash=hash_stub("parcial"),
                     market_rows=mercado([1300.0] * 5),
                     weather_rows=[])
    ]))

    assert parcial["status"] == "SUCCESS"

    linhas = linhas_core(ingest_conn, serie, "preco_arabica", "usd_brl")
    assert len(linhas) == 5
    assert all(l["preco_arabica"] == 1300.0 for l in linhas)
    assert all(l["usd_brl"] == 5.25 for l in linhas), \
        "Coluna não enviada na segunda carga deve manter o valor anterior"


def test_raw_acumula_por_carga_e_mantem_a_origem_de_cada_linha(ingest_conn, executar_pipeline):
    primeira = executar_pipeline()
    segunda = executar_pipeline(StubAgrobrClient([
        arquivo_stub(content_hash=hash_stub("outra"),
                     market_rows=mercado([2000.0] * 5), weather_rows=[])
    ]))

    assert contar_raw(ingest_conn, primeira["run_id"]) == 5
    assert contar_raw(ingest_conn, segunda["run_id"]) == 5
    assert contar_raw(ingest_conn) == 10


# ---------------------------------------------------------------------------
# Teste Obrigatório 4 — deduplicação na carga
# ---------------------------------------------------------------------------

def test_duplicidades_dentro_do_lote_sao_resolvidas_antes_de_staging(
    ingest_conn, executar_pipeline
):
    serie = datas(5)
    duplicado = mercado([1200.0, 1500.0, 1202.0, 1203.0, 1204.0])
    # Revisão da mesma chave, com valor menor: a última deve vencer (não o MAX).
    duplicado.append(linha_mercado(serie[1], "preco_arabica", 1400.0, "R$/sc 60kg"))

    metricas = executar_pipeline(
        StubAgrobrClient([arquivo_stub(market_rows=duplicado, weather_rows=[])])
    )

    assert metricas["raw_market"] == 6, "raw preserva a duplicidade coletada"
    assert metricas["duplicidades_removidas"] == 1
    assert metricas["staging_market"] == 5
    assert metricas["core_market_dates"] == 5
    assert "duplicidades" in metricas["checks"]["warning_failures"]

    linhas = preco_core(ingest_conn, serie)
    assert len(linhas) == 5
    assert linhas[1]["preco_arabica"] == 1400.0


def test_duplicidade_entre_duas_origens_tambem_e_resolvida(ingest_conn, executar_pipeline):
    serie = datas(3)
    arquivos = [
        arquivo_stub(content_hash=hash_stub("origem-a"),
                     file_name="a.csv",
                     market_rows=mercado([1000.0, 1001.0, 1002.0], source="ORIGEM-A"),
                     weather_rows=[]),
        arquivo_stub(content_hash=hash_stub("origem-b"),
                     file_name="b.csv",
                     market_rows=mercado([1100.0, 1101.0, 1102.0], source="ORIGEM-B"),
                     weather_rows=[]),
    ]

    metricas = executar_pipeline(StubAgrobrClient(arquivos))

    assert metricas["arquivos_coletados"] == 2
    assert metricas["raw_market"] == 6
    assert metricas["duplicidades_removidas"] == 3
    assert metricas["staging_market"] == 3
    assert metricas["core_market_dates"] == 3
    assert [l["preco_arabica"] for l in preco_core(ingest_conn, serie)] == [
        1100.0, 1101.0, 1102.0
    ]


def test_carga_sem_duplicidade_nao_remove_nada(ingest_conn, executar_pipeline):
    metricas = executar_pipeline()

    assert metricas["duplicidades_removidas"] == 0
    assert metricas["staging_market"] == 5
    assert HASH_STUB != hash_stub("qualquer")
