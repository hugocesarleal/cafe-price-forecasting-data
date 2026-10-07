"""Download da série histórica do CEPEA: leitura da planilha, conferência e gravação.

Nenhum teste acessa o site: a planilha é montada em memória no formato que o
CEPEA exporta (linhas de título, cabeçalho "Data", datas dd/mm/aaaa e números
com vírgula decimal).
"""

import io
from datetime import date, timedelta

import pandas as pd
import pytest
from openpyxl import Workbook

from src import cepea_series as cs
from src.config import settings

ARABICA, ROBUSTA = cs.SERIES["arabica"], cs.SERIES["robusta"]
DIAS = [d for d in (date(2026, 9, 1) + timedelta(days=i) for i in range(40)) if d.weekday() < 5]


def br(valor):
    """1234.5 -> '1.234,50', como o CEPEA escreve."""
    inteiro, decimal = f"{valor:,.2f}".split(".")
    return inteiro.replace(",", ".") + "," + decimal


def planilha(dias=DIAS, base=1500.0, titulo="INDICADOR DO CAFÉ ARÁBICA CEPEA/ESALQ",
             cabecalho=("Data", "À vista R$", "À vista US$"), iso=False, extras=()):
    livro = Workbook()
    folha = livro.active
    folha.append([titulo])
    folha.append(["Fonte: CEPEA"])
    folha.append([])
    folha.append(list(cabecalho))
    for i, dia in enumerate(dias):
        data = dia.isoformat() if iso else dia.strftime("%d/%m/%Y")
        folha.append([data, br(base + i), br((base + i) / 5)])
    for linha in extras:
        folha.append(list(linha))
    saida = io.BytesIO()
    livro.save(saida)
    return saida.getvalue()


def csv_existente(pasta, serie, dias, base=1500.0):
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / serie.file_name
    caminho.write_text("data,preco_brl_saca,preco_usd_saca\n" + "".join(
        f"{d.isoformat()},{base + i:.2f},{(base + i) / 5:.2f}\n" for i, d in enumerate(dias)),
        encoding="utf-8")
    return caminho


# ---------------------------------------------------------------------------
# Leitura da planilha
# ---------------------------------------------------------------------------

def test_planilha_do_cepea_vira_serie_com_datas_e_precos():
    serie = cs.parse_series(planilha(), ARABICA.palavras_chave)

    assert list(serie.columns) == ["data", "preco_brl_saca", "preco_usd_saca"]
    assert len(serie) == len(DIAS)
    assert serie["data"].iloc[0] == pd.Timestamp(DIAS[0])
    assert serie["preco_brl_saca"].iloc[0] == 1500.0, "'1.500,00' é mil e quinhentos"
    assert serie["preco_usd_saca"].iloc[0] == 300.0
    assert serie["data"].is_monotonic_increasing


def test_data_dia_mes_ano_nao_e_lida_como_mes_dia():
    serie = cs.parse_series(planilha(dias=[date(2026, 2, 3), date(2026, 2, 4)]))

    assert serie["data"].dt.date.tolist() == [date(2026, 2, 3), date(2026, 2, 4)]


def test_data_iso_tambem_e_aceita_sem_trocar_dia_por_mes():
    serie = cs.parse_series(planilha(dias=[date(2026, 2, 3), date(2026, 2, 4)], iso=True))

    assert serie["data"].dt.date.tolist() == [date(2026, 2, 3), date(2026, 2, 4)]


def test_zero_e_linha_sem_preco_nao_sao_cotacao():
    extras = [("15/12/2026", "0,00", "0,00"), ("16/12/2026", "", ""), ("Fonte: Cepea", None, None)]

    serie = cs.parse_series(planilha(extras=extras))

    assert len(serie) == len(DIAS)
    assert (serie["preco_brl_saca"] > 0).all()


def test_data_repetida_fica_com_a_ultima_cotacao():
    extras = [(DIAS[0].strftime("%d/%m/%Y"), "9.999,00", "1.999,80")]

    serie = cs.parse_series(planilha(extras=extras))

    assert len(serie) == len(DIAS)
    assert serie.loc[serie["data"] == pd.Timestamp(DIAS[0]), "preco_brl_saca"].item() == 9999.0


