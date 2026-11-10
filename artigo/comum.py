"""Leitura da base e correção do preço, compartilhadas pelos scripts do artigo."""
from pathlib import Path

import numpy as np
import pandas as pd

# Período analisado: calendário da view vw_cafe_diario no build de 16/08/2026.
INI, FIM = "2023-08-17", "2026-08-14"

# Pregões do CEPEA ausentes do base_modelo.csv usado na primeira versão do artigo.
GAP = pd.date_range("2026-07-10", "2026-07-24")

CSV_CEPEA = "cafe_arabica_cepea_1996_2026.csv"
RAIZ = Path(__file__).resolve().parent.parent
DADOS_MANUAIS = RAIZ / "base" / "dados_manuais"


def carregar(base_csv, dados_manuais):
    """Lê o base_modelo.csv e o histórico do CEPEA recortado no calendário da base."""
    base = pd.read_csv(base_csv, parse_dates=["data"]).set_index("data")
    cal = base.index
    cep = pd.read_csv(Path(dados_manuais) / CSV_CEPEA, parse_dates=["data"]).set_index("data")
    cep = cep.loc[str(cal[0].date()):str(cal[-1].date())]
    return base, cep


def preco_corrigido(base, cep):
    """Preço do arábica refeito com o histórico completo do CEPEA, no calendário da base."""
    cal = base.index
    p = cep["preco_brl_saca"]
    return p.reindex(p.index.union(cal)).ffill().bfill(limit=45).reindex(cal)


def recalcular_mercado(base):
    """Recalcula as derivadas de preço como em derivar_mercado() de build_base_cafe.py."""
    base["ret_1d"] = np.log(base["preco_arabica"]).diff()
    base["vol_20d"] = base["ret_1d"].rolling(20, min_periods=10).std() * np.sqrt(252)
    base["preco_media_20d"] = base["preco_arabica"].rolling(20, min_periods=10).mean()
    base["base_local"] = base["preco_arabica"] - base["ice_kc_brl_saca"]
    base["base_local_pct"] = base["base_local"] / base["ice_kc_brl_saca"]
    base["spread_arab_rob"] = base["preco_arabica"] - base["preco_robusta"]


def corrigir(base, cep):
    """Aplica a correção do preço e das derivadas de mercado à base (no lugar)."""
    base["preco_arabica"] = preco_corrigido(base, cep)
    recalcular_mercado(base)


def alvo_91(preco):
    """Log-retorno acumulado dos 91 dias seguintes."""
    return np.log(preco.shift(-91)) - np.log(preco)
