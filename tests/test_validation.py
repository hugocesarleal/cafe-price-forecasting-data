"""Testes Obrigatórios 4, 6, 7 e 8: validações de qualidade antes de publicar.

Cobrem as 12 checagens de ``src/validation.py``: deduplicação, coluna ausente,
tipo inválido e valor fora dos limites físicos, além das regras de data,
unidade, continuidade e ausência de informação futura.
"""

import uuid
from datetime import timedelta

import pytest

from conftest import JOB_NAME_TESTES, arquivo_stub, datas, linha_clima, linha_mercado
from src.validation import (
    MARKET_KIND,
    MAX_INVALID_SHARE,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    TARGET_VARIABLE,
    WEATHER_KIND,
    dedup_key,
    deduplicate,
    record_checks,
    validate_batch,
)

CHECKS_ESPERADAS = {
    "colunas_obrigatorias", "valores_nulos", "tipos_numericos", "datas_validas",
    "datas_futuras", "limites_fisicos", "unidades", "duplicidades",
    "continuidade_temporal", "registros_minimos", "disponibilidade_horizontes",
    "ausencia_informacao_futura",
}


def lote_valido(dias: int = 5):
    return [
        linha_mercado(d, "preco_arabica", 1200.0 + i, "R$/sc 60kg")
        for i, d in enumerate(datas(dias))
    ]


def checagem(report, nome):
    encontradas = [r for r in report.results if r.check_name == nome]
    assert encontradas, f"Checagem {nome!r} não foi registrada."
    return encontradas[0]


# ---------------------------------------------------------------------------
# Cenário base
# ---------------------------------------------------------------------------

def test_lote_valido_e_aprovado():
    outcome = validate_batch(lote_valido(), MARKET_KIND)

    assert outcome.report.passed is True
    assert outcome.report.critical_failures == []
    assert len(outcome.valid_rows) == 5
    assert outcome.invalid_rows == []
    assert outcome.approved is True


def test_as_doze_checagens_sao_registradas():
    report = validate_batch(lote_valido(), MARKET_KIND).report

    assert {r.check_name for r in report.results} == CHECKS_ESPERADAS
    assert report.summary()["total_checks"] == 12


def test_kind_desconhecido_levanta_erro():
    with pytest.raises(ValueError, match="kind inválido"):
        validate_batch(lote_valido(), "mercado")


# ---------------------------------------------------------------------------
# Teste Obrigatório 6 — coluna obrigatória ausente
# ---------------------------------------------------------------------------

def test_coluna_obrigatoria_ausente_reprova_e_quarentena():
    rows = lote_valido()
    del rows[2]["observation_date"]

    outcome = validate_batch(rows, MARKET_KIND)
    resultado = checagem(outcome.report, "colunas_obrigatorias")

    assert resultado.passed is False
    assert resultado.severity == SEVERITY_CRITICAL
    assert resultado.details["reprovadas"] == 1
    assert "observation_date" in resultado.details["exemplos"][0]
    assert outcome.report.passed is False

    # A linha sem data também falha em datas_validas e vai para a quarentena.
    assert len(outcome.invalid_rows) == 1
    assert len(outcome.valid_rows) == 4
    assert checagem(outcome.report, "datas_validas").passed is False


def test_campo_obrigatorio_vazio_tambem_reprova():
    rows = lote_valido()
    rows[0]["variable_name"] = ""

    resultado = checagem(validate_batch(rows, MARKET_KIND).report, "colunas_obrigatorias")

    assert resultado.passed is False
    assert resultado.details["reprovadas"] == 1


def test_regiao_e_obrigatoria_apenas_para_clima():
    sem_regiao = [
        {"observation_date": d, "variable_name": "temp_min", "value": 15.0,
         "unit": "°C", "source": "STUB"}
        for d in datas(3)
    ]

    assert checagem(validate_batch(sem_regiao, WEATHER_KIND).report,
                    "colunas_obrigatorias").passed is False


