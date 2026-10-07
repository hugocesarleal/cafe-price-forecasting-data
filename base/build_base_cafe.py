"""
============================================================================
BASE DE DADOS PARA PREVISAO DE PRECO DO CAFE
Recorte: regiao de Bambui/MG (Centro-Oeste Mineiro)
============================================================================
Monta um SQLite em FORMATO LARGO com granularidade diaria (calendario cheio,
inclusive fins de semana e feriados), cobrindo os ultimos 3 anos.

Variavel-alvo: Indicador CEPEA/ESALQ Cafe Arabica.

Fontes:
  - agrobr (CEPEA, BCB/PTAX+SGS, CFTC COT, NASA POWER, B3)
  - yfinance (petroleo Brent, ICE Arabica KC, DXY)
  - NOAA CPC (indice ONI - El Nino / La Nina)

Uso:
    pip install agrobr==1.1.0 yfinance
    python build_base_cafe.py

    (o agrobr ja traz pandas, numpy, requests, openpyxl e xlrd)

Saida: cafe_centro_oeste_mg.db
    - tabela  `cafe_diario`   -> base larga, 1 linha por dia
    - tabela  `coleta_log`    -> log de proveniencia de cada coleta
============================================================================
"""

from __future__ import annotations

import re
import sqlite3
import sys
import traceback
import unicodedata
import warnings
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ============================================================================
# CONFIGURACAO
# ============================================================================

ANOS_HISTORICO = 3
VERSAO = "3.0"
DATA_FIM = date.today()
DATA_INICIO = DATA_FIM - timedelta(days=365 * ANOS_HISTORICO)

DB_PATH = "cafe_centro_oeste_mg.db"
CSV_MODELO = "base_modelo.csv"
SCHEMA_SQL = "schema.sql"

# ---------------------------------------------------------------------------
# MODO DE PREENCHIMENTO DE SERIES NAO-DIARIAS
#
#   "periodo" -> preenche TODOS os dias do periodo de referencia com o valor.
#                Ex.: dado mensal de marco preenche 01/03 a 31/03.
#                E o que foi pedido. Simples e intuitivo.
#
#   "causal"  -> so preenche a partir da data em que o dado ficou publico.
#                Ex.: COT de terca so aparece na base a partir da sexta.
#                Evita que o modelo "veja o futuro" no backtest.
#
# Troque para "causal" quando forem rodar o backtest definitivo.
# ---------------------------------------------------------------------------
MODO_PREENCHIMENTO = "periodo"

# Pontos climaticos. NASA POWER tem grid de 0.5 graus (~55 km):
# nao adianta colocar municipios vizinhos, cairiam na mesma celula.
PONTOS_CLIMA = {
    "bambui": (-20.01, -45.98),  # Bambui/MG    - regiao alvo do projeto
    "sulmg": (-21.55, -45.43),  # Varginha/MG    - Sul de Minas
    "cerrado": (-18.94, -46.99),  # Patrocinio/MG  - Cerrado Mineiro
}

# Tickers externos (yfinance)
TICKERS_YF = {
    "brent": "BZ=F",       # petroleo Brent
    "ice_kc": "KC=F",      # ICE Arabica Coffee C
    "dxy": "DX-Y.NYB",     # indice dolar
}

URLS_ONI = [
    "https://psl.noaa.gov/data/correlation/nina34.anom.data",
    "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt",
    "https://origin.cpc.ncep.noaa.gov/products/analysis_monitoring/"
    "ensostuff/detrend.nino34.ascii.txt",
]

LOG: list[dict] = []

# ---------------------------------------------------------------------------
# HISTORICO MANUAL DO CEPEA
#
# A pagina de indicador do CEPEA que o agrobr raspa so mostra ~60 cotacoes.
# Para 3 anos de historico e preciso baixar o XLS de:
#   https://www.cepea.esalq.usp.br/br/consultas-ao-banco-de-dados-do-site.aspx
# e salvar em ./dados_manuais/. O script le o arquivo e usa o agrobr apenas
# para completar os dias mais recentes.
# ---------------------------------------------------------------------------
PASTA_MANUAL = Path("dados_manuais")
# Palavras-chave usadas para reconhecer cada serie. O arquivo e identificado
# pelo nome OU pelo titulo interno, entao nao e preciso renomear o download.
CHAVES_ARABICA = ("arabica",)
CHAVES_ROBUSTA = ("robusta", "conillon", "conilon")

# Ordem de preferencia quando mais de um arquivo bate com a mesma serie.
# CSV vem primeiro: e mais rapido de ler e normalmente ja esta tratado.
PREFERENCIA_EXT = (".csv", ".xlsx", ".xls")

# ---------------------------------------------------------------------------
# ARQUIVOS FIXOS (opcional)
#
# Preencha para forcar um arquivo especifico e desligar a deteccao automatica.
# Use quando houver mais de um arquivo da mesma serie na pasta e voce quiser
# escolher qual vale. Aceita caminho relativo a dados_manuais/ ou absoluto.
#
# Exemplo:
#   ARQUIVOS_FIXOS = {
#       "arabica": "cafe_arabica_cepea_1996_2026.csv",
#       "robusta": "cafe_robusta_cepea_2001_2026.csv",
#   }
# ---------------------------------------------------------------------------
ARQUIVOS_FIXOS: dict[str, str] = {
    "arabica": "",
    "robusta": "",
}


def _slug(texto: str) -> str:
    """Normaliza nome de coluna para uso seguro em SQL."""
    txt = unicodedata.normalize("NFKD", str(texto))
    txt = txt.encode("ascii", "ignore").decode("ascii").lower()
    txt = re.sub(r"[^a-z0-9]+", "_", txt)
    return txt.strip("_")


def _log(fonte: str, status: str, linhas: int = 0, obs: str = "") -> None:
    LOG.append(
        {
            "fonte": fonte,
            "status": status,
            "linhas": linhas,
            "observacao": obs,
            "coletado_em": pd.Timestamp.now().isoformat(timespec="seconds"),
        }
    )
    marca = "OK " if status == "ok" else "FALHA"
    print(f"  [{marca}] {fonte:28} {linhas:>6} linhas  {obs}")


def coletor(nome: str):
    """Decorator: isola falhas. Uma fonte fora do ar nao derruba a coleta toda."""

    def wrapper(func):
        def inner(*args, **kwargs):
            try:
                df = func(*args, **kwargs)
                if df is None or df.empty:
                    _log(nome, "vazio", 0, "retornou vazio")
                    return None
                _log(nome, "ok", len(df))
                return df
            except Exception as e:  # noqa: BLE001
                _log(nome, "erro", 0, f"{type(e).__name__}: {e}")
                if "--debug" in sys.argv:
                    traceback.print_exc()
                return None

        return inner

    return wrapper


