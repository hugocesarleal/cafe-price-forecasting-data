"""Contrato de dados com o componente de rede neural.

Implementa os passos 13 e 14 do fluxo descrito em ``docs/architecture.md`` e o
que ``docs/model_contract.md`` formaliza:

13. expõe a última versão válida do dataset — metadados, colunas e tipos, data
    de corte, horizontes e status de qualidade — e as features dela;
14. recebe, valida e registra as previsões em ``predictions.forecasts``.

O modelo em si não vive aqui. ``mock_neural_model_predict`` é só um consumidor
de exemplo, usado para exercitar o contrato de ponta a ponta.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
import psycopg
from psycopg import sql

from src.config import settings
from src.dataset_versioning import (
    DatasetVersion,
    get_dataset_version,
    get_latest_valid_version,
    register_run_finish,
    register_run_start,
)
from src.db import get_connection
from src.feature_builder import CONTROL_COLUMNS, target_column
from src.logging_config import logger
from src.pruning import pruning_status

JOB_NAME = "previsao_simulada"
MOCK_MODEL_VERSION = "mock_linear_baseline_v0.1"

QUALITY_OK = "OK"
QUALITY_WARNING = "WARNING"

CAMPOS_PREVISAO = (
    "reference_date", "target_date", "horizon_days", "predicted_value",
    "dataset_version_id", "model_version", "pipeline_run_id",
)


# ---------------------------------------------------------------------------
# Consumo: última versão válida
# ---------------------------------------------------------------------------

def _describe_columns(conn: psycopg.Connection) -> Dict[str, str]:
    """Colunas de dados de ``features.model_features`` e seus tipos."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, numeric_precision, numeric_scale "
            "FROM information_schema.columns "
            "WHERE table_schema = 'features' AND table_name = 'model_features' "
            "ORDER BY ordinal_position"
        )
        tipos = {}
        for r in cur.fetchall():
            if r["column_name"] in CONTROL_COLUMNS and r["column_name"] != "data_ref":
                continue
            tipo = r["data_type"]
            if tipo == "numeric" and r["numeric_precision"] is not None:
                tipo = f"numeric({r['numeric_precision']},{r['numeric_scale']})"
            tipos[r["column_name"]] = tipo
    return tipos


