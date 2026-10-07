"""Pipeline de ingestão: coleta → raw → validação → staging → core.

Implementa os passos 1 a 9 e 15 do fluxo descrito em ``docs/architecture.md``:

1.  gera ``run_id`` (UUID v4) e registra a execução como RUNNING;
2.  adquire advisory lock para impedir execução concorrente;
3.  coleta os dados pela estratégia configurada em ``AGROBR_MODE``;
4.  registra metadados de cada origem em ``raw.ingestion_files``;
5.  compara o hash com a coleta anterior e detecta mudança na fonte;
6.  grava os brutos em ``raw`` (imutável, com quarentena dos inválidos);
7.  executa as 12 validações e as registra em ``audit.data_quality_checks``;
8.  deduplica e carrega ``staging`` apenas com as linhas aprovadas;
9.  publica em ``core`` pelas stored procedures de upsert;
15. finaliza a execução com status e métricas e libera o lock.

Garantias:

* **Idempotência** — origens cujo hash não mudou desde a última carga bem
  sucedida são ignoradas; a publicação em
  ``core`` usa ``ON CONFLICT DO UPDATE`` com ``COALESCE``, então executar duas
  vezes não duplica nem sobrescreve valores existentes por NULL.
* **Falha crítica não publica** — se alguma checagem CRÍTICA reprova, nada vai
  para ``staging``/``core``; o estado anterior de ``core`` permanece intacto e a
  execução termina como FAILED. Os brutos já coletados são preservados.
* **Concorrência bloqueada** — uma segunda execução simultânea termina como
  BLOCKED sem tocar nos dados.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg

from src.agrobr_client import AgrobrClient, SourceFile, get_agrobr_client
from src.config import settings
from src.db import acquire_advisory_lock, get_connection
from src.logging_config import logger
from src.validation import (
    MARKET_KIND,
    WEATHER_KIND,
    ValidationOutcome,
    deduplicate,
    record_checks,
    validate_batch,
)

JOB_NAME = "ingestao_agrobr"
EVENTO_CONCLUSAO = "INGESTAO_CONCLUIDA"
EVENTO_BLOQUEIO_QUALIDADE = "INGESTAO_BLOQUEADA_POR_QUALIDADE"


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_INSERT_INGESTION_FILE = """
    INSERT INTO raw.ingestion_files
        (file_id, pipeline_run_id, source_name, source_url, git_commit,
         file_name, file_hash, collected_at, row_count, has_changed)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

# Só vale como "carga anterior" a de uma execução que terminou em SUCCESS: se a
# última tentativa falhou depois de registrar o arquivo, o conteúdo ainda não
# chegou a core e precisa ser processado de novo.
_ULTIMO_HASH = """
    SELECT f.file_hash FROM raw.ingestion_files f
    JOIN audit.pipeline_runs r ON r.run_id = f.pipeline_run_id
    WHERE f.source_name = %s AND f.file_name = %s AND f.pipeline_run_id <> %s
      AND r.status = 'SUCCESS'
    ORDER BY f.collected_at DESC
    LIMIT 1
"""

