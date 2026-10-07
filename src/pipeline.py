"""Orquestrador do ciclo completo: ingestão -> features -> versão do dataset.

Encadeia os passos de ``docs/architecture.md`` que cabem ao componente de dados
numa única execução auditada. É o que os jobs de ``jobs/`` chamam.

* Uma execução "mãe" é registrada em ``audit.pipeline_runs`` com o nome do job;
  ingestão e construção do dataset mantêm suas próprias execuções, e a mãe
  guarda os ``run_id`` delas em ``metadata``.
* O advisory lock é adquirido uma vez, na conexão compartilhada pelas etapas,
  então nenhum outro job entra no meio do ciclo.
* Se a ingestão não termina em SUCCESS, o dataset não é reconstruído: a última
  versão válida continua sendo a entregue.
* Ao final ficam registradas a data da última observação disponível e a hora da
  última ingestão.

Rodar o ciclo de novo é seguro: origem inalterada não é republicada, conteúdo
idêntico reaproveita a versão existente e previsões são gravadas por upsert.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import date
from typing import Any, Dict, Optional

import psycopg

from src.agrobr_client import AgrobrClient, get_agrobr_client
from src.config import settings
from src.dataset_versioning import (
    JOB_NAME as JOB_DATASET,
    register_run_finish,
    register_run_start,
    run_dataset_build,
)
from src.db import acquire_advisory_lock, get_connection
from src.ingestion import JOB_NAME as JOB_INGESTAO, IngestionPipeline
from src.logging_config import logger

MODOS_SIMULADOS = ("simulated", "simulado", "simulate")

STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_BLOCKED = "BLOCKED"


def build_client(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> AgrobrClient:
    """Cliente de coleta do modo configurado, restrito à janela pedida.

    Só o modo simulado aceita janela; pedir uma no modo real é erro, para que um
    reprocessamento de período nunca vire, em silêncio, uma coleta completa.
    """
    simulado = (settings.AGROBR_MODE or "simulated").strip().lower() in MODOS_SIMULADOS
    janela = {k: v for k, v in (("start_date", start_date), ("end_date", end_date)) if v}
    if janela and not simulado:
        raise ValueError(
            "Janela de coleta (start_date/end_date) só é suportada com "
            f"AGROBR_MODE=simulated; modo atual: {settings.AGROBR_MODE!r}."
        )
    return get_agrobr_client(**janela)


def read_freshness(conn: psycopg.Connection) -> Dict[str, Any]:
    """Última observação disponível em ``core`` e última ingestão bem sucedida."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(data_ref) FILTER (WHERE preco_arabica IS NOT NULL) AS alvo, "
            "       max(data_ref) AS mercado FROM core.market_daily"
        )
        mercado = cur.fetchone()
        cur.execute("SELECT max(data_ref) AS clima FROM core.weather_daily")
        clima = cur.fetchone()
        cur.execute(
            "SELECT max(f.collected_at) AS coletado_em FROM raw.ingestion_files f "
            "JOIN audit.pipeline_runs r ON r.run_id = f.pipeline_run_id "
            "WHERE r.status = 'SUCCESS'"
        )
        ingestao = cur.fetchone()
    return {
        "ultima_observacao_preco_arabica": mercado["alvo"],
        "ultima_observacao_mercado": mercado["mercado"],
        "ultima_observacao_clima": clima["clima"],
        "ultima_ingestao_em": ingestao["coletado_em"],
    }


def _save_metadata(conn: psycopg.Connection, run_id: str, dados: Dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE audit.pipeline_runs SET metadata = metadata || %s::jsonb WHERE run_id = %s",
            (json.dumps(dados, ensure_ascii=False, default=str), run_id),
        )
    conn.commit()


