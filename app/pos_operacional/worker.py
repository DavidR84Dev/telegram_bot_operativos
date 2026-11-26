"""Cola de envío del Pos-Operacional a Odoo (asyncio, dentro del proceso FastAPI).

Mismo ciclo que ``PruebaRutaSender``: reclama los registros pendientes, los crea en ``pos.operacional`` con backoff y
avisa al conductor del resultado final. No hay ubicación que esperar: sale apenas se recibe.

CLI de operación::

    python -m app.pos_operacional.worker --once
    python -m app.pos_operacional.worker --requeue 3 4
    python -m app.pos_operacional.worker --requeue-all
    python -m app.pos_operacional.worker --listar
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Awaitable, Callable

from sqlalchemy import select

from ..preop.db import Database
from ..preop.models import PosOperacional, SyncStatus
from ..preop.odoo_writer import OdooWriter
from ..preop.worker import _PeriodicTask, build_writer
from .service import PosOperacionalOutcome, claim_batch, requeue_failed, send_one

logger = logging.getLogger(__name__)

PosOperacionalNotifier = Callable[[PosOperacionalOutcome], Awaitable[None]]


class PosOperacionalSender(_PeriodicTask):
    def __init__(self, db: Database, writer: OdooWriter, interval_seconds: float, max_attempts: int,
                 lease_seconds: int, notify: PosOperacionalNotifier | None = None, batch_size: int = 10) -> None:
        super().__init__("pos-operacional-sender", interval_seconds)
        self._db = db
        self._writer = writer
        self._max_attempts = max_attempts
        self._lease = lease_seconds
        self._notify = notify
        self._batch = batch_size

    async def run_once(self) -> list[PosOperacionalOutcome]:
        outcomes: list[PosOperacionalOutcome] = []
        while True:
            ids = await asyncio.to_thread(claim_batch, self._db, self._batch, self._lease)
            if not ids:
                return outcomes
            for pos_id in ids:
                outcome = await asyncio.to_thread(send_one, self._db, self._writer, pos_id, self._max_attempts)
                outcomes.append(outcome)
                if self._notify and outcome.status in (SyncStatus.ENVIADO, SyncStatus.FALLIDO):
                    try:
                        await self._notify(outcome)
                    except Exception:
                        logger.exception("No se pudo notificar al usuario %s", outcome.telegram_user_id)
            if len(ids) < self._batch:
                return outcomes


def listar(db: Database, limite: int = 30) -> list[str]:
    """Últimos registros con su estado (para la operación)."""
    with db.session() as session:
        filas = session.scalars(select(PosOperacional).order_by(PosOperacional.id.desc()).limit(limite)).all()
        return [f"#{p.id} {p.sync_status.value:9} intentos={p.attempts} odoo_id={p.odoo_id} "
                f"{p.empleado.nombre} entrada={p.check_in:%Y-%m-%d %H:%M} salida={p.check_out:%Y-%m-%d %H:%M} UTC"
                + (f" error={p.error_log}" if p.error_log else "") for p in filas]


def main() -> None:  # pragma: no cover - CLI de operación
    from ..config import get_settings

    parser = argparse.ArgumentParser(description="Cola de envío del Pos-Operacional a Odoo")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="procesa la cola una vez y sale")
    group.add_argument("--listar", action="store_true", help="muestra los últimos registros y su estado")
    group.add_argument("--requeue", type=int, nargs="+", metavar="ID", help="reencola registros fallidos")
    group.add_argument("--requeue-all", action="store_true", help="reencola todos los fallidos")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    settings = get_settings()
    db = Database(settings.database_url)
    if args.listar:
        print("\n".join(listar(db)) or "Sin registros")
        return
    if args.requeue or args.requeue_all:
        print(f"Reencolados: {requeue_failed(db, args.requeue)}")
        return
    sender = PosOperacionalSender(db, build_writer(settings), settings.preop_worker_interval_seconds,
                                  settings.preop_max_attempts, settings.preop_sending_lease_seconds)
    for outcome in asyncio.run(sender.run_once()):
        print(outcome)


if __name__ == "__main__":  # pragma: no cover
    main()