# ============================================================================
# DIAGNOSTICO PREVIO
# ============================================================================
#
# Antes de gastar 5-10 minutos coletando, testa cada fonte e mostra de uma vez
# tudo que vai falhar. Checa tres coisas por fonte: modulo Python instalado,
# arquivo local presente e host acessivel na rede.
#
# Host acessivel significa que o DNS resolveu e o servidor respondeu. Um 403
# ou 404 conta como acessivel: a fonte esta no ar e o problema seria outro.
# ----------------------------------------------------------------------------

# fonte -> (host, modulos necessarios, colunas que a fonte gera)
FONTES = {
    "CEPEA cafe arabica": (None, [], ["preco_arabica", "preco_arabica_usd"]),
    "CEPEA cafe robusta": (None, [], ["preco_robusta", "preco_robusta_usd"]),
    "BCB PTAX (USD/BRL)": ("https://olinda.bcb.gov.br", [], ["usd_brl", "usd_brl_compra"]),
    "BCB SGS (Selic, IPCA)": ("https://api.bcb.gov.br", [], ["selic", "ipca"]),
    "CFTC COT cafe": ("https://publicreporting.cftc.gov", [],
                      ["cot_mm_net", "cot_open_interest", "cot_data_pub"]),
    "B3 futuro cafe (ICF)": ("https://arquivos.b3.com.br", [],
                             ["b3_cafe_ajuste", "b3_spread_2_1", "b3_open_interest"]),
    "NASA POWER": ("https://power.larc.nasa.gov", [],
                   ["temp_min_*", "precip_mm_*", "radiacao_mj_*", "vento_ms_*"]),
    "yfinance (Brent, KC, DXY)": ("https://query1.finance.yahoo.com", ["yfinance"],
                                  ["brent", "ice_kc", "dxy"]),
    "NOAA ONI (El Nino)": ("https://psl.noaa.gov", ["requests"],
                           ["oni", "oni_fase", "oni_data_pub"]),
}

# Variaveis derivadas que deixam de existir se a fonte-base faltar
DEPENDENTES = {
    "preco_arabica": ["ret_1d", "vol_20d", "preco_media_20d", "base_local", "spread_arab_rob"],
    "preco_robusta": ["spread_arab_rob"],
    "ice_kc": ["ice_kc_brl_saca", "base_local", "base_local_pct"],
    "usd_brl": ["ice_kc_brl_saca", "base_local", "base_local_pct"],
    "cot_mm_net": ["cot_mm_net_pct_oi"],
}


def _checar_modulo(nome: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(nome) is not None


def _checar_host(url: str, timeout: int = 10) -> tuple[bool, str]:
    """(acessivel, motivo). DNS resolvido + resposta do servidor = acessivel."""
    try:
        import requests
    except ImportError:
        return False, "requests nao instalado"

    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
        return True, f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "getaddrinfo" in msg or "name or service" in msg or "nodename" in msg:
            return False, "DNS nao resolve"
        if "timed out" in msg or "timeout" in msg:
            return False, "timeout"
        if "certificate" in msg or "ssl" in msg:
            return False, "erro de SSL"
        if "proxy" in msg:
            return False, "bloqueado por proxy"
        if isinstance(e, Exception) and "connection" in type(e).__name__.lower():
            return False, "conexao recusada"
        return False, type(e).__name__


def diagnostico() -> set[str]:
    """Testa todas as fontes e devolve o conjunto de colunas indisponiveis."""
    print("\n" + "=" * 70)
    print("  DIAGNOSTICO PREVIO DAS FONTES")
    print("=" * 70)

    indisponiveis: set[str] = set()
    problemas: list[tuple[str, str]] = []

    for fonte, (host, modulos, colunas) in FONTES.items():
        motivos = []

        for mod in modulos:
            if not _checar_modulo(mod):
                motivos.append(f"falta o modulo '{mod}' (pip install {mod})")

        # CEPEA depende de arquivo local, nao de rede
        if fonte.startswith("CEPEA"):
            chaves = CHAVES_ARABICA if "arabica" in fonte else CHAVES_ROBUSTA
            achado = _achar_arquivo(chaves)
            if achado is None:
                motivos.append(
                    f"nenhum arquivo com '{chaves[0]}' em {PASTA_MANUAL}/ "
                    "(agrobr traria so ~60 dias)"
                )
            else:
                print(f"  [OK ] {fonte:28} {achado}")
                continue

        elif host and not motivos:
            ok, motivo = _checar_host(host)
            if ok:
                print(f"  [OK ] {fonte:28} {host.split('//')[1]}  {motivo}")
                continue
            motivos.append(f"{host.split('//')[1]}: {motivo}")

        print(f"  [!! ] {fonte:28} {motivos[0]}")
        problemas.append((fonte, motivos[0]))
        indisponiveis.update(colunas)

    # Propaga para as variaveis derivadas
    derivadas_perdidas: set[str] = set()
    for base, filhas in DEPENDENTES.items():
        if base in indisponiveis:
            derivadas_perdidas.update(filhas)

    print("-" * 70)
    if not problemas:
        print("  Todas as fontes acessiveis.")
    else:
        print(f"  {len(problemas)} fonte(s) com problema.\n")
        print("  Variaveis que NAO serao coletadas:")
        for c in sorted(indisponiveis):
            print(f"    - {c}")
        if derivadas_perdidas:
            print("\n  Variaveis derivadas que deixam de existir por consequencia:")
            for c in sorted(derivadas_perdidas):
                print(f"    - {c}")

        if any(c in indisponiveis for c in ("preco_arabica",)):
            print(
                "\n  " + "!" * 62 + "\n"
                "  CRITICO: a variavel-alvo esta indisponivel.\n"
                "  A base nao servira para treinar o modelo.\n"
                "  " + "!" * 62
            )
    print("=" * 70)

    return indisponiveis | derivadas_perdidas


# ============================================================================
# COLETORES - agrobr
# ============================================================================


def _datas(serie: pd.Series) -> pd.Series:
    """
    Converte coluna de data detectando o formato.

    Necessario porque o export .xls do CEPEA usa dd/mm/aaaa enquanto um CSV
    ja tratado costuma usar ISO. Aplicar dayfirst=True em data ISO troca dia
    por mes silenciosamente (1996-09-02 vira 09/02/1996) e gera colisoes.
    """
    if pd.api.types.is_datetime64_any_dtype(serie):
        return pd.to_datetime(serie, errors="coerce")

    s = serie.astype(str).str.strip()
    iso = s.str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}").mean()
    if iso > 0.8:
        return pd.to_datetime(s, errors="coerce")
    return pd.to_datetime(s, dayfirst=True, errors="coerce")