def test_planilha_de_outro_indicador_e_rejeitada():
    outra = planilha(titulo="INDICADOR DA SOJA CEPEA/ESALQ - PARANÁ")

    with pytest.raises(cs.CepeaSeriesError, match="não parece ser a série esperada"):
        cs.parse_series(outra, ARABICA.palavras_chave)


@pytest.mark.parametrize("titulo", ["INDICADOR DO CAFÉ ROBUSTA CEPEA/ESALQ",
                                    "Indicador do Café Conilon/Robusta"])
def test_titulo_do_robusta_e_reconhecido_com_ou_sem_acento(titulo):
    assert len(cs.parse_series(planilha(titulo=titulo), ROBUSTA.palavras_chave)) == len(DIAS)
    with pytest.raises(cs.CepeaSeriesError):
        cs.parse_series(planilha(titulo=titulo), ARABICA.palavras_chave)


def test_planilha_sem_cabecalho_ou_ilegivel_e_erro():
    with pytest.raises(cs.CepeaSeriesError, match="Cabeçalho 'Data'"):
        cs.parse_series(planilha(cabecalho=("Dia", "Valor", "Dolar")))
    with pytest.raises(cs.CepeaSeriesError, match="ilegível"):
        cs.parse_series(b"<html>acesso negado</html>")


# ---------------------------------------------------------------------------
# Conferência contra o CSV existente
# ---------------------------------------------------------------------------

def test_serie_mais_atual_e_aceita_e_informa_os_dias_novos(tmp_path):
    caminho = csv_existente(tmp_path, ARABICA, DIAS[:20])

    resumo = cs.check_against_existing(cs.parse_series(planilha()), caminho)

    assert resumo["linhas"] == len(DIAS) and resumo["linhas_antes"] == 20
    assert resumo["dias_novos"] == len(DIAS) - 20
    assert resumo["diferenca_mediana"] == 0.0
    assert resumo["ate"] == DIAS[-1].isoformat()


def test_serie_menor_que_o_arquivo_atual_e_rejeitada(tmp_path):
    antigos = [DIAS[0] - timedelta(days=i) for i in range(1, 400)]
    caminho = csv_existente(tmp_path, ARABICA, sorted(antigos) + DIAS)

    with pytest.raises(cs.CepeaSeriesError, match="histórico maior por um menor"):
        cs.check_against_existing(cs.parse_series(planilha()), caminho)


def test_serie_que_termina_antes_do_arquivo_atual_e_rejeitada(tmp_path):
    caminho = csv_existente(tmp_path, ARABICA, DIAS)
    # Mais linhas que o arquivo (tem dias antigos a mais), mas sem o último pregão.
    antigos = [DIAS[0] - timedelta(days=i) for i in (3, 2, 1)]

    with pytest.raises(cs.CepeaSeriesError, match="antes do arquivo atual"):
        cs.check_against_existing(
            cs.parse_series(planilha(dias=antigos + DIAS[:-1], base=1497.0)), caminho)


def test_serie_com_precos_de_outro_produto_e_rejeitada(tmp_path):
    caminho = csv_existente(tmp_path, ARABICA, DIAS, base=1500.0)

    with pytest.raises(cs.CepeaSeriesError, match="não parece a mesma série"):
        cs.check_against_existing(cs.parse_series(planilha(base=900.0)), caminho)


def test_sem_arquivo_anterior_nao_ha_o_que_conferir(tmp_path):
    resumo = cs.check_against_existing(cs.parse_series(planilha()), tmp_path / "novo.csv")

    assert resumo == {"linhas": len(DIAS), "de": DIAS[0].isoformat(), "ate": DIAS[-1].isoformat()}


# ---------------------------------------------------------------------------
# Gravação
# ---------------------------------------------------------------------------

