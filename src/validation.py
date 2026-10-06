"""Validações de qualidade executadas antes de publicar dados em staging/core.

Regras do documento de especificação: nenhum dado inválido chega às tabelas de
decisão, toda checagem é registrada em ``audit.data_quality_checks`` e uma falha
CRÍTICA interrompe a publicação preservando o estado anterior de ``core``.

As checagens se dividem em dois grupos:

* **Por linha** — colunas obrigatórias, nulos, tipos, datas, limites físicos.
  Linhas reprovadas são separadas em ``invalid_rows`` (quarentena) e nunca vão
  para staging.
* **Por lote** — duplicidades, continuidade temporal, volume mínimo,
  disponibilidade para os horizontes e ausência de informação futura.

``passed`` reflete a qualidade da fonte: uma checagem por linha só "passa" se
nenhuma linha foi reprovada por ela. A exceção é ``limites_fisicos``, que tolera
uma pequena parcela de valores inválidos desde que a variável alvo não seja
afetada — caso contrário uma única leitura ruim da NASA POWER bloquearia o
pipeline diário inteiro.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from src.config import settings
from src.logging_config import logger

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_WARNING = "WARNING"

TARGET_VARIABLE = "preco_arabica"

MARKET_KIND = "market"
WEATHER_KIND = "weather"

TABLE_BY_KIND = {
    MARKET_KIND: "raw.market_observations",
    WEATHER_KIND: "raw.weather_observations",
}

# Parcela de valores fora dos limites físicos tolerada antes de bloquear a carga.
MAX_INVALID_SHARE = 0.05
EXEMPLOS_DETALHE = 5

# Lacuna máxima (dias corridos) aceita entre observações consecutivas. Preços só
# existem em dias úteis, por isso o mercado tolera fins de semana e feriados.
MAX_GAP_DAYS = {MARKET_KIND: 7, WEATHER_KIND: 3}

REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    MARKET_KIND: ("observation_date", "variable_name", "value", "unit", "source"),
    WEATHER_KIND: ("observation_date", "region", "variable_name", "value", "unit", "source"),
}

PHYSICAL_LIMITS: Dict[str, Tuple[float, float]] = {
    "preco_arabica": (10.0, 20000.0),
    "preco_robusta": (10.0, 20000.0),
    "usd_brl": (0.5, 20.0),
    "usd_brl_compra": (0.5, 20.0),
    "ice_kc": (20.0, 1000.0),
    "b3_cafe_ajuste": (20.0, 5000.0),
    "temp_min": (-15.0, 45.0),
    "temp_max": (-15.0, 50.0),
    "temp_media": (-15.0, 47.0),
    "precip_mm": (0.0, 600.0),
    "umidade_rel": (0.0, 100.0),
    "radiacao_mj": (0.0, 45.0),
    "vento_ms": (0.0, 70.0),
}

EXPECTED_UNITS: Dict[str, str] = {
    "preco_arabica": "R$/sc 60kg",
    "preco_robusta": "R$/sc 60kg",
    "usd_brl": "BRL",
    "usd_brl_compra": "BRL",
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


# ---------------------------------------------------------------------------
# Estruturas
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    check_name: str
    table_name: str
    severity: str
    passed: bool
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    results: List[CheckResult] = field(default_factory=list)

    def add(self, check_name: str, table_name: str, severity: str,
            passed: bool, **details: Any) -> CheckResult:
        result = CheckResult(check_name, table_name, severity, passed, details)
        self.results.append(result)
        return result

    @property
    def critical_failures(self) -> List[CheckResult]:
        return [r for r in self.results
                if not r.passed and r.severity == SEVERITY_CRITICAL]

    @property
    def warning_failures(self) -> List[CheckResult]:
        return [r for r in self.results
                if not r.passed and r.severity == SEVERITY_WARNING]

    @property
    def passed(self) -> bool:
        """True quando nenhuma checagem CRÍTICA falhou."""
        return not self.critical_failures

    def summary(self) -> Dict[str, Any]:
        return {
            "total_checks": len(self.results),
            "critical_failures": [r.check_name for r in self.critical_failures],
            "warning_failures": [r.check_name for r in self.warning_failures],
            "passed": self.passed,
        }


@dataclass
class ValidationOutcome:
    report: ValidationReport
    valid_rows: List[Dict]
    invalid_rows: List[Dict]

    @property
    def approved(self) -> bool:
        return self.report.passed and bool(self.valid_rows)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def today_in_tz() -> date:
    return datetime.now(ZoneInfo(settings.TIMEZONE)).date()


def as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def dedup_key(row: Dict, kind: str) -> Tuple:
    """Chave de idempotência documentada em docs/architecture.md."""
    base = (as_date(row.get("observation_date")), row.get("variable_name"))
    if kind == WEATHER_KIND:
        return (base[0], row.get("region"), base[1])
    return base


def deduplicate(rows: Sequence[Dict], kind: str) -> List[Dict]:
    """Remove chaves repetidas mantendo a última ocorrência (revisão da fonte)."""
    ultima: Dict[Tuple, Dict] = {}
    for row in rows:
        ultima[dedup_key(row, kind)] = row
    return list(ultima.values())


def _exemplos(rows: Iterable[Dict]) -> List[str]:
    return [
        f"{r.get('observation_date')}/{r.get('region') or '-'}/{r.get('variable_name')}: "
        + "; ".join(r.get("invalid_reasons", []))
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Validação principal
# ---------------------------------------------------------------------------

def validate_batch(
    rows: Sequence[Dict],
    kind: str,
    cutoff_date: Optional[date] = None,
    min_records: int = 1,
    table_name: Optional[str] = None,
) -> ValidationOutcome:
    """Valida um lote de observações em formato longo.

    ``cutoff_date`` é a data de referência da previsão: nada posterior a ela
    pode ser usado como feature. Em ingestão diária coincide com hoje.
    """
    if kind not in REQUIRED_FIELDS:
        raise ValueError(f"kind inválido: {kind!r}. Use {list(REQUIRED_FIELDS)}.")

    table = table_name or TABLE_BY_KIND[kind]
    cutoff = cutoff_date or today_in_tz()
    report = ValidationReport()

    problemas: Dict[str, List[Dict]] = {
        "colunas_obrigatorias": [],
        "valores_nulos": [],
        "tipos_numericos": [],
        "datas_validas": [],
        "datas_futuras": [],
        "limites_fisicos": [],
    }
    unidade_errada: List[str] = []
    datas_por_variavel: Dict[str, List[date]] = {}
    processadas: List[Dict] = []

    for row in rows:
        motivos: List[str] = []
        regras: List[str] = []

        faltantes = [f for f in REQUIRED_FIELDS[kind] if row.get(f) in (None, "")]
        if faltantes:
            regras.append("colunas_obrigatorias")
            motivos.append(f"campos ausentes {faltantes}")

        obs_date = as_date(row.get("observation_date"))
        if obs_date is None:
            regras.append("datas_validas")
            motivos.append("data inválida")
        elif obs_date > cutoff:
            regras.append("datas_futuras")
            motivos.append(f"data posterior ao corte {cutoff}")

        value = row.get("value")
        if value is None:
            regras.append("valores_nulos")
            motivos.append("valor nulo")
        elif not _is_finite_number(value):
            regras.append("tipos_numericos")
            motivos.append(f"valor não numérico: {value!r}")
        else:
            limites = PHYSICAL_LIMITS.get(row.get("variable_name"))
            if limites and not (limites[0] <= float(value) <= limites[1]):
                regras.append("limites_fisicos")
                motivos.append(f"fora do intervalo {list(limites)}: {value}")

        esperado = EXPECTED_UNITS.get(row.get("variable_name"))
        if esperado and row.get("unit") != esperado:
            unidade_errada.append(
                f"{row.get('variable_name')}: {row.get('unit')!r} != {esperado!r}"
            )

        if regras:
            marcada = dict(row)
            marcada["invalid_reasons"] = motivos
            for regra in regras:
                problemas[regra].append(marcada)
            processadas.append(marcada)
        else:
            processadas.append(row)

        if obs_date is not None:
            datas_por_variavel.setdefault(row.get("variable_name"), []).append(obs_date)

    valid_rows = [r for r in processadas if "invalid_reasons" not in r]
    invalid_rows = [r for r in processadas if "invalid_reasons" in r]

    total = len(rows)
    for nome in ("colunas_obrigatorias", "valores_nulos", "tipos_numericos",
                 "datas_validas", "datas_futuras"):
        reprovadas = problemas[nome]
        extras = {"cutoff_date": cutoff.isoformat()} if nome == "datas_futuras" else {}
        report.add(nome, table, SEVERITY_CRITICAL, not reprovadas,
                   reprovadas=len(reprovadas),
                   exemplos=_exemplos(reprovadas[:EXEMPLOS_DETALHE]), **extras)

    # Limites físicos: tolera ruído pontual, mas nunca na variável alvo.
    fora = problemas["limites_fisicos"]
    alvo_afetado = any(r.get("variable_name") == TARGET_VARIABLE for r in fora)
    share = (len(fora) / total) if total else 0.0
    report.add("limites_fisicos", table, SEVERITY_CRITICAL,
               not fora or (share <= MAX_INVALID_SHARE and not alvo_afetado),
               reprovadas=len(fora), share=round(share, 4),
               tolerancia=MAX_INVALID_SHARE, variavel_alvo_afetada=alvo_afetado,
               exemplos=_exemplos(fora[:EXEMPLOS_DETALHE]))

    report.add("unidades", table, SEVERITY_WARNING,
               not unidade_errada,
               reprovadas=len(unidade_errada),
               exemplos=sorted(set(unidade_errada))[:EXEMPLOS_DETALHE])

    # Duplicidades dentro do mesmo lote.
    chaves = Counter(dedup_key(r, kind) for r in rows)
    duplicadas = {k: v for k, v in chaves.items() if v > 1}
    report.add("duplicidades", table, SEVERITY_WARNING,
               not duplicadas,
               chaves_duplicadas=len(duplicadas),
               linhas_excedentes=sum(v - 1 for v in duplicadas.values()),
               exemplos=[str(k) for k in list(duplicadas)[:EXEMPLOS_DETALHE]])

    # Continuidade temporal da variável alvo (ou da primeira variável disponível).
    serie = datas_por_variavel.get(TARGET_VARIABLE) or next(iter(datas_por_variavel.values()), [])
    datas_unicas = sorted(set(serie))
    maior_lacuna = 0
    if len(datas_unicas) > 1:
        maior_lacuna = max(
            (b - a).days for a, b in zip(datas_unicas, datas_unicas[1:])
        )
    limite_lacuna = MAX_GAP_DAYS[kind]
    report.add("continuidade_temporal", table, SEVERITY_WARNING,
               maior_lacuna <= limite_lacuna,
               maior_lacuna_dias=maior_lacuna, limite_dias=limite_lacuna,
               inicio=datas_unicas[0].isoformat() if datas_unicas else None,
               fim=datas_unicas[-1].isoformat() if datas_unicas else None)

    report.add("registros_minimos", table, SEVERITY_CRITICAL,
               total >= min_records,
               registros=total, minimo_esperado=min_records)

    # Disponibilidade para os horizontes: a série precisa ser mais longa que o
    # maior horizonte, senão nenhum alvo y_h é calculável.
    horizontes = settings.horizons_list
    maior_horizonte = max(horizontes) if horizontes else 0
    span = (datas_unicas[-1] - datas_unicas[0]).days if len(datas_unicas) > 1 else 0
    tem_alvo = TARGET_VARIABLE in datas_por_variavel
    report.add("disponibilidade_horizontes", table, SEVERITY_WARNING,
               (not tem_alvo) or span >= maior_horizonte,
               variavel_alvo_presente=tem_alvo,
               abrangencia_dias=span, maior_horizonte_dias=maior_horizonte,
               horizontes=horizontes)

    # Ausência de informação futura: nada além do corte pode virar feature.
    datas_alvo = datas_por_variavel.get(TARGET_VARIABLE, [])
    max_data = max(datas_alvo) if datas_alvo else (max(datas_unicas) if datas_unicas else None)
    report.add("ausencia_informacao_futura", table, SEVERITY_CRITICAL,
               max_data is None or max_data <= cutoff,
               cutoff_date=cutoff.isoformat(),
               max_observation_date=max_data.isoformat() if max_data else None)

    if invalid_rows:
        logger.warning(
            "%s: %d de %d linhas reprovadas e colocadas em quarentena.",
            table, len(invalid_rows), total,
        )
    for falha in report.critical_failures:
        logger.error("Checagem CRÍTICA reprovada: %s -> %s", falha.check_name, falha.details)
    for aviso in report.warning_failures:
        logger.warning("Checagem de alerta reprovada: %s -> %s", aviso.check_name, aviso.details)

    return ValidationOutcome(report=report, valid_rows=valid_rows, invalid_rows=invalid_rows)


# ---------------------------------------------------------------------------
# Persistência da auditoria
# ---------------------------------------------------------------------------

def record_checks(conn, report: ValidationReport, run_id: Optional[str] = None) -> int:
    """Grava todas as checagens em ``audit.data_quality_checks``."""
    with conn.cursor() as cur:
        for result in report.results:
            cur.execute(
                """
                INSERT INTO audit.data_quality_checks
                    (pipeline_run_id, check_name, table_name, severity, passed, details)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    run_id,
                    result.check_name,
                    result.table_name,
                    result.severity,
                    result.passed,
                    _json_dumps(result.details),
                ),
            )
    conn.commit()
    return len(report.results)


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)
