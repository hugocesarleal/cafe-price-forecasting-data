"""Gera as 7 figuras do artigo a partir da base corrigida e do JSON de resultados.

Formatação em português: vírgula decimal, sinal de menos tipográfico, rótulos
sem abreviações e legendas fora dos dados.

Uso:
    python artigo/figuras.py <base_modelo.csv> <resultados.json> <pasta_saida>
                             [--dados-manuais <pasta>]
"""
import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from comum import DADOS_MANUAIS, FIM, INI, carregar, preco_corrigido  # noqa: E402

AZUL, LARANJA, CINZA, VERDE = "#1f3b73", "#c8561e", "#8c8c8c", "#2e8b57"


def configurar_estilo():
    tem_times = any("Times New Roman" in f.name for f in font_manager.fontManager.ttflist)
    fam = "Times New Roman" if tem_times else "serif"
    plt.rcParams.update({
        "font.family": fam, "font.size": 8, "axes.titlesize": 8, "axes.labelsize": 8,
        "xtick.labelsize": 7, "ytick.labelsize": 7, "legend.fontsize": 7,
        "axes.unicode_minus": True, "axes.spines.top": False, "axes.spines.right": False,
        "savefig.bbox": "tight", "savefig.pad_inches": 0.02, "pdf.fonttype": 42,
    })
    return fam


def br(x, casas):
    """Número com vírgula decimal e sinal de menos tipográfico."""
    s = f"{x:.{casas}f}".replace(".", ",")
    return s.replace("-", "−")


def fmt(casas):
    return FuncFormatter(lambda x, _: br(x, casas))


def milhar(x, _):
    return f"{x:,.0f}".replace(",", ".")


def salvar(fig, pasta, nome):
    fig.savefig(pasta / nome)
    plt.close(fig)


def fig_serie(b, pasta):
    fig, ax = plt.subplots(figsize=(3.45, 2.3))
    ax.plot(b.index, b["preco_arabica"], color=AZUL, lw=0.9, label="Indicador CEPEA do arábica")
    ax.plot(b.index, b["ice_kc_brl_saca"], color=LARANJA, lw=0.9, label="ICE KC convertido")
    ax.set_ylabel("R$ por saca de 60 kg")
    ax.yaxis.set_major_formatter(FuncFormatter(milhar))
    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=[1, 7]))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%y"))
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=2, frameon=False)
    salvar(fig, pasta, "fig1_serie.pdf")


def fig_acf(R, pasta):
    fig, axs = plt.subplots(1, 2, figsize=(3.45, 2.0), sharey=True)
    paineis = [(axs[0], "acf_pregao", R["retornos_pregao"], "Pregões", "defasagem (pregões)"),
               (axs[1], "acf_cal", R["n_cal"], "Calendário preenchido", "defasagem (dias)")]
    for ax, chave, n, titulo, xl in paineis:
        v = np.array(R[chave])[1:16]
        ax.bar(np.arange(1, 16), v, color=AZUL, width=0.7)
        banda = 1.96 / np.sqrt(n)
        for s in (1, -1):
            ax.axhline(s * banda, color=LARANJA, ls="--", lw=0.8)
        ax.axhline(0, color="black", lw=0.5)
        ax.set_title(f"{titulo} (n = {n:,})".replace(",", "."))
        ax.set_xlabel(xl)
        ax.set_xticks([1, 5, 10, 15])
    axs[0].set_ylabel("ACF do log-retorno")
    axs[0].yaxis.set_major_formatter(fmt(2))
    salvar(fig, pasta, "fig2_acf.pdf")