def run_pipeline(
    job_name: str,
    cutoff_date: Optional[date] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    force: bool = False,
    conn: Optional[psycopg.Connection] = None,
    client: Optional[AgrobrClient] = None,
    ingestion_job: str = JOB_INGESTAO,
    dataset_job: str = JOB_DATASET,
    version_tag: Optional[str] = None,
) -> Dict[str, Any]:
    """Executa ingestão e construção do dataset e devolve as métricas do ciclo.

    ``cutoff_date`` é o dia que o job está fechando: nada posterior a ele é
    aceito na ingestão e é nele que a versão do dataset termina. Sem ele, a
    ingestão valida contra hoje e a versão termina no último dia com preço.
    ``start_date``/``end_date`` restringem a coleta (``end_date`` assume o
    corte); ``force`` reprocessa origens mesmo sem mudança de hash.

    ``status`` é SUCCESS, FAILED (etapa reprovada, ver ``failed_stage``) ou
    BLOCKED. Erros inesperados são registrados como FAILED e propagados.
    """
    run_id = str(uuid.uuid4())
    fecha_conexao = conn is None
    conn = conn or get_connection()
    resultado: Dict[str, Any] = {"run_id": run_id, "job_name": job_name}

    try:
        register_run_start(conn, run_id, job_name, {
            "cutoff_date": cutoff_date, "start_date": start_date,
            "end_date": end_date, "force": force,
        })
        logger.info("Job iniciado: %s run_id=%s corte=%s", job_name, run_id, cutoff_date)

        try:
            stack = contextlib.ExitStack()
            stack.enter_context(acquire_advisory_lock(conn))
        except RuntimeError as exc:
            logger.warning("Job %s não executado: %s", job_name, exc)
            register_run_finish(conn, run_id, STATUS_BLOCKED, error=str(exc))
            return {**resultado, "status": STATUS_BLOCKED, "blocked_reason": str(exc)}

        with stack:
            try:
                return _run_stages(
                    conn, run_id, resultado, cutoff_date, start_date, end_date, force,
                    client, ingestion_job, dataset_job, version_tag)
            except Exception:
                # Desfaz antes de sair do bloco: o lock só pode ser liberado
                # numa transação sã.
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise

    except Exception as exc:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.error("Job %s falhou (run_id=%s): %s", job_name, run_id, exc)
        register_run_finish(conn, run_id, STATUS_FAILED, error=str(exc))
        raise
    finally:
        if fecha_conexao:
            conn.close()


def _run_stages(
    conn: psycopg.Connection,
    run_id: str,
    resultado: Dict[str, Any],
    cutoff_date: Optional[date],
    start_date: Optional[date],
    end_date: Optional[date],
    force: bool,
    client: Optional[AgrobrClient],
    ingestion_job: str,
    dataset_job: str,
    version_tag: Optional[str],
) -> Dict[str, Any]:
    client = client or build_client(start_date, end_date or cutoff_date)

    ingestao = IngestionPipeline(client=client, conn=conn, job_name=ingestion_job).run(
        cutoff_date=cutoff_date, force=force)
    resultado["ingestion"] = ingestao
    ingeridos = ingestao.get("raw_market", 0) + ingestao.get("raw_weather", 0)

    if ingestao["status"] != STATUS_SUCCESS:
        motivo = ingestao.get("blocked_reason") or ingestao["status"]
        return _finish(conn, run_id, resultado, STATUS_FAILED, "ingestion",
                       f"Ingestão {ingestao['status']}: {motivo}", ingeridos)

    dataset = run_dataset_build(
        conn, cutoff_date=cutoff_date, job_name=dataset_job, version_tag=version_tag)
    resultado["dataset"] = dataset

    if dataset["status"] != STATUS_SUCCESS:
        motivo = dataset.get("blocked_reason") or dataset["status"]
        return _finish(conn, run_id, resultado, STATUS_FAILED, "dataset",
                       f"Dataset {dataset['status']}: {motivo}", ingeridos)

    versao = dataset["versao"]
    return _finish(conn, run_id, resultado, STATUS_SUCCESS, None, None, ingeridos,
                   features=0 if versao["reused"] else versao["row_count"])


def _finish(
    conn: psycopg.Connection,
    run_id: str,
    resultado: Dict[str, Any],
    status: str,
    failed_stage: Optional[str],
    error: Optional[str],
    ingeridos: int,
    features: int = 0,
) -> Dict[str, Any]:
    frescor = read_freshness(conn)
    versao = (resultado.get("dataset") or {}).get("versao") or {}
    _save_metadata(conn, run_id, {
        "ingestion_run_id": (resultado.get("ingestion") or {}).get("run_id"),
        "dataset_run_id": (resultado.get("dataset") or {}).get("run_id"),
        "dataset_version_id": versao.get("dataset_version_id"),
        "version_tag": versao.get("version_tag"),
        "version_reused": versao.get("reused"),
        "failed_stage": failed_stage,
        **frescor,
    })
    register_run_finish(conn, run_id, status, records_features=features,
                        error=error, records_ingested=ingeridos)

    resultado.update(status=status, freshness=frescor)
    if failed_stage:
        resultado.update(failed_stage=failed_stage, error=error)
        logger.error("Job %s terminou FAILED na etapa %s: %s",
                     resultado["job_name"], failed_stage, error)
    else:
        logger.info(
            "Job %s concluído: %d registros ingeridos, versão %s%s. Última observação "
            "de preço: %s; última ingestão: %s.",
            resultado["job_name"], ingeridos, versao.get("version_tag"),
            " (reaproveitada)" if versao.get("reused") else "",
            frescor["ultima_observacao_preco_arabica"], frescor["ultima_ingestao_em"],
        )
    return resultado
