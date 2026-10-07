"""Poda lógica da janela histórica de uma versão do dataset.

Regras de ``docs/architecture.md`` (seção 5):

* **Poda estritamente lógica** — marca ``is_pruned = TRUE`` nas linhas mais
  antigas de ``features.model_features``. Nada é apagado, em tabela alguma;
  ``raw`` e ``core`` nem são tocados. A poda pode ser desfeita.
* **Teto de 2 anos** — a soma do que já foi podado numa versão com o que se quer
  podar não pode passar de ``MAX_PRUNE_YEARS`` (730 dias).
* **Mínimo de segurança** — a janela ativa que sobra não pode ficar menor que
  ``DATASET_MIN_DAYS``.
* **Trava de confirmação** — sem ``CONFIRM_HISTORICAL_WINDOW=true`` nada é
  podado: a divergência entre a janela de 9 anos e a de ~1.096 dias precisa ser
  assumida por alguém antes de se descartar histórico.

A poda vale para uma versão. Versões novas nascem com a janela inteira.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import uuid
from datetime import date, timedelta
from typing import Any, Dict, Optional

import psycopg

from src.config import settings
from src.dataset_versioning import (
    DatasetVersion,
    get_dataset_version,
    get_latest_valid_version,
    register_run_finish,
    register_run_start,
)
from src.db import get_connection
from src.logging_config import logger

JOB_NAME = "poda_dataset"
EVENTO_PODA = "DATASET_PODADO"
EVENTO_RESTAURACAO = "DATASET_PODA_DESFEITA"
DIAS_POR_ANO = 365


class PruneRejected(ValueError):
    """A poda pedida viola uma das regras de contenção."""


def max_prune_days() -> int:
    return settings.MAX_PRUNE_YEARS * DIAS_POR_ANO


def validate_prune(start_date: date, cutoff_date: date, new_start_date: date) -> int:
    """Confere a poda contra as regras e devolve quantos dias ela descarta.

    ``start_date`` e ``cutoff_date`` são os da versão; ``new_start_date`` é o
    primeiro dia que continua ativo. A conta é sempre feita a partir do início
    original da versão, então podas sucessivas não contornam o teto.
    """
    if not settings.CONFIRM_HISTORICAL_WINDOW:
        raise PruneRejected(
            "Poda recusada: defina CONFIRM_HISTORICAL_WINDOW=true depois de confirmar "
            f"com a equipe a janela histórica (HISTORICAL_YEARS={settings.HISTORICAL_YEARS} "
            f"anos contra o mínimo de {settings.DATASET_MIN_DAYS} dias)."
        )
    if new_start_date <= start_date:
        raise PruneRejected(
            f"Nada a podar: o novo início {new_start_date} não é posterior ao início "
            f"{start_date} da versão."
        )
    if new_start_date > cutoff_date:
        raise PruneRejected(
            f"Poda recusada: o novo início {new_start_date} é posterior ao corte "
            f"{cutoff_date} e não sobraria nenhuma linha."
        )

    podados = (new_start_date - start_date).days
    if podados > max_prune_days():
        raise PruneRejected(
            f"Poda recusada: descartaria {podados} dias, acima do teto de "
            f"{max_prune_days()} dias (MAX_PRUNE_YEARS={settings.MAX_PRUNE_YEARS})."
        )

    restantes = (cutoff_date - new_start_date).days + 1
    if restantes < settings.DATASET_MIN_DAYS:
        raise PruneRejected(
            f"Poda recusada: sobrariam {restantes} dias, abaixo do mínimo de segurança "
            f"DATASET_MIN_DAYS={settings.DATASET_MIN_DAYS}."
        )
    return podados


def _require_version(conn: psycopg.Connection, dataset_version_id: Optional[str]) -> DatasetVersion:
    versao = (get_dataset_version(conn, dataset_version_id) if dataset_version_id
              else get_latest_valid_version(conn))
    if versao is None:
        raise LookupError(
            f"Versão de dataset inexistente: {dataset_version_id}" if dataset_version_id
            else "Nenhuma versão válida de dataset disponível."
        )
    return versao


def pruning_status(conn: psycopg.Connection, dataset_version_id: str) -> Dict[str, Any]:
    """Quantas linhas da versão estão podadas e onde começa a janela ativa."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS total, "
            "       count(*) FILTER (WHERE is_pruned) AS podadas, "
            "       min(data_ref) FILTER (WHERE NOT is_pruned) AS inicio_ativo "
            "FROM features.model_features WHERE dataset_version_id = %s",
            (dataset_version_id,),
        )
        row = cur.fetchone()
    return {
        "linhas": row["total"],
        "linhas_podadas": row["podadas"],
        "linhas_ativas": row["total"] - row["podadas"],
        "inicio_ativo": row["inicio_ativo"],
    }


def _emit(conn: psycopg.Connection, event_type: str, payload: Dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT audit.fn_emit_event(%s, %s::jsonb)",
            (event_type, json.dumps(payload, ensure_ascii=False, default=str)),
        )