def fig_interregional(b, pasta):
    familias = ["temp_min", "temp_media", "temp_max", "umidade_rel", "radiacao_mj", "vento_ms", "precip_mm",
                "precip_30d", "precip_90d", "precip_90d_anom", "tmin_min_30d", "dias_quente_30d",
                "deficit_hidrico_60d", "dias_secos_seq"]
    pares = [("bambui", "cerrado", "Bambuí e Cerrado", AZUL),
             ("bambui", "sulmg", "Bambuí e Sul de Minas", LARANJA),
             ("sulmg", "cerrado", "Sul de Minas e Cerrado", CINZA)]
    fig, ax = plt.subplots(figsize=(3.45, 3.3))
    y = np.arange(len(familias))[::-1]
    h = 0.26
    for i, (a, c, rot, cor) in enumerate(pares):
        vals = [b[[f"{f}_{a}", f"{f}_{c}"]].dropna().corr().iloc[0, 1] for f in familias]
        ax.barh(y + (1 - i) * h, vals, height=h, color=cor, label=rot)
    ax.axvline(0.95, color="black", ls="--", lw=0.8)
    ax.text(0.95, len(familias) - 0.3, "limiar 0,95", ha="right", va="bottom", fontsize=7)
    ax.set_yticks(y)
    ax.set_yticklabels(familias, fontfamily="monospace", fontsize=6.5)
    ax.set_xlim(0.3, 1.0)
    ax.set_xlabel("correlação de Pearson")
    ax.xaxis.set_major_formatter(fmt(1))
    ax.legend(loc="upper center", bbox_to_anchor=(0.45, -0.12), ncol=2, frameon=False)
    salvar(fig, pasta, "fig3_interregional.pdf")


def fig_mapa_calor(b, pasta):
    ordem = ["cot_mm_net_pct_oi", "ice_kc", "preco_robusta", "usd_brl", "sin_ano", "umidade_rel_sulmg",
             "umidade_rel_cerrado", "cos_ano", "precip_30d_sulmg", "precip_30d_cerrado", "tmin_min_30d_sulmg",
             "tmin_min_30d_cerrado", "precip_90d_sulmg", "precip_90d_cerrado", "radiacao_mj_sulmg",
             "radiacao_mj_cerrado", "temp_media_cerrado", "dias_quente_30d_sulmg", "dias_quente_30d_cerrado"]
    b["cot_mm_net_pct_oi"] = b["cot_mm_net"] / b["cot_open_interest"]
    cm = b[ordem].corr()
    # Largura de uma coluna: só o triângulo inferior (a matriz é simétrica), sem a diagonal.
    tri = np.where(np.tril(np.ones(cm.shape, dtype=bool), k=-1), cm.values, np.nan)
    cmap = plt.get_cmap("RdBu_r").copy()
    cmap.set_bad("white")
    fig, ax = plt.subplots(figsize=(2.5, 2.8))
    im = ax.imshow(tri, cmap=cmap, vmin=-1, vmax=1)
    ax.set_xticks(range(len(ordem) - 1))
    ax.set_yticks(range(1, len(ordem)))
    ax.set_xticklabels(ordem[:-1], rotation=90, fontfamily="monospace", fontsize=5.5)
    ax.set_yticklabels(ordem[1:], fontfamily="monospace", fontsize=5.5)
    ax.set_xlim(-0.5, len(ordem) - 1.5)
    ax.set_ylim(len(ordem) - 0.5, 0.5)
    ax.spines[:].set_visible(False)
    ax.tick_params(length=0)
    cb = fig.colorbar(im, ax=ax, fraction=0.045, pad=0.02, format=fmt(1), ticks=[-1, -0.5, 0, 0.5, 1])
    cb.set_label("correlação de Pearson")
    salvar(fig, pasta, "fig4_heatmap.pdf")


