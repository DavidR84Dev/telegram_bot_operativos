"""Cotejo de la ubicación del tanqueo con el GPS de la flota (solo lectura).

``GET <GPS_FLOTA_URL>`` con la cabecera ``x-api-key``. La envoltura de la respuesta no está confirmada:
se acepta una lista en la raíz o dentro de un dict (``data``/``items``/``vehiculos``/``flota``...). Cada
elemento::

    {"vehiculo_id": "27407752", "timestamp": "2026-11-26T09:26:44-05:00",
     "coordenadas": {"lat": 6.68136055555, "long": -75.9113777778},
     "matricula": "Agrale/Ma 8.7 Euro V/EQX557", "velocidad": 29.0}

El vehículo se identifica por la placa: el **último segmento** de ``matricula`` tras ``/``.

El GPS nunca retiene ni rechaza un gasto: cualquier fallo (sin configurar, red, HTTP, respuesta rara,
vehículo ausente) devuelve ``None`` y el texto enviado a Odoo es ``GPS no disponible``. La clave nunca se escribe en logs.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

NO_DISPONIBLE = "GPS no disponible"
COLOMBIA = timezone(timedelta(hours=-5))  # sin DST; evita depender de tzdata en Windows
_CLAVES_ENVOLTURA = ("data", "items", "vehiculos", "flota", "result", "results")
_RADIO_TIERRA_M = 6_371_000.0


def normalizar_placa(valor: str | None) -> str:
    """Mayúsculas y solo letras/dígitos (sin espacios ni guiones)."""
    return re.sub(r"[^A-Z0-9]", "", (valor or "").upper())


def placa_desde_matricula(matricula: str | None) -> str:
    """Placa normalizada: último segmento de ``Agrale/Ma 8.7 Euro V/EQX557`` -> ``EQX557``."""
    return normalizar_placa((matricula or "").rsplit("/", 1)[-1])


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * _RADIO_TIERRA_M * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class PosicionGps:
    latitud: float
    longitud: float
    momento: datetime  # con zona horaria


def texto_telegram(latitud: float, longitud: float) -> str:
    """``"6.167371,-75.641203"``."""
    return f"{latitud:.6f},{longitud:.6f}"


def distancia_m(pos: PosicionGps, latitud: float, longitud: float) -> int:
    return round(haversine_m(pos.latitud, pos.longitud, latitud, longitud))


def texto_vehiculo(pos: PosicionGps | None, latitud: float, longitud: float) -> str:
    """``"6.167500,-75.641000 · 2026-11-26 09:04 · 18 m"`` (hora de Colombia) o ``GPS no disponible``."""
    if pos is None:
        return NO_DISPONIBLE
    cuando = pos.momento.astimezone(COLOMBIA).strftime("%Y-%m-%d %H:%M")
    return f"{pos.latitud:.6f},{pos.longitud:.6f} · {cuando} · {distancia_m(pos, latitud, longitud)} m"


def _lista(cuerpo: Any, nivel: int = 0) -> list[Any]:
    if isinstance(cuerpo, list):
        return cuerpo
    if isinstance(cuerpo, dict) and nivel < 2:
        for clave in _CLAVES_ENVOLTURA:
            if clave in cuerpo:
                encontrada = _lista(cuerpo[clave], nivel + 1)
                if encontrada:
                    return encontrada
    return []


def _numero(valor: Any) -> float | None:
    if isinstance(valor, bool) or not isinstance(valor, (int, float, str)):
        return None
    try:
        n = float(valor)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _posicion(item: Any) -> tuple[str, PosicionGps] | None:
    if not isinstance(item, dict) or not isinstance(item.get("matricula"), str):
        return None
    coords = item.get("coordenadas")
    if not isinstance(coords, dict):
        return None
    lat = _numero(coords.get("lat"))
    lon = _numero(coords.get("long", coords.get("lng", coords.get("lon"))))
    stamp = item.get("timestamp")
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or not isinstance(stamp, str):
        return None
    try:
        momento = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=COLOMBIA)
    return placa_desde_matricula(item["matricula"]), PosicionGps(lat, lon, momento.astimezone(UTC))


def buscar_posicion(cuerpo: Any, placa: str) -> PosicionGps | None:
    """Posición más reciente del vehículo con esa placa en la respuesta, o None."""
    buscada = normalizar_placa(placa)
    if not buscada:
        return None
    candidatas = [p for p in map(_posicion, _lista(cuerpo)) if p is not None and p[0] == buscada]
    return max((p for _, p in candidatas), key=lambda p: p.momento, default=None)


class FlotaGpsClient:
    """Cliente mínimo de la API GPS de flota. ``consultar`` nunca lanza: devuelve None si no hay dato."""

    def __init__(self, url: str, api_key: str, timeout_seconds: float = 10,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._url = url
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._transport = transport

    def consultar(self, placa: str) -> PosicionGps | None:
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as http:
                response = http.get(self._url, headers={"x-api-key": self._api_key, "Accept": "application/json"})
            if response.status_code != 200:
                logger.warning("GPS flota respondió HTTP %s", response.status_code)
                return None
            posicion = buscar_posicion(response.json(), placa)
        except (httpx.HTTPError, ValueError) as exc:
            # Solo el tipo de error: el mensaje de httpx puede incluir la URL.
            logger.warning("GPS flota no disponible: %s", type(exc).__name__)
            return None
        except Exception:  # el GPS jamás debe detener un gasto
            logger.exception("GPS flota: error inesperado")
            return None
        if posicion is None:
            logger.warning("GPS flota: sin posición válida para la placa %s", normalizar_placa(placa))
        return posicion


def build_gps_client(settings: Any) -> FlotaGpsClient | None:
    """Cliente según ``GPS_FLOTA_*``; None (GPS desactivado) si falta la URL o la clave."""
    clave = settings.gps_flota_api_key.get_secret_value() if settings.gps_flota_api_key else ""
    if not settings.gps_flota_url or not clave:
        logger.warning("GPS_FLOTA_URL/GPS_FLOTA_API_KEY sin configurar: los gastos irán con «%s»", NO_DISPONIBLE)
        return None
    return FlotaGpsClient(settings.gps_flota_url, clave, settings.gps_flota_timeout_seconds)
