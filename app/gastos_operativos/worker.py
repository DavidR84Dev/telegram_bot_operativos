"""Cola de envío de Gastos Operativos a Odoo (asyncio, dentro del proceso FastAPI).

Mismo ciclo que ``CombustibleSender``: recordatorios de ubicación (10 y 30 min), reclamo de los envíos que ya
tienen ubicación y envío de un ``hr.expense`` por cada gasto.

CLI de operación::

    python -m app.gastos_operativos.worker --once
    python -m app.gastos_operativos.worker --requeue 3 4
    python -m app.gastos_operativos.worker --requeue-all
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Awaitable, Callable

from ..combustible.gps import FlotaGpsClient, build_gps_client
from ..combustible.service import RecordatorioUbicacion
from ..combustible.worker import build_rest_client
from ..preop.db import Database
from ..preop.models import SyncStatus
from ..preop.odoo_writer import OdooWriter
from ..preop.rest_client import OdooRestClient
from ..preop.worker import _PeriodicTask, build_writer
from .service import GastosOutcome, claim_batch, recordatorios_pendientes, requeue_failed, send_one

logger = logging.getLogger(__name__)

GastosNotifier = Callable[[GastosOutcome], Awaitable[None]]
UbicacionReminder = Callable[[RecordatorioUbicacion], Awaitable[None]]


class GastosOperativosSender(_PeriodicTask):
    def __init__(self, db: Database, writer: OdooWriter, interval_seconds: float, max_attempts: int,
                 lease_seconds: int, notify: GastosNotifier | None = None, batch_size: int = 10,
                 rest_client: OdooRestClient | None = None, remind: UbicacionReminder | None = None,
                 gps: FlotaGpsClient | None = None) -> None:
        super().__init__("gastos-operativos-sender", interval_seconds)
        self._db = db
        self._writer = writer
        self._rest = rest_client  # /api/gastos/register
        self._max_attempts = max_attempts
        self._lease = lease_seconds
        self._notify = notify
        self._remind = remind
        self._gps = gps  # cotejo de la ubicación
        self._batch = batch_size

    async def _recordar(self) -> None:
        """Recordatorios de ubicación (10 y 30 min); un fallo de Telegram no frena la cola."""
        if self._remind is None:
            return
        try:
            pendientes = await asyncio.to_thread(recordatorios_pendientes, self._db)
        except Exception:
            logger.exception("No se pudieron calcular los recordatorios de ubicación de gastos operativos")
            return
        for recordatorio in pendientes:
            try:
                await self._remind(recordatorio)
            except Exception:
                logger.exception("No se pudo recordar la ubicación al usuario %s", recordatorio.telegram_user_id)

    async def run_once(self) -> list[GastosOutcome]:
        outcomes: list[GastosOutcome] = []
        await self._recordar()
        while True:
            ids = await asyncio.to_thread(claim_batch, self._db, self._batch, self._lease)
            if not ids:
                return outcomes
            for gasto_id in ids:
                outcome = await asyncio.to_thread(send_one, self._db, self._writer, gasto_id, self._max_attempts,
                                                  self._rest, self._gps)
                outcomes.append(outcome)
                if self._notify and outcome.status in (SyncStatus.ENVIADO, SyncStatus.FALLIDO):
                    try:
                        await self._notify(outcome)
                    except Exception:
                        logger.exception("No se pudo notificar al usuario %s", outcome.telegram_user_id)
            if len(ids) < self._batch:
                return outcomes


def main() -> None:  # pragma: no cover - CLI de operación
    from ..config import get_settings

    parser = argparse.ArgumentParser(description="Cola de envío de Gastos Operativos a Odoo")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="procesa la cola una vez y sale")
    group.add_argument("--requeue", type=int, nargs="+", metavar="ID", help="reencola envíos fallidos")
    group.add_argument("--requeue-all", action="store_true", help="reencola todos los fallidos")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    settings = get_settings()
    db = Database(settings.database_url)
    if args.requeue or args.requeue_all:
        print(f"Reencolados: {requeue_failed(db, args.requeue)}")
        return
    rest = build_rest_client(settings)
    try:
        sender = GastosOperativosSender(db, build_writer(settings), settings.preop_worker_interval_seconds,
                                        settings.preop_max_attempts, settings.preop_sending_lease_seconds,
                                        rest_client=rest, gps=build_gps_client(settings))
        for outcome in asyncio.run(sender.run_once()):
            print(outcome)
    finally:
        if rest is not None:
            rest.close()


if __name__ == "__main__":  # pragma: no cover
    main()
