"""Execução sob demanda do ciclo completo, fora do agendamento.

    python -m jobs.run_on_demand                       # corte = último dia com preço
    python -m jobs.run_on_demand --cutoff 2026-09-30   # fecha um dia específico
    python -m jobs.run_on_demand --force               # reprocessa origens inalteradas
"""

import argparse
import json
import sys

from jobs.runner import EXIT_CODES, parse_date, run_job

JOB_NAME = "run_on_demand"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobs.run_on_demand", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cutoff", type=parse_date, default=None,
                        help="dia a fechar (AAAA-MM-DD); padrão: último dia com preço")
    parser.add_argument("--force", action="store_true",
                        help="reprocessa as origens mesmo sem mudança de conteúdo")
    args = parser.parse_args(argv)

    resultado = run_job(JOB_NAME, cutoff_date=args.cutoff, force=args.force)
    print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
    return EXIT_CODES.get(resultado["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
