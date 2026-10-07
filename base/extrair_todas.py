# -*- coding: utf-8 -*-
"""
extrair_todas.py — gera de uma vez os 25 CSVs de auditoria do lote.

Uso:
    python extrair_todas.py

Le base_modelo.csv (mesma pasta) e cria a pasta auditoria/ com um arquivo
por variavel:

    auditoria/auditoria_<variavel>.csv
        data | colunas-insumo | variavel | preco_arabica

Ao final imprime um resumo com cobertura e faixa de cada variavel, que ja
serve para preencher o bloco de estatisticas das fichas.

As duas variaveis nao gravadas (b3_spread_2_1, cot_mm_net_pct_oi) nao
geram arquivo — nao existem no CSV; a ficha delas e de decisao de schema.
"""

from pathlib import Path

import pandas as pd

BASE = "base_modelo.csv"
PASTA_SAIDA = Path("auditoria")

# variavel -> colunas-insumo necessarias para recalcular a mao
# (conforme derivar_clima / derivar_mercado / derivar_calendario)
INSUMOS = {
    # ---- clima -----------------------------------------------------------
    "tmin_min_30d_cerrado":      ["temp_min_cerrado"],
    "dias_quente_30d_bambui":    ["temp_max_bambui"],
    "dias_quente_30d_sulmg":     ["temp_max_sulmg"],
    "dias_quente_30d_cerrado":   ["temp_max_cerrado"],
    "deficit_hidrico_60d_bambui":  ["precip_mm_bambui", "temp_media_bambui",
                                    "temp_max_bambui", "temp_min_bambui"],
    "deficit_hidrico_60d_sulmg":   ["precip_mm_sulmg", "temp_media_sulmg",
                                    "temp_max_sulmg", "temp_min_sulmg"],
    "deficit_hidrico_60d_cerrado": ["precip_mm_cerrado", "temp_media_cerrado",
                                    "temp_max_cerrado", "temp_min_cerrado"],
    "dias_secos_seq_bambui":     ["precip_mm_bambui"],
    "dias_secos_seq_sulmg":      ["precip_mm_sulmg"],
    "dias_secos_seq_cerrado":    ["precip_mm_cerrado"],
    # ---- mercado ---------------------------------------------------------
    "ret_1d":          ["preco_arabica"],
    "vol_20d":         ["preco_arabica"],          # via ret_1d
    "preco_media_20d": ["preco_arabica"],
    "ice_kc_brl_saca": ["ice_kc", "usd_brl"],
    "base_local":      ["preco_arabica", "ice_kc", "usd_brl"],
    "base_local_pct":  ["preco_arabica", "ice_kc", "usd_brl"],
    "spread_arab_rob": ["preco_arabica", "preco_robusta"],
    # ---- calendario (insumo e a propria data) ----------------------------
    "sin_ano": [], "cos_ano": [], "mes": [], "semana_ano": [],
    "ano_carga_alta": [], "fase_fenologica": [], "risco_geada": [],
    "dia_util": [],
}

NAO_GRAVADAS = ("b3_spread_2_1", "cot_mm_net_pct_oi")


def main() -> None:
    if not Path(BASE).exists():
        print(f"{BASE} nao encontrado. Rode este script na mesma pasta "
              "do base_modelo.csv.")
        return

    df = pd.read_csv(BASE)
    PASTA_SAIDA.mkdir(exist_ok=True)

    gerados, problemas = [], []

    print(f"Lendo {BASE}: {len(df)} linhas x {len(df.columns)} colunas\n")
    print(f"{'variavel':<28} {'linhas':>7} {'cobert.':>8} "
          f"{'min':>12} {'max':>12}")
    print("-" * 72)

    for var, insumos in INSUMOS.items():
        cols = ["data"] + insumos + [var]
        if "preco_arabica" not in cols:
            cols.append("preco_arabica")       # p/ correlacao com o alvo

        faltando = [c for c in cols if c not in df.columns]
        if faltando:
            problemas.append((var, faltando))
            print(f"{var:<28} {'--- colunas ausentes: ' + ', '.join(faltando)}")
            continue

        out = df[cols].copy()
        destino = PASTA_SAIDA / f"auditoria_{var}.csv"
        out.to_csv(destino, index=False)
        gerados.append(destino.name)

        s = pd.to_numeric(out[var], errors="coerce")
        cobert = f"{100 * s.notna().mean():.1f}%"
        if s.notna().any():
            print(f"{var:<28} {len(out):>7} {cobert:>8} "
                  f"{s.min():>12.4f} {s.max():>12.4f}")
        else:
            # categorica (fase_fenologica) ou serie vazia
            niveis = out[var].dropna().unique()
            print(f"{var:<28} {len(out):>7} "
                  f"{100 * out[var].notna().mean():>7.1f}% "
                  f"{'categorica: ' + ', '.join(map(str, niveis[:4]))}")

    print("-" * 72)
    print(f"\n{len(gerados)} arquivos gerados em {PASTA_SAIDA}/")

    if problemas:
        print(f"\n{len(problemas)} variavel(is) com colunas ausentes no "
              f"{BASE} — confira a versao do pipeline:")
        for var, faltando in problemas:
            print(f"  {var}: falta {', '.join(faltando)}")

    print("\nSem arquivo (nao existem no CSV; ficha de decisao de schema):")
    for var in NAO_GRAVADAS:
        print(f"  {var}")


if __name__ == "__main__":
    main()
