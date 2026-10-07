"""Testes Obrigatórios 12 e 13: poda limitada a 2 anos e preservação dos brutos.

* 12 — a poda nunca descarta mais que ``MAX_PRUNE_YEARS`` da janela;
* 13 — podar não apaga nada: ``raw`` e ``core`` ficam intactos e as linhas de
  features são apenas marcadas.

A primeira parte roda em memória. A segunda exige PostgreSQL e trabalha numa
transação que nunca é confirmada.
"""

import pathlib
import re
from datetime import date, timedelta

import pytest

from conftest import JOB_NAME_TESTES, TAG_VERSAO_TESTE
from src import dataset_versioning as dv
from src import model_contract as mc
from src import pruning
from src.config import settings
from src.feature_builder import build_feature_matrix

INICIO = date(2017, 1, 1)
CORTE = date(2025, 12, 31)   # janela de 9 anos

RAIZ = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Teste Obrigatório 12 — teto de 2 anos (em memória)
# ---------------------------------------------------------------------------

def test_configuracao_padrao_do_teto_e_de_dois_anos():
    assert settings.MAX_PRUNE_YEARS == 2
    assert pruning.max_prune_days() == 730


def test_poda_de_exatamente_dois_anos_e_aceita():
    assert pruning.validate_prune(INICIO, CORTE, INICIO + timedelta(days=730)) == 730


def test_poda_de_um_dia_alem_do_teto_e_rejeitada():
    with pytest.raises(pruning.PruneRejected, match="teto de 730 dias"):
        pruning.validate_prune(INICIO, CORTE, INICIO + timedelta(days=731))


@pytest.mark.parametrize("dias", [1, 30, 365, 729])
def test_podas_dentro_do_teto_devolvem_os_dias_descartados(dias):
    assert pruning.validate_prune(INICIO, CORTE, INICIO + timedelta(days=dias)) == dias


def test_teto_acompanha_a_configuracao(monkeypatch):
    monkeypatch.setattr(settings, "MAX_PRUNE_YEARS", 1)

    assert pruning.validate_prune(INICIO, CORTE, INICIO + timedelta(days=365)) == 365
    with pytest.raises(pruning.PruneRejected, match="teto de 365 dias"):
        pruning.validate_prune(INICIO, CORTE, INICIO + timedelta(days=366))


def test_poda_que_deixa_menos_que_o_minimo_de_seguranca_e_rejeitada():
    # Janela legada de ~1.096 dias: qualquer poda já a deixaria abaixo do mínimo.
    inicio = CORTE - timedelta(days=settings.DATASET_MIN_DAYS - 1)

    with pytest.raises(pruning.PruneRejected, match="DATASET_MIN_DAYS"):
        pruning.validate_prune(inicio, CORTE, inicio + timedelta(days=1))


def test_poda_sem_confirmacao_da_janela_e_rejeitada(monkeypatch):
    monkeypatch.setattr(settings, "CONFIRM_HISTORICAL_WINDOW", False)

    with pytest.raises(pruning.PruneRejected, match="CONFIRM_HISTORICAL_WINDOW=true"):
        pruning.validate_prune(INICIO, CORTE, INICIO + timedelta(days=30))


@pytest.mark.parametrize("novo_inicio, trecho", [
    (INICIO, "Nada a podar"),
    (INICIO - timedelta(days=10), "Nada a podar"),
    (CORTE + timedelta(days=1), "posterior ao corte"),
])
def test_novo_inicio_fora_da_janela_e_rejeitado(novo_inicio, trecho):
    with pytest.raises(pruning.PruneRejected, match=trecho):
        pruning.validate_prune(INICIO, CORTE, novo_inicio)


def test_execucao_exige_exatamente_uma_forma_de_dizer_a_janela():
    with pytest.raises(ValueError, match="apenas um"):
        pruning.run_prune()
    with pytest.raises(ValueError, match="apenas um"):
        pruning.run_prune(new_start_date=INICIO, prune_days=10)


# ---------------------------------------------------------------------------
# Teste Obrigatório 13 — nada é apagado (em memória)
# ---------------------------------------------------------------------------

def test_nenhum_codigo_do_pipeline_apaga_dados():
    """Nem ``src``, nem ``jobs``, nem o SQL têm DELETE, TRUNCATE ou DROP TABLE."""
    destrutivo = re.compile(r"\b(DELETE\s+FROM|TRUNCATE|DROP\s+TABLE)\b", re.IGNORECASE)
    arquivos = [
        *RAIZ.glob("src/*.py"), *RAIZ.glob("jobs/*.py"),
        *RAIZ.glob("sql/*.sql"), *RAIZ.glob("migrations/*.sql"),
    ]
    assert len(arquivos) > 20, "A varredura precisa enxergar o código"

    achados = [
        f"{arquivo.relative_to(RAIZ)}:{numero}"
        for arquivo in arquivos
        for numero, linha in enumerate(arquivo.read_text(encoding="utf-8").splitlines(), 1)
        if destrutivo.search(linha)
    ]
    assert achados == []


def test_poda_so_escreve_em_model_features():
    fonte = (RAIZ / "src" / "pruning.py").read_text(encoding="utf-8")
    escritas = re.findall(r"\b(?:UPDATE|INSERT INTO)\s+([a-z_]+\.[a-z_]+)", fonte)

    assert set(escritas) == {"features.model_features"}


