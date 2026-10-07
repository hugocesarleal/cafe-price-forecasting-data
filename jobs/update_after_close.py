"""Job de pós-fechamento: fecha o próprio dia, depois que o mercado encerrou.

Roda às ``AFTER_CLOSE_TIME`` no fuso ``TIMEZONE``, horário em que o indicador
CEPEA e os ajustes do dia já devem estar publicados. O corte é hoje.

    python -m jobs.update_after_close
    python -m jobs.update_after_close --schedule
"""

import sys

from jobs.runner import scheduled_job_main, today_local
from src.config import settings

JOB_NAME = "update_after_close"


def main(argv=None) -> int:
    return scheduled_job_main(
        JOB_NAME, "Atualização após o fechamento do mercado: fecha o dia corrente.",
        cutoff=today_local, horario=settings.AFTER_CLOSE_TIME, argv=argv)


if __name__ == "__main__":
    sys.exit(main())
