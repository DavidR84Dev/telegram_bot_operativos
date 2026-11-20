"""Tareas en segundo plano del preoperacional (asyncio, dentro del proceso FastAPI).

* ``PreopSender``: toma registros ``pendiente``/``error`` y los envía a Odoo por
  XML-RPC. Corre cada ``PREOP_WORKER_INTERVAL_SECONDS`` y se despierta al
  instante cuando llega un formulario nuevo (``wake``). La respuesta al usuario
  en Telegram nunca espera a Odoo.
* ``MasterSync``: sincroniza empleados/flota/extractos cada
  ``MASTER_SYNC_INTERVAL_SECONDS`` (primera pasada al arrancar).

El trabajo bloqueante (SQLAlchemy, XML-RPC, httpx) corre en hilos
(``asyncio.to_thread``). Con un solo proceso basta; si se escala a varios
réplicas, ``claim_batch`` usa ``FOR UPDATE SKIP LOCKED`` (PostgreSQL) para que
no se envíe dos veces el mismo registro.

CLI de operación::

    python -m app.preop.worker --once           # un ciclo de envío y salir
    python -m app.preop.worker --requeue 12 15  # reencolar fallidos concretos
    python -m app.preop.worker --requeue-all    # reencolar todos los fallidos
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from typing import Awaitable, Callable

from .db import Database
from .models import SyncStatus
from .odoo_writer import OdooWriter
from .service import SendOutcome, claim_batch, requeue_failed, send_one

logger = logging.getLogger(__name__)

Notifier = Callable[[SendOutcome], Awaitable[None]]


class _PeriodicTask:
    def __init__(self, name: str, interval_seconds: float) -> None:
        self._name = name
        self._interval = interval_seconds
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> object:  # pragma: no cover - lo implementan las subclases
        raise NotImplementedError

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Un ciclo fallido nunca mata el loop; se reintenta en el siguiente.
                logger.exception("%s: ciclo fallido", self._name)
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name=self._name)

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


class PreopSender(_PeriodicTask):
    def __init__(self, db: Database, writer: OdooWriter, interval_seconds: float, max_attempts: int,
                 lease_seconds: int, notify: Notifier | None = None, batch_size: int = 20) -> None:
        super().__init__("preop-sender", interval_seconds)
        self._db = db
        self._writer = writer
        self._max_attempts = max_attempts
        self._lease = lease_seconds
        self._notify = notify
        self._batch = batch_size

    async def run_once(self) -> list[SendOutcome]:
        outcomes: list[SendOutcome] = []
        while True:
            ids = await asyncio.to_thread(claim_batch, self._db, self._batch, self._lease)
            if not ids:
                return outcomes
            for preop_id in ids:
                outcome = await asyncio.to_thread(send_one, self._db, self._writer, preop_id, self._max_attempts)
                outcomes.append(outcome)
                if self._notify and outcome.status in (SyncStatus.ENVIADO, SyncStatus.FALLIDO):
                    try:
                        await self._notify(outcome)
                    except Exception:
                        logger.exception("No se pudo notificar al usuario %s", outcome.telegram_user_id)
            if len(ids) < self._batch:
                return outcomes


class MasterSync(_PeriodicTask):
    def __init__(self, interval_seconds: float, run: Callable[[], object]) -> None:
        super().__init__("master-sync", interval_seconds)
        self._run = run

    async def run_once(self) -> None:
        await asyncio.to_thread(self._run)


def main() -> None:  # pragma: no cover - CLI de operación
    from ..config import get_settings

    parser = argparse.ArgumentParser(description="Cola de envío del preoperacional a Odoo")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="procesa la cola una vez y sale")
    group.add_argument("--requeue", type=int, nargs="+", metavar="ID", help="reencola registros fallidos")
    group.add_argument("--requeue-all", action="store_true", help="reencola todos los fallidos")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    settings = get_settings()
    db = Database(settings.database_url)
    if args.requeue or args.requeue_all:
        print(f"Reencolados: {requeue_failed(db, args.requeue)}")
        return
    sender = PreopSender(db, build_writer(settings), settings.preop_worker_interval_seconds,
                         settings.preop_max_attempts, settings.preop_sending_lease_seconds)
    for outcome in asyncio.run(sender.run_once()):
        print(outcome)


def build_writer(settings) -> OdooWriter:
    password = settings.odoo_write_password or settings.odoo_password
    return OdooWriter(
        settings.odoo_url, settings.odoo_db,
        settings.odoo_write_username or settings.odoo_username,
        password.get_secret_value(), settings.rpc_timeout_seconds,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