# ---------------------------------------------------------------------------
# Teste Obrigatório 7 — tipo inválido
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("valor_ruim", ["não-numérico", "NaN", True, float("nan"), float("inf")])
def test_tipo_invalido_reprova(valor_ruim):
    rows = lote_valido()
    rows[1]["value"] = valor_ruim

    outcome = validate_batch(rows, MARKET_KIND)
    resultado = checagem(outcome.report, "tipos_numericos")

    assert resultado.passed is False
    assert resultado.severity == SEVERITY_CRITICAL
    assert resultado.details["reprovadas"] == 1
    assert outcome.report.passed is False


def test_tipo_invalido_nao_passa_por_numero_valido():
    rows = lote_valido()
    rows[0]["value"] = 1300  # int é aceito

    assert checagem(validate_batch(rows, MARKET_KIND).report, "tipos_numericos").passed is True


# ---------------------------------------------------------------------------
# Teste Obrigatório 8 — valor fora dos limites físicos
# ---------------------------------------------------------------------------

def test_valor_fora_dos_limites_reprova():
    rows = lote_valido()
    rows[0]["value"] = -50.0  # preco_arabica mínimo é 10.0

    outcome = validate_batch(rows, MARKET_KIND)
    resultado = checagem(outcome.report, "limites_fisicos")

    assert resultado.passed is False
    assert resultado.details["reprovadas"] == 1
    assert resultado.details["variavel_alvo_afetada"] is True
    assert outcome.report.passed is False
    assert len(outcome.invalid_rows) == 1


def test_ruido_pontual_fora_dos_limites_e_tolerado():
    """Até MAX_INVALID_SHARE de leituras ruins não bloqueia a carga diária."""
    serie = datas(100)
    rows = [
        linha_clima(d, "bambui", "precip_mm", -1.0 if i < 5 else 10.0, "mm")
        for i, d in enumerate(serie)
    ]

    outcome = validate_batch(rows, WEATHER_KIND)
    resultado = checagem(outcome.report, "limites_fisicos")

    assert resultado.details["share"] == 0.05
    assert resultado.details["share"] <= MAX_INVALID_SHARE
    assert resultado.passed is True, "5% de ruído em variável não-alvo deveria ser tolerado"
    assert outcome.report.passed is True
    # Mesmo toleradas, as linhas ruins ficam em quarentena.
    assert len(outcome.invalid_rows) == 5


def test_ruido_acima_da_tolerancia_bloqueia():
    serie = datas(100)
    rows = [
        linha_clima(d, "bambui", "precip_mm", -1.0 if i < 6 else 10.0, "mm")
        for i, d in enumerate(serie)
    ]

    resultado = checagem(validate_batch(rows, WEATHER_KIND).report, "limites_fisicos")

    assert resultado.details["share"] > MAX_INVALID_SHARE
    assert resultado.passed is False


def test_variavel_alvo_fora_dos_limites_nunca_e_tolerada():
    serie = datas(100)
    rows = [
        linha_mercado(d, "preco_arabica", -1.0 if i == 0 else 1200.0, "R$/sc 60kg")
        for i, d in enumerate(serie)
    ]

    resultado = checagem(validate_batch(rows, MARKET_KIND).report, "limites_fisicos")

    assert resultado.details["share"] == 0.01
    assert resultado.details["variavel_alvo_afetada"] is True
    assert resultado.passed is False, f"Limite inválido em {TARGET_VARIABLE} deve bloquear"


# ---------------------------------------------------------------------------
# Teste Obrigatório 4 — deduplicação
# ---------------------------------------------------------------------------

def test_chave_de_deduplicacao_por_tipo():
    mercado = {"observation_date": datas(1)[0], "variable_name": "preco_arabica",
               "region": None}
    clima = {"observation_date": datas(1)[0], "variable_name": "temp_min",
             "region": "bambui"}

    assert dedup_key(mercado, MARKET_KIND) == (datas(1)[0], "preco_arabica")
    assert dedup_key(clima, WEATHER_KIND) == (datas(1)[0], "bambui", "temp_min")


def test_mesma_regiao_e_data_colidem_apenas_em_clima():
    base = datas(1)[0]
    a = {"observation_date": base, "region": "bambui", "variable_name": "temp_min"}
    b = {"observation_date": base, "region": "cerrado", "variable_name": "temp_min"}

    assert dedup_key(a, WEATHER_KIND) != dedup_key(b, WEATHER_KIND)
    assert dedup_key(a, MARKET_KIND) == dedup_key(b, MARKET_KIND)


