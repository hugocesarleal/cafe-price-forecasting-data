"""Coleta real: transformação de cada fonte, cache da B3 e tratamento de falhas.

Os downloads são substituídos por dados fixos, então estes testes rodam sem rede
e sem banco. O único teste que acessa as fontes de verdade fica desligado por
padrão; para rodá-lo: ``RUN_LIVE_TESTS=1 python -m pytest tests/test_agrobr_real.py``.
"""

import os
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src import agrobr_real
from src.agrobr_client import SourceFetchError, get_agrobr_client
from src.agrobr_real import RealAgrobrClient, front_contract
from src.config import settings
from src.validation import validate_batch

HOJE = date(2026, 10, 7)          # quarta-feira
INICIO = date(2026, 9, 21)        # segunda-feira
FIM = date(2026, 10, 6)
UTEIS = [d for d in (INICIO + timedelta(days=i) for i in range((FIM - INICIO).days + 1))
         if d.weekday() < 5]      # 12 pregões


def escrever_csv(pasta, nome, linhas):
    pasta.mkdir(parents=True, exist_ok=True)
    conteudo = "data,preco_brl_saca,preco_usd_saca\n" + "".join(
        f"{d.isoformat()},{brl},{usd}\n" for d, brl, usd in linhas)
    (pasta / nome).write_text(conteudo, encoding="utf-8")


def ajustes_do_dia(dia, proximo=None, base=350.0):
    """Arquivo de ajustes de um pregão, no formato devolvido pelo agrobr."""
    linhas = [
        {"data": dia, "vencimento_codigo": "H27", "vencimento_mes": 3, "vencimento_ano": 2027,
         "ajuste_atual": base - 5},
        {"data": dia, "vencimento_codigo": "Z26", "vencimento_mes": 12, "vencimento_ano": 2026,
         "ajuste_atual": base},
    ]
    if proximo:  # a linha "do pregão seguinte" que só repete o ajuste de hoje
        linhas.append({"data": proximo, "vencimento_codigo": "Z26", "vencimento_mes": 12,
                       "vencimento_ano": 2026, "ajuste_atual": base})
    return pd.DataFrame(linhas).assign(data=lambda f: pd.to_datetime(f["data"]))


class ClienteFalso(RealAgrobrClient):
    """Cliente real com as cinco chamadas de rede trocadas por dados fixos."""

    def __init__(self, tmp_path, start_date=INICIO, end_date=FIM, feriados=()):
        super().__init__(start_date=start_date, end_date=end_date,
                         manual_dir=str(tmp_path / "manual"),
                         cache_dir=str(tmp_path / "cache"), today=lambda: HOJE)
        self.feriados = set(feriados)
        self.b3_pedidos = []
        self.chamadas = {"ptax": 0}
        escrever_csv(tmp_path / "manual", self.ARABICA_FILE,
                     [(d, 1500 + i, 300 + i) for i, d in enumerate(UTEIS[:8])])
        escrever_csv(tmp_path / "manual", self.ROBUSTA_FILE,
                     [(d, 900 + i, 180 + i) for i, d in enumerate(UTEIS[:8])])

    def _download_cepea_series(self, serie_id):
        raise ConnectionError("site do CEPEA indisponível")  # cai para CSV + agrobr

    def _download_cepea(self, produto):
        base = 1600.0 if produto == "cafe" else 950.0
        return pd.DataFrame({"data": pd.to_datetime(UTEIS[6:]),
                             "valor": [base + i for i in range(len(UTEIS[6:]))]})

    def _download_ptax(self):
        self.chamadas["ptax"] += 1
        return pd.DataFrame({
            "data_hora": [pd.Timestamp(d) + pd.Timedelta(hours=13) for d in UTEIS],
            "cotacao_venda": [5.0 + i / 100 for i in range(len(UTEIS))],
            "cotacao_compra": [4.99 + i / 100 for i in range(len(UTEIS))],
        })

    def _download_b3(self, dias):
        self.b3_pedidos.append(list(dias))
        return {d: None if d in self.feriados
                else ajustes_do_dia(d, proximo=d + timedelta(days=1), base=350.0 + d.day)
                for d in dias}

    def _download_weather(self, lat, lon):
        dias = pd.date_range(self.start_date, self.end_date, freq="D")
        quadro = pd.DataFrame({"data": dias, "lat": lat, "lon": lon})
        for i, campo in enumerate(agrobr_real.WEATHER_FIELDS):
            quadro[campo] = 10.0 + i + np.arange(len(dias)) / 10
        quadro.loc[quadro.index[-2:], list(agrobr_real.WEATHER_FIELDS)] = np.nan  # ainda não publicado
        return quadro

    def _download_ice(self):
        return pd.Series([300.0 + i for i in range(len(UTEIS))], index=pd.to_datetime(UTEIS))

    def _ice_session_closed(self, dia):
        return True


