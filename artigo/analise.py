"""Análises estatísticas do artigo sobre o base_modelo.csv.

Calcula as estatísticas de preço e de estrutura temporal, a correlação cruzada
entre ICE e CEPEA, a correlação de cada variável com o log-retorno dos 91 dias
seguintes (com intervalo de confiança por moving block bootstrap e estabilidade
por terços), o ONI defasado e a checagem do lote 1. O resultado vai para um JSON.

Uso:
    python artigo/analise.py <base_modelo.csv> <saida.json> [--modo original|corrigida]
                             [--com-temperaturas] [--dados-manuais <pasta>]
"""
import argparse
import json

import numpy as np
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar
from statsmodels.stats.diagnostic import acorr_ljungbox
from statsmodels.tsa.stattools import acf, adfuller, kpss

from comum import DADOS_MANUAIS, FIM, GAP, INI, alvo_91, carregar, corrigir

# Classe de cada variável na Tabela IV: M (mantida), R (removida), T (testar).
VARS = {
    "spread_arab_rob": "R", "selic": "T", "preco_media_20d": "R", "ice_kc": "M", "ice_kc_brl_saca": "R",
    "base_local_pct": "R", "sin_ano": "M", "precip_90d_cerrado": "M", "deficit_hidrico_60d_cerrado": "R",
    "preco_robusta": "M", "umidade_rel_cerrado": "M", "precip_90d_sulmg": "M", "umidade_rel_sulmg": "M",
    "deficit_hidrico_60d_sulmg": "R", "precip_30d_cerrado": "M", "tmin_min_30d_sulmg": "M", "cos_ano": "M",
    "usd_brl": "M", "tmin_min_30d_cerrado": "M", "precip_30d_sulmg": "M", "radiacao_mj_sulmg": "M",
    "radiacao_mj_cerrado": "M", "cot_mm_net_pct_oi": "M", "precip_mm_cerrado": "R", "precip_mm_sulmg": "R",
    "ret_1d": "R", "vol_20d": "R", "dias_frio_30d_sulmg": "R", "temp_media_cerrado": "M",
    "dias_secos_seq_sulmg": "R", "dias_quente_30d_sulmg": "M", "brent": "T", "dias_secos_seq_cerrado": "R",
    "oni": "T", "dias_quente_30d_cerrado": "M", "ano_carga_alta": "R", "dxy": "T", "precip_90d_anom_sulmg": "R",
}

# Temperaturas diárias do lote 1 (parecer "testar"). Ficam no fim da lista para
# que as 38 variáveis anteriores consumam a mesma sequência aleatória.
TEMPS = ["temp_min_bambui", "temp_min_sulmg", "temp_min_cerrado", "temp_max_bambui",
         "temp_max_sulmg", "temp_max_cerrado", "temp_media_bambui", "temp_media_sulmg"]

# Feriados de Sexta-Feira Santa no período, em que a ICE não abre.
SEXTAS_SANTAS = ["2024-03-29", "2025-04-18", "2026-04-03"]


def mbb(x, y, rng, bloco=91, reps=2000):
    """IC95% da correlação de Pearson por moving block bootstrap."""
    n = len(x)
    nb = int(np.ceil(n / bloco))
    starts_max = n - bloco
    out = np.empty(reps)
    for i in range(reps):
        st = rng.integers(0, starts_max + 1, nb)
        idx = (st[:, None] + np.arange(bloco)[None, :]).ravel()[:n]
        out[i] = np.corrcoef(x[idx], y[idx])[0, 1]
    return np.nanpercentile(out, [2.5, 97.5])


def dias_pregao_ice():
    """Dias úteis do período em que a ICE abriu."""
    fed = USFederalHolidayCalendar().holidays(INI, FIM)
    # A ICE abre no Columbus Day e no Veterans Day.
    fed = fed[~((fed.month == 10) | ((fed.month == 11) & (fed.day <= 12)))]
    feriados = fed.union(pd.to_datetime(SEXTAS_SANTAS))
    return pd.bdate_range(INI, FIM).difference(feriados)