def test_csv_gravado_tem_o_formato_que_o_pipeline_le(tmp_path):
    from src.agrobr_client import read_price_csv

    caminho = csv_existente(tmp_path, ARABICA, DIAS[:20])
    resumo = cs.refresh_csv(ARABICA, tmp_path, download=lambda serie_id: planilha())

    linhas = caminho.read_text(encoding="utf-8").splitlines()
    assert linhas[0] == "data,preco_brl_saca,preco_usd_saca"
    assert linhas[1] == f"{DIAS[0].isoformat()},1500.0,300.0"
    assert len(linhas) == len(DIAS) + 1
    assert resumo["gravado"] is True and resumo["serie"] == "arabica"

    lidas, _ = read_price_csv(caminho, "preco_arabica", DIAS[0], DIAS[-1], "CEPEA/ESALQ")
    assert len(lidas) == len(DIAS)
    assert lidas[-1]["value"] == 1500.0 + len(DIAS) - 1


def test_arquivo_anterior_fica_guardado_como_bak(tmp_path):
    caminho = csv_existente(tmp_path, ARABICA, DIAS[:20])
    antes = caminho.read_text(encoding="utf-8")

    cs.refresh_csv(ARABICA, tmp_path, download=lambda serie_id: planilha())

    assert (tmp_path / (ARABICA.file_name + ".bak")).read_text(encoding="utf-8") == antes
    assert not list(tmp_path.glob("*.tmp"))


def test_download_usa_o_id_da_serie(tmp_path):
    pedidos = []

    def baixar(serie_id):
        pedidos.append(serie_id)
        return planilha(titulo="INDICADOR DO CAFÉ ROBUSTA")

    cs.refresh_csv(ARABICA, tmp_path / "a", download=lambda i: pedidos.append(i) or planilha())
    cs.refresh_csv(ROBUSTA, tmp_path / "r", download=baixar)

    assert pedidos == [23, 24]


@pytest.mark.parametrize("falha", [
    lambda i: (_ for _ in ()).throw(ConnectionError("fora do ar")),
    lambda i: planilha(titulo="INDICADOR DA SOJA"),
    lambda i: planilha(base=900.0),
    lambda i: b"<html>verificando seu navegador</html>",
])
def test_qualquer_falha_deixa_o_csv_existente_intacto(tmp_path, falha):
    caminho = csv_existente(tmp_path, ARABICA, DIAS)
    antes = caminho.read_bytes()

    with pytest.raises((ConnectionError, cs.CepeaSeriesError)):
        cs.refresh_csv(ARABICA, tmp_path, download=falha)

    assert caminho.read_bytes() == antes
    assert not (tmp_path / (ARABICA.file_name + ".bak")).exists()


def test_modo_de_conferencia_nao_grava(tmp_path):
    caminho = csv_existente(tmp_path, ARABICA, DIAS[:20])
    antes = caminho.read_bytes()

    resumo = cs.refresh_csv(ARABICA, tmp_path, download=lambda i: planilha(), write=False)

    assert resumo["gravado"] is False and resumo["dias_novos"] == len(DIAS) - 20
    assert caminho.read_bytes() == antes


# ---------------------------------------------------------------------------
# Download e linha de comando
# ---------------------------------------------------------------------------

class RespostaFalsa:
    def __init__(self, content, content_type):
        self.content, self.headers = content, {"content-type": content_type}

    def raise_for_status(self):
        pass


def test_download_pede_a_serie_certa_e_se_identifica(monkeypatch):
    import httpx

    chamadas = []

    def get(url, **kwargs):
        chamadas.append((url, kwargs))
        return RespostaFalsa(b"conteudo-xls", "application/vnd.ms-excel")

    monkeypatch.setattr(httpx, "get", get)

    assert cs.download_series(23) == b"conteudo-xls"
    url, kwargs = chamadas[0]
    assert url == cs.SERIES_URL and kwargs["params"] == {"id": 23}
    assert "cafe-price-forecasting-data" in kwargs["headers"]["User-Agent"]


