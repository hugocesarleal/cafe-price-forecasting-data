"""Job de pré-abertura: revisa o dia anterior antes de o mercado abrir.

Roda às ``BEFORE_OPEN_TIME`` no fuso ``TIMEZONE``. O corte ainda é ontem; a
execução serve para capturar o que foi publicado ou revisado durante a
madrugada. Se nada mudou desde o job diário, nada é regravado.

    python -m jobs.update_before_open
    python -m jobs.update_before_open --schedule
"""

import sys

from jobs.runner import scheduled_job_main, yesterday_local
from src.config import settings

JOB_NAME = "update_before_open"


def main(argv=None) -> int:
    return scheduled_job_main(
        JOB_NAME, "Atualização antes da abertura do mercado: revisa o dia anterior.",
        cutoff=yesterday_local, horario=settings.BEFORE_OPEN_TIME, argv=argv)


if __name__ == "__main__":
    sys.exit(main())
