"""Reprocessamento manual de um período.

Coleta de novo a janela pedida, ignorando a checagem de "origem inalterada", e
reconstrói o dataset. ``raw`` ganha as linhas da nova coleta (é imutável e
acumula por carga); ``core`` é atualizado por upsert, sem duplicar datas; o
dataset só ganha versão nova se o conteúdo mudar.

    python -m jobs.reprocess_period --start 2025-01-01 --end 2025-12-31

A janela só é suportada com AGROBR_MODE=simulated.
"""

import argparse
import json
import sys

from jobs.runner import EXIT_CODES, parse_date, run_job

JOB_NAME = "reprocess_period"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobs.reprocess_period", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=parse_date, required=True, help="início da janela (AAAA-MM-DD)")
    parser.add_argument("--end", type=parse_date, required=True, help="fim da janela (AAAA-MM-DD)")
    args = parser.parse_args(argv)
    if args.start > args.end:
        parser.error(f"--start {args.start} é posterior a --end {args.end}")

    resultado = run_job(JOB_NAME, start_date=args.start, end_date=args.end, force=True)
    print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
    return EXIT_CODES.get(resultado["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
