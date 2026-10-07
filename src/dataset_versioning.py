"""Versionamento imutável do dataset de features.

Implementa os passos 11 e 12 do fluxo descrito em ``docs/architecture.md``:

11. congela a matriz de features numa versão de ``features.dataset_versions``,
    assinada por um checksum SHA-256 do conteúdo gravado;
12. sinaliza a prontidão — o trigger ``trg_dataset_version_ready`` emite
    ``DATASET_READY`` em ``audit.pending_events`` quando a versão vira válida.

Garantias:

* **Imutabilidade** — uma versão nunca é reescrita. Conteúdo novo gera versão
  nova; a única mudança admitida numa versão existente é ``is_valid``.
* **Idempotência** — construir duas vezes o mesmo conteúdo devolve a versão já
  existente, sem linhas nem eventos repetidos.
* **Falha não substitui a última versão válida** — a versão nasce inválida, as
  features são gravadas e só então ela é validada, tudo na mesma transação. Uma
  falha crítica de qualidade ou qualquer erro deixa a versão anterior como a
  mais recente disponível.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Dict, Mapping, Optional

import pandas as pd
import psycopg
from psycopg import sql

from src.config import settings
from src.db import acquire_advisory_lock, get_connection
from src.feature_builder import (
    FeatureMatrix,
    build_feature_matrix,
    column_scales,
    publish_features,
    storage_frame,
)
from src.logging_config import logger
from src.validation import (
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    TARGET_VARIABLE,
    ValidationReport,
    record_checks,
)

JOB_NAME = "dataset_features"
DATASET_SCHEMA_VERSION = "1.0.0"
QUALITY_TABLE = "features.model_features"

EVENTO_BLOQUEIO_QUALIDADE = "DATASET_BLOQUEADO_POR_QUALIDADE"
EVENTO_INVALIDACAO = "DATASET_INVALIDADO"

_COLUNAS_VERSAO = (
    "dataset_version_id, version_tag, cutoff_date, start_date, row_count, "
    "feature_count, sha256_checksum, is_valid, created_at"
)


@dataclass(frozen=True)
class DatasetVersion:
    dataset_version_id: str
    version_tag: str
    cutoff_date: date
    start_date: date
    row_count: int
    feature_count: int
    sha256_checksum: str
    is_valid: bool
    created_at: Optional[datetime] = None
    # True quando a versão já existia e foi reaproveitada em vez de criada.
    reused: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _version_from_row(row: Mapping[str, Any], reused: bool = False) -> DatasetVersion:
    return DatasetVersion(
        dataset_version_id=str(row["dataset_version_id"]),
        version_tag=row["version_tag"],
        cutoff_date=row["cutoff_date"],
        start_date=row["start_date"],
        row_count=row["row_count"],
        feature_count=row["feature_count"],
        sha256_checksum=row["sha256_checksum"].strip(),
        is_valid=row["is_valid"],
        created_at=row["created_at"],
        reused=reused,
    )


# ---------------------------------------------------------------------------
# Checksum
# ---------------------------------------------------------------------------

def _formatar(valor: Any, escala: Optional[int]) -> str:
    if valor is None or (isinstance(valor, float) and math.isnan(valor)):
        return ""
    return f"{valor:.{escala}f}" if escala is not None else str(valor)


def compute_checksum(frame: pd.DataFrame, scales: Mapping[str, int]) -> str:
    """SHA-256 do conteúdo, na forma em que fica gravado.

    Cobre todas as colunas de dados da tabela, em ordem alfabética, com as
    ausentes em ``frame`` contando como nulas. O mesmo cálculo serve para a
    matriz em memória e para as linhas lidas de volta do banco.
    """
    colunas = sorted(scales)
    frame = frame.reindex(columns=colunas)
    linhas = ["data_ref|" + "|".join(colunas)]
    for data_ref, valores in zip(frame.index, frame.itertuples(index=False, name=None)):
        dia = data_ref.date() if isinstance(data_ref, datetime) else data_ref
        linhas.append(
            dia.isoformat() + "|"
            + "|".join(_formatar(v, scales[c]) for c, v in zip(colunas, valores))
        )
    return hashlib.sha256("\n".join(linhas).encode("utf-8")).hexdigest()


def stored_checksum(conn: psycopg.Connection, dataset_version_id: str) -> str:
    """Recalcula o checksum a partir das linhas gravadas da versão."""
    scales = column_scales(conn)
    colunas = sorted(scales)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT data_ref, {} FROM features.model_features "
                    "WHERE dataset_version_id = %s ORDER BY data_ref")
            .format(sql.SQL(", ").join(map(sql.Identifier, colunas))),
            (dataset_version_id,),
        )
        linhas = cur.fetchall()

    frame = pd.DataFrame(
        [[linha[c] for c in colunas] for linha in linhas],
        index=[linha["data_ref"] for linha in linhas],
        columns=colunas,
        dtype=object,
    )
    return compute_checksum(frame, scales)


def verify_dataset_version(conn: psycopg.Connection, dataset_version_id: str) -> bool:
    """Confere se o conteúdo gravado ainda bate com o checksum da versão."""
    versao = get_dataset_version(conn, dataset_version_id)
    if versao is None:
        raise ValueError(f"Versão de dataset inexistente: {dataset_version_id}")
    return stored_checksum(conn, dataset_version_id) == versao.sha256_checksum


# ---------------------------------------------------------------------------
# Qualidade da matriz
# ---------------------------------------------------------------------------

def check_matrix(matrix: FeatureMatrix, dataset_version_id: Optional[str] = None) -> ValidationReport:
    """Checagens que decidem se a matriz pode virar uma versão válida.

    Falha CRÍTICA impede a criação da versão. As de alerta ficam registradas e
    aparecem no status de qualidade entregue ao consumidor.
    """
    report = ValidationReport()
    frame = matrix.frame
    features = frame[matrix.feature_columns]
    ref = {"dataset_version_id": dataset_version_id}

    report.add("matriz_nao_vazia", QUALITY_TABLE, SEVERITY_CRITICAL,
               matrix.row_count > 0, linhas=matrix.row_count, **ref)

    vazadas = sorted(
        c for c in matrix.feature_columns
        if c == TARGET_VARIABLE or c in matrix.target_columns
    )
    report.add("alvo_fora_das_features", QUALITY_TABLE, SEVERITY_CRITICAL,
               not vazadas, colunas=vazadas, **ref)

    vazias = sorted(c for c in matrix.feature_columns if features[c].isna().all())
    report.add("features_sem_coluna_vazia", QUALITY_TABLE, SEVERITY_CRITICAL,
               not vazias, colunas_vazias=vazias, **ref)

    incompletas = sorted(features.columns[features.iloc[-1].isna()]) if len(frame) else []
    report.add("linha_de_corte_completa", QUALITY_TABLE, SEVERITY_WARNING,
               bool(len(frame)) and not incompletas,
               cutoff_date=matrix.cutoff_date.isoformat(), features_nulas=incompletas, **ref)

    sem_alvo = sorted(c for c in matrix.target_columns if frame[c].isna().all())
    report.add("alvos_disponiveis", QUALITY_TABLE, SEVERITY_WARNING,
               not sem_alvo, alvos_sem_valor=sem_alvo, **ref)

    report.add("janela_minima", QUALITY_TABLE, SEVERITY_WARNING,
               matrix.row_count >= settings.DATASET_MIN_DAYS,
               linhas=matrix.row_count, minimo_esperado=settings.DATASET_MIN_DAYS, **ref)

    for falha in report.critical_failures:
        logger.error("Checagem CRÍTICA reprovada: %s -> %s", falha.check_name, falha.details)
    for aviso in report.warning_failures:
        logger.warning("Checagem de alerta reprovada: %s -> %s", aviso.check_name, aviso.details)
    return report


# ---------------------------------------------------------------------------
# Consulta
# ---------------------------------------------------------------------------

def get_dataset_version(conn: psycopg.Connection, dataset_version_id: str) -> Optional[DatasetVersion]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUNAS_VERSAO} FROM features.dataset_versions "
            "WHERE dataset_version_id = %s",
            (dataset_version_id,),
        )
        row = cur.fetchone()
    return _version_from_row(row) if row else None


def get_latest_valid_version(conn: psycopg.Connection) -> Optional[DatasetVersion]:
    """A versão válida mais recente — a que o componente de previsão deve ler."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUNAS_VERSAO} FROM features.dataset_versions "
            "WHERE is_valid = TRUE ORDER BY created_at DESC LIMIT 1"
        )
        row = cur.fetchone()
    return _version_from_row(row) if row else None


