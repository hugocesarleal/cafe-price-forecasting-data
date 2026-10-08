"""Adaptador Django → pipeline: caixa preta de fronteira, sem regra de negócio.

Django, Celery ou um management command chamam este módulo; ele monta a
configuração canônica (``src.config.Settings``), abre a conexão própria do
ciclo (``src.db.pipeline_connection``) e executa a função pública do pipeline
(``run_pipeline`` / ``run_ingestion`` / ``run_dataset_build``), devolvendo o
dicionário de resultado — ``run_id``, ``status``, métricas — com logs e
auditoria preservados.

Este módulo NÃO importa Django: o acoplamento fica confinado aos exemplos de
``examples/django_integration/``. Nada de regra de negócio aqui: só construção
de configuração, conexão dedicada ao ciclo e tradução de erros de fronteira.

Precedência de configuração, por campo:
1. configuração explícita do chamador (``explicit``);
2. configuração adaptada do Django (``django_config``);
3. variáveis de ambiente / ``.env`` (lidas por ``Settings``);
4. padrão do código.

Conflito nunca passa em silêncio: campo injetado que diverge do ambiente é
honrado, com aviso registrando qual fonte venceu; campo que o ciclo lê direto
do ambiente divergente derruba a chamada com ``PipelineConfigError``.

O advisory lock fica na conexão aberta AQUI para o ciclo inteiro; nunca use a
conexão gerenciada pelo ciclo de requests do Django para mantê-lo.
"""

from __future__ import annotations

import time as time_module
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

import psycopg
from pydantic import ValidationError

from src.config import Settings
from src.dataset_versioning import JOB_NAME as JOB_DATASET, run_dataset_build
from src.db import pipeline_connection
from src.ingestion import JOB_NAME as JOB_INGESTAO, IngestionPipeline
from src.logging_config import logger
from src.pipeline import build_client, run_pipeline
from jobs.runner import run_job


class PipelineConfigError(ValueError):
    """Configuração de fronteira inválida ou divergente do ambiente.

    Falha explícita: valores divergentes nunca são escolhidos em silêncio.
    """


class PipelineIntegrationError(RuntimeError):
    """Falha de fronteira da integração (ex.: banco do pipeline inacessível)."""


# Campos cujos valores nunca aparecem em log ou mensagem de erro.
SENSITIVE_FIELDS = frozenset({"DB_PASSWORD", "PIPELINE_DATABASE_URL"})

# Campos que a fronteira consegue injetar fisicamente no ciclo: parâmetros de
# conexão e as duas alavancas de repetição de ``run_job``. Divergência do
# ambiente nesses campos é honrada (com aviso). Os demais campos são lidos
# direto do ambiente pelo ciclo/agendador: divergência explícita derruba.
INJECTED_FIELDS = frozenset({
    "PIPELINE_DATABASE_URL",
    "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
    "JOB_MAX_RETRIES", "JOB_RETRY_WAIT_SECONDS",
})

_PARTES_CONEXAO = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")


@dataclass(frozen=True)
class ResolvedConfig:
    """Configuração canônica resolvida e a origem de cada campo."""

    settings: Settings
    sources: Dict[str, str]


def _valor_loggavel(campo: str, valor: Any) -> str:
    return "(omitido: sensível)" if campo in SENSITIVE_FIELDS else repr(valor)


def resolve_config(
    explicit: Optional[Mapping[str, Any]] = None,
    django_config: Optional[Mapping[str, Any]] = None,
) -> ResolvedConfig:
    """Resolve a configuração canônica com precedência explícita > Django > ambiente.

    Valida os nomes contra ``Settings`` (chave desconhecida é erro: um typo não
    pode ser descartado em silêncio), valida os valores via Pydantic e monta o
    mapa de origem de cada campo (``explicit``, ``django``, ``env`` ou
    ``default``). Divergência entre fonte injetada e ambiente segue a regra de
    ``INJECTED_FIELDS``.
    """
    ambiente = Settings()
    campos_validos = set(Settings.model_fields)

    overrides: Dict[str, Any] = {}
    fontes: Dict[str, str] = {}
    for fonte, valores in (("explicit", explicit), ("django", django_config)):
        for campo, valor in (valores or {}).items():
            if campo not in campos_validos:
                raise PipelineConfigError(
                    f"Configuração {fonte} tem campo desconhecido: {campo!r}. "
                    f"Campos válidos: {', '.join(sorted(campos_validos))}."
                )
            if campo not in fontes:  # explícito vence Django quando ambos informam
                overrides[campo] = valor
                fontes[campo] = fonte

    try:
        efetiva = Settings(**overrides)
    except ValidationError as exc:
        detalhes = "; ".join(
            f"{'.'.join(str(p) for p in erro['loc'])}: {erro['msg']}"
            for erro in exc.errors()
        )
        raise PipelineConfigError(f"Configuração inválida: {detalhes}") from exc

    for campo, fonte in fontes.items():
        valor_efetivo = getattr(efetiva, campo)
        valor_ambiente = getattr(ambiente, campo)
        if valor_efetivo == valor_ambiente:
            continue
        if campo in INJECTED_FIELDS:
            logger.warning(
                "Configuração %s vence o ambiente para %s: %s; valor do ambiente "
                "ignorado: %s.",
                fonte, campo, _valor_loggavel(campo, valor_efetivo),
                _valor_loggavel(campo, valor_ambiente),
            )
            continue
        raise PipelineConfigError(
            f"{campo}={_valor_loggavel(campo, valor_efetivo)} divergiu do ambiente "
            f"({_valor_loggavel(campo, valor_ambiente)}), mas {campo} não é injetável: "
            "o ciclo o lê diretamente das variáveis de ambiente. Ajuste o ambiente "
            f"ou remova {campo} da configuração injetada."
        )

    fontes_completas = {
        campo: fontes.get(
            campo, "env" if campo in ambiente.model_fields_set else "default")
        for campo in Settings.model_fields
    }
    return ResolvedConfig(settings=efetiva, sources=fontes_completas)