_INSERT_RAW_MARKET = """
    INSERT INTO raw.market_observations
        (file_id, pipeline_run_id, source, observation_date, region,
         variable_name, value, unit, load_version, validation_status)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_RAW_WEATHER = """
    INSERT INTO raw.weather_observations
        (file_id, pipeline_run_id, source, observation_date, region,
         variable_name, value, unit, load_version, validation_status)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_STAGING_MARKET = """
    INSERT INTO staging.market_observations
        (pipeline_run_id, source, observation_date, region,
         variable_name, value, unit, load_version)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_STAGING_WEATHER = """
    INSERT INTO staging.weather_observations
        (pipeline_run_id, source, observation_date, region,
         variable_name, value, unit, load_version)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class IngestionPipeline:
    """Orquestra uma execução completa de ingestão."""

    def __init__(
        self,
        client: Optional[AgrobrClient] = None,
        conn: Optional[psycopg.Connection] = None,
        job_name: str = JOB_NAME,
        load_version: str = "agrobr_v1",
    ) -> None:
        self.client = client or get_agrobr_client()
        self._conn = conn
        self.job_name = job_name
        self.load_version = load_version

    # -- ciclo de vida ------------------------------------------------------

    def run(
        self,
        cutoff_date: Optional[date] = None,
        min_records: int = 1,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Executa a ingestão e devolve as métricas da execução.

        ``force`` reprocessa também as origens cujo hash não mudou — é o que um
        reprocessamento manual de período precisa.
        """
        run_id = uuid.uuid4()
        conn = self._conn or get_connection()
        fecha_conexao = self._conn is None

        try:
            self._register_start(conn, run_id)

            try:
                stack = contextlib.ExitStack()
                stack.enter_context(acquire_advisory_lock(conn))
            except RuntimeError as exc:
                logger.warning("Execução concorrente detectada: %s", exc)
                self._register_finish(conn, run_id, "BLOCKED", error=str(exc))
                self._emit_event(conn, "INGESTAO_BLOQUEADA_CONCORRENCIA",
                                 {"run_id": str(run_id), "motivo": str(exc)})
                return {"run_id": str(run_id), "status": "BLOCKED",
                        "blocked_reason": str(exc)}

            with stack:
                return self._execute(conn, run_id, cutoff_date, min_records, force)

        except Exception as exc:
            # A transação pode estar abortada; precisa de rollback antes de auditar.
            with contextlib.suppress(Exception):
                conn.rollback()
            logger.error("Ingestão falhou (run_id=%s): %s", run_id, exc)
            self._register_finish(conn, run_id, "FAILED", error=str(exc))
            raise
        finally:
            if fecha_conexao:
                conn.close()

    # -- passos 1 e 15: auditoria da execução -------------------------------

    def _register_start(self, conn: psycopg.Connection, run_id: uuid.UUID) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "CALL audit.sp_register_pipeline_start(%s, %s, %s::jsonb)",
                (str(run_id), self.job_name,
                 json.dumps({"modo": settings.AGROBR_MODE,
                             "load_version": self.load_version})),
            )
        conn.commit()
        logger.info("Execução iniciada: run_id=%s job=%s", run_id, self.job_name)

    def _register_finish(
        self,
        conn: psycopg.Connection,
        run_id: uuid.UUID,
        status: str,
        ingested: int = 0,
        error: Optional[str] = None,
    ) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "CALL audit.sp_register_pipeline_finish(%s, %s, %s, 0, %s)",
                (str(run_id), status, ingested, error),
            )
        conn.commit()
        logger.info("Execução finalizada: run_id=%s status=%s registros=%d",
                    run_id, status, ingested)

    def _emit_event(self, conn: psycopg.Connection, event_type: str, payload: Dict) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT audit.fn_emit_event(%s, %s::jsonb) AS event_id",
                (event_type, json.dumps(payload, ensure_ascii=False, default=str)),
            )
            row = cur.fetchone()
        conn.commit()
        return row["event_id"] if row else -1

    # -- passos 3 a 6: coleta, metadados e brutos ---------------------------

    def _register_file(
        self,
        conn: psycopg.Connection,
        run_id: uuid.UUID,
        source_file: SourceFile,
    ) -> Tuple[uuid.UUID, bool]:
        """Grava os metadados da origem e informa se o conteúdo mudou."""
        file_id = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(_ULTIMO_HASH,
                        (source_file.source_name, source_file.file_name, str(run_id)))
            anterior = cur.fetchone()
            # file_hash é CHAR(64): sem strip, um valor mais curto voltaria
            # preenchido com espaços e toda coleta pareceria "alterada".
            hash_anterior = anterior["file_hash"].strip() if anterior else None
            has_changed = hash_anterior != source_file.content_hash

            cur.execute(_INSERT_INGESTION_FILE, (
                str(file_id), str(run_id), source_file.source_name,
                source_file.source_url, source_file.git_commit,
                source_file.file_name, source_file.content_hash,
                source_file.collected_at, source_file.row_count, has_changed,
            ))
        conn.commit()

        if not has_changed:
            logger.info("Fonte inalterada, carga anterior preservada: %s/%s",
                        source_file.source_name, source_file.file_name)
        return file_id, has_changed

    def _insert_raw(
        self,
        conn: psycopg.Connection,
        run_id: uuid.UUID,
        file_id: uuid.UUID,
        rows: Sequence[Dict],
        kind: str,
        status: str,
    ) -> int:
        if not rows:
            return 0
        sql = _INSERT_RAW_MARKET if kind == MARKET_KIND else _INSERT_RAW_WEATHER
        params = [
            (str(file_id), str(run_id), r["source"], r["observation_date"],
             r.get("region"), r["variable_name"], r["value"], r["unit"],
             self.load_version, status)
            for r in rows
        ]
        with conn.cursor() as cur:
            cur.executemany(sql, params)
        conn.commit()
        return len(params)

    def _insert_staging(
        self,
        conn: psycopg.Connection,
        run_id: uuid.UUID,
        rows: Sequence[Dict],
        kind: str,
    ) -> int:
        if not rows:
            return 0
        sql = _INSERT_STAGING_MARKET if kind == MARKET_KIND else _INSERT_STAGING_WEATHER
        params = [
            (str(run_id), r["source"], r["observation_date"], r.get("region"),
             r["variable_name"], r["value"], r["unit"], self.load_version)
            for r in rows
        ]
        with conn.cursor() as cur:
            cur.executemany(sql, params)
        return len(params)

    # -- orquestração -------------------------------------------------------

    def _execute(
        self,
        conn: psycopg.Connection,
        run_id: uuid.UUID,
        cutoff_date: Optional[date],
        min_records: int,
        force: bool = False,
    ) -> Dict[str, Any]:
        coletados: List[SourceFile] = self.client.fetch()
        logger.info("Coleta concluída: %d origens.", len(coletados))

        alterados: List[Tuple[SourceFile, uuid.UUID]] = []
        for source_file in coletados:
            file_id, has_changed = self._register_file(conn, run_id, source_file)
            if has_changed or force:
                alterados.append((source_file, file_id))

        metricas: Dict[str, Any] = {
            "run_id": str(run_id),
            "arquivos_coletados": len(coletados),
            "arquivos_alterados": len(alterados),
            "arquivos_inalterados": len(coletados) - len(alterados),
            "forcado": force,
            "raw_market": 0,
            "raw_weather": 0,
            "raw_invalidos": 0,
            "duplicidades_removidas": 0,
            "staging_market": 0,
            "staging_weather": 0,
            "core_market_dates": 0,
            "core_weather_rows": 0,
            "checks": {},
        }

        if not alterados:
            logger.info("Nenhuma fonte mudou desde a última carga; nada a publicar.")
            self._register_finish(conn, run_id, "SUCCESS", 0)
            metricas["status"] = "SUCCESS"
            return metricas

        # Passo 6: brutos (imutáveis). Linhas reprovadas ficam em quarentena.
        aprovadas = {MARKET_KIND: [], WEATHER_KIND: []}
        reprovadas: List[Dict] = []
        outcomes: List[ValidationOutcome] = []

        for source_file, file_id in alterados:
            for kind, rows in ((MARKET_KIND, source_file.market_rows),
                               (WEATHER_KIND, source_file.weather_rows)):
                if not rows:
                    continue

                outcome = validate_batch(rows, kind, cutoff_date=cutoff_date,
                                         min_records=min_records)
                outcomes.append(outcome)
                reprovadas.extend(outcome.invalid_rows)

                metricas[f"raw_{kind}"] += self._insert_raw(
                    conn, run_id, file_id, outcome.valid_rows, kind, "VALID")
                metricas[f"raw_{kind}"] += self._insert_raw(
                    conn, run_id, file_id, outcome.invalid_rows, kind, "INVALID")
                aprovadas[kind].extend(outcome.valid_rows)

        metricas["raw_invalidos"] = len(reprovadas)
        for outcome in outcomes:
            record_checks(conn, outcome.report, str(run_id))
        metricas["checks"] = {
            "batches": len(outcomes),
            "critical_failures": sorted({
                nome for o in outcomes for nome in o.report.summary()["critical_failures"]
            }),
            "warning_failures": sorted({
                nome for o in outcomes for nome in o.report.summary()["warning_failures"]
            }),
        }

        # Passo 7: falha crítica interrompe a publicação e preserva core.
        bloqueado = any(not o.report.passed for o in outcomes)
        if bloqueado:
            motivo = "; ".join(metricas["checks"]["critical_failures"]) or "validação reprovada"
            logger.error("Publicação interrompida por falha CRÍTICA: %s", motivo)
            self._emit_event(conn, EVENTO_BLOQUEIO_QUALIDADE,
                             {"run_id": str(run_id), "motivo": motivo,
                              "registros_quarentena": len(reprovadas)})
            self._register_finish(
                conn, run_id, "FAILED",
                ingested=metricas["raw_market"] + metricas["raw_weather"],
                error=f"Validação reprovada: {motivo}",
            )
            metricas["status"] = "FAILED"
            metricas["blocked_reason"] = motivo
            return metricas

        # Passos 8 e 9: staging e publicação transacional em core.
        # Deduplicação: raw preserva tudo que chegou (é imutável); a publicação
        # considera apenas a última revisão de cada chave.
        for kind in (MARKET_KIND, WEATHER_KIND):
            antes = len(aprovadas[kind])
            aprovadas[kind] = deduplicate(aprovadas[kind], kind)
            metricas["duplicidades_removidas"] += antes - len(aprovadas[kind])

        metricas["staging_market"] = self._insert_staging(
            conn, run_id, aprovadas[MARKET_KIND], MARKET_KIND)
        metricas["staging_weather"] = self._insert_staging(
            conn, run_id, aprovadas[WEATHER_KIND], WEATHER_KIND)

        with conn.cursor() as cur:
            cur.execute("CALL core.sp_upsert_market_observations(%s)", (str(run_id),))
            cur.execute("CALL core.sp_upsert_weather_observations(%s)", (str(run_id),))
            cur.execute(
                "SELECT count(*) AS n FROM core.market_daily WHERE pipeline_run_id = %s",
                (str(run_id),))
            metricas["core_market_dates"] = cur.fetchone()["n"]
            cur.execute(
                "SELECT count(*) AS n FROM core.weather_daily WHERE pipeline_run_id = %s",
                (str(run_id),))
            metricas["core_weather_rows"] = cur.fetchone()["n"]

        total_raw = metricas["raw_market"] + metricas["raw_weather"]
        event_id = self._emit_event(conn, EVENTO_CONCLUSAO, {
            "run_id": str(run_id),
            "registros_raw": total_raw,
            "registros_staging": metricas["staging_market"] + metricas["staging_weather"],
            "datas_core_mercado": metricas["core_market_dates"],
            "linhas_core_clima": metricas["core_weather_rows"],
        })
        self._register_finish(conn, run_id, "SUCCESS", ingested=total_raw)

        metricas["status"] = "SUCCESS"
        metricas["event_id"] = event_id
        logger.info(
            "Ingestão publicada: raw=%d (mercado=%d, clima=%d), staging=%d, "
            "core mercado=%d datas, core clima=%d linhas.",
            total_raw, metricas["raw_market"], metricas["raw_weather"],
            metricas["staging_market"] + metricas["staging_weather"],
            metricas["core_market_dates"], metricas["core_weather_rows"],
        )
        return metricas


def run_ingestion(
    mode: Optional[str] = None,
    cutoff_date: Optional[date] = None,
    min_records: int = 1,
    force: bool = False,
    **client_kwargs,
) -> Dict[str, Any]:
    """Atalho para executar a ingestão com o cliente configurado."""
    client = get_agrobr_client(mode=mode, **client_kwargs)
    return IngestionPipeline(client=client).run(
        cutoff_date=cutoff_date, min_records=min_records, force=force)


if __name__ == "__main__":
    resultado = run_ingestion()
    print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