def _quality_status(conn: psycopg.Connection, dataset_version_id: str) -> Dict[str, Any]:
    """Status de qualidade da versão, a partir das checagens da sua construção."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT check_name FROM audit.data_quality_checks "
            "WHERE details->>'dataset_version_id' = %s AND NOT passed "
            "ORDER BY check_name",
            (dataset_version_id,),
        )
        alertas = [r["check_name"] for r in cur.fetchall()]
    # Falha crítica nunca chega aqui: ela impede a versão de ser criada.
    return {"status": QUALITY_WARNING if alertas else QUALITY_OK, "alertas": alertas}


def get_latest_dataset(conn: psycopg.Connection) -> Optional[Dict[str, Any]]:
    """Metadados da última versão válida, ou None se ainda não existe nenhuma."""
    versao = get_latest_valid_version(conn)
    if versao is None:
        return None

    tipos = _describe_columns(conn)
    alvos = [target_column(h) for h in settings.horizons_list if target_column(h) in tipos]
    features = [c for c in tipos if c != "data_ref" and not c.startswith("y_")]
    poda = pruning_status(conn, versao.dataset_version_id)
    return {
        "dataset_version_id": versao.dataset_version_id,
        "version_tag": versao.version_tag,
        "cutoff_date": versao.cutoff_date,
        "start_date": versao.start_date,
        "row_count": versao.row_count,
        # Janela que load_features entrega de fato, descontada a poda lógica.
        "active_start_date": poda["inicio_ativo"],
        "active_row_count": poda["linhas_ativas"],
        "feature_count": versao.feature_count,
        "sha256_checksum": versao.sha256_checksum,
        "created_at": versao.created_at,
        "horizons": settings.horizons_list,
        "feature_columns": features,
        "target_columns": alvos,
        "columns": tipos,
        "quality": _quality_status(conn, versao.dataset_version_id),
    }


def load_features(
    conn: psycopg.Connection,
    dataset_version_id: Optional[str] = None,
) -> pd.DataFrame:
    """Features e alvos de uma versão válida, em ordem cronológica.

    Sem ``dataset_version_id`` lê a última versão válida. Linhas podadas
    (``is_pruned``) ficam de fora. É a consulta padrão do contrato.
    """
    if dataset_version_id is None:
        versao = get_latest_valid_version(conn)
        if versao is None:
            raise LookupError("Nenhuma versão válida de dataset disponível.")
    else:
        versao = get_dataset_version(conn, dataset_version_id)
        if versao is None or not versao.is_valid:
            raise LookupError(f"Versão inexistente ou inválida: {dataset_version_id}")

    colunas = list(_describe_columns(conn))
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT {} FROM features.model_features "
                    "WHERE dataset_version_id = %s AND is_pruned = FALSE "
                    "ORDER BY data_ref ASC")
            .format(sql.SQL(", ").join(map(sql.Identifier, colunas))),
            (versao.dataset_version_id,),
        )
        linhas = cur.fetchall()

    frame = pd.DataFrame(linhas, columns=colunas)
    valores = [c for c in colunas if c != "data_ref"]
    frame[valores] = frame[valores].astype(float)
    frame.attrs["dataset_version_id"] = versao.dataset_version_id
    return frame


# ---------------------------------------------------------------------------
# Consumidor de exemplo (não é o modelo)
# ---------------------------------------------------------------------------

def mock_neural_model_predict(
    dataset_df: pd.DataFrame,
    dataset_version_id: str,
    pipeline_run_id: str,
    model_version: str = MOCK_MODEL_VERSION,
    horizons: Optional[Sequence[int]] = None,
) -> List[Dict[str, Any]]:
    """Simula o consumo das features e devolve uma previsão por horizonte.

    Os valores não têm significado: servem apenas para validar o contrato.
    """
    if dataset_df.empty:
        raise ValueError("Dataset de features está vazio.")

    cutoff_date = pd.to_datetime(dataset_df["data_ref"].iloc[-1]).date()
    proxy = dataset_df["preco_robusta"].dropna()
    base_price = float(proxy.iloc[-1]) if len(proxy) else 1500.0

    return [
        {
            "forecast_id": str(uuid.uuid4()),
            "reference_date": cutoff_date,
            "target_date": cutoff_date + timedelta(days=h),
            "horizon_days": h,
            "predicted_value": round(base_price * (1.0 + h * 0.001), 2),
            "dataset_version_id": dataset_version_id,
            "model_version": model_version,
            "pipeline_run_id": pipeline_run_id,
            "forecast_status": "ACTIVE",
        }
        for h in (horizons if horizons is not None else settings.horizons_list)
    ]


# ---------------------------------------------------------------------------
# Retorno: registro das previsões
# ---------------------------------------------------------------------------

def validate_forecasts(
    forecasts: Sequence[Dict[str, Any]],
    version: DatasetVersion,
    horizons: Optional[Sequence[int]] = None,
) -> None:
    """Rejeita o lote inteiro se alguma previsão viola o contrato."""
    permitidos = set(horizons if horizons is not None else settings.horizons_list)
    problemas: List[str] = []
    chaves = set()

    if not forecasts:
        problemas.append("lote de previsões vazio")
    if not version.is_valid:
        problemas.append(f"versão de dataset inválida: {version.version_tag}")

    for i, f in enumerate(forecasts):
        faltantes = [c for c in CAMPOS_PREVISAO if f.get(c) in (None, "")]
        if faltantes:
            problemas.append(f"[{i}] campos ausentes: {faltantes}")
            continue

        h, ref, alvo, valor = (f["horizon_days"], f["reference_date"],
                               f["target_date"], f["predicted_value"])
        if not isinstance(ref, date) or not isinstance(alvo, date):
            problemas.append(f"[{i}] reference_date e target_date precisam ser datas")
            continue
        if h not in permitidos:
            problemas.append(f"[{i}] horizonte {h} fora de {sorted(permitidos)}")
        elif alvo != ref + timedelta(days=h):
            problemas.append(f"[{i}] target_date {alvo} != reference_date {ref} + {h} dias")
        if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not valor > 0:
            problemas.append(f"[{i}] predicted_value inválido: {valor!r}")
        if str(f["dataset_version_id"]) != version.dataset_version_id:
            problemas.append(f"[{i}] dataset_version_id difere da versão informada")
        if ref > version.cutoff_date:
            problemas.append(
                f"[{i}] reference_date {ref} posterior ao corte {version.cutoff_date} da versão")

        chave = (ref, h, f["model_version"])
        if chave in chaves:
            problemas.append(f"[{i}] previsão repetida no lote para {chave}")
        chaves.add(chave)

    if problemas:
        raise ValueError("Previsões fora do contrato: " + "; ".join(problemas))


def register_forecasts(
    conn: psycopg.Connection,
    forecasts: Sequence[Dict[str, Any]],
) -> int:
    """Grava um lote de previsões de uma mesma versão de dataset.

    Uma previsão já registrada para a mesma data de referência, horizonte e
    versão de modelo é atualizada, não duplicada. Registra também a execução do
    modelo em ``audit.model_runs``. Não faz commit.
    """
    if not forecasts:
        raise ValueError("Previsões fora do contrato: lote de previsões vazio")

    version_id = str(forecasts[0].get("dataset_version_id"))
    versao = get_dataset_version(conn, version_id)
    if versao is None:
        raise ValueError(f"Previsões fora do contrato: versão de dataset inexistente: {version_id}")
    validate_forecasts(forecasts, versao)

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO predictions.forecasts
                (forecast_id, reference_date, target_date, horizon_days, predicted_value,
                 dataset_version_id, model_version, pipeline_run_id, forecast_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (reference_date, horizon_days, model_version) DO UPDATE SET
                target_date = EXCLUDED.target_date,
                predicted_value = EXCLUDED.predicted_value,
                generated_at = clock_timestamp(),
                dataset_version_id = EXCLUDED.dataset_version_id,
                pipeline_run_id = EXCLUDED.pipeline_run_id,
                forecast_status = EXCLUDED.forecast_status
            """,
            [
                (str(f.get("forecast_id") or uuid.uuid4()), f["reference_date"],
                 f["target_date"], f["horizon_days"], f["predicted_value"],
                 version_id, f["model_version"], str(f["pipeline_run_id"]),
                 f.get("forecast_status") or "ACTIVE")
                for f in forecasts
            ],
        )

        execucoes: Dict[tuple, int] = {}
        for f in forecasts:
            chave = (str(f["pipeline_run_id"]), f["model_version"])
            execucoes[chave] = execucoes.get(chave, 0) + 1
        for (run_id, model_version), quantidade in execucoes.items():
            cur.execute(
                "INSERT INTO audit.model_runs "
                "(model_run_id, pipeline_run_id, model_version, input_dataset_version_id, "
                " records_predicted, status) VALUES (%s, %s, %s, %s, %s, 'COMPLETED')",
                (str(uuid.uuid4()), run_id, model_version, version_id, quantidade),
            )

    logger.info("predictions.forecasts: %d previsões registradas (versão %s).",
                len(forecasts), versao.version_tag)
    return len(forecasts)