# ---------------------------------------------------------------------------
# Com PostgreSQL
# ---------------------------------------------------------------------------

TABELAS_PRESERVADAS = (
    "raw.market_observations", "raw.weather_observations", "raw.ingestion_files",
    "core.market_daily", "core.weather_daily",
)


def contagens(conn):
    resultado = {}
    with conn.cursor() as cur:
        for tabela in TABELAS_PRESERVADAS:
            cur.execute(f"SELECT count(*) AS n FROM {tabela}")
            resultado[tabela] = cur.fetchone()["n"]
    return resultado


@pytest.fixture
def versao(ingest_conn, core_no_banco, monkeypatch):
    """Versão de 140 dias; o mínimo de segurança é reduzido para caber nela."""
    monkeypatch.setattr(settings, "DATASET_MIN_DAYS", 30)
    matriz = build_feature_matrix(
        ingest_conn, cutoff_date=core_no_banco["corte"], start_date=core_no_banco["inicio"])
    return dv.create_dataset_version(ingest_conn, matriz, version_tag=TAG_VERSAO_TESTE + "poda")


def test_poda_marca_as_linhas_antigas_sem_apagar_nada(ingest_conn, versao):
    antes = contagens(ingest_conn)
    novo_inicio = versao.start_date + timedelta(days=20)

    resultado = pruning.prune_dataset_version(ingest_conn, novo_inicio, versao.dataset_version_id)

    assert resultado["dias_podados"] == 20
    assert resultado["linhas_marcadas_agora"] == 20
    assert resultado["linhas"] == versao.row_count, "Nenhuma linha de features some"
    assert resultado["linhas_ativas"] == versao.row_count - 20
    assert resultado["inicio_ativo"] == novo_inicio
    assert contagens(ingest_conn) == antes, "raw e core não podem mudar com a poda"
    assert dv.verify_dataset_version(ingest_conn, versao.dataset_version_id), \
        "O conteúdo assinado da versão continua íntegro"


def test_consumidor_recebe_apenas_a_janela_ativa(ingest_conn, versao):
    novo_inicio = versao.start_date + timedelta(days=20)
    pruning.prune_dataset_version(ingest_conn, novo_inicio, versao.dataset_version_id)

    features = mc.load_features(ingest_conn, versao.dataset_version_id)
    dataset = mc.get_latest_dataset(ingest_conn)

    assert len(features) == versao.row_count - 20
    assert features["data_ref"].iloc[0] == novo_inicio
    assert dataset["start_date"] == versao.start_date
    assert dataset["active_start_date"] == novo_inicio
    assert dataset["active_row_count"] == versao.row_count - 20


def test_podas_sucessivas_nao_contornam_o_teto(ingest_conn, versao, monkeypatch):
    monkeypatch.setattr(pruning, "max_prune_days", lambda: 30)
    pruning.prune_dataset_version(
        ingest_conn, versao.start_date + timedelta(days=20), versao.dataset_version_id)

    with pytest.raises(pruning.PruneRejected, match="teto de 30 dias"):
        pruning.prune_dataset_version(
            ingest_conn, versao.start_date + timedelta(days=40), versao.dataset_version_id)

    assert pruning.pruning_status(ingest_conn, versao.dataset_version_id)["linhas_podadas"] == 20


def test_poda_rejeitada_nao_altera_nenhuma_linha(ingest_conn, versao, monkeypatch):
    monkeypatch.setattr(settings, "DATASET_MIN_DAYS", 1096)

    with pytest.raises(pruning.PruneRejected, match="DATASET_MIN_DAYS"):
        pruning.prune_dataset_version(
            ingest_conn, versao.start_date + timedelta(days=5), versao.dataset_version_id)

    assert pruning.pruning_status(ingest_conn, versao.dataset_version_id)["linhas_podadas"] == 0


def test_poda_pode_ser_desfeita(ingest_conn, versao):
    pruning.prune_dataset_version(
        ingest_conn, versao.start_date + timedelta(days=20), versao.dataset_version_id)

    resultado = pruning.restore_pruned(ingest_conn, versao.dataset_version_id)

    assert resultado["linhas_restauradas"] == 20
    assert resultado["linhas_podadas"] == 0
    assert resultado["inicio_ativo"] == versao.start_date
    assert len(mc.load_features(ingest_conn, versao.dataset_version_id)) == versao.row_count


def test_execucao_para_versao_inexistente_e_auditada_e_nao_poda_nada(ingest_conn):
    inexistente = "00000000-0000-0000-0000-00000000dead"

    with pytest.raises(LookupError, match="inexistente"):
        pruning.run_prune(prune_days=10, dataset_version_id=inexistente,
                          conn=ingest_conn, job_name=JOB_NAME_TESTES)

    with ingest_conn.cursor() as cur:
        cur.execute(
            "SELECT status, error_message FROM audit.pipeline_runs WHERE job_name = %s",
            (JOB_NAME_TESTES,),
        )
        registro = cur.fetchone()
    assert registro["status"] == "FAILED"
    assert "inexistente" in registro["error_message"]
