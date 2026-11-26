"""Edición controlada del ``.env`` desde el Panel Admin.

* La lista blanca de parámetros vive **aquí, en código** (nunca en el ``.env``): lo que no esté en
  ``PARAMETROS`` no se puede leer ni escribir desde el panel (tokens de Telegram, ``ADMIN_*``,
  ``DATABASE_URL``, credenciales de Odoo, URLs base...).
* Los secretos (``secreto=True``) se pueden **escribir** pero nunca se devuelven: solo "configurado" o "vacío".
* Se valida el tipo con el del campo en ``Settings`` (int, float, str...).
* Al guardar: respaldo en ``<carpeta del .env>/run/env-backups/.env.<marca de tiempo>`` (se conservan los
  últimos 20), solo se tocan las líneas de las claves cambiadas (comentarios y orden intactos; las claves
  nuevas se añaden al final) y se escribe **en el mismo archivo** (no con ``rename``): en Docker el ``.env`` es
  un bind mount de un solo archivo y el ``rename`` falla con EBUSY.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import Settings

BACKUPS_KEEP = 20
_LOCK = threading.Lock()
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
# Caracteres que obligarían a entrecomillar o que dotenv/docker compose interpretarían (no se permiten).
_PROHIBIDOS = re.compile(r"[\"'`$#\\\x00-\x1f\x7f]")


class ParametroError(ValueError):
    """Parámetro fuera de la lista blanca o con un valor inválido."""


@dataclass(frozen=True)
class Parametro:
    clave: str
    descripcion: str
    secreto: bool = False
    validador: Callable[[str], str] | None = None  # normaliza/valida el texto ya comprobado por tipo


def _log_level(valor: str) -> str:
    valor = valor.upper()
    if valor not in _LOG_LEVELS:
        raise ParametroError(f"debe ser uno de: {', '.join(sorted(_LOG_LEVELS))}")
    return valor


def _url_o_vacia(valor: str) -> str:
    if valor and not re.fullmatch(r"https?://[^\s]+", valor):
        raise ParametroError("debe ser una URL http(s):// o quedar vacía")
    return valor


def _lista_enteros(valor: str) -> str:
    if not re.fullmatch(r"\d+(\s*,\s*\d+)*", valor):
        raise ParametroError("debe ser una lista de números separados por coma (p. ej. 1,2)")
    return ",".join(p.strip() for p in valor.split(","))


def _tipos_gastos(valor: str) -> str:
    from ..gastos_operativos.catalogo import TiposGastoError, parse_tipos, texto_tipos

    try:
        return texto_tipos(parse_tipos(valor))
    except TiposGastoError as exc:
        raise ParametroError(str(exc).removeprefix("GASTOS_OPERATIVOS_TIPOS: ")) from exc


def _topes_gastos(valor: str) -> str:
    """Formato (Nombre:valor, enteros > 0). Que cada nombre exista en los tipos se cruza en ``guardar``."""
    from ..gastos_operativos.catalogo import TopesGastoError, parse_topes, texto_topes

    try:
        return texto_topes(parse_topes(valor))
    except TopesGastoError as exc:
        raise ParametroError(str(exc).removeprefix("GASTOS_OPERATIVOS_TOPES: ")) from exc


def _cruzar_topes_y_tipos(ruta: Path, nuevos: dict[str, str]) -> None:
    """Los topes solo pueden nombrar tipos que existan: se comprueba con los valores que quedarían en el ``.env``.

    Si se guardan topes, los nombres se escriben tal como están en los tipos (``alimentacion`` -> ``Alimentación``).
    """
    if not {"GASTOS_OPERATIVOS_TOPES", "GASTOS_OPERATIVOS_TIPOS"} & set(nuevos):
        return
    from ..gastos_operativos.catalogo import TiposGastoError, TopesGastoError, parse_tipos, parse_topes, texto_topes

    actuales = valores_actuales(ruta)
    tipos_txt = nuevos.get("GASTOS_OPERATIVOS_TIPOS", actuales.get("GASTOS_OPERATIVOS_TIPOS"))
    if tipos_txt is None:
        tipos_txt = _por_defecto("GASTOS_OPERATIVOS_TIPOS")
    topes_txt = nuevos.get("GASTOS_OPERATIVOS_TOPES", actuales.get("GASTOS_OPERATIVOS_TOPES", ""))
    try:
        topes = parse_topes(topes_txt, parse_tipos(tipos_txt))
    except (TiposGastoError, TopesGastoError) as exc:
        raise ParametroError(str(exc)) from exc
    if "GASTOS_OPERATIVOS_TOPES" in nuevos:
        nuevos["GASTOS_OPERATIVOS_TOPES"] = texto_topes(topes)


PARAMETROS: dict[str, Parametro] = {p.clave: p for p in (
    Parametro("LOG_LEVEL", "Nivel de log (DEBUG, INFO, WARNING, ERROR, CRITICAL)", validador=_log_level),
    Parametro("POLL_INTERVAL_SECONDS", "Segundos entre consultas de la vigilancia de tickets"),
    Parametro("WATCH_TTL_HOURS", "Horas que dura una vigilancia de ticket"),
    Parametro("EMPLOYEE_CACHE_SECONDS", "Segundos de caché de acceso por empleado"),
    Parametro("MASTER_SYNC_INTERVAL_SECONDS", "Segundos entre sincronizaciones de empleados, flota y extractos"),
    Parametro("PREOP_WORKER_INTERVAL_SECONDS", "Segundos entre pasadas de las colas de envío a Odoo"),
    Parametro("PREOP_MAX_ATTEMPTS", "Intentos máximos de envío a Odoo antes de marcar fallido"),
    Parametro("PREOP_SENDING_LEASE_SECONDS", "Segundos tras los que un envío «enviando» se considera abandonado"),
    Parametro("PREOP_AUTH_MAX_AGE_SECONDS", "Antigüedad máxima del initData de las Mini Apps (segundos)"),
    Parametro("PRUEBA_RUTA_ACUERDOS", "Acuerdos autorizados para la Prueba de Ruta, separados por «;»"),
    Parametro("GPS_FLOTA_URL", "URL de la API GPS de la flota (vacía = cotejo desactivado)", validador=_url_o_vacia),
    Parametro("GPS_FLOTA_TIMEOUT_SECONDS", "Tiempo máximo de espera de la API GPS (segundos)"),
    Parametro("COMBUSTIBLE_GASTOS_PRODUCT_ID", "product_id del gasto de combustible en hr.expense"),
    Parametro("GASTOS_OPERATIVOS_TIPOS", "Tipos de Gastos Operativos: Nombre:product_id separados por «;»",
              validador=_tipos_gastos),
    Parametro("GASTOS_OPERATIVOS_TOPES",
              "Tope máximo por gasto y por tipo (COP): Nombre:valor separados por «;»; vacío = sin topes",
              validador=_topes_gastos),
    Parametro("ODOO_API_COMPANY_IDS", "Compañías de Odoo a sincronizar (ids separados por coma)",
              validador=_lista_enteros),
    Parametro("ODOO_API_PAGE_SIZE", "Registros por página en la sincronización con la API de Odoo"),
    Parametro("GPS_FLOTA_API_KEY", "Clave de la API GPS (cabecera x-api-key)", secreto=True),
)}


def _tipo_base(clave: str) -> type:
    """int, float, bool o str según la anotación del campo en ``Settings`` (SecretStr | None -> str)."""
    anotacion: Any = Settings.model_fields[clave.lower()].annotation
    return anotacion if anotacion in (int, float, bool) else str


def validar(clave: str, bruto: Any) -> str:
    """Valida y normaliza un valor para ``clave``; devuelve el texto que se escribirá en el ``.env``."""
    param = PARAMETROS.get(clave)
    if param is None:
        raise ParametroError("parámetro no editable desde el panel")
    if not isinstance(bruto, (str, int, float)) or isinstance(bruto, bool):
        raise ParametroError("el valor debe ser texto o número")
    valor = str(bruto).strip()
    if _PROHIBIDOS.search(valor):
        raise ParametroError("contiene caracteres no permitidos (comillas, #, $, \\ o saltos de línea)")
    tipo = _tipo_base(clave)
    if tipo is int:
        if not re.fullmatch(r"\d+", valor) or int(valor) <= 0:
            raise ParametroError("debe ser un número entero mayor que cero")
        valor = str(int(valor))
    elif tipo is float:
        if "," in valor:
            raise ParametroError("usa punto como separador decimal (10.5)")
        if not re.fullmatch(r"\d+(\.\d+)?", valor) or float(valor) <= 0:
            raise ParametroError("debe ser un número mayor que cero")
    elif tipo is bool:
        if valor.lower() not in ("true", "false"):
            raise ParametroError("debe ser true o false")
        valor = valor.lower()
    if param.validador is not None:
        valor = param.validador(valor)
    return valor


# ──────────────────────────────────── lectura / escritura ────────────────────────────────────

_LINEA = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _decodificar(valor: str) -> str:
    valor = valor.strip()
    if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
        return valor[1:-1]
    return re.split(r"\s+#", valor, maxsplit=1)[0].strip()


def _leer_texto(ruta: Path) -> str:
    try:
        return ruta.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ParametroError(f"no se encontró el archivo de entorno ({ruta})") from exc


def valores_actuales(ruta: Path) -> dict[str, str]:
    """Clave -> valor tal como está en el archivo (la última línea activa de cada clave)."""
    out: dict[str, str] = {}
    for linea in _leer_texto(ruta).splitlines():
        m = _LINEA.match(linea)
        if m and not linea.lstrip().startswith("#"):
            out[m.group(1)] = _decodificar(m.group(2))
    return out


def listar(ruta: Path) -> list[dict[str, Any]]:
    """Parámetros editables para la UI. De un secreto solo se informa si está configurado."""
    actuales = valores_actuales(ruta)
    items: list[dict[str, Any]] = []
    for clave, param in PARAMETROS.items():
        valor = actuales.get(clave)
        tipo = _tipo_base(clave).__name__
        item: dict[str, Any] = {"clave": clave, "descripcion": param.descripcion, "tipo": tipo,
                                "secreto": param.secreto, "en_archivo": clave in actuales}
        if param.secreto:
            item["configurado"] = bool(valor)
        else:
            item["valor"] = valor if valor is not None else _por_defecto(clave)
        items.append(item)
    return items


def _por_defecto(clave: str) -> str:
    campo = Settings.model_fields[clave.lower()]
    return "" if campo.is_required() or campo.default is None else str(campo.default)


def _respaldar(ruta: Path, contenido: bytes) -> Path:
    carpeta = ruta.parent / "run" / "env-backups"
    carpeta.mkdir(parents=True, exist_ok=True)
    marca = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    destino = carpeta / f".env.{marca}"
    destino.write_bytes(contenido)
    try:
        destino.chmod(0o600)  # contiene secretos
    except OSError:  # pragma: no cover - Windows / sistemas sin chmod
        pass
    antiguos = sorted(carpeta.glob(".env.*"))
    for viejo in antiguos[:-BACKUPS_KEEP]:
        viejo.unlink(missing_ok=True)
    return destino


def guardar(ruta: Path, cambios: dict[str, Any]) -> tuple[list[str], Path | None]:
    """Valida y escribe ``cambios``. Devuelve (claves realmente modificadas, respaldo creado).

    Todo o nada: si algún valor es inválido no se toca el archivo. Sin cambios reales no se respalda.
    """
    if not cambios:
        raise ParametroError("no hay cambios que guardar")
    nuevos = {clave: validar(clave, valor) for clave, valor in cambios.items()}
    with _LOCK:
        original = ruta.read_bytes() if ruta.exists() else None
        if original is None:
            raise ParametroError(f"no se encontró el archivo de entorno ({ruta})")
        _cruzar_topes_y_tipos(ruta, nuevos)
        texto = original.decode("utf-8")
        salto = "\r\n" if "\r\n" in texto else "\n"
        lineas = texto.split("\n") if "\n" in texto else [texto]
        lineas = [ln[:-1] if ln.endswith("\r") else ln for ln in lineas]
        sin_final = lineas and lineas[-1] == ""
        if sin_final:
            lineas.pop()
        cambiadas: list[str] = []
        for clave, valor in nuevos.items():
            indice = None
            for i, ln in enumerate(lineas):
                m = _LINEA.match(ln)
                if m and not ln.lstrip().startswith("#") and m.group(1) == clave:
                    indice = i  # la última línea activa es la que rige
            if indice is None:
                lineas.append(f"{clave}={valor}")
                cambiadas.append(clave)
            elif _decodificar(_LINEA.match(lineas[indice]).group(2)) != valor:  # type: ignore[union-attr]
                lineas[indice] = f"{clave}={valor}"
                cambiadas.append(clave)
        if not cambiadas:
            return [], None
        respaldo = _respaldar(ruta, original)
        nuevo = (salto.join(lineas) + salto).encode("utf-8")
        # En el mismo archivo (bind mount de un solo archivo: rename falla con EBUSY).
        with open(ruta, "r+b") as f:
            f.seek(0)
            f.write(nuevo)
            f.truncate()
        return cambiadas, respaldo
