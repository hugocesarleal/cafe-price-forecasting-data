"""Download da série histórica dos indicadores de café do CEPEA/ESALQ.

O CEPEA publica a série completa de cada indicador como uma planilha XLS num
endereço fixo. Este módulo baixa as duas séries (arábica e robusta), confere o
conteúdo e atualiza os CSVs de ``MANUAL_DATA_DIR`` que o restante do pipeline lê
— o que antes era um download manual.

    python -m src.cepea_series            # baixa e atualiza os dois CSVs
    python -m src.cepea_series --check    # baixa e confere, sem gravar
    python -m src.cepea_series --importar arabica.xls robusta.xls

O site do CEPEA pode recusar o download automático (HTTP 403). Nesse caso baixe
as planilhas pelo navegador e use ``--importar``: a série de cada arquivo é
reconhecida pelo título, e as conferências são as mesmas.

Um CSV só é substituído se a planilha baixada for claramente a mesma série, mais
atual: título do indicador certo, cobertura não menor que a do arquivo existente
e preços coincidentes nas datas em comum. O arquivo anterior fica como ``.bak``.

Os dados do CEPEA são CC BY-NC 4.0: uso não comercial, com citação da fonte.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from src.config import settings
from src.logging_config import logger

SERIES_URL = "https://www.cepea.org.br/br/indicador/series/cafe.aspx"
USER_AGENT = "cafe-price-forecasting-data/1.0 (pipeline de pesquisa; python-httpx)"
TIMEOUT_SECONDS = 60

CSV_COLUMNS = ["data", "preco_brl_saca", "preco_usd_saca"]

# Linhas do topo da planilha examinadas em busca do título e do cabeçalho.
LINHAS_DE_TOPO = 15
# A série nova pode perder poucas linhas em relação ao CSV (revisões do CEPEA),
# mas não encolher; e os preços das datas em comum precisam coincidir.
MIN_COVERAGE = 0.98
MAX_MEDIAN_DIFF = 0.01


class CepeaSeriesError(Exception):
    """A planilha baixada não pôde ser lida ou não é a série esperada."""


@dataclass(frozen=True)
class Serie:
    nome: str
    serie_id: int
    file_name: str
    # Pelo menos uma destas palavras precisa aparecer no título da planilha.
    palavras_chave: Tuple[str, ...]


SERIES: Dict[str, Serie] = {
    "arabica": Serie("arabica", 23, "cafe_arabica_cepea_1996_2026.csv", ("arabica",)),
    "robusta": Serie("robusta", 24, "cafe_robusta_cepea_2001_2026.csv",
                     ("robusta", "conilon", "conillon")),
}


def _slug(texto: object) -> str:
    sem_acento = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", sem_acento.lower()).strip("_")


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_series(serie_id: int) -> bytes:
    """Baixa a planilha da série; devolve o conteúdo bruto."""
    import httpx

    resposta = httpx.get(SERIES_URL, params={"id": serie_id}, timeout=TIMEOUT_SECONDS,
                         follow_redirects=True, headers={"User-Agent": USER_AGENT})
    resposta.raise_for_status()
    tipo = resposta.headers.get("content-type", "")
    if "html" in tipo.lower():
        # Página de erro ou de verificação no lugar da planilha.
        raise CepeaSeriesError(
            f"CEPEA devolveu uma página ({tipo}) em vez da planilha da série {serie_id}."
        )
    return resposta.content


# ---------------------------------------------------------------------------
# Leitura da planilha
# ---------------------------------------------------------------------------

def _read_sheet(conteudo: bytes) -> pd.DataFrame:
    """Lê a planilha sem interpretar cabeçalho.

    O calamine vem primeiro: o xlrd acusa "Workbook corruption" no XLS exportado
    pelo CEPEA, embora o arquivo seja válido.
    """
    erros: List[str] = []
    for engine in ("calamine", None):
        try:
            return pd.read_excel(io.BytesIO(conteudo), header=None, engine=engine)
        except Exception as exc:  # tenta o próximo leitor
            erros.append(f"{engine or 'auto'}: {type(exc).__name__}: {exc}")
    raise CepeaSeriesError("Planilha ilegível (" + "; ".join(erros) + ")")


def _to_number(serie: pd.Series) -> pd.Series:
    """Converte '1.234,56' (pt-BR) e 1234.56 para float."""
    if pd.api.types.is_numeric_dtype(serie):
        return serie.astype(float)
    texto = serie.astype(str).str.strip().str.replace(r"[^\d,.\-]", "", regex=True)
    com_virgula = texto.str.contains(",")
    texto = texto.where(~com_virgula,
                        texto.str.replace(".", "", regex=False).str.replace(",", ".", regex=False))
    return pd.to_numeric(texto, errors="coerce")


def _to_date(serie: pd.Series) -> pd.Series:
    """Converte a coluna de data detectando o formato (dd/mm/aaaa ou ISO).

    Aplicar ``dayfirst`` numa data ISO troca dia por mês em silêncio.
    """
    if pd.api.types.is_datetime64_any_dtype(serie):
        return pd.to_datetime(serie, errors="coerce")
    texto = serie.astype(str).str.strip()
    if texto.str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}").mean() > 0.8:
        return pd.to_datetime(texto, errors="coerce")
    return pd.to_datetime(texto, dayfirst=True, errors="coerce")


def parse_series(conteudo: bytes, palavras_chave: Sequence[str] = ()) -> pd.DataFrame:
    """Extrai ``data``, ``preco_brl_saca`` e ``preco_usd_saca`` da planilha do CEPEA.

    A linha de cabeçalho é procurada (começa por "Data"), porque o número de
    linhas de título muda entre exportações. Com ``palavras_chave``, exige que
    uma delas apareça no título — é o que impede gravar a série errada.
    """
    bruto = _read_sheet(conteudo)
    topo = bruto.head(LINHAS_DE_TOPO)

    cabecalho = next(
        (i for i in range(len(topo)) if _slug(topo.iloc[i, 0]).startswith("data")), None)
    if cabecalho is None:
        raise CepeaSeriesError("Cabeçalho 'Data' não encontrado na planilha.")

    if palavras_chave:
        titulo = _slug(" ".join(map(str, bruto.head(cabecalho + 1).values.ravel())))
        if not any(p in titulo for p in palavras_chave):
            raise CepeaSeriesError(
                f"A planilha não parece ser a série esperada: nenhuma de {list(palavras_chave)} "
                "aparece no título."
            )

    dados = bruto.iloc[cabecalho + 1:]
    numericas = [c for c in dados.columns[1:] if _to_number(dados[c]).notna().mean() > 0.5]
    if not numericas:
        raise CepeaSeriesError("Nenhuma coluna de preço encontrada na planilha.")

    saida = pd.DataFrame({
        "data": _to_date(dados.iloc[:, 0]),
        "preco_brl_saca": _to_number(dados[numericas[0]]),
        # A segunda coluna numérica é a cotação em dólar; o CEPEA exporta as duas.
        "preco_usd_saca": _to_number(dados[numericas[1]]) if len(numericas) > 1 else float("nan"),
    }).dropna(subset=["data", "preco_brl_saca"])

    # O CEPEA usa 0 como marcador de ausência no início de algumas séries.
    saida = saida[saida["preco_brl_saca"] > 0]
    saida["data"] = saida["data"].dt.normalize()
    saida = saida.drop_duplicates(subset="data", keep="last").sort_values("data")
    if saida.empty:
        raise CepeaSeriesError("A planilha não trouxe nenhuma cotação válida.")
    return saida.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Conferência e gravação
# ---------------------------------------------------------------------------

def check_against_existing(nova: pd.DataFrame, path: Path) -> Dict[str, object]:
    """Compara a série baixada com o CSV existente; levanta se não for a mesma série."""
    resumo: Dict[str, object] = {
        "linhas": len(nova),
        "de": nova["data"].min().date().isoformat(),
        "ate": nova["data"].max().date().isoformat(),
    }
    if not path.is_file():
        return resumo

    atual = pd.read_csv(path, parse_dates=["data"])
    resumo.update(linhas_antes=len(atual), ate_antes=atual["data"].max().date().isoformat())

    if len(nova) < len(atual) * MIN_COVERAGE:
        raise CepeaSeriesError(
            f"{path.name}: a série baixada tem {len(nova)} linhas e o arquivo atual {len(atual)}; "
            "não vou trocar um histórico maior por um menor."
        )
    if nova["data"].max() < atual["data"].max():
        raise CepeaSeriesError(
            f"{path.name}: a série baixada termina em {resumo['ate']}, antes do arquivo atual "
            f"({resumo['ate_antes']})."
        )

    comum = nova.merge(atual, on="data", suffixes=("_nova", "_atual"))
    if comum.empty:
        raise CepeaSeriesError(f"{path.name}: nenhuma data em comum com o arquivo atual.")
    diferenca = ((comum["preco_brl_saca_nova"] - comum["preco_brl_saca_atual"]).abs()
                 / comum["preco_brl_saca_atual"]).median()
    resumo["diferenca_mediana"] = round(float(diferenca), 6)
    if diferenca > MAX_MEDIAN_DIFF:
        raise CepeaSeriesError(
            f"{path.name}: os preços baixados diferem {diferenca:.1%} (mediana) dos atuais nas "
            "datas em comum — não parece a mesma série."
        )
    resumo["dias_novos"] = int((nova["data"] > atual["data"].max()).sum())
    return resumo


def write_csv(serie: pd.DataFrame, path: Path) -> None:
    """Grava o CSV no formato lido pelo pipeline, guardando o anterior como ``.bak``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporario = path.with_suffix(".tmp")
    serie.assign(data=serie["data"].dt.strftime("%Y-%m-%d"))[CSV_COLUMNS].round(2).to_csv(
        temporario, index=False, lineterminator="\n")
    if path.is_file():
        shutil.copy2(path, path.with_suffix(".csv.bak"))
    temporario.replace(path)