def fig_intervalos(R, pasta):
    V = R["vars"]
    nomes = sorted(V, key=lambda k: V[k]["r"])
    # Largura de uma coluna, alta: uma linha por variável.
    fig, ax = plt.subplots(figsize=(2.45, 5.6))
    for i, k in enumerate(nomes):
        s = V[k]
        cor = CINZA if s["cobre_zero"] else VERDE
        ax.plot(s["ic"], [i, i], color=cor, lw=1.2, solid_capstyle="butt")
        ax.plot(s["r"], i, "o", color="black", ms=2.6, zorder=3)
        for t, mk in zip(s["tercos"], ["v", "s", "^"]):
            if t == t:  # terço sem variação fica NaN e não é desenhado
                ax.plot(t, i, mk, mfc="none", mec=LARANJA, ms=2.6, mew=0.6, zorder=2)
    ax.set_yticks(range(len(nomes)))
    ax.set_yticklabels([f"{k}  [{V[k]['classe']}{'*' if k == 'cot_mm_net_pct_oi' else ''}]" for k in nomes],
                       fontfamily="monospace", fontsize=5.6)
    ax.set_ylim(-0.7, len(nomes) - 0.3)
    ax.axvline(0, color="black", lw=0.6)
    ax.set_xlim(-1, 1)
    ax.set_xlabel("correlação com o retorno dos 91 dias seguintes")
    ax.set_xticks([-1, -0.5, 0, 0.5, 1])
    ax.xaxis.set_major_formatter(fmt(1))
    ax.grid(axis="x", color="#e5e5e5", lw=0.5)
    leg = [Line2D([], [], color=CINZA, lw=1.2, label="IC95% cobre zero"),
           Line2D([], [], color=VERDE, lw=1.2, label="IC95% exclui zero"),
           Line2D([], [], marker="o", color="black", ls="", ms=2.6, label="amostra completa"),
           Line2D([], [], marker="v", mfc="none", mec=LARANJA, ls="", ms=2.6, label="1º terço"),
           Line2D([], [], marker="s", mfc="none", mec=LARANJA, ls="", ms=2.6, label="2º terço"),
           Line2D([], [], marker="^", mfc="none", mec=LARANJA, ls="", ms=2.6, label="3º terço")]
    ax.legend(handles=leg, loc="upper center", bbox_to_anchor=(0.3, -0.06), ncol=2, frameon=False,
              fontsize=6, handlelength=1.4, columnspacing=1.0)
    salvar(fig, pasta, "fig5_corr91.pdf")


def fig_ccf(R, pasta):
    cc = {int(k): v for k, v in R["ccf"].items()}
    lags = sorted(cc)
    fig, ax = plt.subplots(figsize=(3.45, 1.9))
    ax.bar(lags, [cc[lag] for lag in lags], color=[LARANJA if lag == 0 else AZUL for lag in lags], width=0.7)
    for s in (1, -1):
        ax.axhline(s * R["ccf_banda"], color=CINZA, ls="--", lw=0.8)
    ax.axhline(0, color="black", lw=0.5)
    ax.set_xlabel("defasagem do ICE em pregões (positiva: o ICE antecede o CEPEA)")
    ax.set_ylabel("correlação dos log-retornos")
    ax.yaxis.set_major_formatter(fmt(1))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: br(x, 0)))
    salvar(fig, pasta, "fig6_ccf.pdf")


def fig_anomalia(b, pasta):
    a = b["precip_90d_anom_sulmg"].dropna()
    lim = 2 / np.sqrt(3)
    fig, ax = plt.subplots(figsize=(3.45, 1.9))
    ax.hist(a, bins=40, color=AZUL)
    for s in (1, -1):
        ax.axvline(s * lim, color=LARANJA, ls="--", lw=0.9)
    ax.set_xlabel("precip_90d_anom_sulmg (z-score)")
    ax.set_ylabel("dias")
    ax.xaxis.set_major_formatter(fmt(1))
    ax.text(0.5, 1.02, "linhas: limite teórico ±2/√3 = ±1,155 para n = 3", transform=ax.transAxes,
            ha="center", va="bottom", fontsize=7)
    salvar(fig, pasta, "fig7_anomalia.pdf")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("base", help="base_modelo.csv gerado por base/build_base_cafe.py")
    ap.add_argument("resultados", help="JSON gerado por analise.py")
    ap.add_argument("saida", help="pasta onde as figuras são gravadas")
    ap.add_argument("--dados-manuais", default=DADOS_MANUAIS, help="pasta com os CSVs do CEPEA")
    args = ap.parse_args()

    fam = configurar_estilo()
    pasta = Path(args.saida)
    pasta.mkdir(parents=True, exist_ok=True)
    base, cep = carregar(args.base, args.dados_manuais)
    base["preco_arabica"] = preco_corrigido(base, cep)
    b = base.loc[INI:FIM].copy()
    with open(args.resultados, encoding="utf-8") as f:
        R = json.load(f)

    fig_serie(b, pasta)
    fig_acf(R, pasta)
    fig_interregional(b, pasta)
    fig_mapa_calor(b, pasta)
    fig_intervalos(R, pasta)
    fig_ccf(R, pasta)
    fig_anomalia(b, pasta)
    print("fonte:", fam)


if __name__ == "__main__":
    main()
