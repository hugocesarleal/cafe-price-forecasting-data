"""Confere a Tabela II: correlação com y(91) reportada nas fichas contra a recalculada.

Valores das fichas transcritos de:
- lote 2: relatório de auditoria do lote 2, Tabelas 1 e 2, coluna "retorno 91d";
- lote 3: ficha de auditoria do lote 3, campo "Corr. c/ alvo ... ret. 91d".

Uso:
    python artigo/tabela2.py <base_modelo.csv> [--dados-manuais <pasta>]
"""
import argparse

import numpy as np
import pandas as pd

from comum import DADOS_MANUAIS, FIM, INI, alvo_91, carregar, corrigir

LOTE2_PRIMARIAS = {
    "temp_media_cerrado": 0.11, "precip_mm_bambui": -0.05, "precip_mm_sulmg": -0.04,
    "precip_mm_cerrado": -0.06, "umidade_rel_bambui": 0.14, "umidade_rel_sulmg": 0.08,
    "umidade_rel_cerrado": 0.16, "radiacao_mj_bambui": 0.12, "radiacao_mj_sulmg": 0.09,
    "radiacao_mj_cerrado": 0.13, "vento_ms_bambui": 0.02, "vento_ms_sulmg": 0.01,
    "vento_ms_cerrado": 0.03, "tmin_min_30d_bambui": -0.08, "tmin_min_30d_sulmg": -0.11,
}
LOTE2_DERIVADAS = {
    "precip_30d_bambui": -0.13, "precip_30d_sulmg": -0.07, "precip_30d_cerrado": -0.15,
    "precip_90d_bambui": -0.28, "precip_90d_sulmg": -0.15, "precip_90d_cerrado": -0.31,
    "precip_90d_anom_bambui": 0.13, "precip_90d_anom_sulmg": 0.46, "precip_90d_anom_cerrado": 0.05,
    "dias_frio_30d_sulmg": 0.13,
}
LOTE3 = {
    "tmin_min_30d_cerrado": -0.07, "dias_quente_30d_bambui": 0.31, "dias_quente_30d_sulmg": 0.15,
    "dias_quente_30d_cerrado": 0.27, "deficit_hidrico_60d_bambui": -0.25, "deficit_hidrico_60d_sulmg": -0.14,
    "deficit_hidrico_60d_cerrado": -0.28, "dias_secos_seq_bambui": 0.24, "dias_secos_seq_sulmg": 0.15,
    "dias_secos_seq_cerrado": 0.23, "ret_1d": 0.07, "vol_20d": 0.09, "preco_media_20d": -0.63,
    "ice_kc_brl_saca": -0.57, "base_local": 0.14, "base_local_pct": -0.51, "spread_arab_rob": -0.74,
    "sin_ano": -0.43, "cos_ano": -0.09, "mes": 0.28, "semana_ano": 0.28, "ano_carga_alta": 0.34,
    "risco_geada": 0.15, "dia_util": 0.00,
}
GRUPOS = [("lote 2 primarias + tmin_min_30d", LOTE2_PRIMARIAS),
          ("lote 2 derivadas de chuva/frio", LOTE2_DERIVADAS),
          ("lote 3 numericas", LOTE3)]

# Diferença acima da qual o valor da ficha é dado como não reproduzido.
TOLERANCIA = 0.03


def preparar(base, cep, modo):
    b = base.copy()
    if modo == "corrigida":
        corrigir(b, cep)
    b = b.loc[INI:FIM]
    return b, alvo_91(b["preco_arabica"])


def comparar(b, y, grupo):
    difs = []
    for v, ficha in grupo.items():
        d = pd.concat([b[v], y], axis=1).dropna()
        r = d.corr().iloc[0, 1] if d.iloc[:, 0].std() > 0 else float("nan")
        troca_sinal = np.sign(r) != np.sign(ficha) and abs(ficha) > 0
        difs.append((v, ficha, r, abs(r - ficha), troca_sinal))
    return difs


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("base", help="base_modelo.csv gerado por base/build_base_cafe.py")
    ap.add_argument("--dados-manuais", default=DADOS_MANUAIS, help="pasta com os CSVs do CEPEA")
    args = ap.parse_args()

    base, cep = carregar(args.base, args.dados_manuais)
    for modo in ["original", "corrigida"]:
        b, y = preparar(base, cep, modo)
        print(f"\n######## base {modo}")
        for nome, grupo in GRUPOS:
            difs = comparar(b, y, grupo)
            n_nao = sum(1 for _, f, r, dd, s in difs if dd > TOLERANCIA or s)
            print(f"{nome}: n={len(difs)} max|dr|={max(d[3] for d in difs):.2f}  "
                  f"nao reproduzem (>0,03 ou sinal)={n_nao}")
            if modo == "original":
                for v, f, r, dd, s in difs:
                    print(f"   {v:28} ficha={f:+.2f} recalc={r:+.3f} |dr|={dd:.3f}{'  SINAL' if s else ''}")


if __name__ == "__main__":
    main()