def _ler_planilha(caminho: Path, header) -> pd.DataFrame:
    """
    Le csv/xls/xlsx. Para .xls tenta xlrd e cai para calamine, porque o
    export do CEPEA dispara 'Workbook corruption' no xlrd embora o arquivo
    seja valido. O calamine vem instalado junto com o agrobr.
    """
    if caminho.suffix.lower() == ".csv":
        return pd.read_csv(caminho, header=header, sep=None, engine="python")

    erros = []
    for engine in (None, "calamine", "openpyxl"):
        try:
            return pd.read_excel(caminho, header=header, engine=engine)
        except Exception as e:  # noqa: BLE001
            erros.append(f"{engine or 'auto'}: {type(e).__name__}")
    raise RuntimeError(f"nao foi possivel ler {caminho.name} ({'; '.join(erros)})")


def _achar_arquivo(chaves: tuple[str, ...]) -> Path | None:
    """
    Procura em dados_manuais/ um arquivo cujo NOME contenha uma das chaves.
    Se nenhum bater, abre cada arquivo e procura a chave no titulo interno
    (o export do CEPEA vem como CEPEA_20260816101737.xls, sem pista no nome).

    Havendo empate, vence a extensao mais alta em PREFERENCIA_EXT (.csv).
    ARQUIVOS_FIXOS desliga a deteccao e forca um arquivo especifico.
    """
    rotulo = "arabica" if "arabica" in chaves else "robusta"
    fixo = ARQUIVOS_FIXOS.get(rotulo, "").strip()
    if fixo:
        p = Path(fixo)
        if not p.is_absolute():
            p = PASTA_MANUAL / p
        return p if p.exists() else None

    if not PASTA_MANUAL.exists():
        return None

    def _ordem(p: Path) -> tuple[int, str]:
        ext = p.suffix.lower()
        pos = PREFERENCIA_EXT.index(ext) if ext in PREFERENCIA_EXT else len(PREFERENCIA_EXT)
        return (pos, p.name.lower())

    candidatos = sorted(
        (p for p in PASTA_MANUAL.iterdir()
         if p.suffix.lower() in PREFERENCIA_EXT and not p.name.startswith("~$")),
        key=_ordem,
    )

    for p in candidatos:
        if any(k in _slug(p.stem) for k in chaves):
            return p

    for p in candidatos:
        try:
            cabecalho = _ler_planilha(p, header=None).head(8)
            texto = _slug(" ".join(map(str, cabecalho.values.ravel())))
            if any(k in texto for k in chaves):
                return p
        except Exception:  # noqa: BLE001, S112
            continue

    return None


def _ler_cepea_manual(chaves: tuple[str, ...]) -> pd.DataFrame | None:
    """
    Le a serie historica exportada por 'Consultas ao Banco de Dados' do CEPEA.

    Aceita tanto o .xls original quanto CSV ja tratado. Procura a linha de
    cabecalho em vez de assumir posicao fixa, porque o numero de linhas de
    titulo muda entre exportacoes.
    """
    caminho = _achar_arquivo(chaves)
    if caminho is None:
        return None

    bruto = _ler_planilha(caminho, header=None)

    linha_cab = None
    for i in range(min(15, len(bruto))):
        if str(bruto.iloc[i, 0]).strip().lower().startswith("data"):
            linha_cab = i
            break
    if linha_cab is None:
        raise ValueError(f"cabecalho 'Data' nao encontrado em {caminho.name}")

    df = _ler_planilha(caminho, header=linha_cab)
    df.columns = [_slug(c) for c in df.columns]

    def _num(serie: pd.Series) -> pd.Series:
        """Converte '1.234,56' (pt-BR) e '1234.56' para float."""
        if pd.api.types.is_numeric_dtype(serie):
            return serie.astype(float)
        s = serie.astype(str).str.strip()
        s = s.str.replace(r"[^\d,.\-]", "", regex=True)
        tem_virgula = s.str.contains(",")
        s = s.where(~tem_virgula,
                    s.str.replace(".", "", regex=False).str.replace(",", ".", regex=False))
        return pd.to_numeric(s, errors="coerce")

    col_data = df.columns[0]
    numericas = [c for c in df.columns[1:] if _num(df[c]).notna().sum() > len(df) * 0.5]
    if not numericas:
        raise ValueError(f"nenhuma coluna numerica em {caminho.name}")

    out = pd.DataFrame()
    out["data"] = _datas(df[col_data])
    out["preco_brl"] = _num(df[numericas[0]])
    # Segunda coluna numerica e a cotacao em USD (o CEPEA exporta as duas)
    if len(numericas) > 1:
        out["preco_usd"] = _num(df[numericas[1]])

    out = out.dropna(subset=["data", "preco_brl"])

    # O CEPEA usa 0 como marcador de ausencia no inicio de algumas series
    # (ex.: robusta em 2001). Zero nao e preco.
    out = out[out["preco_brl"] > 0]

    out = out.set_index("data").sort_index()
    out.index = out.index.normalize()
    return out[~out.index.duplicated(keep="last")]


def _fundir(historico: pd.DataFrame | None, recente: pd.DataFrame | None) -> pd.DataFrame | None:
    """Historico manual como base; agrobr completa os dias mais novos."""
    partes = [p for p in (historico, recente) if p is not None and not p.empty]
    if not partes:
        return None
    df = pd.concat(partes).sort_index()
    return df[~df.index.duplicated(keep="last")]


@coletor("CEPEA cafe arabica")
def get_cafe_arabica() -> pd.DataFrame:
    from agrobr.sync import cepea

    hist = _ler_cepea_manual(CHAVES_ARABICA)
    if hist is not None:
        hist = hist.rename(columns={"preco_brl": "preco_arabica",
                                    "preco_usd": "preco_arabica_usd"})

    recente = None
    try:
        df = cepea.indicador("cafe", inicio=DATA_INICIO, fim=DATA_FIM)
        df["data"] = pd.to_datetime(df["data"]).dt.normalize()
        # A pagina do CEPEA devolve o indicador nacional (posto Sao Paulo).
        # Se vier mais de uma praca, tira a media.
        recente = df.groupby("data")[["valor"]].mean().rename(
            columns={"valor": "preco_arabica"}
        )
    except Exception as e:  # noqa: BLE001
        print(f"       (agrobr recente indisponivel: {e})")

    out = _fundir(hist, recente)
    if out is None:
        raise RuntimeError(
            f"Sem dados de arabica. Coloque o export do CEPEA em {PASTA_MANUAL}/"
        )

    origem = "manual+agrobr" if (hist is not None and recente is not None) else (
        "manual" if hist is not None else "agrobr (~60 dias!)"
    )
    print(f"       origem: {origem}")
    return out.loc[str(DATA_INICIO):str(DATA_FIM)]