def test_deduplicate_mantem_a_ultima_revisao():
    serie = datas(3)
    rows = [
        linha_mercado(serie[0], "preco_arabica", 1000.0, "R$/sc 60kg"),
        linha_mercado(serie[1], "preco_arabica", 1100.0, "R$/sc 60kg"),
        linha_mercado(serie[1], "preco_arabica", 1150.0, "R$/sc 60kg"),  # revisão
    ]

    unicas = deduplicate(rows, MARKET_KIND)

    assert len(unicas) == 2
    assert {r["value"] for r in unicas} == {1000.0, 1150.0}


def test_duplicidades_sao_reportadas_como_aviso():
    serie = datas(3)
    rows = [
        linha_mercado(serie[0], "preco_arabica", 1000.0, "R$/sc 60kg"),
        linha_mercado(serie[0], "preco_arabica", 1000.0, "R$/sc 60kg"),
        linha_mercado(serie[1], "preco_arabica", 1100.0, "R$/sc 60kg"),
    ]

    resultado = checagem(validate_batch(rows, MARKET_KIND).report, "duplicidades")

    assert resultado.passed is False
    assert resultado.severity == SEVERITY_WARNING
    assert resultado.details["chaves_duplicadas"] == 1
    assert resultado.details["linhas_excedentes"] == 1


def test_chaves_distintas_nao_sao_duplicidade():
    base = datas(1)[0]
    rows = [
        linha_clima(base, "bambui", "temp_min", 15.0, "°C"),
        linha_clima(base, "cerrado", "temp_min", 16.0, "°C"),
        linha_clima(base, "bambui", "temp_max", 25.0, "°C"),
    ]

    assert checagem(validate_batch(rows, WEATHER_KIND).report, "duplicidades").passed is True


# ---------------------------------------------------------------------------
# Datas, unidades, continuidade e horizontes
# ---------------------------------------------------------------------------

def test_valor_nulo_vai_para_quarentena():
    rows = lote_valido()
    rows[1]["value"] = None

    outcome = validate_batch(rows, MARKET_KIND)

    assert checagem(outcome.report, "valores_nulos").passed is False
    assert checagem(outcome.report, "tipos_numericos").passed is True
    assert len(outcome.invalid_rows) == 1


def test_data_invalida_reprova():
    rows = lote_valido()
    rows[0]["observation_date"] = "31/02/1990"

    resultado = checagem(validate_batch(rows, MARKET_KIND).report, "datas_validas")

    assert resultado.passed is False
    assert resultado.severity == SEVERITY_CRITICAL


def test_data_posterior_ao_corte_reprova():
    rows = lote_valido()
    corte = datas(1)[0]
    rows[0]["observation_date"] = corte + timedelta(days=10)

    resultado = checagem(validate_batch(rows, MARKET_KIND, cutoff_date=corte).report,
                         "datas_futuras")

    assert resultado.passed is False
    assert resultado.details["cutoff_date"] == corte.isoformat()


def test_corte_nao_afeta_datas_anteriores():
    corte = datas(1)[0] + timedelta(days=30)

    resultado = checagem(validate_batch(lote_valido(), MARKET_KIND, cutoff_date=corte).report,
                         "datas_futuras")

    assert resultado.passed is True


def test_ausencia_de_informacao_futura_usa_a_variavel_alvo():
    serie = datas(5)
    corte = serie[2]
    rows = [
        linha_mercado(serie[0], "preco_arabica", 1200.0, "R$/sc 60kg"),
        linha_mercado(serie[1], "preco_arabica", 1210.0, "R$/sc 60kg"),
        # Clima posterior ao corte é aceitável: não é a variável alvo.
        linha_clima(serie[4], "bambui", "temp_min", 15.0, "°C"),
    ]

    resultado = checagem(validate_batch(rows, MARKET_KIND, cutoff_date=corte).report,
                         "ausencia_informacao_futura")

    assert resultado.passed is True
    assert resultado.details["max_observation_date"] == serie[1].isoformat()