@pytest.fixture
def cliente(tmp_path):
    return ClienteFalso(tmp_path)


def valores(rows):
    return {r["observation_date"]: r["value"] for r in rows}


# ---------------------------------------------------------------------------
# Fábrica e janela
# ---------------------------------------------------------------------------

def test_fabrica_devolve_o_cliente_real_com_a_janela(tmp_path):
    criado = get_agrobr_client("real", start_date=INICIO, end_date=FIM,
                               cache_dir=str(tmp_path))

    assert isinstance(criado, RealAgrobrClient)
    assert (criado.start_date, criado.end_date) == (INICIO, FIM)


def test_janela_padrao_recua_os_anos_configurados(tmp_path):
    criado = RealAgrobrClient(cache_dir=str(tmp_path), today=lambda: HOJE)

    assert criado.end_date == HOJE
    assert criado.start_date == date(HOJE.year - settings.HISTORICAL_YEARS, 10, 7)


def test_janela_invertida_e_rejeitada(tmp_path):
    with pytest.raises(ValueError, match="Janela inválida"):
        RealAgrobrClient(start_date=FIM, end_date=INICIO, cache_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# CEPEA: CSV manual + complemento recente
# ---------------------------------------------------------------------------

def test_cepea_junta_o_csv_manual_com_as_cotacoes_recentes(cliente):
    precos = valores(cliente._cepea_rows("arabica", "preco_arabica", "cafe"))

    assert sorted(precos) == UTEIS, "Histórico e complemento cobrem a janela sem duplicar datas"
    assert precos[UTEIS[0]] == 1500.0, "Dias antigos vêm do arquivo"
    assert precos[UTEIS[-1]] == 1605.0, "Dias além do arquivo vêm do agrobr"
    # Nos dois dias em que ambos têm dado, vale a cotação recente.
    assert precos[UTEIS[6]] == 1600.0 and precos[UTEIS[7]] == 1601.0


def test_cepea_tira_a_media_quando_ha_mais_de_uma_praca_no_dia(cliente, monkeypatch):
    dia = pd.Timestamp(UTEIS[-1])
    monkeypatch.setattr(cliente, "_download_cepea", lambda produto: pd.DataFrame(
        {"data": [dia, dia], "valor": [1000.0, 1100.0]}))

    assert valores(cliente._cepea_rows("arabica", "preco_arabica", "cafe"))[UTEIS[-1]] == 1050.0


def test_cepea_ignora_cotacao_fora_da_janela(cliente, monkeypatch):
    monkeypatch.setattr(cliente, "_download_cepea", lambda produto: pd.DataFrame(
        {"data": pd.to_datetime([FIM, HOJE]), "valor": [1700.0, 9999.0]}))

    precos = valores(cliente._cepea_rows("arabica", "preco_arabica", "cafe"))

    assert precos[FIM] == 1700.0
    assert HOJE not in precos


def test_cepea_sem_complemento_usa_so_o_arquivo(cliente, monkeypatch, caplog):
    def falhar(produto):
        raise ConnectionError("CEPEA fora do ar")

    monkeypatch.setattr(cliente, "_download_cepea", falhar)

    precos = valores(cliente._cepea_rows("arabica", "preco_arabica", "cafe"))

    assert sorted(precos) == UTEIS[:8]
    assert "complemento recente indisponível" in caplog.text


def test_cepea_avisa_quando_ha_buraco_entre_o_arquivo_e_o_agrobr(tmp_path, caplog):
    inicio = INICIO - timedelta(days=60)
    cliente = ClienteFalso(tmp_path, start_date=inicio)
    escrever_csv(tmp_path / "manual", cliente.ARABICA_FILE, [(inicio, 1400, 280)])

    cliente._cepea_rows("arabica", "preco_arabica", "cafe")

    assert "buraco" in caplog.text and "exportação atualizada" in caplog.text


def test_cepea_baixa_a_serie_do_site_e_atualiza_o_csv(cliente, monkeypatch):
    from test_cepea_series import planilha

    pedidos = []
    monkeypatch.setattr(settings, "CEPEA_AUTO_DOWNLOAD", True)
    monkeypatch.setattr(cliente, "_download_cepea_series",
                        lambda serie_id: pedidos.append(serie_id) or planilha(dias=UTEIS, base=1500.0))
    monkeypatch.setattr(cliente, "_download_cepea",
                        lambda produto: pytest.fail("com a série do site, o agrobr não é consultado"))

    precos = valores(cliente._cepea_rows("arabica", "preco_arabica", "cafe"))

    assert pedidos == [23]
    assert sorted(precos) == UTEIS, "A série do site cobre a janela inteira, sem buraco"
    assert precos[UTEIS[-1]] == 1500.0 + len(UTEIS) - 1
    gravado = (cliente.manual_dir / cliente.ARABICA_FILE).read_text(encoding="utf-8")
    assert len(gravado.splitlines()) == len(UTEIS) + 1, "O CSV manual foi atualizado"


def test_cepea_serie_suspeita_nao_substitui_o_csv(cliente, monkeypatch, caplog):
    from test_cepea_series import planilha

    antes = (cliente.manual_dir / cliente.ARABICA_FILE).read_bytes()
    monkeypatch.setattr(settings, "CEPEA_AUTO_DOWNLOAD", True)
    monkeypatch.setattr(cliente, "_download_cepea_series",
                        lambda serie_id: planilha(dias=UTEIS, titulo="INDICADOR DA SOJA"))

    precos = valores(cliente._cepea_rows("arabica", "preco_arabica", "cafe"))

    assert (cliente.manual_dir / cliente.ARABICA_FILE).read_bytes() == antes
    assert "série não atualizada pelo site" in caplog.text
    assert sorted(precos) == UTEIS, "Segue com o CSV existente mais o complemento do agrobr"


def test_download_do_cepea_vem_desligado_por_padrao(cliente, monkeypatch):
    assert settings.CEPEA_AUTO_DOWNLOAD is False
    monkeypatch.setattr(cliente, "_download_cepea_series",
                        lambda serie_id: pytest.fail("download desligado"))

    assert sorted(valores(cliente._cepea_rows("arabica", "preco_arabica", "cafe"))) == UTEIS


def test_cepea_sem_arquivo_manual_e_erro_de_coleta(cliente):
    (cliente.manual_dir / cliente.ARABICA_FILE).unlink()

    with pytest.raises(SourceFetchError, match="CSV de preços não encontrado"):
        cliente._cepea_rows("arabica", "preco_arabica", "cafe")


# ---------------------------------------------------------------------------
# BCB PTAX
# ---------------------------------------------------------------------------

def test_ptax_usa_a_cotacao_de_venda_do_ultimo_boletim_do_dia(cliente, monkeypatch):
    dia = pd.Timestamp(FIM)
    monkeypatch.setattr(cliente, "_download_ptax", lambda: pd.DataFrame({
        "data_hora": [dia + pd.Timedelta(hours=13), dia + pd.Timedelta(hours=10)],
        "cotacao_venda": [5.25, 5.10],
        "cotacao_compra": [5.24, 5.09],
    }))

    linhas = cliente._ptax_rows()

    assert valores(linhas) == {FIM: 5.25}
    assert linhas[0]["variable_name"] == "usd_brl" and linhas[0]["unit"] == "BRL"


# ---------------------------------------------------------------------------
# B3
# ---------------------------------------------------------------------------

def test_primeiro_vencimento_e_o_mais_proximo():
    ajustes = pd.concat([ajustes_do_dia(UTEIS[0], base=360.0), ajustes_do_dia(UTEIS[1], base=370.0)])
    ajustes["data"] = ajustes["data"].dt.date

    assert front_contract(ajustes).to_dict() == {UTEIS[0]: 360.0, UTEIS[1]: 370.0}


def test_b3_entrega_o_ajuste_do_primeiro_vencimento_de_cada_pregao(cliente):
    ajustes = valores(cliente._b3_rows())

    assert sorted(ajustes) == UTEIS
    assert ajustes[UTEIS[0]] == 350.0 + UTEIS[0].day


def test_b3_descarta_a_linha_datada_do_pregao_seguinte(tmp_path):
    """O arquivo de sexta traz uma linha de segunda repetindo o ajuste de sexta."""
    sexta = date(2026, 9, 25)
    cliente = ClienteFalso(tmp_path, start_date=sexta, end_date=sexta + timedelta(days=1))

    assert valores(cliente._b3_rows()) == {sexta: 350.0 + 25}
    assert set(cliente._load_b3_cache()["data"]) == {sexta}


def test_b3_baixa_em_lotes_pequenos(tmp_path):
    cliente = ClienteFalso(tmp_path, start_date=date(2026, 8, 3), end_date=FIM)

    cliente._b3_rows()

    assert [len(lote) for lote in cliente.b3_pedidos] == [20, 20, 7]
    assert max(map(len, cliente.b3_pedidos)) <= agrobr_real.B3_BATCH_DAYS


def test_b3_nao_baixa_de_novo_o_que_esta_no_cache(cliente):
    primeira = cliente._b3_rows()
    cliente.b3_pedidos.clear()

    segunda = cliente._b3_rows()

    assert cliente.b3_pedidos == [], "Nada pendente, nenhuma requisição"
    assert segunda == primeira


def test_b3_feriado_antigo_e_marcado_e_nao_e_pedido_de_novo(tmp_path):
    feriado = UTEIS[2]
    cliente = ClienteFalso(tmp_path, feriados=[feriado])

    ajustes = valores(cliente._b3_rows())
    cliente.b3_pedidos.clear()
    cliente._b3_rows()

    assert feriado not in ajustes
    assert len(ajustes) == len(UTEIS) - 1
    assert cliente.b3_pedidos == []


def test_b3_pregao_recente_ainda_nao_publicado_e_tentado_de_novo(tmp_path):
    ontem = FIM  # dentro do prazo em que o arquivo pode só estar atrasado
    cliente = ClienteFalso(tmp_path, feriados=[ontem])

    assert ontem not in valores(cliente._b3_rows())
    cliente.b3_pedidos.clear()
    cliente.feriados.clear()        # o arquivo foi publicado
    ajustes = valores(cliente._b3_rows())

    assert cliente.b3_pedidos == [[ontem]], "Só o dia que faltava é baixado"
    assert ontem in ajustes


def test_b3_coleta_interrompida_continua_de_onde_parou(tmp_path):
    cliente = ClienteFalso(tmp_path, start_date=date(2026, 8, 3), end_date=FIM)
    original = cliente._download_b3

    def cair_no_segundo_lote(dias):
        if len(cliente.b3_pedidos) == 1:
            raise SourceFetchError("B3: falha de rede")
        return original(dias)

    cliente._download_b3 = cair_no_segundo_lote
    with pytest.raises(SourceFetchError, match="falha de rede"):
        cliente._b3_rows()
    assert len(set(cliente._load_b3_cache()["data"])) == 20, "O primeiro lote ficou salvo"

    cliente._download_b3 = original
    cliente.b3_pedidos.clear()
    ajustes = cliente._b3_rows()

    assert [len(lote) for lote in cliente.b3_pedidos] == [20, 7], "Só o que faltava"
    assert len(ajustes) == 47


# ---------------------------------------------------------------------------
# ICE e clima
# ---------------------------------------------------------------------------

def test_ice_ignora_o_pregao_em_andamento(cliente, monkeypatch):
    monkeypatch.setattr(cliente, "_ice_session_closed", lambda dia: dia < FIM)

    fechamentos = valores(cliente._ice_rows())

    assert FIM not in fechamentos, "Cotação parcial do dia não é fechamento"
    assert sorted(fechamentos) == UTEIS[:-1]


def test_pregao_da_ice_so_conta_depois_de_encerrado(tmp_path):
    cliente = RealAgrobrClient(start_date=INICIO, end_date=FIM, cache_dir=str(tmp_path))

    assert cliente._ice_session_closed(date(2020, 1, 2))
    assert not cliente._ice_session_closed(date(2999, 1, 1))


def test_clima_descarta_dias_ainda_nao_publicados(cliente):
    linhas = cliente._weather_rows()
    ultimo = max(r["observation_date"] for r in linhas)

    assert ultimo == FIM - timedelta(days=2), "Os dois últimos dias vieram vazios"
    assert all(r["value"] is not None and not np.isnan(r["value"]) for r in linhas)
    assert {r["region"] for r in linhas} == {"bambui", "sulmg", "cerrado"}
    assert {r["variable_name"] for r in linhas} == set(agrobr_real.WEATHER_FIELDS)
    assert len(linhas) == 3 * 7 * ((FIM - INICIO).days + 1 - 2)


# ---------------------------------------------------------------------------
# Coleta completa
# ---------------------------------------------------------------------------

def test_coleta_devolve_as_seis_origens_com_as_variaveis_do_modelo(cliente):
    arquivos = cliente.fetch()

    assert [(a.source_name, a.file_name) for a in arquivos] == [
        ("CEPEA/ESALQ", cliente.ARABICA_FILE), ("CEPEA/ESALQ", cliente.ROBUSTA_FILE),
        ("BCB PTAX", "ptax_venda"), ("ICE US", "KC=F"),
        ("B3", "ajustes_icf_1o_vencimento"), ("NASA POWER", "clima_diario"),
    ]
    mercado = {r["variable_name"] for a in arquivos for r in a.market_rows}
    assert mercado == {"preco_arabica", "preco_robusta", "usd_brl", "ice_kc", "b3_cafe_ajuste"}
    assert all(len(a.content_hash) == 64 and a.source_url.startswith("https://") for a in arquivos)


def test_o_que_a_coleta_entrega_passa_na_validacao(cliente):
    for arquivo in cliente.fetch():
        for tipo, linhas in (("market", arquivo.market_rows), ("weather", arquivo.weather_rows)):
            if linhas:
                resultado = validate_batch(linhas, tipo, cutoff_date=FIM)
                assert resultado.report.passed, (arquivo.file_name, resultado.report.summary())
                assert resultado.invalid_rows == []
                assert "unidades" not in resultado.report.summary()["warning_failures"]


def test_hash_so_muda_quando_o_conteudo_muda(cliente, monkeypatch):
    antes = {a.file_name: a.content_hash for a in cliente.fetch()}
    iguais = {a.file_name: a.content_hash for a in cliente.fetch()}
    serie = cliente._download_ice()
    serie.iloc[-1] += 1.0
    monkeypatch.setattr(cliente, "_download_ice", lambda: serie)
    depois = {a.file_name: a.content_hash for a in cliente.fetch()}

    assert iguais == antes, "Coleta repetida sem novidade não parece uma origem alterada"
    assert [nome for nome in antes if antes[nome] != depois[nome]] == ["KC=F"]


def test_falha_de_rede_e_repetida_e_depois_vira_erro_de_coleta(cliente, monkeypatch):
    monkeypatch.setattr(settings, "AGROBR_MAX_RETRIES", 2)

    def cair():
        cliente.chamadas["ptax"] += 1
        raise ConnectionError("BCB fora do ar")

    monkeypatch.setattr(cliente, "_download_ptax", cair)

    with pytest.raises(SourceFetchError, match="BCB PTAX: ConnectionError"):
        cliente.fetch()
    assert cliente.chamadas["ptax"] == 2


def test_fonte_sem_nenhum_dado_na_janela_e_erro_de_coleta(cliente, monkeypatch):
    monkeypatch.setattr(settings, "AGROBR_MAX_RETRIES", 1)
    monkeypatch.setattr(cliente, "_download_ice", lambda: pd.Series(dtype=float))

    with pytest.raises(SourceFetchError, match="ICE US/KC=F"):
        cliente.fetch()


def test_dependencia_ausente_e_erro_claro_e_nao_e_repetida(cliente, monkeypatch):
    def sem_biblioteca():
        cliente.chamadas["ptax"] += 1
        raise ImportError("No module named 'agrobr'")

    monkeypatch.setattr(cliente, "_download_ptax", sem_biblioteca)

    with pytest.raises(RuntimeError, match="pip install -r requirements.txt"):
        cliente.fetch()
    assert cliente.chamadas["ptax"] == 1


# ---------------------------------------------------------------------------
# Contra as fontes de verdade (desligado por padrão)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1",
                    reason="acessa CEPEA, BCB, B3, NASA POWER e Yahoo; defina RUN_LIVE_TESTS=1")
def test_coleta_real_de_uma_semana(tmp_path):
    fim = date.today() - timedelta(days=7)
    cliente = RealAgrobrClient(start_date=fim - timedelta(days=9), end_date=fim,
                               cache_dir=str(tmp_path))

    arquivos = cliente.fetch()

    assert len(arquivos) == 6
    assert all(a.row_count > 0 for a in arquivos)
    for arquivo in arquivos:
        for tipo, linhas in (("market", arquivo.market_rows), ("weather", arquivo.weather_rows)):
            if linhas:
                assert validate_batch(linhas, tipo, cutoff_date=fim).report.passed