def test_pagina_html_no_lugar_da_planilha_e_erro(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, **k: RespostaFalsa(b"<html>", "text/html; charset=UTF-8"))

    with pytest.raises(cs.CepeaSeriesError, match="em vez da planilha"):
        cs.download_series(23)


def test_linha_de_comando_atualiza_as_duas_series(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "MANUAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cs, "download_series", lambda serie_id: planilha(
        titulo="CAFÉ ARÁBICA" if serie_id == 23 else "CAFÉ ROBUSTA"))

    assert cs.main([]) == 0
    assert (tmp_path / ARABICA.file_name).is_file() and (tmp_path / ROBUSTA.file_name).is_file()
    assert '"gravado": true' in capsys.readouterr().out


def test_linha_de_comando_segue_para_a_outra_serie_quando_uma_falha(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "MANUAL_DATA_DIR", str(tmp_path))
    # O id do robusta devolvendo a planilha de outro indicador.
    monkeypatch.setattr(cs, "download_series", lambda serie_id: planilha(titulo="CAFÉ ARÁBICA"))

    assert cs.main([]) == 1
    assert (tmp_path / ARABICA.file_name).is_file()
    assert not (tmp_path / ROBUSTA.file_name).exists()
    assert "não parece ser a série esperada" in capsys.readouterr().out


def test_planilha_baixada_pelo_navegador_e_importada_pela_serie_do_titulo(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(settings, "MANUAL_DATA_DIR", str(tmp_path / "manual"))
    monkeypatch.setattr(cs, "download_series",
                        lambda serie_id: pytest.fail("importar não acessa o site"))
    # Nomes de arquivo sem pista, como o CEPEA exporta.
    (tmp_path / "CEPEA_1.xlsx").write_bytes(planilha(titulo="INDICADOR DO CAFÉ ROBUSTA", base=950.0))
    (tmp_path / "CEPEA_2.xlsx").write_bytes(planilha(titulo="INDICADOR DO CAFÉ ARÁBICA"))

    assert cs.main(["--importar", str(tmp_path / "CEPEA_1.xlsx"), str(tmp_path / "CEPEA_2.xlsx")]) == 0

    arabica = (tmp_path / "manual" / ARABICA.file_name).read_text(encoding="utf-8").splitlines()
    robusta = (tmp_path / "manual" / ROBUSTA.file_name).read_text(encoding="utf-8").splitlines()
    assert arabica[1].split(",")[1] == "1500.0" and robusta[1].split(",")[1] == "950.0"
    assert '"origem"' in capsys.readouterr().out


def test_importar_recusa_planilha_de_outro_indicador_e_arquivo_ilegivel(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MANUAL_DATA_DIR", str(tmp_path / "manual"))
    (tmp_path / "soja.xlsx").write_bytes(planilha(titulo="INDICADOR DA SOJA"))
    (tmp_path / "lixo.xls").write_bytes(b"isto nao e uma planilha")

    with pytest.raises(cs.CepeaSeriesError, match="arábica nem do robusta"):
        cs.import_file(tmp_path / "soja.xlsx")
    with pytest.raises(cs.CepeaSeriesError, match="ilegível"):
        cs.import_file(tmp_path / "lixo.xls")
    assert cs.main(["--importar", str(tmp_path / "soja.xlsx")]) == 1
    assert not (tmp_path / "manual").exists()


def test_importar_tambem_confere_contra_o_csv_existente(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MANUAL_DATA_DIR", str(tmp_path))
    caminho = csv_existente(tmp_path, ARABICA, DIAS, base=1500.0)
    antes = caminho.read_bytes()
    (tmp_path / "errada.xlsx").write_bytes(planilha(base=900.0))

    with pytest.raises(cs.CepeaSeriesError, match="não parece a mesma série"):
        cs.import_file(tmp_path / "errada.xlsx")
    assert caminho.read_bytes() == antes


def test_check_pela_linha_de_comando_nao_cria_arquivo(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MANUAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cs, "download_series", lambda serie_id: planilha())

    assert cs.main(["--check", "--serie", "arabica"]) == 0
    assert list(tmp_path.iterdir()) == []
