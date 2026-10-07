"""Coleta real das fontes externas.

Uma origem por variável de mercado e uma para o clima:

==================  ======================================  =====================
Variável            Fonte                                   Como
==================  ======================================  =====================
``preco_arabica``   CEPEA/ESALQ                             CSV da série + agrobr
``preco_robusta``   CEPEA/ESALQ                             CSV da série + agrobr
``usd_brl``         BCB PTAX (venda)                        agrobr
``b3_cafe_ajuste``  B3, contrato ICF, 1º vencimento         agrobr, dia a dia
``ice_kc``          ICE US Coffee C (``KC=F``), fechamento  Yahoo Finance
clima (7 séries)    NASA POWER, um ponto por região         agrobr
==================  ======================================  =====================

Particularidades que moldam o código:

* **CEPEA** — o histórico vem dos CSVs de ``MANUAL_DATA_DIR``, completados pelo
  agrobr, cuja página só traz as cotações mais recentes; se o arquivo estiver
  velho demais, fica um buraco entre os dois, e a coleta avisa. Os CSVs são
  atualizados por ``src/cepea_series.py`` a partir da planilha do site. Com
  ``CEPEA_AUTO_DOWNLOAD`` a própria coleta tenta baixá-la, mas o site recusa
  acesso automático (403), então por padrão isso fica desligado.
* **B3** — cada pregão é um arquivo com o mercado inteiro (~4 s e centenas de MB de
  memória para processar). Pedir anos de uma vez estoura a memória, então os
  dias são baixados em lotes pequenos e o resultado fica num cache em disco:
  cada pregão é baixado uma única vez, e uma coleta interrompida continua de
  onde parou.
* **NASA POWER** — os últimos dias chegam vazios (a fonte publica com alguns
  dias de atraso). Valores ausentes são descartados, não gravados como nulos.
* **Yahoo Finance** — durante o pregão devolve a cotação parcial do dia. Ela
  só é aceita depois do fechamento da ICE.

Falhas de rede viram ``SourceFetchError``, que a política de novas tentativas
de ``AgrobrClient.fetch`` repete.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from src.agrobr_client import (
    AgrobrClient,
    SourceFetchError,
    SourceFile,
    _default_start,
    _hash_rows,
    _market_row,
    _now_utc,
    _resolve_git_commit,
    _today,
    _weather_row,
    read_price_csv,
)
from src.cepea_series import SERIES as CEPEA_SERIES, download_series, refresh_csv
from src.config import settings
from src.logging_config import logger

try:
    # O agrobr registra cada requisição em nível info/debug pelo structlog; numa
    # coleta de anos isso são milhares de linhas. Ficam só os avisos e erros.
    import logging as _logging

    import structlog as _structlog

    _structlog.configure(
        wrapper_class=_structlog.make_filtering_bound_logger(_logging.WARNING))
except ImportError:  # sem agrobr instalado o erro aparece na hora da coleta
    pass

# Pontos de clima. A grade do NASA POWER é de 0,5° (~55 km): municípios
# vizinhos cairiam na mesma célula.
WEATHER_POINTS: Dict[str, Tuple[float, float]] = {
    "bambui": (-20.01, -45.98),   # Bambuí/MG — região alvo
    "sulmg": (-21.55, -45.43),    # Varginha/MG — Sul de Minas
    "cerrado": (-18.94, -46.99),  # Patrocínio/MG — Cerrado Mineiro
}
WEATHER_FIELDS: Sequence[str] = (
    "temp_min", "temp_max", "temp_media", "precip_mm",
    "umidade_rel", "radiacao_mj", "vento_ms",
)

CEPEA_URL = "https://www.cepea.esalq.usp.br"
PTAX_URL = "https://olinda.bcb.gov.br"
B3_URL = "https://arquivos.b3.com.br"
NASA_URL = "https://power.larc.nasa.gov"
YAHOO_URL = "https://finance.yahoo.com/quote/KC=F"

B3_CONTRACT = "cafe_arabica"  # ICF no agrobr
B3_CACHE_FILE = "b3_icf_ajustes.csv"
B3_CACHE_COLUMNS = ["data", "vencimento_codigo", "vencimento_mes", "vencimento_ano", "ajuste_atual"]
# Cada arquivo em processamento ocupa ~300 MB; dois por vez mantém o pico em ~600 MB.
B3_CONCURRENCY = 2
B3_BATCH_DAYS = 20
# Um dia útil sem arquivo só é dado como "sem pregão" depois deste prazo; antes
# disso o arquivo pode simplesmente ainda não ter sido publicado.
B3_SETTLE_DAYS = 5

ICE_TICKER = "KC=F"
ICE_TIMEZONE = "America/New_York"
ICE_CLOSE = time(14, 0)  # o Coffee C encerra às 13:30 de Nova York

# Buraco máximo, em dias corridos, tolerado entre o fim do CSV manual do CEPEA e
# o início do que o agrobr devolve, antes de avisar.
CEPEA_MAX_GAP_DAYS = 7


def _business_days(start: date, end: date) -> List[date]:
    dias = (start + timedelta(days=i) for i in range((end - start).days + 1))
    return [d for d in dias if d.weekday() < 5]


def front_contract(ajustes: pd.DataFrame) -> pd.Series:
    """Ajuste do 1º vencimento (o mais próximo) de cada pregão, indexado por data."""
    validos = ajustes.dropna(subset=["ajuste_atual", "vencimento_ano", "vencimento_mes"])
    ordenado = validos.sort_values(["data", "vencimento_ano", "vencimento_mes"])
    return ordenado.groupby("data")["ajuste_atual"].first()


class RealAgrobrClient(AgrobrClient):
    """Coleta real via agrobr e Yahoo Finance, na janela ``start_date``..``end_date``."""

    ARABICA_FILE = CEPEA_SERIES["arabica"].file_name
    ROBUSTA_FILE = CEPEA_SERIES["robusta"].file_name

    def __init__(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        manual_dir: Optional[str] = None,
        cache_dir: Optional[str] = None,
        today: Callable[[], date] = _today,
    ) -> None:
        self._today = today
        self.end_date = end_date or today()
        self.start_date = start_date or _default_start(self.end_date)
        if self.start_date > self.end_date:
            raise ValueError(
                f"Janela inválida: start_date={self.start_date} > end_date={self.end_date}"
            )
        self.manual_dir = Path(manual_dir or settings.MANUAL_DATA_DIR)
        self.cache_dir = Path(cache_dir or settings.COLLECTION_CACHE_DIR)

    # ------------------------------------------------------------------
    # Downloads — um método por fonte, sem regra de negócio. São os únicos
    # pontos que tocam a rede; os testes os substituem.
    # ------------------------------------------------------------------

    def _download_cepea_series(self, serie_id: int) -> bytes:
        """Planilha XLS com a série histórica completa do indicador."""
        return download_series(serie_id)

    def _download_cepea(self, produto: str) -> pd.DataFrame:
        """Cotações recentes do indicador: colunas ``data`` e ``valor``."""
        from agrobr.sync import cepea

        return cepea.indicador(produto, inicio=self.start_date, fim=self.end_date)

    def _download_ptax(self) -> pd.DataFrame:
        """PTAX do período: ``data_hora`` e ``cotacao_venda``."""
        from agrobr.sync import bcb

        # A API PTAX exige dd/mm/aaaa.
        return bcb.ptax(data_inicial=self.start_date.strftime("%d/%m/%Y"),
                        data_final=self.end_date.strftime("%d/%m/%Y"))

    def _download_b3(self, dias: Sequence[date]) -> Dict[date, Optional[pd.DataFrame]]:
        """Ajustes do ICF de cada dia pedido; ``None`` onde não há arquivo."""
        import httpx
        from agrobr.b3 import api as b3_api
        from agrobr.exceptions import SourceUnavailableError

        async def baixar_todos() -> List[Optional[pd.DataFrame]]:
            limite = asyncio.Semaphore(B3_CONCURRENCY)

            async def baixar(dia: date) -> Optional[pd.DataFrame]:
                async with limite:
                    try:
                        return await b3_api.ajustes(data=dia, contrato=B3_CONTRACT)
                    except SourceUnavailableError:
                        return None  # feriado, ou pregão ainda não publicado

            return await asyncio.gather(*(baixar(d) for d in dias))

        try:
            return dict(zip(dias, asyncio.run(baixar_todos())))
        except httpx.HTTPError as exc:
            raise SourceFetchError(f"B3: falha de rede ao baixar ajustes: {exc}") from exc

    def _download_weather(self, lat: float, lon: float) -> pd.DataFrame:
        """Clima diário do ponto: ``data`` e as colunas de ``WEATHER_FIELDS``."""
        from agrobr.sync import nasa_power

        return nasa_power.clima_ponto(lat=lat, lon=lon,
                                      inicio=self.start_date.isoformat(),
                                      fim=self.end_date.isoformat())

    def _download_ice(self) -> pd.Series:
        """Fechamentos do Coffee C, indexados por data."""
        import yfinance as yf

        historico = yf.Ticker(ICE_TICKER).history(
            start=self.start_date.isoformat(),
            end=(self.end_date + timedelta(days=1)).isoformat(),  # fim exclusivo
        )
        if historico.empty:
            return pd.Series(dtype=float)
        serie = historico["Close"]
        serie.index = pd.to_datetime(serie.index).tz_localize(None).normalize()
        return serie

    def _guarded(self, fonte: str, baixar: Callable, *args):
        """Executa um download transformando qualquer falha em ``SourceFetchError``."""
        try:
            return baixar(*args)
        except SourceFetchError:
            raise
        except ImportError as exc:
            raise RuntimeError(
                f"{fonte}: dependência ausente ({exc}). Rode `pip install -r requirements.txt`."
            ) from exc
        except Exception as exc:  # qualquer falha de fonte externa é repetível
            raise SourceFetchError(f"{fonte}: {type(exc).__name__}: {exc}") from exc

    def _in_window(self, dia: date) -> bool:
        return self.start_date <= dia <= self.end_date

    # ------------------------------------------------------------------
    # CEPEA: série completa do site; na falta dela, CSV existente + agrobr
    # ------------------------------------------------------------------

    def _refresh_cepea_csv(self, serie: str) -> bool:
        """Atualiza o CSV da série pelo site; ``False`` se ficou o arquivo que já havia."""
        if not settings.CEPEA_AUTO_DOWNLOAD:
            return False
        try:
            refresh_csv(CEPEA_SERIES[serie], self.manual_dir,
                        download=self._download_cepea_series)
            return True
        except Exception as exc:  # o CSV existente continua sendo uma coleta válida
            logger.warning("CEPEA %s: série não atualizada pelo site (%s: %s); usando o "
                           "CSV existente.", serie, type(exc).__name__, exc)
            return False

    def _cepea_rows(self, serie: str, variable: str, produto: str) -> List[Dict]:
        atualizado = self._refresh_cepea_csv(serie)
        manual, _ = read_price_csv(self.manual_dir / CEPEA_SERIES[serie].file_name, variable,
                                   self.start_date, self.end_date, "CEPEA/ESALQ")
        if atualizado:
            return manual  # a série do site já vai até a última cotação publicada

        por_data = {r["observation_date"]: r for r in manual}
        fim_manual = max(por_data) if por_data else None

        try:
            recente = self._download_cepea(produto)
        except Exception as exc:  # o histórico manual sozinho ainda é uma coleta válida
            logger.warning("CEPEA %s: complemento recente indisponível (%s: %s); "
                           "usando só o CSV manual.", produto, type(exc).__name__, exc)
            recente = pd.DataFrame(columns=["data", "valor"])

        if not recente.empty:
            recente = recente.assign(data=pd.to_datetime(recente["data"]).dt.date)
            # Se vier mais de uma praça para o mesmo dia, vale a média.
            medias = recente.dropna(subset=["valor"]).groupby("data")["valor"].mean()
            novos = {d: v for d, v in medias.items() if self._in_window(d) and v > 0}
            if novos and fim_manual and (min(novos) - fim_manual).days > CEPEA_MAX_GAP_DAYS:
                logger.warning(
                    "CEPEA %s: buraco de %s a %s entre o CSV manual e o agrobr. Baixe uma "
                    "exportação atualizada do CEPEA para %s.",
                    produto, fim_manual + timedelta(days=1), min(novos) - timedelta(days=1),
                    self.manual_dir,
                )
            for dia, valor in novos.items():  # a cotação recente prevalece sobre o arquivo
                por_data[dia] = _market_row(dia, variable, float(valor), "CEPEA/ESALQ")

        return [por_data[d] for d in sorted(por_data)]

    # ------------------------------------------------------------------
    # BCB PTAX
    # ------------------------------------------------------------------

    def _ptax_rows(self) -> List[Dict]:
        df = self._guarded("BCB PTAX", self._download_ptax)
        if df.empty:
            return []
        momento = pd.to_datetime(df["data_hora"])
        df = df.assign(_dia=momento.dt.date, _momento=momento).sort_values("_momento")
        # Mais de um boletim no dia: vale o último (o de fechamento).
        fechamento = df.dropna(subset=["cotacao_venda"]).groupby("_dia")["cotacao_venda"].last()
        return [_market_row(d, "usd_brl", float(v), "BCB PTAX")
                for d, v in fechamento.items() if self._in_window(d)]

    # ------------------------------------------------------------------
    # B3: ajustes diários, com cache em disco
    # ------------------------------------------------------------------

    @property
    def _b3_cache_path(self) -> Path:
        return self.cache_dir / B3_CACHE_FILE

    def _load_b3_cache(self) -> pd.DataFrame:
        if not self._b3_cache_path.is_file():
            return pd.DataFrame(columns=B3_CACHE_COLUMNS)
        cache = pd.read_csv(self._b3_cache_path)
        cache["data"] = pd.to_datetime(cache["data"]).dt.date
        return cache[B3_CACHE_COLUMNS]

    def _save_b3_cache(self, cache: pd.DataFrame) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporario = self._b3_cache_path.with_suffix(".tmp")
        cache.sort_values(["data", "vencimento_ano", "vencimento_mes"]).to_csv(
            temporario, index=False)
        temporario.replace(self._b3_cache_path)  # troca atômica: nunca fica pela metade

    def _b3_day_rows(self, dia: date, ajustes: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        """Linhas de cache de um dia; ``None`` se o dia ainda deve ser tentado de novo."""
        if ajustes is not None and not ajustes.empty:
            datas = pd.to_datetime(ajustes["data"]).dt.date
            # O arquivo de D traz também uma linha datada do pregão seguinte que
            # apenas repete o ajuste de D. Só as linhas do próprio dia valem.
            do_dia = ajustes.loc[datas == dia].assign(data=dia)
            if not do_dia.empty:
                return do_dia[B3_CACHE_COLUMNS]

        if dia <= self._today() - timedelta(days=B3_SETTLE_DAYS):
            # Dia útil sem pregão (feriado): marca para não baixar de novo.
            return pd.DataFrame([{"data": dia}], columns=B3_CACHE_COLUMNS)
        return None

    def _b3_rows(self) -> List[Dict]:
        cache = self._load_b3_cache()
        pendentes = sorted(set(_business_days(self.start_date, self.end_date)) - set(cache["data"]))
        if pendentes:
            logger.info(
                "B3: %d pregões a baixar (%s a %s), cerca de %d min. Os já baixados "
                "ficam em %s.", len(pendentes), pendentes[0], pendentes[-1],
                max(1, len(pendentes) // 15), self._b3_cache_path,
            )

        for i in range(0, len(pendentes), B3_BATCH_DAYS):
            lote = pendentes[i:i + B3_BATCH_DAYS]
            baixados = self._guarded("B3", self._download_b3, lote)
            novos = [linhas for dia in lote
                     if (linhas := self._b3_day_rows(dia, baixados.get(dia))) is not None]
            if novos:
                cache = pd.concat([cache, *novos], ignore_index=True)
                self._save_b3_cache(cache)  # salva a cada lote: a coleta é retomável
            logger.info("B3: %d de %d pregões processados.",
                        min(i + B3_BATCH_DAYS, len(pendentes)), len(pendentes))

        ajustes = front_contract(cache)
        return [_market_row(d, "b3_cafe_ajuste", float(v), "B3")
                for d, v in ajustes.items() if self._in_window(d) and v > 0]

    # ------------------------------------------------------------------
    # ICE (Yahoo Finance)
    # ------------------------------------------------------------------

    def _ice_session_closed(self, dia: date) -> bool:
        agora = datetime.now(ZoneInfo(ICE_TIMEZONE))
        return dia < agora.date() or (dia == agora.date() and agora.time() >= ICE_CLOSE)

    def _ice_rows(self) -> List[Dict]:
        serie = self._guarded("Yahoo Finance", self._download_ice).dropna()
        return [
            _market_row(d, "ice_kc", float(v), "ICE US/Yahoo")
            for d, v in ((ts.date(), valor) for ts, valor in serie.items())
            if self._in_window(d) and v > 0 and self._ice_session_closed(d)
        ]

    # ------------------------------------------------------------------
    # NASA POWER
    # ------------------------------------------------------------------

    def _weather_rows(self) -> List[Dict]:
        rows: List[Dict] = []
        for regiao, (lat, lon) in WEATHER_POINTS.items():
            df = self._guarded(f"NASA POWER {regiao}", self._download_weather, lat, lon)
            if df.empty:
                continue
            datas = pd.to_datetime(df["data"]).dt.date
            for variavel in (v for v in WEATHER_FIELDS if v in df.columns):
                # Dia ainda não publicado vem vazio: não é uma observação.
                for dia, valor in zip(datas, df[variavel]):
                    if pd.notna(valor) and self._in_window(dia):
                        rows.append(_weather_row(dia, regiao, variavel, float(valor), "NASA POWER"))
            logger.info("NASA POWER %s: %d dias recebidos.", regiao, len(df))
        return rows

    # ------------------------------------------------------------------
    # Montagem
    # ------------------------------------------------------------------

    def _source_file(
        self,
        source_name: str,
        file_name: str,
        source_url: str,
        collected_at: datetime,
        market_rows: Optional[List[Dict]] = None,
        weather_rows: Optional[List[Dict]] = None,
        git_commit: Optional[str] = None,
    ) -> SourceFile:
        market_rows, weather_rows = market_rows or [], weather_rows or []
        return SourceFile(
            source_name=source_name,
            file_name=file_name,
            # Hash do conteúdo entregue: muda se a fonte publicar ou revisar algo.
            content_hash=_hash_rows(market_rows + weather_rows),
            collected_at=collected_at,
            source_url=source_url,
            git_commit=git_commit,
            market_rows=market_rows,
            weather_rows=weather_rows,
        )

    def _fetch(self) -> List[SourceFile]:
        collected_at = _now_utc()
        commit = _resolve_git_commit(self.manual_dir)
        logger.info("Coleta real: janela %s a %s.", self.start_date, self.end_date)

        arabica = self._cepea_rows("arabica", "preco_arabica", "cafe")
        robusta = self._cepea_rows("robusta", "preco_robusta", "cafe_robusta")
        if not arabica:
            raise SourceFetchError(
                f"Nenhum preço de arábica na janela {self.start_date}..{self.end_date}. "
                f"Verifique o CSV do CEPEA em {self.manual_dir}."
            )

        files = [
            self._source_file("CEPEA/ESALQ", self.ARABICA_FILE, CEPEA_URL, collected_at,
                              market_rows=arabica, git_commit=commit),
            self._source_file("CEPEA/ESALQ", self.ROBUSTA_FILE, CEPEA_URL, collected_at,
                              market_rows=robusta, git_commit=commit),
            self._source_file("BCB PTAX", "ptax_venda", PTAX_URL, collected_at,
                              market_rows=self._ptax_rows()),
            self._source_file("ICE US", ICE_TICKER, YAHOO_URL, collected_at,
                              market_rows=self._ice_rows()),
            self._source_file("B3", "ajustes_icf_1o_vencimento", B3_URL, collected_at,
                              market_rows=self._b3_rows()),
            self._source_file("NASA POWER", "clima_diario", NASA_URL, collected_at,
                              weather_rows=self._weather_rows()),
        ]

        vazias = [f"{f.source_name}/{f.file_name}" for f in files if f.row_count == 0]
        if vazias:
            raise SourceFetchError(
                f"Fontes sem nenhum dado na janela {self.start_date}..{self.end_date}: {vazias}."
            )

        logger.info(
            "Coleta real concluída: %d origens, %d obs de mercado, %d obs de clima.",
            len(files), sum(len(f.market_rows) for f in files),
            sum(len(f.weather_rows) for f in files),
        )
        return files