@coletor("CEPEA cafe robusta")
def get_cafe_robusta() -> pd.DataFrame:
    from agrobr.sync import cepea

    hist = _ler_cepea_manual(CHAVES_ROBUSTA)
    if hist is not None:
        hist = hist.rename(columns={"preco_brl": "preco_robusta",
                                    "preco_usd": "preco_robusta_usd"})

    recente = None
    try:
        df = cepea.indicador("cafe_robusta", inicio=DATA_INICIO, fim=DATA_FIM)
        df["data"] = pd.to_datetime(df["data"]).dt.normalize()
        recente = df.groupby("data")[["valor"]].mean().rename(
            columns={"valor": "preco_robusta"}
        )
    except Exception as e:  # noqa: BLE001
        print(f"       (agrobr recente indisponivel: {e})")

    out = _fundir(hist, recente)
    if out is None:
        raise RuntimeError(
            f"Sem dados de robusta. Coloque o export do CEPEA em {PASTA_MANUAL}/"
        )
    return out.loc[str(DATA_INICIO):str(DATA_FIM)]


@coletor("BCB PTAX (USD/BRL)")
def get_ptax() -> pd.DataFrame:
    from agrobr.sync import bcb

    # A API PTAX do BCB exige dd/mm/aaaa (diferente do SGS e do resto do script)
    df = bcb.ptax(
        data_inicial=DATA_INICIO.strftime("%d/%m/%Y"),
        data_final=DATA_FIM.strftime("%d/%m/%Y"),
    )
    df["data"] = pd.to_datetime(df["data_hora"]).dt.normalize()
    out = df.groupby("data")[["cotacao_venda", "cotacao_compra"]].last()
    return out.rename(columns={"cotacao_venda": "usd_brl",
                               "cotacao_compra": "usd_brl_compra"})


@coletor("BCB SGS (Selic, IPCA)")
def get_macro_bcb() -> pd.DataFrame:
    from agrobr.sync import bcb

    frames = []
    for serie in ("selic", "ipca"):
        try:
            df = bcb.sgs(serie, data_inicial=DATA_INICIO.strftime("%d/%m/%Y"))
        except Exception as e:  # noqa: BLE001
            print(f"       ({serie} indisponivel: {type(e).__name__})")
            continue
        df["data"] = pd.to_datetime(df["data"]).dt.normalize()
        frames.append(df.set_index("data")[["valor"]].rename(columns={"valor": serie}))

    return pd.concat(frames, axis=1) if frames else pd.DataFrame()


