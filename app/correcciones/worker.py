"""Cola de envío de Correcciones Post y Gastos a Odoo (asyncio, dentro del proceso FastAPI).

Mismo ciclo que ``GastosOperativosSender`` sin la espera de ubicación: reclama las correcciones pendientes, envía el
gasto de corrección por REST o escribe el Post-operacional por XML-RPC, con backoff, y avisa al conductor del
resultado final (enviada, fallida o descartada).

CLI de operación::

    python -m app.correcciones.worker --once
    python -m app.correcciones.worker --requeue 3 4
    python -m app.correcciones.worker --requeue-all
    python -m app.correcciones.worker --listar
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Awaitable, Callable

from sqlalchemy import select

from ..combustible.worker import build_rest_client
from ..preop.db import Database
from ..preop.models import Correccion, SyncStatus
from ..preop.odoo_writer import OdooWriter
from ..preop.rest_client import OdooRestClient
from ..preop.worker import _PeriodicTask, build_writer
from .service import CorreccionOutcome, claim_batch, requeue_failed, send_one

logger = logging.getLogger(__name__)

CorreccionNotifier = Callable[[CorreccionOutcome], Awaitable[None]]


class CorreccionesSender(_PeriodicTask):
    def __init__(self, db: Database, writer: OdooWriter, interval_seconds: float, max_attempts: int,
                 lease_seconds: int, notify: CorreccionNotifier | None = None, batch_size: int = 10,
                 rest_client: OdooRestClient | None = None) -> None:
        super().__init__("correcciones-sender", interval_seconds)
        self._db = db
        self._writer = writer
        self._rest = rest_client  # /api/gastos/register (correcciones de gastos)
        self._max_attempts = max_attempts
        self._lease = lease_seconds
        self._notify = notify
        self._batch = batch_size

    async def run_once(self) -> list[CorreccionOutcome]:
        outcomes: list[CorreccionOutcome] = []
        while True:
            ids = await asyncio.to_thread(claim_batch, self._db, self._batch, self._lease)
            if not ids:
                return outcomes
            for correccion_id in ids:
                outcome = await asyncio.to_thread(send_one, self._db, self._writer, correccion_id,
                                                  self._max_attempts, self._rest)
                outcomes.append(outcome)
                if self._notify and outcome.status in (SyncStatus.ENVIADO, SyncStatus.FALLIDO):
                    try:
                        await self._notify(outcome)
                    except Exception:
                        logger.exception("No se pudo notificar al usuario %s", outcome.telegram_user_id)
            if len(ids) < self._batch:
                return outcomes


def listar(db: Database, limite: int = 30) -> list[str]:
    """Últimas correcciones con su estado (para la operación)."""
    with db.session() as session:
        filas = session.scalars(select(Correccion).order_by(Correccion.id.desc()).limit(limite)).all()
        return [f"#{c.id} {c.tipo:5} {c.sync_status.value:9} intentos={c.attempts} original={c.odoo_id_original} "
                f"odoo_id={c.odoo_id} {c.empleado.nombre}"
                + (" DESCARTADA" if c.descartado else "") + (f" error={c.error_log}" if c.error_log else "")
                for c in filas]


def main() -> None:  # pragma: no cover - CLI de operación
    from ..config import get_settings

    parser = argparse.ArgumentParser(description="Cola de envío de Correcciones Post y Gastos a Odoo")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="procesa la cola una vez y sale")
    group.add_argument("--listar", action="store_true", help="muestra las últimas correcciones y su estado")
    group.add_argument("--requeue", type=int, nargs="+", metavar="ID", help="reencola correcciones fallidas")
    group.add_argument("--requeue-all", action="store_true", help="reencola todas las fallidas (no las descartadas)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    settings = get_settings()
    db = Database(settings.database_url)
    if args.listar:
        print("\n".join(listar(db)) or "Sin correcciones")
        return
    if args.requeue or args.requeue_all:
        print(f"Reencoladas: {requeue_failed(db, args.requeue)}")
        return
    rest = build_rest_client(settings)
    try:
        sender = CorreccionesSender(db, build_writer(settings), settings.preop_worker_interval_seconds,
                                    settings.preop_max_attempts, settings.preop_sending_lease_seconds,
                                    rest_client=rest)
        for outcome in asyncio.run(sender.run_once()):
            print(outcome)
    finally:
        if rest is not None:
            rest.close()


if __name__ == "__main__":  # pragma: no cover
    main()