def estrutura_temporal(R, b, P, dias_cot):
    pq = P.reindex(dias_cot)
    rt = np.log(pq).diff().dropna()
    R["pregoes"] = len(pq)
    R["retornos_pregao"] = len(rt)
    a = adfuller(pq, autolag="AIC")
    k = kpss(pq, regression="c", nlags="auto")
    R["adf_nivel"] = [round(a[0], 2), round(a[1], 2)]
    R["kpss_nivel"] = round(k[0], 2)
    a = adfuller(rt, autolag="AIC")
    k = kpss(rt, regression="c", nlags="auto")
    R["adf_ret_p"] = float(a[1])
    R["kpss_ret"] = [round(k[0], 2), round(k[1], 3)]
    ac = acf(rt, nlags=15)
    R["acf_1_2"] = [round(ac[1], 3), round(ac[2], 3)]
    R["acf_pregao"] = [float(x) for x in ac]
    R["ljung_q10"] = round(float(acorr_ljungbox(rt, lags=[10])["lb_stat"].iloc[0]), 1)
    rc = np.log(P).diff().dropna()
    R["zeros_pct"] = round(100 * (rc == 0).mean(), 1)
    R["acf_cal"] = [float(x) for x in acf(rc, nlags=15)]
    R["n_cal"] = len(rc)
    R["vol_pregao"] = round(100 * rt.std() * np.sqrt(252), 1)
    R["vol20_media"] = round(100 * b["vol_20d"].mean(), 1)


def correlacao_cruzada(R, b, P, dias_cot):
    """Correlação dos log-retornos de CEPEA e ICE nos pregões comuns, defasagens de -5 a 5."""
    alinh = dias_cot.intersection(dias_pregao_ice())
    ri = np.log(b["ice_kc_brl_saca"].reindex(alinh)).diff()
    rp = np.log(P.reindex(alinh)).diff()
    d = pd.concat([rp, ri], axis=1).dropna()
    d.columns = ["cepea", "ice"]
    R["ccf_n"] = len(d)
    # Defasagem positiva: o ICE antecede o CEPEA.
    R["ccf"] = {lag: float(d["cepea"].corr(d["ice"].shift(lag))) for lag in range(-5, 6)}
    R["ccf_banda"] = round(1.96 / np.sqrt(len(d)), 3)


def correlacao_alvo(R, b, y91, variaveis):
    """Correlação com y(91) na amostra comum, IC por bootstrap, terços e Tabela IV."""
    comum = y91.notna()
    for v in variaveis:
        comum &= b[v].notna()
    R["amostra_comum"] = int(comum.sum())
    res = {}
    rng = np.random.default_rng(42)
    for v, cls in variaveis.items():
        x, y = b.loc[comum, v].to_numpy(float), y91[comum].to_numpy(float)
        r = float(np.corrcoef(x, y)[0, 1])
        lo, hi = mbb(x, y, rng)
        tercos = [float(np.corrcoef(x[s], y[s])[0, 1]) if np.std(x[s]) > 0 else float("nan")
                  for s in np.array_split(np.arange(len(x)), 3)]
        sinais = {np.sign(t) for t in tercos if not np.isnan(t)}
        res[v] = {"classe": cls, "n": len(x), "r": r, "ic": [float(lo), float(hi)],
                  "cobre_zero": bool(lo <= 0 <= hi), "troca_sinal": len(sinais) > 1, "tercos": tercos}
    R["vars"] = res
    tab = {}
    for cls in ["M", "R", "T"]:
        sel = [s for s in res.values() if s["classe"] == cls]
        tab[cls] = [len(sel), sum(s["cobre_zero"] for s in sel), sum(s["troca_sinal"] for s in sel)]
    R["tabela_iv"] = tab
    return res


