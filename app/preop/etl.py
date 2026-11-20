"""ETL de tablas maestras: API REST de Odoo -> empleados / flota / extractos (UPSERT).

Por cada entidad y compañía:

1. Descarga todas las páginas (``OdooRestClient``, con reintentos).
2. Valida y normaliza cada objeto con Pydantic (``*Api.to_row``): aplana los
   anidados (``responsable.mobile``, ``vehiculo.id``...). Un objeto inválido se
   registra y se omite; no detiene la carga.
3. UPSERT por ``odoo_id`` (``INSERT ... ON CONFLICT (odoo_id) DO UPDATE``) en
   lotes, dentro de una sola transacción por entidad/compañía.
4. Desactiva (``activo = False``) los registros de esa compañía que ya no
   llegaron. Si la descarga falla, la transacción se revierte y no se desactiva
   nada. Si la API devuelve 0 registros y la tabla tiene activos, no se
   desactiva (protege contra una API vacía por error).
5. Extractos: si la API no trae ``vehiculo.id``, se resuelve contra ``flota``
   por nombre exacto (solo si el nombre es único).

Uso manual::

    python -m app.preop.etl            # una pasada completa
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from pydantic import ValidationError
from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .db import Database
from .models import Empleado, Extracto, Flota, MasterMixin, SyncRun, utc_now
from .rest_client import OdooApiError, OdooRestClient
from .schemas import EmpleadoApi, ExtractoApi, FlotaApi, _ApiRecord

logger = logging.getLogger(__name__)

_BATCH = 500


@dataclass(frozen=True)
class Entity:
    name: str
    path: str
    model: type[MasterMixin]
    schema: type[_ApiRecord]


# El orden importa: extractos resuelve vehiculo_id contra flota.
ENTITIES: tuple[Entity, ...] = (
    Entity("flota", "/api/flota", Flota, FlotaApi),
    Entity("empleados", "/api/empleados", Empleado, EmpleadoApi),
    Entity("extractos", "/api/extractos", Extracto, ExtractoApi),
)


@dataclass
class SyncResult:
    entidad: str
    company_odoo_id: int
    recibidos: int = 0
    upserts: int = 0
    invalidos: int = 0
    desactivados: int = 0
    error: str | None = None


def transform(entity: Entity, items: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[int]]:
    """Valida y aplana. Devuelve (filas válidas sin duplicados, ids inválidos)."""
    rows: dict[int, dict[str, Any]] = {}
    invalid_ids: list[int] = []
    for raw in items:
        try:
            record = entity.schema.model_validate(raw)
        except ValidationError as exc:
            raw_id = raw.get("id") if isinstance(raw, dict) else None
            logger.warning("%s: registro inválido id=%s omitido (%d errores)", entity.name, raw_id, exc.error_count())
            if isinstance(raw_id, int) and raw_id > 0:
                invalid_ids.append(raw_id)
            continue
        row = record.to_row()  # type: ignore[attr-defined]  # cada *Api define to_row
        row["odoo_id"] = record.id
        row["raw"] = raw
        # Último gana: un mismo id dos veces en un INSERT ... ON CONFLICT falla en PostgreSQL.
        rows[record.id] = row
    return list(rows.values()), invalid_ids


def _insert_for(session: Session):
    return pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert


def upsert(session: Session, model: type[MasterMixin], rows: Sequence[dict[str, Any]]) -> int:
    """INSERT ... ON CONFLICT (odoo_id) DO UPDATE, en lotes. Devuelve filas procesadas."""
    if not rows:
        return 0
    insert = _insert_for(session)
    table = model.__table__  # type: ignore[attr-defined]
    protected = {"id", "odoo_id", "created_at"}
    for start in range(0, len(rows), _BATCH):
        chunk = rows[start:start + _BATCH]
        stmt = insert(table).values(list(chunk))
        update_cols = {
            col.name: stmt.excluded[col.name]
            for col in table.columns
            if col.name not in protected and col.name in chunk[0]
        }
        update_cols["updated_at"] = func.now()
        session.execute(stmt.on_conflict_do_update(index_elements=["odoo_id"], set_=update_cols))
    return len(rows)


def _resolve_extracto_vehicles(session: Session, company_id: int) -> None:
    """vehiculo_id faltante <- flota.odoo_id con el mismo nombre (si es único)."""
    same_name = Flota.nombre == Extracto.vehiculo
    match = select(func.min(Flota.odoo_id)).where(same_name).scalar_subquery()
    unique = select(func.count(Flota.id)).where(same_name).scalar_subquery()
    session.execute(
        update(Extracto)
        .where(and_(Extracto.company_odoo_id == company_id, Extracto.vehiculo_id.is_(None),
                    Extracto.vehiculo.is_not(None), unique == 1))
        .values(vehiculo_id=match)
        .execution_options(synchronize_session=False)
    )


def sync_entity(db: Database, client: OdooRestClient, entity: Entity, company_id: int,
                page_size: int = 500) -> SyncResult:
    result = SyncResult(entity.name, company_id)
    started = utc_now()
    with db.session() as session:
        run = SyncRun(entidad=entity.name, company_odoo_id=company_id, started_at=started)
        session.add(run)
    try:
        items = client.fetch_all(entity.path, company_id, page_size)
        result.recibidos = len(items)
        rows, invalid_ids = transform(entity, items)
        result.invalidos = len(invalid_ids)
        for row in rows:
            row.update(company_odoo_id=company_id, activo=True, synced_at=started)

        with db.session() as session:
            result.upserts = upsert(session, entity.model, rows)
            model: Any = entity.model
            if invalid_ids:
                # Inválidos en esta pasada: se conservan como estaban, sin desactivarlos.
                session.execute(update(model).where(model.odoo_id.in_(invalid_ids)).values(synced_at=started))
            if entity.model is Extracto:
                _resolve_extracto_vehicles(session, company_id)
            result.desactivados = _deactivate_missing(session, model, company_id, started, len(items))
    except Exception as exc:  # una entidad que falla no detiene las demás; queda en sync_runs
        result.error = str(exc) if isinstance(exc, OdooApiError) else f"{type(exc).__name__}: {exc}"
        logger.exception("Sincronización de %s (compañía %s) falló", entity.name, company_id)

    with db.session() as session:
        finished = session.get(SyncRun, run.id)
        assert finished is not None
        finished.finished_at = utc_now()
        finished.status = "error" if result.error else "ok"
        finished.recibidos, finished.upserts = result.recibidos, result.upserts
        finished.invalidos, finished.desactivados = result.invalidos, result.desactivados
        finished.error_log = result.error
    logger.info("Sync %s compañía=%s: recibidos=%d upserts=%d inválidos=%d desactivados=%d%s",
                entity.name, company_id, result.recibidos, result.upserts, result.invalidos,
                result.desactivados, f" ERROR={result.error}" if result.error else "")
    return result


def _deactivate_missing(session: Session, model: Any, company_id: int, started: datetime, received: int) -> int:
    active = model.activo.is_(True) & (model.company_odoo_id == company_id)
    if received == 0:
        still_active = session.scalar(select(func.count()).select_from(model).where(active)) or 0
        if still_active:
            logger.warning("%s: la API devolvió 0 registros para la compañía %s; no se desactivan %d filas",
                           model.__tablename__, company_id, still_active)
            return 0
    result = session.execute(
        update(model).where(active, model.synced_at < started).values(activo=False, updated_at=func.now())
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]  # CursorResult


def sync_all(db: Database, client: OdooRestClient, company_ids: Sequence[int], page_size: int = 500) -> list[SyncResult]:
    return [sync_entity(db, client, entity, company, page_size) for company in company_ids for entity in ENTITIES]


def run_from_settings(db: Database | None = None) -> list[SyncResult]:
    """Pasada completa con la configuración de .env (la usan el scheduler y la CLI)."""
    from ..config import get_settings

    settings = get_settings()
    if not settings.master_sync_enabled:
        raise RuntimeError("ODOO_API_LOGIN / ODOO_API_PASSWORD no configurados: sincronización desactivada")
    assert settings.odoo_api_password is not None
    db = db or Database(settings.database_url)
    with OdooRestClient(settings.odoo_api_url or settings.odoo_url, settings.odoo_api_login,
                        settings.odoo_api_password.get_secret_value(),
                        settings.odoo_api_timeout_seconds) as client:
        return sync_all(db, client, settings.api_company_ids, settings.odoo_api_page_size)


def main() -> None:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description="Sincroniza empleados, flota y extractos desde la API de Odoo")
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    results = run_from_settings()
    raise SystemExit(1 if any(r.error for r in results) else 0)


if __name__ == "__main__":  # pragma: no cover
    main()