def refresh_csv(
    serie: Serie,
    manual_dir: Optional[Path] = None,
    download=None,
    write: bool = True,
) -> Dict[str, object]:
    """Baixa uma série, confere e atualiza o CSV correspondente.

    Levanta ``CepeaSeriesError`` (ou o erro de rede) sem tocar no arquivo
    existente se qualquer conferência falhar.
    """
    path = Path(manual_dir or settings.MANUAL_DATA_DIR) / serie.file_name
    nova = parse_series((download or download_series)(serie.serie_id), serie.palavras_chave)
    resumo = check_against_existing(nova, path)
    if write:
        write_csv(nova, path)
    resumo.update(serie=serie.nome, arquivo=str(path), gravado=write)
    logger.info("CEPEA %s: %s linhas, %s a %s%s.", serie.nome, resumo["linhas"], resumo["de"],
                resumo["ate"], "" if write else " (não gravado)")
    return resumo


def identify_series(conteudo: bytes) -> Serie:
    """Descobre, pelo título, de qual série é uma planilha."""
    for serie in SERIES.values():
        try:
            parse_series(conteudo, serie.palavras_chave)
            return serie
        except CepeaSeriesError as exc:
            if "série esperada" not in str(exc):
                raise  # planilha ilegível ou sem cotações: não adianta tentar a outra
    raise CepeaSeriesError("A planilha não é do indicador de café arábica nem do robusta.")