def oni_causal(R, b, y91):
    """ONI do mês m disponível só a partir do 1.º dia de m+1."""
    oni_mes = b["oni"].groupby(b.index.to_period("M")).first()
    serie = pd.Series(b.index.to_period("M").map(lambda m: oni_mes.get(m - 1, np.nan)), index=b.index)
    dd = pd.concat([serie, y91], axis=1).dropna()
    R["oni_causal_r"] = round(float(dd.corr().iloc[0, 1]), 2)


def lote1_usd_brl(R, cep, P, dias_cot):
    """Arábica em dólar (CSV do CEPEA) contra o preço em reais."""
    usd = cep["preco_usd_saca"].reindex(dias_cot)
    brl = P.reindex(dias_cot)
    R["arab_usd_brl_nivel"] = round(float(np.corrcoef(usd, brl)[0, 1]), 3)
    R["arab_usd_brl_ret"] = round(float(np.log(usd).diff().corr(np.log(brl).diff())), 2)


def resumo(R, res):
    print({k: R[k] for k in ["pregoes", "retornos_pregao", "adf_nivel", "kpss_nivel", "kpss_ret", "acf_1_2",
                              "ljung_q10", "zeros_pct", "vol_pregao", "vol20_media", "ccf_n", "ccf_banda",
                              "y91_validos", "tabela_iv", "oni_causal_r", "arab_usd_brl_nivel",
                              "arab_usd_brl_ret"]})
    print("ccf:", {k: round(v, 3) for k, v in R["ccf"].items()})
    for v in ["precip_90d_cerrado", "precip_90d_anom_sulmg", "selic", "ice_kc", "preco_media_20d",
              "ice_kc_brl_saca", "spread_arab_rob", "base_local_pct", "dias_frio_30d_sulmg"]:
        s = res[v]
        print(f"{v:24} r={s['r']:.2f} ic=[{s['ic'][0]:.2f};{s['ic'][1]:.2f}] "
              f"tercos={[round(t, 2) for t in s['tercos']]}")
    print("IC exclui zero:", [v for v, s in res.items() if not s["cobre_zero"]])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("base", help="base_modelo.csv gerado por base/build_base_cafe.py")
    ap.add_argument("saida", help="arquivo JSON de resultados")
    ap.add_argument("--modo", choices=["original", "corrigida"], default="corrigida",
                    help="original: base como gerada; corrigida: preço refeito com o histórico do CEPEA")
    ap.add_argument("--com-temperaturas", action="store_true",
                    help="inclui as 8 temperaturas diárias do lote 1 (46 variáveis)")
    ap.add_argument("--dados-manuais", default=DADOS_MANUAIS, help="pasta com os CSVs do CEPEA")
    args = ap.parse_args()

    base, cep = carregar(args.base, args.dados_manuais)
    if args.modo == "corrigida":
        corrigir(base, cep)
        dias_cot = cep.loc[INI:FIM].index
    else:
        dias_cot = cep.loc[INI:FIM].index.difference(GAP)

    b = base.loc[INI:FIM].copy()
    b["cot_mm_net_pct_oi"] = b["cot_mm_net"] / b["cot_open_interest"]
    P = b["preco_arabica"]
    variaveis = dict(VARS)
    if args.com_temperaturas:
        variaveis.update({t: "T" for t in TEMPS})

    R = {}
    estrutura_temporal(R, b, P, dias_cot)
    correlacao_cruzada(R, b, P, dias_cot)
    y91 = alvo_91(P)
    R["y91_validos"] = int(y91.notna().sum())
    res = correlacao_alvo(R, b, y91, variaveis)
    oni_causal(R, b, y91)
    lote1_usd_brl(R, cep, P, dias_cot)

    with open(args.saida, "w", encoding="utf-8") as f:
        json.dump(R, f, ensure_ascii=False, indent=1)
    resumo(R, res)


if __name__ == "__main__":
    main()