def prune_dataset_version(
    conn: psycopg.Connection,
    new_start_date: date,
    dataset_version_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Marca como podadas as linhas anteriores a ``new_start_date``.

    Sem ``dataset_version_id`` atua na última versão válida. Levanta
    ``PruneRejected`` se a poda violar alguma regra, sem alterar nada. Não faz
    commit.
    """
    versao = _require_version(conn, dataset_version_id)
    logger.info(
        "Poda pedida para %s: janela %s a %s, novo início %s. Configurado: "
        "HISTORICAL_YEARS=%d, MAX_PRUNE_YEARS=%d (%d dias), DATASET_MIN_DAYS=%d.",
        versao.version_tag, versao.start_date, versao.cutoff_date, new_start_date,
        settings.HISTORICAL_YEARS, settings.MAX_PRUNE_YEARS, max_prune_days(),
        settings.DATASET_MIN_DAYS,
    )
    dias = validate_prune(versao.start_date, versao.cutoff_date, new_start_date)

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE features.model_features SET is_pruned = TRUE "
            "WHERE dataset_version_id = %s AND data_ref < %s AND NOT is_pruned",
            (versao.dataset_version_id, new_start_date),
        )
        marcadas = cur.rowcount

    resultado = {
        "dataset_version_id": versao.dataset_version_id,
        "version_tag": versao.version_tag,
        "novo_inicio": new_start_date,
        "dias_podados": dias,
        "linhas_marcadas_agora": marcadas,
        **pruning_status(conn, versao.dataset_version_id),
    }
    _emit(conn, EVENTO_PODA, resultado)
    logger.warning("Versão %s podada: %d dias fora da janela ativa (%d linhas marcadas agora).",
                   versao.version_tag, dias, marcadas)
    return resultado


def restore_pruned(conn: psycopg.Connection, dataset_version_id: Optional[str] = None) -> Dict[str, Any]:
    """Desfaz a poda de uma versão: todas as linhas voltam à janela ativa. Não faz commit."""
    versao = _require_version(conn, dataset_version_id)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE features.model_features SET is_pruned = FALSE "
            "WHERE dataset_version_id = %s AND is_pruned",
            (versao.dataset_version_id,),
        )
        restauradas = cur.rowcount

    resultado = {
        "dataset_version_id": versao.dataset_version_id,
        "version_tag": versao.version_tag,
        "linhas_restauradas": restauradas,
        **pruning_status(conn, versao.dataset_version_id),
    }
    _emit(conn, EVENTO_RESTAURACAO, resultado)
    logger.info("Poda da versão %s desfeita: %d linhas restauradas.",
                versao.version_tag, restauradas)
    return resultado


# ---------------------------------------------------------------------------
# Execução auditada (linha de comando)
# ---------------------------------------------------------------------------

def run_prune(
    new_start_date: Optional[date] = None,
    prune_days: Optional[int] = None,
    dataset_version_id: Optional[str] = None,
    restore: bool = False,
    conn: Optional[psycopg.Connection] = None,
    job_name: str = JOB_NAME,
) -> Dict[str, Any]:
    """Poda (ou restaura) uma versão numa execução registrada em ``audit.pipeline_runs``.

    A janela a descartar é dada por ``new_start_date`` ou por ``prune_days``
    contados a partir do início da versão. Uma poda recusada termina como
    ``FAILED`` com o motivo, sem alterar nada.
    """
    if not restore and (new_start_date is None) == (prune_days is None):
        raise ValueError("Informe new_start_date ou prune_days (apenas um).")

    run_id = str(uuid.uuid4())
    fecha_conexao = conn is None
    conn = conn or get_connection()
    try:
        register_run_start(conn, run_id, job_name, {
            "dataset_version_id": dataset_version_id, "new_start_date": new_start_date,
            "prune_days": prune_days, "restore": restore,
        })
        try:
            if restore:
                resultado = restore_pruned(conn, dataset_version_id)
            else:
                if new_start_date is None:
                    versao = _require_version(conn, dataset_version_id)
                    new_start_date = versao.start_date + timedelta(days=prune_days)
                resultado = prune_dataset_version(conn, new_start_date, dataset_version_id)
            conn.commit()
        except PruneRejected as exc:
            conn.rollback()
            logger.error("%s", exc)
            register_run_finish(conn, run_id, "FAILED", error=str(exc))
            return {"run_id": run_id, "status": "FAILED", "error": str(exc)}

        register_run_finish(conn, run_id, "SUCCESS")
        return {"run_id": run_id, "status": "SUCCESS", **resultado}
    except Exception as exc:
        with contextlib.suppress(Exception):
            conn.rollback()
        register_run_finish(conn, run_id, "FAILED", error=str(exc))
        raise
    finally:
        if fecha_conexao:
            conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.pruning",
        description="Poda lógica da janela histórica de uma versão do dataset.")
    parser.add_argument("--version", default=None,
                        help="dataset_version_id; padrão: última versão válida")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--start", type=date.fromisoformat,
                       help="primeiro dia que continua ativo (AAAA-MM-DD)")
    grupo.add_argument("--days", type=int, help="dias a descartar a partir do início da versão")
    grupo.add_argument("--restore", action="store_true", help="desfaz a poda da versão")
    args = parser.parse_args(argv)

    resultado = run_prune(args.start, args.days, args.version, restore=args.restore)
    print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
    return 0 if resultado["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
