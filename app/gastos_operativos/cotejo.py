"""Cotejo de la ubicación del gasto con los locales autorizados: SOLO SE ALMACENA.

Cuando el envío recibe su ubicación por el chat se calcula, para cada línea, el local autorizado activo más cercano
**de su categoría** (o de categoría vacía, que vale para todos los tipos) y se guarda en la línea: id y nombre del
local, distancia en metros y ``local_cotejado_at``. Sin locales cargados los campos quedan en ``None`` y la línea
se marca como cotejada. No va a Odoo, no se muestra en el formulario y no bloquea nada: ``cotejar_seguro`` nunca
lanza (un fallo se registra en el log y el envío sigue).
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from ..combustible.gps import haversine_m
from ..preop.db import Database
from ..preop.models import GastoOperativo, GastoOperativoLinea, LocalAutorizado, utc_now
from .catalogo import clave_tipo

logger = logging.getLogger(__name__)


def cotejar_envio(db: Database, gasto_id: int, now: datetime | None = None) -> int:
    """Cotejo de las líneas del envío que aún no lo tienen. Devuelve cuántas líneas se cotejaron."""
    now = now or utc_now()
    with db.session() as session:
        cab = session.get(GastoOperativo, gasto_id)
        if cab is None or cab.latitud is None or cab.longitud is None:
            return 0
        lat, lon = cab.latitud, cab.longitud
        locales = [(loc.id, loc.nombre, loc.categoria and clave_tipo(loc.categoria), loc.latitud, loc.longitud)
                   for loc in session.scalars(select(LocalAutorizado).where(LocalAutorizado.activo.is_(True)))]
        lineas = list(session.scalars(select(GastoOperativoLinea).where(
            GastoOperativoLinea.gasto_id == gasto_id, GastoOperativoLinea.local_cotejado_at.is_(None))))
        for linea in lineas:
            clave = clave_tipo(linea.tipo_nombre)
            mejor: tuple[float, int, str] | None = None
            for local_id, nombre, categoria, llat, llon in locales:
                if categoria is not None and categoria != clave:
                    continue
                distancia = haversine_m(lat, lon, llat, llon)
                if mejor is None or distancia < mejor[0]:
                    mejor = (distancia, local_id, nombre)
            if mejor is None:
                linea.local_autorizado_id = linea.local_nombre = linea.distancia_local_m = None
            else:
                linea.local_autorizado_id, linea.local_nombre = mejor[1], mejor[2]
                linea.distancia_local_m = round(mejor[0])
            linea.local_cotejado_at = now
        return len(lineas)


def cotejar_seguro(db: Database, gasto_id: int) -> None:
    """``cotejar_envio`` sin propagar errores: el cotejo nunca frena ni retrasa el envío a Odoo."""
    try:
        cotejar_envio(db, gasto_id)
    except Exception:
        logger.exception("Gasto operativo %s: no se pudo cotejar con los locales autorizados (el envío sigue)",
                         gasto_id)