def test_unidade_errada_e_apenas_aviso():
    rows = lote_valido()
    rows[0]["unit"] = "USD/sc"

    outcome = validate_batch(rows, MARKET_KIND)
    resultado = checagem(outcome.report, "unidades")

    assert resultado.passed is False
    assert resultado.severity == SEVERITY_WARNING
    assert outcome.report.passed is True, "Unidade divergente não pode bloquear a carga"
    assert len(outcome.valid_rows) == 5


def test_lacuna_temporal_acima_do_limite_e_aviso():
    serie = [datas(1)[0], datas(1)[0] + timedelta(days=15)]
    rows = [linha_mercado(d, "preco_arabica", 1200.0, "R$/sc 60kg") for d in serie]

    resultado = checagem(validate_batch(rows, MARKET_KIND).report, "continuidade_temporal")

    assert resultado.passed is False
    assert resultado.details["maior_lacuna_dias"] == 15
    assert resultado.severity == SEVERITY_WARNING


def test_clima_exige_continuidade_mais_estricta_que_mercado():
    serie = [datas(1)[0], datas(1)[0] + timedelta(days=5)]
    mercado = [linha_mercado(d, "preco_arabica", 1200.0, "R$/sc 60kg") for d in serie]
    clima = [linha_clima(d, "bambui", "temp_min", 15.0, "°C") for d in serie]

    assert checagem(validate_batch(mercado, MARKET_KIND).report,
                    "continuidade_temporal").passed is True
    assert checagem(validate_batch(clima, WEATHER_KIND).report,
                    "continuidade_temporal").passed is False


def test_volume_minimo_de_registros():
    resultado = checagem(validate_batch(lote_valido(2), MARKET_KIND, min_records=10).report,
                         "registros_minimos")

    assert resultado.passed is False
    assert resultado.severity == SEVERITY_CRITICAL
    assert resultado.details["registros"] == 2


def test_lote_vazio_reprova_por_volume_minimo():
    resultado = checagem(validate_batch([], MARKET_KIND).report, "registros_minimos")

    assert resultado.passed is False


def test_horizontes_exigem_abrangencia_maior_que_o_maior_horizonte():
    resultado = checagem(validate_batch(lote_valido(5), MARKET_KIND).report,
                         "disponibilidade_horizontes")

    assert resultado.passed is False
    assert resultado.details["abrangencia_dias"] == 4
    assert resultado.details["maior_horizonte_dias"] == 90
    assert resultado.severity == SEVERITY_WARNING


def test_horizontes_atendidos_com_serie_longa():
    resultado = checagem(validate_batch(lote_valido(120), MARKET_KIND).report,
                         "disponibilidade_horizontes")

    assert resultado.passed is True


# ---------------------------------------------------------------------------
# Persistência da auditoria
# ---------------------------------------------------------------------------

def test_record_checks_grava_as_doze_checagens(ingest_conn):
    run_id = uuid.uuid4()
    with ingest_conn.cursor() as cur:
        cur.execute("CALL audit.sp_register_pipeline_start(%s, %s, '{}'::jsonb)",
                    (str(run_id), JOB_NAME_TESTES))
    ingest_conn.commit()

    report = validate_batch(lote_valido(), MARKET_KIND).report
    gravadas = record_checks(ingest_conn, report, str(run_id))

    assert gravadas == 12
    with ingest_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS total, count(*) FILTER (WHERE passed) AS ok "
            "FROM audit.data_quality_checks WHERE pipeline_run_id = %s",
            (str(run_id),),
        )
        row = cur.fetchone()

    assert row["total"] == 12
    assert row["ok"] >= 6


def test_record_checks_sem_run_id_tambem_funciona(ingest_conn):
    report = validate_batch(lote_valido(), MARKET_KIND).report

    assert record_checks(ingest_conn, report) == 12


# ---------------------------------------------------------------------------
# SourceFile do stub
# ---------------------------------------------------------------------------

def test_arquivo_stub_conta_as_linhas_dos_dois_tipos():
    arquivo = arquivo_stub(dias=4)

    assert arquivo.row_count == 8
    assert len(arquivo.market_rows) == 4
    assert len(arquivo.weather_rows) == 4
