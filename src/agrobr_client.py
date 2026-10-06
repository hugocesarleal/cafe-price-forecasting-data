"""Adaptador configurável de coleta das fontes externas.

Duas estratégias, conforme a especificação:

* ``SimulatedAgrobrClient`` — usa os CSVs locais de ``base/dados_manuais`` para
  preços e gera clima/futuros sintéticos determinísticos. Serve para desenvolver
  e testar o pipeline de ponta a ponta sem depender de rede.
* ``RealAgrobrClient`` — coleta real. A estrutura das APIs (Agro.br, CEPEA/ESALQ,
  BCB PTAX, NASA POWER, B3 e ICE US) não foi fornecida, então nenhum endpoint,
  contrato de resposta ou parser foi inventado: a classe falha alto explicando o
  que é necessário para implementá-la.

Toda observação devolvida usa o formato longo de ``raw.market_observations`` e
``raw.weather_observations``: uma linha por (data, região, variável).
"""

from __future__ import annotations

import csv
import hashlib
import math
import os
import random
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import settings
from src.logging_config import logger


class SourceFetchError(Exception):
    """Falha transitória de coleta (rede, arquivo ilegível, resposta inválida)."""


# ---------------------------------------------------------------------------
# Constantes de domínio
# ---------------------------------------------------------------------------

LB_PER_SAC = 132.277  # 60 kg em libras, usado para converter USD/sc em cUSD/lb

WEATHER_REGIONS: Sequence[str] = ("bambui", "sulmg", "cerrado")

UNITS: Dict[str, str] = {
    "preco_arabica": "R$/sc 60kg",
    "preco_robusta": "R$/sc 60kg",
    "usd_brl": "BRL",
    "ice_kc": "cUSD/lb",
    "b3_cafe_ajuste": "USD/sc",
    "temp_min": "°C",
    "temp_max": "°C",
    "temp_media": "°C",
    "precip_mm": "mm",
    "umidade_rel": "%",
    "radiacao_mj": "MJ/m²",
    "vento_ms": "m/s",
}

# Ajustes finos por região: o Cerrado mineiro é mais quente e seco que o Sul de
# Minas, e Bambui fica entre os dois.
REGION_OFFSETS: Dict[str, Dict[str, float]] = {
    "cerrado": {"temp": 1.4, "precip": -0.10, "umidade": -5.0, "radiacao": 1.5},
    "sulmg": {"temp": -1.1, "precip": 0.10, "umidade": 5.0, "radiacao": -1.2},
    "bambui": {"temp": 0.0, "precip": 0.0, "umidade": 0.0, "radiacao": 0.0},
}

CSV_PRECO_COLUNAS = ("data", "preco_brl_saca", "preco_usd_saca")


# ---------------------------------------------------------------------------
# Estruturas de saída
# ---------------------------------------------------------------------------

@dataclass
class SourceFile:
    """Um arquivo/coleta de origem, com metadados para ``raw.ingestion_files``."""

    source_name: str
    file_name: str
    content_hash: str
    collected_at: datetime
    source_url: Optional[str] = None
    git_commit: Optional[str] = None
    market_rows: List[Dict] = field(default_factory=list)
    weather_rows: List[Dict] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.market_rows) + len(self.weather_rows)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> date:
    return datetime.now(ZoneInfo(settings.TIMEZONE)).date()


def _default_start(end: Optional[date] = None) -> date:
    """Início da janela histórica configurada em ``HISTORICAL_YEARS``."""
    end = end or _today()
    try:
        return end.replace(year=end.year - settings.HISTORICAL_YEARS)
    except ValueError:  # 29 de fevereiro
        return end.replace(year=end.year - settings.HISTORICAL_YEARS, day=28)


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_rows(rows: List[Dict]) -> str:
    """Hash estável do conteúdo produzido (fontes sintéticas não têm arquivo)."""
    linhas = [
        "|".join([
            str(r["observation_date"]),
            str(r.get("region") or ""),
            str(r["variable_name"]),
            f"{float(r['value']):.4f}",
        ])
        for r in rows
    ]
    return _hash_bytes("\n".join(sorted(linhas)).encode("utf-8"))


