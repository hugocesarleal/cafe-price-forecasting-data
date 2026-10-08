"""Módulo de configuração centralizada via Pydantic Settings.

Fonte única da verdade dos parâmetros: quem integra o pipeline por fora
(Django, worker, cron) monta esta configuração explicitamente em vez de
reinterpretar variáveis de ambiente por conta própria.

A validação acontece no carregamento: fuso e horários inválidos derrubam o
processo no início, com mensagem que diz qual parâmetro corrigir.
"""

import os
from datetime import time
from typing import List, Optional
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator


def parse_hhmm(valor: str) -> time:
    """Converte ``"HH:MM"`` em ``datetime.time``; erro claro se inválido."""
    try:
        horas, minutos = valor.strip().split(":")
        return time(int(horas), int(minutos))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Horário inválido: {valor!r}. Use HH:MM.") from exc


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # PostgreSQL
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 5433
    DB_NAME: str = "cafe_previsao"
    DB_USER: str = "postgres"
    DB_PASSWORD: str = "postgres"
    DB_POOL_MIN: int = 1
    DB_POOL_MAX: int = 10
    # URL completa do banco do pipeline (postgresql://...). Quando definida,
    # tem precedência sobre DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD na
    # conexão do pipeline; quem passa parâmetros explícitos vence a URL.
    PIPELINE_DATABASE_URL: Optional[str] = None
    # Chave do advisory lock que serializa os ciclos do pipeline no PostgreSQL.
    ADVISORY_LOCK_KEY: int = Field(84729103, ge=0, lt=2**63)

    # Janela e Poda
    HISTORICAL_YEARS: int = 9
    MAX_PRUNE_YEARS: int = 2
    DATASET_MIN_DAYS: int = 1096
    CONFIRM_HISTORICAL_WINDOW: bool = True

    # Horizontes (padrão: 7, 15, 30, 90)
    FORECAST_HORIZONS: str = "7,15,30,90"

    # Timezone e Execução
    TIMEZONE: str = "America/Sao_Paulo"
    UPDATE_TIME: str = "00:00"
    AFTER_CLOSE_TIME: str = "19:00"
    BEFORE_OPEN_TIME: str = "08:00"
    JOB_MAX_RETRIES: int = 2
    JOB_RETRY_WAIT_SECONDS: int = 60

    # Agendador (APScheduler)
    SCHEDULER_ENABLED: bool = True
    # Id estável do job no agendador; padrão: o nome do job.
    SCHEDULER_JOB_ID: Optional[str] = None
    # Tolerância (segundos) para um disparo atrasado ainda rodar uma vez
    # (máquina dormiu, reinício). Além dela o disparo é registrado como
    # misfire e não roda sozinho; o próximo horário normal assume.
    MISFIRE_GRACE_SECONDS: int = Field(3600, ge=1)

    # Agro.br / Modo de Coleta
    AGROBR_MODE: str = "simulated"
    AGROBR_TIMEOUT_SECONDS: int = 60
    AGROBR_MAX_RETRIES: int = 3
    COLLECTION_CACHE_DIR: str = ".cache/coleta"
    # Tenta baixar a série histórica do CEPEA a cada coleta real. Desligado por
    # padrão: em 07/10/2026 o site respondia 403 ao download automático.
    CEPEA_AUTO_DOWNLOAD: bool = False
    # Dias recoletados pelos jobs agendados; 0 = a janela histórica inteira.
    COLLECTION_WINDOW_DAYS: int = 0

    # Caminhos Locais
    SQLITE_SOURCE_PATH: str = "base/cafe_centro_oeste_mg.db"
    MANUAL_DATA_DIR: str = "base/dados_manuais"
    LOG_LEVEL: str = "INFO"

    @field_validator("TIMEZONE")
    @classmethod
    def _valida_timezone(cls, valor: str) -> str:
        try:
            ZoneInfo(valor)
        except (KeyError, ValueError) as exc:
            raise ValueError(
                f"Fuso horário inválido: {valor!r}. Use um nome IANA, ex.: America/Sao_Paulo."
            ) from exc
        return valor

    @field_validator("UPDATE_TIME", "AFTER_CLOSE_TIME", "BEFORE_OPEN_TIME")
    @classmethod
    def _valida_horario(cls, valor: str) -> str:
        parse_hhmm(valor)
        return valor

    @property
    def horizons_list(self) -> List[int]:
        return [int(x.strip()) for x in self.FORECAST_HORIZONS.split(",") if x.strip()]

    @property
    def conn_string(self) -> str:
        return f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"



settings = Settings()
