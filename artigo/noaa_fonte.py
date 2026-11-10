"""Identifica a origem da coluna `oni` do base_modelo.csv.

Compara, mês a mês, o valor da base com a anomalia mensal do Niño 3.4 da NOAA PSL
(ERSST V6, 1981-2010) e com o ONI do NOAA CPC (média móvel trimestral), a partir
das cópias dos arquivos em artigo/noaa/.

Uso:
    python artigo/noaa_fonte.py <base_modelo.csv>
"""
import argparse
from pathlib import Path

import pandas as pd

from comum import FIM, INI

PASTA_NOAA = Path(__file__).resolve().parent / "noaa"
SEM_DADO = -99.99


def ler_psl(caminho):
    """Anomalia mensal do Niño 3.4 no formato da PSL (ano seguido de 12 valores)."""
    valores = {}
    for linha in Path(caminho).read_text(encoding="utf-8").splitlines()[1:]:
        campos = linha.split()
        if len(campos) != 13:
            break
        ano = int(campos[0])
        for mes, v in enumerate(campos[1:], start=1):
            if float(v) != SEM_DADO:
                valores[pd.Period(year=ano, month=mes, freq="M")] = float(v)
    return pd.Series(valores)


def ler_cpc(caminho):
    """ONI do CPC; cada trimestre (DJF, JFM, ...) é atribuído ao seu mês central."""
    tri = ["DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ"]
    valores = {}
    for linha in Path(caminho).read_text(encoding="utf-8").splitlines()[1:]:
        seas, ano, _, anom = linha.split()
        valores[pd.Period(year=int(ano), month=tri.index(seas) + 1, freq="M")] = float(anom)
    return pd.Series(valores)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("base", help="base_modelo.csv gerado por base/build_base_cafe.py")
    args = ap.parse_args()

    base = pd.read_csv(args.base, parse_dates=["data"]).set_index("data").loc[INI:FIM]
    oni_base = base["oni"].groupby(base.index.to_period("M")).first()
    fontes = {"PSL (Niño 3.4 mensal)": ler_psl(PASTA_NOAA / "nina34.anom.data.txt"),
              "CPC (ONI trimestral)": ler_cpc(PASTA_NOAA / "oni.ascii.txt")}
    print(f"meses na base: {len(oni_base)} ({oni_base.index[0]} a {oni_base.index[-1]})")
    for nome, serie in fontes.items():
        comum = oni_base.index.intersection(serie.index)
        iguais = int((oni_base[comum].round(2) == serie[comum].round(2)).sum())
        diferentes = [str(m) for m in comum if round(oni_base[m], 2) != round(serie[m], 2)]
        print(f"{nome}: {iguais} de {len(comum)} meses idênticos"
              + (f"; diferem: {', '.join(diferentes)}" if len(diferentes) <= 3 else ""))


if __name__ == "__main__":
    main()