def _resolve_git_commit(path: Path) -> Optional[str]:
    """Commit do repositório que contém ``path``; None se não for um repo git."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = out.stdout.strip()
    return commit or None


def _seeded_rng(*parts: object) -> random.Random:
    """RNG determinística por chave — reproducível entre processos e máquinas."""
    key = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def _season(day_of_year: int) -> float:
    """+1 no auge do verão (meados de janeiro), -1 no auge do inverno."""
    return math.cos(2 * math.pi * (day_of_year - 15) / 365.25)


def _market_row(obs_date: date, variable: str, value: float, source: str) -> Dict:
    return {
        "observation_date": obs_date,
        "region": None,
        "variable_name": variable,
        "value": round(value, 4),
        "unit": UNITS[variable],
        "source": source,
    }


def _weather_row(obs_date: date, region: str, variable: str, value: float, source: str) -> Dict:
    return {
        "observation_date": obs_date,
        "region": region,
        "variable_name": variable,
        "value": round(value, 4),
        "unit": UNITS[variable],
        "source": source,
    }


# ---------------------------------------------------------------------------
# Contrato
# ---------------------------------------------------------------------------

class AgrobrClient(ABC):
    """Estratégia de coleta. ``fetch`` aplica a política de tentativas do config."""

    def fetch(self) -> List[SourceFile]:
        tentativas = max(1, settings.AGROBR_MAX_RETRIES)
        retryer = Retrying(
            stop=stop_after_attempt(tentativas),
            wait=wait_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception_type(SourceFetchError),
            reraise=True,
        )
        return retryer(self._fetch)

    @abstractmethod
    def _fetch(self) -> List[SourceFile]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Modo simulado
# ---------------------------------------------------------------------------

class SimulatedAgrobrClient(AgrobrClient):
    """Coleta simulada a partir de CSVs locais + séries sintéticas determinísticas."""

    ARABICA_FILE = "cafe_arabica_cepea_1996_2026.csv"
    ROBUSTA_FILE = "cafe_robusta_cepea_2001_2026.csv"

    def __init__(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        manual_dir: Optional[str] = None,
    ) -> None:
        self.end_date = end_date or _today()
        self.start_date = start_date or _default_start(self.end_date)
        if self.start_date > self.end_date:
            raise ValueError(
                f"Janela inválida: start_date={self.start_date} > end_date={self.end_date}"
            )
        self.manual_dir = Path(manual_dir or settings.MANUAL_DATA_DIR)
        logger.warning(
            "Modo simulado ativo: preços vêm de %s e clima/futuros são SINTÉTICOS. "
            "Nenhum valor deve ser interpretado como medição real.",
            self.manual_dir,
        )

    # -- preços CEPEA -------------------------------------------------------

    def _read_price_csv(self, file_name: str, variable: str) -> Tuple[List[Dict], str]:
        path = self.manual_dir / file_name
        if not path.is_file():
            raise SourceFetchError(f"CSV de preços não encontrado: {path}")

        rows: List[Dict] = []
        with open(path, "rb") as fh:
            raw_bytes = fh.read()

        text = raw_bytes.decode("utf-8-sig")
        reader = csv.DictReader(text.splitlines())
        faltantes = [c for c in CSV_PRECO_COLUNAS if c not in (reader.fieldnames or [])]
        if faltantes:
            raise SourceFetchError(
                f"Colunas ausentes em {file_name}: {faltantes}. "
                f"Esperado: {list(CSV_PRECO_COLUNAS)}."
            )

        for record in reader:
            obs_date = _parse_date(record.get("data"))
            if obs_date is None or not (self.start_date <= obs_date <= self.end_date):
                continue
            brl = _to_float(record.get("preco_brl_saca"))
            if brl is None or brl <= 0:
                continue
            rows.append(_market_row(obs_date, variable, brl, "CEPEA/ESALQ-SIM"))

        rows.sort(key=lambda r: r["observation_date"])
        logger.info("%s: %d observações de %s na janela.", file_name, len(rows), variable)
        return rows, _hash_bytes(raw_bytes)

    def _derive_fx_and_futures(self, arabica_rows: List[Dict]) -> Dict[str, List[Dict]]:
        """Deriva câmbio e futuros a partir das duas colunas de preço do CSV.

        O CSV traz o preço em R$/saca e em USD/saca. A razão entre eles é uma
        aproximação determinística do US$/BRL; a coluna em USD alimenta o ajuste
        B3 e, convertido por LB_PER_SAC, o contrato KC da ICE. Não há ruído
        aleatório: o resultado é explicável e reproduzível.
        """
        path = self.manual_dir / self.ARABICA_FILE
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8-sig")

        usd_saca: Dict[date, float] = {}
        cambio: Dict[date, float] = {}
        for record in csv.DictReader(text.splitlines()):
            obs_date = _parse_date(record.get("data"))
            if obs_date is None or not (self.start_date <= obs_date <= self.end_date):
                continue
            brl = _to_float(record.get("preco_brl_saca"))
            usd = _to_float(record.get("preco_usd_saca"))
            if brl and usd and usd > 0:
                usd_saca[obs_date] = usd
                cambio[obs_date] = brl / usd

        fx_rows, ice_rows, b3_rows = [], [], []
        for obs_date in (r["observation_date"] for r in arabica_rows):
            usd = usd_saca.get(obs_date)
            fx = cambio.get(obs_date)
            if usd is None or fx is None:
                continue
            fx_rows.append(_market_row(obs_date, "usd_brl", fx, "BCB-PTAX-SIM"))
            b3_rows.append(_market_row(obs_date, "b3_cafe_ajuste", usd, "B3-ICF-SIM"))
            ice_rows.append(
                _market_row(obs_date, "ice_kc", usd / LB_PER_SAC * 100, "ICE-KC-SIM")
            )

        return {
            "usd_brl": fx_rows,
            "ice_kc": ice_rows,
            "b3_cafe_ajuste": b3_rows,
        }

    # -- clima sintético ----------------------------------------------------

    def _iter_dates(self):
        atual = self.start_date
        while atual <= self.end_date:
            yield atual
            atual += timedelta(days=1)

    def _synthetic_weather(self, obs_date: date, region: str) -> Dict[str, float]:
        rng = _seeded_rng(obs_date.isoformat(), region)
        off = REGION_OFFSETS.get(region, REGION_OFFSETS["bambui"])
        season = _season(obs_date.timetuple().tm_yday)

        temp_min = 13.5 + 4.0 * season + off["temp"] + rng.uniform(-2.0, 2.0)
        amplitude = 10.0 + 2.0 * season + rng.uniform(0.0, 2.5)
        temp_max = temp_min + amplitude
        temp_media = (temp_min + temp_max) / 2

        prob_chuva = _clamp(0.42 + 0.30 * season + off["precip"], 0.05, 0.90)
        if rng.random() < prob_chuva:
            precip = _clamp(rng.expovariate(1 / 9.0), 0.1, 90.0)
        else:
            precip = 0.0

        umidade = _clamp(60.0 + 18.0 * season + off["umidade"] + rng.uniform(-8.0, 8.0), 30.0, 99.0)
        radiacao = _clamp(18.0 + 5.5 * season + off["radiacao"] + rng.uniform(-3.0, 3.0), 3.0, 38.0)
        vento = _clamp(2.3 + rng.uniform(-1.2, 2.4), 0.2, 8.0)

        return {
            "temp_min": round(temp_min, 2),
            "temp_max": round(temp_max, 2),
            "temp_media": round(temp_media, 2),
            "precip_mm": round(precip, 2),
            "umidade_rel": round(umidade, 2),
            "radiacao_mj": round(radiacao, 2),
            "vento_ms": round(vento, 2),
        }

    def _build_weather_rows(self) -> List[Dict]:
        rows: List[Dict] = []
        for obs_date in self._iter_dates():
            for region in WEATHER_REGIONS:
                values = self._synthetic_weather(obs_date, region)
                for variable, value in values.items():
                    rows.append(
                        _weather_row(obs_date, region, variable, value, "NASA-POWER-SIM")
                    )
        return rows

    # -- montagem -----------------------------------------------------------

    def _fetch(self) -> List[SourceFile]:
        collected_at = _now_utc()
        commit = _resolve_git_commit(self.manual_dir)

        arabica_rows, arabica_hash = self._read_price_csv(self.ARABICA_FILE, "preco_arabica")
        robusta_rows, robusta_hash = self._read_price_csv(self.ROBUSTA_FILE, "preco_robusta")
        derivadas = self._derive_fx_and_futures(arabica_rows)
        weather_rows = self._build_weather_rows()

        if not arabica_rows and not robusta_rows:
            raise SourceFetchError(
                f"Nenhum preço na janela {self.start_date}..{self.end_date}. "
                "Verifique se os CSVs de base/dados_manuais cobrem o período."
            )

        files = [
            SourceFile(
                source_name="CEPEA/ESALQ",
                file_name=self.ARABICA_FILE,
                content_hash=arabica_hash,
                collected_at=collected_at,
                source_url=f"file://{(self.manual_dir / self.ARABICA_FILE).as_posix()}",
                git_commit=commit,
                market_rows=arabica_rows,
            ),
            SourceFile(
                source_name="CEPEA/ESALQ",
                file_name=self.ROBUSTA_FILE,
                content_hash=robusta_hash,
                collected_at=collected_at,
                source_url=f"file://{(self.manual_dir / self.ROBUSTA_FILE).as_posix()}",
                git_commit=commit,
                market_rows=robusta_rows,
            ),
            SourceFile(
                source_name="BCB PTAX",
                file_name="usd_brl_derivado.csv",
                content_hash=_hash_rows(derivadas["usd_brl"]),
                collected_at=collected_at,
                git_commit=commit,
                market_rows=derivadas["usd_brl"],
            ),
            SourceFile(
                source_name="ICE US",
                file_name="ice_kc_sintetico.csv",
                content_hash=_hash_rows(derivadas["ice_kc"]),
                collected_at=collected_at,
                git_commit=commit,
                market_rows=derivadas["ice_kc"],
            ),
            SourceFile(
                source_name="B3",
                file_name="b3_cafe_ajuste_sintetico.csv",
                content_hash=_hash_rows(derivadas["b3_cafe_ajuste"]),
                collected_at=collected_at,
                git_commit=commit,
                market_rows=derivadas["b3_cafe_ajuste"],
            ),
            SourceFile(
                source_name="NASA POWER",
                file_name="clima_sintetico.csv",
                content_hash=_hash_rows(weather_rows),
                collected_at=collected_at,
                git_commit=commit,
                weather_rows=weather_rows,
            ),
        ]

        logger.warning(
            "Coleta simulada concluída: %d arquivos, %d obs de mercado, %d obs de clima "
            "(clima e futuros SINTÉTICOS).",
            len(files),
            sum(len(f.market_rows) for f in files),
            sum(len(f.weather_rows) for f in files),
        )
        return files


# ---------------------------------------------------------------------------
# Modo real
# ---------------------------------------------------------------------------

class RealAgrobrClient(AgrobrClient):
    """Coleta real — ainda não implementável de forma honesta.

    A especificação proíbe gerar código fictício para a estrutura real do
    Agro.br. Os endpoints, o formato de resposta, a autenticação e a periodicidade
    de cada fonte (CEPEA/ESALQ, BCB PTAX, NASA POWER, B3 e ICE US) não foram
    fornecidos, então esta classe mantém apenas a configuração e falha alto.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> None:
        self.base_url = base_url or os.environ.get("AGROBR_BASE_URL")
        # Credencial vem sempre de variável de ambiente, nunca do código.
        self.api_key = api_key or os.environ.get("AGROBR_API_KEY")
        self.timeout_seconds = timeout_seconds or settings.AGROBR_TIMEOUT_SECONDS

    def _fetch(self) -> List[SourceFile]:
        raise NotImplementedError(
            "Modo real indisponível. Para implementá-lo é preciso, nesta ordem: "
            "(1) URLs oficiais e credenciais de Agro.br/CEPEA/BCB/NASA POWER/B3/ICE; "
            "(2) um exemplo real de resposta de cada endpoint (JSON/CSV/XML); "
            "(3) a periodicidade e o horário de publicação de cada fonte. "
            "Com isso, cada fonte vira um SourceFile com content_hash do payload "
            "original. Enquanto a estrutura real for desconhecida, use "
            "AGROBR_MODE=simulated."
        )


# ---------------------------------------------------------------------------
# Fábrica
# ---------------------------------------------------------------------------

def get_agrobr_client(mode: Optional[str] = None, **kwargs) -> AgrobrClient:
    """Instancia a estratégia de coleta indicada por ``AGROBR_MODE``."""
    modo = (mode or settings.AGROBR_MODE or "simulated").strip().lower()
    if modo in ("simulated", "simulado", "simulate"):
        return SimulatedAgrobrClient(**kwargs)
    if modo in ("real", "production", "producao"):
        return RealAgrobrClient(**kwargs)
    raise ValueError(
        f"AGROBR_MODE inválido: {mode!r}. Use 'simulated' ou 'real'."
    )


if __name__ == "__main__":
    cliente = get_agrobr_client()
    arquivos = cliente.fetch()
    for arq in arquivos:
        print(f"{arq.source_name:12s} {arq.file_name:36s} "
              f"mercado={len(arq.market_rows):6d} clima={len(arq.weather_rows):6d} "
              f"hash={arq.content_hash[:12]}")