def connection_options(cfg: ResolvedConfig) -> Dict[str, Any]:
    """Parâmetros de conexão do ciclo conforme a precedência resolvida.

    ``PIPELINE_DATABASE_URL`` sem partes ``DB_*`` injetadas → conexão pela URL
    (valor nunca registrado). Qualquer parte ``DB_*`` injetada tem precedência
    sobre a URL, espelhando ``get_connection``; nesse caso as cinco partes vão
    explícitas com os valores já resolvidos campo a campo.
    """
    partes_injetadas = [
        campo for campo in _PARTES_CONEXAO
        if cfg.sources.get(campo) in ("explicit", "django")
    ]
    url = cfg.settings.PIPELINE_DATABASE_URL

    if url and not partes_injetadas:
        logger.info("Conexão do ciclo via PIPELINE_DATABASE_URL (valor não registrado).")
        return {"dsn": url}
    if partes_injetadas and url:
        logger.info(
            "Conexão do ciclo: partes DB_* injetadas (%s) têm precedência sobre a URL.",
            ", ".join(partes_injetadas),
        )
    return {
        "host": cfg.settings.DB_HOST,
        "port": cfg.settings.DB_PORT,
        "dbname": cfg.settings.DB_NAME,
        "user": cfg.settings.DB_USER,
        "password": cfg.settings.DB_PASSWORD,
    }


# ---------------------------------------------------------------------------
# Execução dos ciclos públicos (a mesma caixa preta dos jobs)
# ---------------------------------------------------------------------------

def _ciclo_com_conexao_propria(opcoes: Dict[str, Any], executar):
    """Devolve o callable que ``run_job`` executa a cada tentativa.

    Cada tentativa abre e fecha a PRÓPRIA conexão — o advisory lock é adquirido
    nela, usado por todas as etapas e liberado antes do fechamento, inclusive
    quando o ciclo falha.
    """
    def ciclo(
        job: str,
        *,
        cutoff_date=None,
        start_date=None,
        end_date=None,
        force=False,
    ) -> Dict[str, Any]:
        with pipeline_connection(**opcoes) as conn:
            return executar(conn, job, cutoff_date, start_date, end_date, force)

    return ciclo


def run_pipeline_job(
    job_name: str,
    *,
    cutoff_date=None,
    start_date=None,
    end_date=None,
    force: bool = False,
    version_tag: Optional[str] = None,
    ingestion_job: str = JOB_INGESTAO,
    dataset_job: str = JOB_DATASET,
    max_retries: Optional[int] = None,
    retry_wait_seconds: Optional[float] = None,
    explicit: Optional[Mapping[str, Any]] = None,
    django_config: Optional[Mapping[str, Any]] = None,
    sleep=time_module.sleep,
) -> Dict[str, Any]:
    """Executa o ciclo completo (ingestão + dataset) pela fronteira.

    Mesmas repetições, idempotência, auditoria e formato de retorno dos jobs:
    devolve o dicionário do ciclo (``run_id``, ``status``, métricas) acrescido
    de ``tentativas``. Erro inesperado não sobe: esgotadas as tentativas, o
    retorno tem ``status=FAILED`` e ``error``.
    """
    cfg = resolve_config(explicit, django_config)
    opcoes = connection_options(cfg)

    def executar(conn, job, cutoff_date, start_date, end_date, force):
        # O cliente é construído pelo próprio ``run_pipeline``: é o mesmo
        # corpo de job dos jobs agendados, sem reimplementar a janela aqui.
        return run_pipeline(
            job, cutoff_date=cutoff_date, start_date=start_date, end_date=end_date,
            force=force, conn=conn, ingestion_job=ingestion_job,
            dataset_job=dataset_job, version_tag=version_tag,
        )

    return run_job(
        job_name, cutoff_date=cutoff_date, start_date=start_date, end_date=end_date,
        force=force, max_retries=max_retries, retry_wait_seconds=retry_wait_seconds,
        pipeline=_ciclo_com_conexao_propria(opcoes, executar), sleep=sleep,
    )


