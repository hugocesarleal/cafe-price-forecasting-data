"""Módulo de configuração centralizada via Pydantic Settings."""

import os
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


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

    # Agro.br / Modo de Coleta
    AGROBR_MODE: str = "simulated"
    AGROBR_TIMEOUT_SECONDS: int = 60
    AGROBR_MAX_RETRIES: int = 3

    # Caminhos Locais
    SQLITE_SOURCE_PATH: str = "base/cafe_centro_oeste_mg.db"
    MANUAL_DATA_DIR: str = "base/dados_manuais"
    LOG_LEVEL: str = "INFO"

    @property
    def horizons_list(self) -> List[int]:
        return [int(x.strip()) for x in self.FORECAST_HORIZONS.split(",") if x.strip()]

    @property
    def conn_string(self) -> str:
        return f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"



settings = Settings()