# ---------------------------------------------------------------------------
# Ciclo completo com o consumidor de exemplo
# ---------------------------------------------------------------------------

def run_mock_forecast(
    conn: Optional[psycopg.Connection] = None,
    job_name: str = JOB_NAME,
    model_version: str = MOCK_MODEL_VERSION,
) -> Dict[str, Any]:
    """Lê a última versão válida, gera previsões simuladas e as registra."""
    run_id = str(uuid.uuid4())
    fecha_conexao = conn is None
    conn = conn or get_connection()

    try:
        register_run_start(conn, run_id, job_name, {"model_version": model_version})
        dataset = get_latest_dataset(conn)
        if dataset is None:
            raise LookupError("Nenhuma versão válida de dataset disponível.")

        features = load_features(conn, dataset["dataset_version_id"])
        previsoes = mock_neural_model_predict(
            features, dataset["dataset_version_id"], run_id, model_version)
        registradas = register_forecasts(conn, previsoes)
        conn.commit()
        register_run_finish(conn, run_id, "SUCCESS")

        return {
            "run_id": run_id,
            "status": "SUCCESS",
            "dataset_version_id": dataset["dataset_version_id"],
            "version_tag": dataset["version_tag"],
            "quality": dataset["quality"],
            "model_version": model_version,
            "previsoes_registradas": registradas,
            "previsoes": previsoes,
        }
    except Exception as exc:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.error("Previsão simulada falhou (run_id=%s): %s", run_id, exc)
        register_run_finish(conn, run_id, "FAILED", error=str(exc))
        raise
    finally:
        if fecha_conexao:
            conn.close()


if __name__ == "__main__":
    print(json.dumps(run_mock_forecast(), ensure_ascii=False, indent=2, default=str))
