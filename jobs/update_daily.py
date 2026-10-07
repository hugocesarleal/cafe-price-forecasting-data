"""Job diário: fecha o dia anterior logo após a virada.

Roda às ``UPDATE_TIME`` (00:00) no fuso ``TIMEZONE``. O dia que acabou de
terminar é o corte: tudo o que foi publicado nele já está disponível.

    python -m jobs.update_daily              # uma vez (cron / Agendador de Tarefas)
    python -m jobs.update_daily --schedule   # fica rodando e dispara todo dia
"""

import sys

from jobs.runner import scheduled_job_main, yesterday_local
from src.config import settings

JOB_NAME = "update_daily"


def main(argv=None) -> int:
    return scheduled_job_main(
        JOB_NAME, "Atualização diária: fecha o dia anterior.",
        cutoff=yesterday_local, horario=settings.UPDATE_TIME, argv=argv)


if __name__ == "__main__":
    sys.exit(main())