@coletor("CFTC COT cafe")
def get_cot() -> pd.DataFrame:
    from agrobr.sync import cftc

    df = cftc.cot("cafe", start=DATA_INICIO.strftime("%Y-%m-%d"))
    df["data"] = pd.to_datetime(df["data"]).dt.normalize()
    df = df.set_index("data")

    out = pd.DataFrame(index=df.index)
    out["cot_mm_net"] = df["managed_money_net"]
    out["cot_mm_long"] = df["managed_money_long"]
    out["cot_mm_short"] = df["managed_money_short"]
    out["cot_open_interest"] = df["open_interest"]
    # Posicao liquida dos fundos como % do open interest: escala-invariante,
    # comparavel ao longo do tempo (melhor que o valor absoluto).
    out["cot_mm_net_pct_oi"] = df["managed_money_net"] / df["open_interest"]

    # Data em que o relatorio ficou publico: a CFTC apura na terca e divulga
    # na sexta seguinte. Sem esta coluna nao ha como montar a base em modo
    # causal, porque o dado de terca so existe a partir de sexta.
    out["cot_data_pub"] = (df.index + pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    return out


@coletor("B3 futuro cafe (ICF)")
def get_b3_cafe() -> pd.DataFrame:
    from agrobr.sync import b3

    df = b3.historico(contrato="cafe_arabica", inicio=DATA_INICIO, fim=DATA_FIM)
    df["data"] = pd.to_datetime(df["data"]).dt.normalize()

    col = "ajuste_atual" if "ajuste_atual" in df.columns else (
        "ajuste" if "ajuste" in df.columns else df.select_dtypes("number").columns[0]
    )

    # Ordena os vencimentos por data para isolar o 1o contrato (o mais liquido)
    if {"vencimento_ano", "vencimento_mes"}.issubset(df.columns):
        df = df.sort_values(["data", "vencimento_ano", "vencimento_mes"])
        df["ordem_vencimento"] = df.groupby("data").cumcount() + 1
        prim = df[df["ordem_vencimento"] == 1].set_index("data")
        out = prim[[col]].rename(columns={col: "b3_cafe_ajuste"})
        # Spread 2o - 1o vencimento: estrutura a termo. Backwardation
        # (2o abaixo do 1o) sinaliza aperto de oferta no fisico.
        seg = df[df["ordem_vencimento"] == 2].set_index("data")[[col]]
        out["b3_spread_2_1"] = seg[col] - out["b3_cafe_ajuste"]
    else:
        out = df.groupby("data")[[col]].mean().rename(columns={col: "b3_cafe_ajuste"})

    # Posicoes em aberto vem de outro endpoint da B3
    try:
        oi = b3.posicoes_abertas(contrato="cafe_arabica", inicio=DATA_INICIO, fim=DATA_FIM)
        oi["data"] = pd.to_datetime(oi["data"]).dt.normalize()
        out["b3_open_interest"] = oi.groupby("data")["posicoes_abertas"].sum()
    except Exception as e:  # noqa: BLE001
        print(f"       (open interest B3 indisponivel: {type(e).__name__})")

    return out


def get_clima() -> pd.DataFrame | None:
    """
    NASA POWER: clima diario por ponto. Uma chamada por regiao.

    Devolve formato LONGO (uma linha por data+regiao), que e o formato
    de tb_clima. A tabela larga do modelo sai depois, da view SQL.
    """
    from agrobr.sync import nasa_power

    campos = ["temp_min", "temp_max", "temp_media", "precip_mm",
              "umidade_rel", "radiacao_mj", "vento_ms"]
    frames = []

    for regiao, (lat, lon) in PONTOS_CLIMA.items():

        @coletor(f"NASA POWER {regiao}")
        def _fetch(lat=lat, lon=lon):
            return nasa_power.clima_ponto(
                lat=lat,
                lon=lon,
                inicio=DATA_INICIO.strftime("%Y-%m-%d"),
                fim=DATA_FIM.strftime("%Y-%m-%d"),
            )

        df = _fetch()
        if df is None:
            continue
        df["data"] = pd.to_datetime(df["data"]).dt.normalize()
        presentes = [c for c in campos if c in df.columns]
        bloco = df[["data"] + presentes].copy()
        bloco["regiao"] = regiao
        frames.append(bloco)

    return pd.concat(frames, ignore_index=True) if frames else None


# ============================================================================
# COLETORES - fontes externas
# ============================================================================


@coletor("yfinance (Brent, KC, DXY)")
def get_mercados_externos() -> pd.DataFrame:
    import yfinance as yf

    frames = []
    for nome, ticker in TICKERS_YF.items():
        h = yf.Ticker(ticker).history(
            start=DATA_INICIO.strftime("%Y-%m-%d"),
            end=DATA_FIM.strftime("%Y-%m-%d"),
        )
        if h.empty:
            continue
        s = h["Close"].rename(nome)
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        frames.append(s)

    return pd.concat(frames, axis=1) if frames else pd.DataFrame()


@coletor("NOAA ONI (El Nino)")
def get_oni() -> pd.DataFrame:
    """
    Indice Nino 3.4 (mensal). Tenta varias URLs porque o CPC as vezes
    nao resolve DNS e alguns firewalls corporativos bloqueiam o dominio.
    """
    import requests

    ultimo_erro = ""
    for url in URLS_ONI:
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            texto = r.text
        except Exception as e:  # noqa: BLE001
            ultimo_erro = f"{type(e).__name__}"
            continue

        linhas = [ln for ln in texto.splitlines() if ln.strip()]

        # Formato A: SEAS YR TOTAL ANOM  (oni.ascii.txt)
        if linhas[0].split()[:2] == ["SEAS", "YR"]:
            reg = []
            for ln in linhas[1:]:
                p = ln.split()
                if len(p) < 4:
                    continue
                try:
                    ano, anom = int(p[1]), float(p[3])
                except ValueError:
                    continue
                # trimestre movel -> mes central
                seas = ["DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ",
                        "JJA", "JAS", "ASO", "SON", "OND", "NDJ"]
                if p[0] not in seas:
                    continue
                reg.append((ano, seas.index(p[0]) + 1, anom))
            df = pd.DataFrame(reg, columns=["ano", "mes", "oni"])

        # Formato B: matriz ano + 12 meses (nina34.anom.data)
        else:
            reg = []
            for ln in linhas[1:]:
                p = ln.split()
                if len(p) != 13:
                    continue
                try:
                    ano = int(p[0])
                except ValueError:
                    continue
                for m, v in enumerate(p[1:], start=1):
                    val = float(v)
                    if val < -90:  # sentinela de faltante
                        continue
                    reg.append((ano, m, val))
            df = pd.DataFrame(reg, columns=["ano", "mes", "oni"])

        if df.empty:
            ultimo_erro = "parser nao reconheceu o layout"
            continue

        df = df[df["ano"] >= DATA_INICIO.year - 1]
        df["data"] = pd.to_datetime(dict(year=df["ano"], month=df["mes"], day=1))
        # Classificacao oficial do CPC: |ONI| >= 0.5 por 5 trimestres seguidos.
        # Aqui e a versao simplificada, por mes.
        df["oni_fase"] = np.select(
            [df["oni"] >= 0.5, df["oni"] <= -0.5],
            ["el_nino", "la_nina"], default="neutro")
        # O ONI de um mes so e divulgado no inicio do mes seguinte.
        df["oni_data_pub"] = (df["data"] + pd.DateOffset(months=1)).dt.strftime("%Y-%m-%d")
        return df.set_index("data")[["oni", "oni_fase", "oni_data_pub"]].sort_index()

    raise RuntimeError(f"todas as URLs falharam ({ultimo_erro})")


# ============================================================================
# PERSISTENCIA - banco normalizado
# ============================================================================
#
# O banco segue o schema.sql: tabelas separadas por dominio, ligadas por
# chaves estrangeiras a duas dimensoes (tb_fonte e tb_regiao).
#
# Cada gravacao resolve as FKs pelas chaves naturais (sigla da regiao, nome
# da fonte, codigo do contrato) e faz upsert respeitando as constraints
# UNIQUE, para que reexecutar o script atualize em vez de duplicar.
# ----------------------------------------------------------------------------


def criar_banco(caminho: str, schema: str = "schema.sql") -> sqlite3.Connection:
    """Cria o banco a partir do schema.sql se ainda nao existir."""
    novo = not Path(caminho).exists()
    con = sqlite3.connect(caminho)
    con.execute("PRAGMA foreign_keys = ON")

    if novo:
        arq = Path(schema)
        if not arq.exists():
            raise FileNotFoundError(
                f"{schema} nao encontrado. Ele precisa estar na mesma pasta "
                "do script para criar as tabelas."
            )
        con.executescript(arq.read_text(encoding="utf-8"))
        con.commit()
        print(f"  banco criado a partir de {schema}")
    else:
        print(f"  banco existente reaproveitado ({caminho})")

    return con


def _mapa(con: sqlite3.Connection, tabela: str, coluna: str) -> dict[str, int]:
    """Chave natural -> id. Usado para resolver as FKs."""
    return {k: i for i, k in con.execute(f"SELECT id, {coluna} FROM {tabela}")}


def _upsert(con: sqlite3.Connection, tabela: str, df: pd.DataFrame,
            chaves: list[str]) -> int:
    """
    Insere ou atualiza. Em conflito na constraint UNIQUE, atualiza apenas
    as colunas cujo valor novo nao e nulo — assim uma fonte que preenche
    parte das colunas (ex.: PTAX em tb_cambio) nao apaga o que outra fonte
    ja gravou (ex.: DXY do yfinance na mesma tabela).
    """
    df = df.dropna(subset=chaves)
    if df.empty:
        return 0

    cols = list(df.columns)
    marcadores = ", ".join("?" * len(cols))
    atualiza = [c for c in cols if c not in chaves]
    set_clause = ", ".join(
        f"{c} = COALESCE(excluded.{c}, {tabela}.{c})" for c in atualiza
    )

    sql = (
        f"INSERT INTO {tabela} ({', '.join(cols)}) VALUES ({marcadores}) "
        f"ON CONFLICT ({', '.join(chaves)}) DO UPDATE SET {set_clause}"
    )

    registros = [
        tuple(None if pd.isna(v) else v for v in linha)
        for linha in df.itertuples(index=False, name=None)
    ]
    con.executemany(sql, registros)
    con.commit()
    return len(registros)


def _iso(serie) -> pd.Series:
    """Datas como texto ISO — SQLite nao tem tipo DATE nativo.
    Aceita Series e DatetimeIndex."""
    dt = pd.to_datetime(serie)
    if isinstance(dt, pd.DatetimeIndex):
        return pd.Series(dt.strftime("%Y-%m-%d"))
    return dt.dt.strftime("%Y-%m-%d")


def gravar_precos(con, df, especie: str, praca: str) -> int:
    if df is None or df.empty:
        return 0
    prod = _mapa(con, "tb_produto", "especie")
    reg = _mapa(con, "tb_regiao", "sigla")
    fonte = _mapa(con, "tb_fonte", "nome")

    col_brl = f"preco_{especie}" if f"preco_{especie}" in df.columns else df.columns[0]
    col_usd = f"preco_{especie}_usd"

    out = pd.DataFrame({
        "data_ref": _iso(df.index),
        "fk_produto_id": prod[especie],
        "fk_regiao_id": reg[praca],
        "fk_fonte_id": fonte["CEPEA/ESALQ"],
        "preco_brl": df[col_brl].values,
        "preco_usd": df[col_usd].values if col_usd in df.columns else None,
    })
    return _upsert(con, "tb_preco_cafe", out,
                   ["data_ref", "fk_produto_id", "fk_regiao_id"])


def gravar_clima(con, df) -> int:
    if df is None or df.empty:
        return 0
    reg = _mapa(con, "tb_regiao", "sigla")
    fonte = _mapa(con, "tb_fonte", "nome")

    out = pd.DataFrame({"data_ref": _iso(df["data"])})
    out["fk_regiao_id"] = df["regiao"].map(reg).values
    out["fk_fonte_id"] = fonte["NASA POWER"]
    for c in ("temp_min", "temp_max", "temp_media", "precip_mm",
              "umidade_rel", "radiacao_mj", "vento_ms"):
        if c in df.columns:
            out[c] = df[c].values
    return _upsert(con, "tb_clima", out, ["data_ref", "fk_regiao_id"])


def gravar_cambio(con, ptax, externos) -> int:
    fonte = _mapa(con, "tb_fonte", "nome")
    total = 0

    if ptax is not None and not ptax.empty:
        out = pd.DataFrame({
            "data_ref": _iso(ptax.index),
            "fk_fonte_id": fonte["BCB PTAX"],
            "usd_brl_venda": ptax["usd_brl"].values,
        })
        if "usd_brl_compra" in ptax.columns:
            out["usd_brl_compra"] = ptax["usd_brl_compra"].values
        total += _upsert(con, "tb_cambio", out, ["data_ref"])

    if externos is not None and "dxy" in getattr(externos, "columns", []):
        out = pd.DataFrame({
            "data_ref": _iso(externos.index),
            "fk_fonte_id": fonte["Yahoo Finance"],
            "dxy": externos["dxy"].values,
        })
        total += _upsert(con, "tb_cambio", out, ["data_ref"])

    return total


def gravar_macro(con, sgs, externos) -> int:
    fonte = _mapa(con, "tb_fonte", "nome")
    total = 0

    if sgs is not None and not sgs.empty:
        out = pd.DataFrame({"data_ref": _iso(sgs.index),
                            "fk_fonte_id": fonte["BCB SGS"]})
        for c in ("selic", "ipca"):
            if c in sgs.columns:
                out[c] = sgs[c].values
        total += _upsert(con, "tb_macro", out, ["data_ref"])

    if externos is not None and "brent" in getattr(externos, "columns", []):
        out = pd.DataFrame({
            "data_ref": _iso(externos.index),
            "fk_fonte_id": fonte["Yahoo Finance"],
            "brent": externos["brent"].values,
        })
        total += _upsert(con, "tb_macro", out, ["data_ref"])

    return total


def gravar_futuros(con, b3df, externos) -> int:
    ctr = _mapa(con, "tb_contrato", "codigo")
    fonte = _mapa(con, "tb_fonte", "nome")
    total = 0

    if b3df is not None and "b3_cafe_ajuste" in getattr(b3df, "columns", []):
        out = pd.DataFrame({
            "data_ref": _iso(b3df.index),
            "fk_contrato_id": ctr["ICF"],
            "fk_fonte_id": fonte["B3"],
            "ajuste": b3df["b3_cafe_ajuste"].values,
            "ordem_vencimento": 1,
        })
        if "b3_open_interest" in b3df.columns:
            out["open_interest"] = b3df["b3_open_interest"].values
        total += _upsert(con, "tb_futuros", out,
                         ["data_ref", "fk_contrato_id", "ordem_vencimento"])

    if externos is not None and "ice_kc" in getattr(externos, "columns", []):
        out = pd.DataFrame({
            "data_ref": _iso(externos.index),
            "fk_contrato_id": ctr["KC"],
            "fk_fonte_id": fonte["Yahoo Finance"],
            "ajuste": externos["ice_kc"].values,
            "ordem_vencimento": 1,
        })
        total += _upsert(con, "tb_futuros", out,
                         ["data_ref", "fk_contrato_id", "ordem_vencimento"])

    return total


def gravar_cot(con, df) -> int:
    if df is None or df.empty:
        return 0
    ctr = _mapa(con, "tb_contrato", "codigo")
    fonte = _mapa(con, "tb_fonte", "nome")

    out = pd.DataFrame({
        "data_ref": _iso(df.index),
        "fk_contrato_id": ctr["KC"],
        "fk_fonte_id": fonte["CFTC"],
        "mm_long": df["cot_mm_long"].values,
        "mm_short": df["cot_mm_short"].values,
        "mm_net": df["cot_mm_net"].values,
        "open_interest": df["cot_open_interest"].values,
    })
    if "cot_data_pub" in df.columns:
        out["data_pub"] = df["cot_data_pub"].values
    return _upsert(con, "tb_cot", out, ["data_ref", "fk_contrato_id"])


def gravar_enso(con, df) -> int:
    if df is None or df.empty:
        return 0
    fonte = _mapa(con, "tb_fonte", "nome")
    out = pd.DataFrame({
        "data_ref": _iso(df.index),
        "fk_fonte_id": fonte["NOAA CPC"],
        "oni": df["oni"].values,
    })
    if "oni_fase" in df.columns:
        out["fase"] = df["oni_fase"].values
    if "oni_data_pub" in df.columns:
        out["data_pub"] = df["oni_data_pub"].values
    return _upsert(con, "tb_enso", out, ["data_ref"])


def gravar_log(con) -> None:
    fonte = _mapa(con, "tb_fonte", "nome")
    # Casa o rotulo do coletor com o nome cadastrado em tb_fonte
    apelidos = {
        "CEPEA": "CEPEA/ESALQ", "PTAX": "BCB PTAX", "SGS": "BCB SGS",
        "CFTC": "CFTC", "B3": "B3", "NASA": "NASA POWER",
        "yfinance": "Yahoo Finance", "NOAA": "NOAA CPC",
    }
    linhas = []
    for reg in LOG:
        alvo = next((v for k, v in apelidos.items() if k in reg["fonte"]), None)
        if alvo is None or alvo not in fonte:
            continue
        linhas.append((fonte[alvo], reg["coletado_em"], reg["status"],
                       reg["linhas"], reg["observacao"][:300]))
    if linhas:
        con.executemany(
            "INSERT INTO tb_coleta_log "
            "(fk_fonte_id, executado_em, status, linhas, observacao) "
            "VALUES (?,?,?,?,?)", linhas)
        con.commit()


def resumo_banco(con) -> None:
    print("\n" + "=" * 70)
    print("  TABELAS DO BANCO")
    print("=" * 70)
    tabelas = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name LIKE 'tb_%' ORDER BY name")]
    for t in tabelas:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        periodo = ""
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
        if "data_ref" in cols and n:
            ini, fim = con.execute(
                f"SELECT MIN(data_ref), MAX(data_ref) FROM {t}").fetchone()
            periodo = f"  {ini} a {fim}"
        print(f"  {t:<16} {n:>7} registros{periodo}")


# ============================================================================
# PREENCHIMENTO PARA GRANULARIDADE DIARIA
# ============================================================================


def montar_calendario() -> pd.DatetimeIndex:
    return pd.date_range(DATA_INICIO, DATA_FIM, freq="D", name="data")


def preencher(df: pd.DataFrame, calendario: pd.DatetimeIndex) -> pd.DataFrame:
    """
    Reindexa no calendario diario cheio e preenche os vazios.

    MODO_PREENCHIMENTO == "periodo":
        Propaga para frente E para tras dentro do periodo de referencia.
        Um dado mensal de marco preenche 01/03 a 31/03, mesmo tendo sido
        divulgado dia 15. Um dado de sexta preenche o fim de semana.

    MODO_PREENCHIMENTO == "causal":
        Apenas propaga para frente (ffill). O valor so entra na base a partir
        do dia em que existiu.
    """
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.reindex(df.index.union(calendario)).sort_index()

    if MODO_PREENCHIMENTO == "periodo":
        # bfill limitado a 45 dias cobre o maior gap esperado (mensal),
        # sem inventar dados no inicio da serie.
        df = df.ffill().bfill(limit=45)
    else:
        df = df.ffill()

    return df.reindex(calendario)


# ============================================================================
# VARIAVEIS DERIVADAS
# ============================================================================


def derivar_clima(df: pd.DataFrame) -> pd.DataFrame:
    """
    Clima cru explica pouco. O que move preco e ANOMALIA e ACUMULADO.
    """
    for regiao in PONTOS_CLIMA:
        p = f"precip_mm_{regiao}"
        tmin = f"temp_min_{regiao}"
        tmax = f"temp_max_{regiao}"
        tmed = f"temp_media_{regiao}"
        if not {p, tmin, tmax}.issubset(df.columns):
            continue

        # Acumulados de chuva
        df[f"precip_30d_{regiao}"] = df[p].rolling(30, min_periods=15).sum()
        df[f"precip_90d_{regiao}"] = df[p].rolling(90, min_periods=45).sum()

        # Anomalia de chuva 90d: z-score contra a media do dia-do-ano.
        # Com 3 anos de base a climatologia e fraca (n=3 por dia).
        # Se estenderem o historico, isso aqui melhora muito.
        acum = df[f"precip_90d_{regiao}"]
        doy = df.index.dayofyear
        clim = acum.groupby(doy).transform("mean")
        desvio = acum.groupby(doy).transform("std")
        df[f"precip_90d_anom_{regiao}"] = (acum - clim) / desvio.replace(0, np.nan)

        # Risco de geada: dias frios na janela movel de 30 dias
        df[f"dias_frio_30d_{regiao}"] = (
            (df[tmin] < 4).rolling(30, min_periods=15).sum()
        )
        df[f"tmin_min_30d_{regiao}"] = df[tmin].rolling(30, min_periods=15).min()

        # Estresse termico na granacao
        df[f"dias_quente_30d_{regiao}"] = (
            (df[tmax] > 32).rolling(30, min_periods=15).sum()
        )

        # Deficit hidrico simplificado: chuva menos evapotranspiracao
        # aproximada por Hargreaves-lite. Nao substitui balanco hidrico
        # completo, mas capta a tendencia de seca.
        if tmed in df.columns:
            etp = (0.0023 * (df[tmed] + 17.8)
                   * (df[tmax] - df[tmin]).clip(lower=0) ** 0.5 * 15)
            df[f"deficit_hidrico_60d_{regiao}"] = (
                (df[p] - etp).rolling(60, min_periods=30).sum()
            )

        # Dias secos consecutivos (veranico)
        seco = (df[p] < 1).astype(int)
        df[f"dias_secos_seq_{regiao}"] = (
            seco.groupby((seco != seco.shift()).cumsum()).cumsum() * seco
        )

    return df


def derivar_mercado(df: pd.DataFrame) -> pd.DataFrame:
    if "preco_arabica" in df.columns:
        df["ret_1d"] = np.log(df["preco_arabica"]).diff()
        df["vol_20d"] = df["ret_1d"].rolling(20, min_periods=10).std() * np.sqrt(252)
        df["preco_media_20d"] = df["preco_arabica"].rolling(20, min_periods=10).mean()

    # Base local: quanto o preco em BRL desvia do equivalente internacional.
    # ICE KC vem em centavos de USD/libra -> converter para R$/saca 60kg.
    if {"ice_kc", "usd_brl", "preco_arabica"}.issubset(df.columns):
        df["ice_kc_brl_saca"] = df["ice_kc"] / 100 * 132.277 * df["usd_brl"]
        df["base_local"] = df["preco_arabica"] - df["ice_kc_brl_saca"]
        df["base_local_pct"] = df["base_local"] / df["ice_kc_brl_saca"]

    if {"preco_arabica", "preco_robusta"}.issubset(df.columns):
        df["spread_arab_rob"] = df["preco_arabica"] - df["preco_robusta"]

    return df


def derivar_calendario(df: pd.DataFrame) -> pd.DataFrame:
    doy = df.index.dayofyear
    df["sin_ano"] = np.sin(2 * np.pi * doy / 365.25)
    df["cos_ano"] = np.cos(2 * np.pi * doy / 365.25)
    df["mes"] = df.index.month
    df["semana_ano"] = df.index.isocalendar().week.astype(int)

    # Bienalidade: anos pares = carga alta no arabica brasileiro.
    # Regra simplificada; conferir com a serie da CONAB antes de usar em producao.
    df["ano_carga_alta"] = (df.index.year % 2 == 0).astype(int)

    # Fase fenologica no Brasil (hemisferio sul)
    m = df.index.month
    fase = np.select(
        [np.isin(m, [9, 10]), np.isin(m, [11, 12, 1, 2]),
         np.isin(m, [5, 6, 7, 8])],
        ["floracao", "granacao", "colheita"],
        default="pos_colheita",
    )
    df["fase_fenologica"] = fase
    df["risco_geada"] = np.isin(m, [6, 7, 8]).astype(int)
    df["dia_util"] = (df.index.dayofweek < 5).astype(int)
    return df


# ============================================================================
# PIPELINE
# ============================================================================


def main() -> None:
    print("=" * 70)
    print(f"  build_base_cafe.py  v{VERSAO}")
    print(f"  arquivo: {Path(__file__).resolve()}")
    print("=" * 70)
    print(f"Periodo: {DATA_INICIO} a {DATA_FIM}")
    print(f"Modo de preenchimento: {MODO_PREENCHIMENTO}")

    indisponiveis = diagnostico()

    if "--check" in sys.argv:
        print("\n(--check: diagnostico apenas, nada foi coletado)")
        return

    if indisponiveis and "--force" not in sys.argv:
        try:
            resp = input("\nContinuar mesmo assim? [s/N] ").strip().lower()
        except EOFError:
            resp = "s"
        if resp not in ("s", "sim", "y", "yes"):
            print("Cancelado. Use --force para pular esta pergunta.")
            return

    # ---- 1. Banco ----------------------------------------------------------
    print("\nPreparando banco...")
    con = criar_banco(DB_PATH)

    # ---- 2. Coleta ---------------------------------------------------------
    print("\nColetando fontes...")
    arabica = get_cafe_arabica()
    robusta = get_cafe_robusta()
    ptax = get_ptax()
    sgs = get_macro_bcb()
    cot = get_cot()
    b3df = get_b3_cafe()
    clima = get_clima()
    externos = get_mercados_externos()
    enso = get_oni()

    if arabica is None or arabica.empty:
        print("\nSem preco de cafe. Nao ha o que gravar. Abortando.")
        con.close()
        sys.exit(1)

    # ---- 3. Gravacao nas tabelas normalizadas ------------------------------
    print("\nGravando nas tabelas...")
    gravados = {
        "tb_preco_cafe": (gravar_precos(con, arabica, "arabica", "sao_paulo")
                          + gravar_precos(con, robusta, "robusta", "es")),
        "tb_clima":   gravar_clima(con, clima),
        "tb_cambio":  gravar_cambio(con, ptax, externos),
        "tb_macro":   gravar_macro(con, sgs, externos),
        "tb_futuros": gravar_futuros(con, b3df, externos),
        "tb_cot":     gravar_cot(con, cot),
        "tb_enso":    gravar_enso(con, enso),
    }
    for tabela, n in gravados.items():
        print(f"  {tabela:<16} {n:>7} registros")
    gravar_log(con)

    resumo_banco(con)

    # ---- 4. Matriz do modelo, a partir da view -----------------------------
    print("\nMontando matriz do modelo (vw_cafe_diario)...")
    base = pd.read_sql("SELECT * FROM vw_cafe_diario", con)
    if base.empty:
        print("  view vazia — verifique se ha precos gravados.")
        con.close()
        return

    base["data_ref"] = pd.to_datetime(base["data_ref"])
    base = base.set_index("data_ref").sort_index()

    calendario = montar_calendario()
    base = preencher(base, calendario)

    print("Calculando variaveis derivadas...")
    base = derivar_clima(base)
    base = derivar_mercado(base)
    base = derivar_calendario(base)

    base = base.reset_index().rename(columns={"index": "data"})
    base["data"] = pd.to_datetime(base["data"]).dt.strftime("%Y-%m-%d")
    base.columns = ["data"] + [_slug(c) for c in base.columns[1:]]
    base = base.loc[:, ~base.columns.duplicated()]

    base.to_csv(CSV_MODELO, index=False)

    # ---- 5. Relatorio ------------------------------------------------------
    print("\n" + "=" * 70)
    print(f"  matriz do modelo: {len(base)} dias x {len(base.columns)} colunas")
    print("=" * 70)

    if "preco_arabica" in base.columns:
        p = base["preco_arabica"].dropna()
        cobertura = len(p) / len(base)
        print(f"\nPreco arabica: R$ {p.min():.2f} a R$ {p.max():.2f} "
              f"(ultimo: R$ {p.iloc[-1]:.2f})")
        print(f"Cobertura da variavel-alvo: {cobertura:.1%} "
              f"({len(p)} de {len(base)} dias)")

        if cobertura < 0.90:
            print(
                "\n" + "!" * 70 + "\n"
                "  ATENCAO: serie-alvo incompleta. A base NAO esta pronta\n"
                "  para treinar o modelo.\n\n"
                "  Baixe o historico completo em:\n"
                "  cepea.esalq.usp.br/br/consultas-ao-banco-de-dados-do-site.aspx\n"
                f"  e coloque o arquivo em {PASTA_MANUAL}/\n"
                + "!" * 70
            )

    con.close()
    print(f"\nBanco:  {DB_PATH}")
    print(f"Matriz: {CSV_MODELO}")
    print("\n  SELECT * FROM tb_preco_cafe ORDER BY data_ref DESC LIMIT 5;")
    print("  SELECT * FROM vw_cafe_diario ORDER BY data_ref DESC LIMIT 5;")
    print("  SELECT f.nome, l.status, l.linhas FROM tb_coleta_log l")
    print("    JOIN tb_fonte f ON f.id = l.fk_fonte_id;\n")


if __name__ == "__main__":
    main()