def import_file(
    arquivo: Path,
    manual_dir: Optional[Path] = None,
    write: bool = True,
) -> Dict[str, object]:
    """Atualiza o CSV de uma série a partir de uma planilha baixada à mão."""
    conteudo = Path(arquivo).read_bytes()
    resumo = refresh_csv(identify_series(conteudo), manual_dir,
                         download=lambda serie_id: conteudo, write=write)
    resumo["origem"] = str(arquivo)
    return resumo


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.cepea_series",
        description="Baixa as séries históricas de café do CEPEA e atualiza os CSVs.")
    parser.add_argument("--check", action="store_true",
                        help="baixa e confere, sem gravar nada")
    parser.add_argument("--serie", choices=sorted(SERIES), action="append",
                        help="série a baixar (padrão: todas)")
    parser.add_argument("--importar", nargs="+", metavar="ARQUIVO", type=Path,
                        help="em vez de baixar, lê planilhas já baixadas pelo navegador")
    args = parser.parse_args(argv)

    if args.importar:
        tarefas = [(str(a), lambda a=a: import_file(a, write=not args.check))
                   for a in args.importar]
    else:
        tarefas = [(nome, lambda nome=nome: refresh_csv(SERIES[nome], write=not args.check))
                   for nome in args.serie or sorted(SERIES)]

    resultados, falhas = [], 0
    for nome, executar in tarefas:
        try:
            resultados.append(executar())
        except Exception as exc:  # um item com problema não impede o outro
            falhas += 1
            logger.error("CEPEA %s: %s", nome, exc)
            resultados.append({"serie": nome, "erro": f"{type(exc).__name__}: {exc}"})

    print(json.dumps(resultados, ensure_ascii=False, indent=2))
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