# ---------------------------------------------------------------------------
# Criação e invalidação
# ---------------------------------------------------------------------------

def default_version_tag(cutoff_date: date) -> str:
    return f"v{DATASET_SCHEMA_VERSION}-{cutoff_date:%Y%m%d}"


def create_dataset_version(
    conn: psycopg.Connection,
    matrix: FeatureMatrix,
    dataset_version_id: Optional[str] = None,
    version_tag: Optional[str] = None,
) -> DatasetVersion:
    """Congela a matriz numa versão nova e a marca como válida.

    Se já existe uma versão válida com o mesmo conteúdo e a mesma janela, ela é
    devolvida (``reused=True``) e nada é gravado. Não faz commit: a versão e
    suas linhas só ficam visíveis quando quem chamou confirmar a transação.
    """
    if matrix.row_count == 0:
        raise ValueError("Matriz de features vazia não pode virar versão de dataset.")

    scales = column_scales(conn, matrix)
    checksum = compute_checksum(storage_frame(matrix, scales), scales)

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUNAS_VERSAO} FROM features.dataset_versions "
            "WHERE sha256_checksum = %s AND cutoff_date = %s AND start_date = %s "
            "AND is_valid = TRUE ORDER BY created_at DESC LIMIT 1",
            (checksum, matrix.cutoff_date, matrix.start_date),
        )
        existente = cur.fetchone()
        if existente:
            logger.info("Conteúdo idêntico ao da versão %s; nenhuma versão nova criada.",
                        existente["version_tag"])
            return _version_from_row(existente, reused=True)

        # Mesmo corte com conteúdo diferente (dado revisado): o checksum desempata.
        tag = version_tag or default_version_tag(matrix.cutoff_date)
        cur.execute("SELECT 1 FROM features.dataset_versions WHERE version_tag = %s", (tag,))
        if cur.fetchone():
            tag = f"{tag}-{checksum[:8]}"

        version_id = dataset_version_id or str(uuid.uuid4())
        cur.execute(
            "INSERT INTO features.dataset_versions "
            "(dataset_version_id, version_tag, cutoff_date, start_date, row_count, "
            " feature_count, sha256_checksum, is_valid) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)",
            (version_id, tag, matrix.cutoff_date, matrix.start_date, matrix.row_count,
             len(matrix.feature_columns), checksum),
        )

    gravadas = publish_features(conn, version_id, matrix, scales)
    if gravadas != matrix.row_count:
        raise RuntimeError(
            f"Versão {tag}: {gravadas} linhas gravadas, esperadas {matrix.row_count}."
        )

    # Só agora a versão passa a valer; é este UPDATE que dispara DATASET_READY.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE features.dataset_versions SET is_valid = TRUE "
            f"WHERE dataset_version_id = %s RETURNING {_COLUNAS_VERSAO}",
            (version_id,),
        )
        versao = _version_from_row(cur.fetchone())

    logger.info("Versão de dataset criada: %s (%d linhas, %d features, corte %s).",
                versao.version_tag, versao.row_count, versao.feature_count, versao.cutoff_date)
    return versao