def run_ingestion_job(
    job_name: str = JOB_INGESTAO,
    *,
    cutoff_date=None,
    start_date=None,
    end_date=None,
    force: bool = False,
    max_retries: Optional[int] = None,
    retry_wait_seconds: Optional[float] = None,
    explicit: Optional[Mapping[str, Any]] = None,
    django_config: Optional[Mapping[str, Any]] = None,
    sleep=time_module.sleep,
) -> Dict[str, Any]:
    """Executa só a ingestão pela fronteira, com o mesmo contrato de retorno."""
    cfg = resolve_config(explicit, django_config)
    opcoes = connection_options(cfg)

    def executar(conn, job, cutoff_date, start_date, end_date, force):
        client = build_client(start_date, end_date or cutoff_date)
        return IngestionPipeline(client=client, conn=conn, job_name=job).run(
            cutoff_date=cutoff_date, force=force)

    return run_job(
        job_name, cutoff_date=cutoff_date, start_date=start_date, end_date=end_date,
        force=force, max_retries=max_retries, retry_wait_seconds=retry_wait_seconds,
        pipeline=_ciclo_com_conexao_propria(opcoes, executar), sleep=sleep,
    )


def run_dataset_job(
    job_name: str = JOB_DATASET,
    *,
    cutoff_date=None,
    start_date=None,
    end_date=None,
    force: bool = False,
    version_tag: Optional[str] = None,
    max_retries: Optional[int] = None,
    retry_wait_seconds: Optional[float] = None,
    explicit: Optional[Mapping[str, Any]] = None,
    django_config: Optional[Mapping[str, Any]] = None,
    sleep=time_module.sleep,
) -> Dict[str, Any]:
    """Constrói a versão do dataset a partir do que já está em ``core``.

    ``end_date`` e ``force`` não se aplicam à construção: os parâmetros existem
    para manter o mesmo contrato de chamada dos demais ciclos.
    """
    cfg = resolve_config(explicit, django_config)
    opcoes = connection_options(cfg)

    def executar(conn, job, cutoff_date, start_date, end_date, force):
        return run_dataset_build(
            conn, cutoff_date=cutoff_date, start_date=start_date, job_name=job,
            version_tag=version_tag,
        )

    return run_job(
        job_name, cutoff_date=cutoff_date, start_date=start_date, end_date=end_date,
        force=force, max_retries=max_retries, retry_wait_seconds=retry_wait_seconds,
        pipeline=_ciclo_com_conexao_propria(opcoes, executar), sleep=sleep,
    )


# ---------------------------------------------------------------------------
# Leitura para o Admin / painel (somente consulta)
# ---------------------------------------------------------------------------

_COLUNAS_EXECUCOES = (
    "run_id::text AS run_id, job_name, status, started_at, finished_at, "
    "records_ingested, records_features, metadata->>'version_tag' AS version_tag, "
    "metadata->>'failed_stage' AS failed_stage, error_message"
)


def _le_execucoes(
    sql: str, params: List[Any], cfg: ResolvedConfig
) -> List[Dict[str, Any]]:
    opcoes = connection_options(cfg)
    try:
        with pipeline_connection(**opcoes) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except psycopg.OperationalError as exc:
        raise PipelineIntegrationError(f"Banco do pipeline inacessível: {exc}") from exc


def read_recent_runs(
    limit: int = 15,
    job_name: Optional[str] = None,
    *,
    explicit: Optional[Mapping[str, Any]] = None,
    django_config: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Últimas execuções de ``audit.pipeline_runs``, mais novas primeiro.

    A versão do pipeline vem de ``metadata->>'version_tag'``: o esquema de
    auditoria não tem coluna própria de versão (limitação documentada).
    """
    sql = f"SELECT {_COLUNAS_EXECUCOES} FROM audit.pipeline_runs"
    params: List[Any] = []
    if job_name:
        sql += " WHERE job_name = %s"
        params.append(job_name)
    sql += " ORDER BY started_at DESC LIMIT %s"
    params.append(limit)
    return _le_execucoes(sql, params, resolve_config(explicit, django_config))


def latest_successful_run(
    job_name: Optional[str] = None,
    *,
    explicit: Optional[Mapping[str, Any]] = None,
    django_config: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Execução bem-sucedida mais recente (ou ``None`` se nunca houve)."""
    sql = f"SELECT {_COLUNAS_EXECUCOES} FROM audit.pipeline_runs WHERE status = 'SUCCESS'"
    params: List[Any] = []
    if job_name:
        sql += " AND job_name = %s"
        params.append(job_name)
    sql += " ORDER BY finished_at DESC LIMIT 1"
    linhas = _le_execucoes(sql, params, resolve_config(explicit, django_config))
    return linhas[0] if linhas else None