def invalidate_dataset_version(
    conn: psycopg.Connection,
    dataset_version_id: str,
    reason: str,
) -> Optional[DatasetVersion]:
    """Retira uma versão de circulação e devolve a que passa a ser a mais recente.

    As linhas da versão são preservadas para auditoria. Não faz commit.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE features.dataset_versions SET is_valid = FALSE "
            "WHERE dataset_version_id = %s AND is_valid = TRUE RETURNING version_tag",
            (dataset_version_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Versão inexistente ou já inválida: {dataset_version_id}")
        cur.execute(
            "SELECT audit.fn_emit_event(%s, %s::jsonb)",
            (EVENTO_INVALIDACAO, json.dumps(
                {"dataset_version_id": dataset_version_id,
                 "version_tag": row["version_tag"], "motivo": reason},
                ensure_ascii=False)),
        )
    logger.warning("Versão %s invalidada: %s", row["version_tag"], reason)
    return get_latest_valid_version(conn)


# ---------------------------------------------------------------------------
# Auditoria da execução
# ---------------------------------------------------------------------------

def register_run_start(
    conn: psycopg.Connection, run_id: str, job_name: str, metadata: Optional[Dict] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "CALL audit.sp_register_pipeline_start(%s, %s, %s::jsonb)",
            (run_id, job_name, json.dumps(metadata or {}, ensure_ascii=False, default=str)),
        )
    conn.commit()


def register_run_finish(
    conn: psycopg.Connection,
    run_id: str,
    status: str,
    records_features: int = 0,
    error: Optional[str] = None,
    records_ingested: int = 0,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "CALL audit.sp_register_pipeline_finish(%s, %s, %s, %s, %s)",
            (run_id, status, records_ingested, records_features, error),
        )
    conn.commit()


def emit_event(conn: psycopg.Connection, event_type: str, payload: Dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT audit.fn_emit_event(%s, %s::jsonb)",
            (event_type, json.dumps(payload, ensure_ascii=False, default=str)),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Execução completa: core -> features -> versão
# ---------------------------------------------------------------------------

def run_dataset_build(
    conn: Optional[psycopg.Connection] = None,
    cutoff_date: Optional[date] = None,
    start_date: Optional[date] = None,
    job_name: str = JOB_NAME,
    version_tag: Optional[str] = None,
) -> Dict[str, Any]:
    """Constrói as features, valida e publica uma versão do dataset.

    Devolve as métricas da execução. Em falha crítica de qualidade termina como
    ``FAILED`` sem criar versão; em erro inesperado registra ``FAILED`` e
    propaga a exceção. Nos dois casos a última versão válida continua intacta.
    """
    run_id = str(uuid.uuid4())
    fecha_conexao = conn is None
    conn = conn or get_connection()

    try:
        register_run_start(conn, run_id, job_name,
                           {"cutoff_date": cutoff_date, "start_date": start_date})
        logger.info("Execução iniciada: run_id=%s job=%s", run_id, job_name)

        try:
            stack = contextlib.ExitStack()
            stack.enter_context(acquire_advisory_lock(conn))
        except RuntimeError as exc:
            logger.warning("Execução concorrente detectada: %s", exc)
            register_run_finish(conn, run_id, "BLOCKED", error=str(exc))
            return {"run_id": run_id, "status": "BLOCKED", "blocked_reason": str(exc)}

        with stack:
            try:
                matriz = build_feature_matrix(conn, cutoff_date, start_date)
                version_id = str(uuid.uuid4())
                report = check_matrix(matriz, version_id)
                record_checks(conn, report, run_id)

                metricas: Dict[str, Any] = {
                    "run_id": run_id,
                    "start_date": matriz.start_date.isoformat(),
                    "cutoff_date": matriz.cutoff_date.isoformat(),
                    "row_count": matriz.row_count,
                    "feature_count": len(matriz.feature_columns),
                    "checks": report.summary(),
                }

                if not report.passed:
                    motivo = "; ".join(report.summary()["critical_failures"])
                    logger.error("Versão não criada por falha CRÍTICA: %s", motivo)
                    anterior = get_latest_valid_version(conn)
                    emit_event(conn, EVENTO_BLOQUEIO_QUALIDADE, {
                        "run_id": run_id, "motivo": motivo,
                        "ultima_versao_valida": anterior.version_tag if anterior else None,
                    })
                    register_run_finish(conn, run_id, "FAILED",
                                        error=f"Qualidade reprovada: {motivo}")
                    metricas.update(
                        status="FAILED", blocked_reason=motivo,
                        ultima_versao_valida=anterior.as_dict() if anterior else None)
                    return metricas

                versao = create_dataset_version(conn, matriz, version_id, version_tag)
                conn.commit()
                register_run_finish(conn, run_id, "SUCCESS",
                                    records_features=0 if versao.reused else versao.row_count)
                metricas.update(status="SUCCESS", versao=versao.as_dict())
                return metricas
            except Exception:
                # Desfaz antes de sair do bloco: o lock só pode ser liberado
                # numa transação sã.
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise

    except Exception as exc:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.error("Construção do dataset falhou (run_id=%s): %s", run_id, exc)
        register_run_finish(conn, run_id, "FAILED", error=str(exc))
        raise
    finally:
        if fecha_conexao:
            conn.close()


if __name__ == "__main__":
    print(json.dumps(run_dataset_build(), ensure_ascii=False, indent=2, default=str))
