"""Pruebas del Panel Admin: autenticación, reservas, usuarios, estaciones (CSV), parámetros del .env y auditoría."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.admin import bot as admin_bot
from app.admin import envfile
from app.admin.router import AdminServices
from app.admin.router import router as admin_router
from app.config import Settings
from app.preop import service as preop_service
from app.preop.models import (
    AdminAuditoria,
    ProveedorCombustible,
    RegistroConductor,
    ReservaHistorial,
    ReservaVehiculo,
)

from . import test_preop as _preop
from .helpers import BOT_TOKEN, make_init_data
from .test_preop import TG_USER, _ids

db = _preop.db
seeded = _preop.seeded

ADMIN_TOKEN = "999:ADMIN"
ADMIN_ID = 555
ENV_INICIAL = (
    "# Entorno\n"
    "APP_ENV=production\n"
    "LOG_LEVEL=INFO\n"
    "\n"
    "# Cola de envío a Odoo\n"
    "PREOP_MAX_ATTEMPTS=8   # intentos\n"
    "TELEGRAM_BOT_TOKEN=1234:SECRETO-TELEGRAM\n"
    "GPS_FLOTA_URL=\n"
    "GPS_FLOTA_API_KEY=clave-super-secreta\n"
    "DATABASE_URL=sqlite:///x\n"
)


@pytest.fixture
def panel(seeded, tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV_INICIAL, encoding="utf-8")
    app = FastAPI()
    app.include_router(admin_router)
    app.state.admin = AdminServices(seeded, ADMIN_TOKEN, frozenset({ADMIN_ID}), env, 3600)
    return SimpleNamespace(client=TestClient(app), db=seeded, env=env)


def _h(user_id=ADMIN_ID, token=ADMIN_TOKEN):
    return {"X-Telegram-Init-Data": make_init_data(user_id=user_id, bot_token=token)}


def _audit(db) -> list[AdminAuditoria]:
    with db.session() as s:
        return list(s.scalars(select(AdminAuditoria).order_by(AdminAuditoria.id)))


def _reservar(db) -> int:
    vehiculo, extracto = _ids(db)
    preop_service.submit(db, preop_service.TelegramIdentity(TG_USER, "ana", "Ana C"),
                         _preop._payload(vehiculo, extracto))
    with db.session() as s:
        return s.scalar(select(ReservaVehiculo.id))


# ───────────────────────────────────────────── autenticación ─────────────────────────────────────────────

ENDPOINTS = [
    ("get", "/api/admin/me"), ("get", "/api/admin/reservas"), ("post", "/api/admin/reservas/1/liberar"),
    ("get", "/api/admin/empleados?q=a"), ("get", "/api/admin/flota?q=a"), ("get", "/api/admin/usuarios"),
    ("delete", "/api/admin/usuarios/1"), ("get", "/api/admin/estaciones"),
    ("post", "/api/admin/estaciones/previsualizar"), ("post", "/api/admin/estaciones/cargar"),
    ("post", "/api/admin/estaciones/1/activo"), ("get", "/api/admin/parametros"), ("put", "/api/admin/parametros"),
    ("get", "/api/admin/auditoria"),
]


@pytest.mark.parametrize(("metodo", "ruta"), ENDPOINTS)
def test_todos_los_endpoints_exigen_init_data_valido_y_admin(panel, metodo, ruta):
    c = panel.client
    llamar = getattr(c, metodo)
    assert llamar(ruta).status_code == 401  # sin initData
    firmado_otro_bot = {"X-Telegram-Init-Data": make_init_data(user_id=ADMIN_ID, bot_token=BOT_TOKEN)}
    assert llamar(ruta, headers=firmado_otro_bot).status_code == 401  # token de Operativos, no el del admin
    alterado = {"X-Telegram-Init-Data": make_init_data(user_id=ADMIN_ID, bot_token=ADMIN_TOKEN, tamper=True)}
    assert llamar(ruta, headers=alterado).status_code == 401
    caducado = {"X-Telegram-Init-Data": make_init_data(user_id=ADMIN_ID, bot_token=ADMIN_TOKEN, auth_date=1)}
    assert llamar(ruta, headers=caducado).status_code == 401
    assert llamar(ruta, headers=_h(user_id=TG_USER)).status_code == 403  # firma válida, pero no es admin


def test_admin_valido_entra_y_lista_vacia_no_deja_entrar_a_nadie(panel):
    r = panel.client.get("/api/admin/me", headers=_h())
    assert r.status_code == 200 and r.json()["user_id"] == ADMIN_ID
    panel.client.app.state.admin.admin_ids = frozenset()
    assert panel.client.get("/api/admin/me", headers=_h()).status_code == 403


def test_settings_admin_ids_y_pagina(panel):
    assert Settings(admin_telegram_ids="1, 2;3,x,,").admin_ids == frozenset({1, 2, 3})
    assert Settings(admin_telegram_ids="").admin_ids == frozenset()
    assert Settings().admin_bot_token is None and Settings().admin_listen_port == 8012
    r = panel.client.get("/admin")
    assert r.status_code == 200 and "Panel Admin" in r.text and r.headers["cache-control"] == "no-cache"
    assert panel.client.get("/").status_code == 200


# ─────────────────────────────────────────────── reservas ───────────────────────────────────────────────

def test_liberar_reserva_la_mueve_al_historial_y_audita(panel):
    reserva_id = _reservar(panel.db)
    d = panel.client.get("/api/admin/reservas", headers=_h()).json()
    assert d["kpis"]["activas"] == 1 and d["total"] == 1
    [item] = d["items"]
    assert item["id"] == reserva_id and item["placa"] == "ABC123" and item["telegram_user_id"] == TG_USER
    assert item["conductor"] == "Ana Conductora" and item["vehiculo_odoo_id"] == 10 and item["empleado_odoo_id"] == 100
    for q in ("ana", "abc123", "10", "100", str(TG_USER)):
        assert panel.client.get("/api/admin/reservas", params={"q": q}, headers=_h()).json()["total"] == 1, q
    assert panel.client.get("/api/admin/reservas", params={"q": "zzz"}, headers=_h()).json()["total"] == 0

    r = panel.client.post(f"/api/admin/reservas/{reserva_id}/liberar", headers=_h())
    assert r.status_code == 200 and r.json()["placa"] == "ABC123"
    with panel.db.session() as s:
        assert s.scalar(select(ReservaVehiculo.id)) is None
        [h] = s.scalars(select(ReservaHistorial)).all()
        assert h.motivo == "manual" and h.telegram_user_id == TG_USER and h.placa == "ABC123"
    [a] = _audit(panel.db)
    assert a.accion == "reserva.liberar" and a.telegram_user_id == ADMIN_ID and str(reserva_id) in a.objetivo
    assert panel.client.get("/api/admin/reservas", headers=_h()).json()["kpis"]["liberadas_hoy"] == 1
    assert panel.client.post(f"/api/admin/reservas/{reserva_id}/liberar", headers=_h()).status_code == 404


# ─────────────────────────────────────────── empleados y flota ───────────────────────────────────────────

def test_busqueda_de_empleados_y_flota_es_de_solo_lectura(panel):
    c = panel.client
    assert c.get("/api/admin/empleados", headers=_h()).json()["items"] == []
    _reservar(panel.db)
    with panel.db.session() as s:
        s.add(RegistroConductor(telegram_user_id=TG_USER, nombre_completo="Ana Conductora", documento="1020304050",
                                correo="ana@x.co", estado="registrado", empleado_odoo_id=100))
    for q in ("Ana", str(TG_USER), "100", "1020304050"):
        [e] = c.get("/api/admin/empleados", params={"q": q}, headers=_h()).json()["items"]
        assert e["nombre"] == "Ana Conductora" and e["puesto"] == "Conductor" and e["contrato"] == "Contrato Minera"
        assert e["responsable"] == "Jefe Uno" and e["vehiculo_reservado"] == "ABC123", q
    assert e["documento"] == "1020304050"
    [jefe] = c.get("/api/admin/empleados", params={"q": "Jefe"}, headers=_h()).json()["items"]
    assert jefe["vehiculo_reservado"] is None
    [v] = c.get("/api/admin/flota", params={"q": "BUS 01"}, headers=_h()).json()["items"]
    assert v["placa"] == "ABC123" and v["reservado_por"] == "Ana Conductora" and v["odoo_id"] == 10
    [v2] = c.get("/api/admin/flota", params={"q": "VAN 02"}, headers=_h()).json()["items"]
    assert v2["reservado_por"] is None
    assert c.post("/api/admin/empleados", headers=_h()).status_code == 405  # sin escritura
    assert _audit(panel.db) == []


# ─────────────────────────────────────────── usuarios registrados ───────────────────────────────────────

def test_eliminar_usuario_registrado_solo_borra_la_fila_local(panel):
    with panel.db.session() as s:
        s.add_all([RegistroConductor(telegram_user_id=1001, nombre_completo="Luis Pérez", documento="555666",
                                     correo="luis@x.co", estado="registrado", empleado_odoo_id=100),
                   RegistroConductor(telegram_user_id=1002, nombre_completo="Marta Gómez", documento="777888",
                                     correo="marta@x.co", estado="no_encontrado")])
    c = panel.client
    d = c.get("/api/admin/usuarios", headers=_h()).json()
    assert d["total"] == 2 and len(d["items"]) == 2
    for q in ("luis", "555666", "marta@x", "1001"):
        assert c.get("/api/admin/usuarios", params={"q": q}, headers=_h()).json()["total"] == 1, q
    uid = next(u["id"] for u in d["items"] if u["telegram_user_id"] == 1001)

    assert c.delete(f"/api/admin/usuarios/{uid}", headers=_h()).status_code == 200
    with panel.db.session() as s:
        assert [r.telegram_user_id for r in s.scalars(select(RegistroConductor))] == [1002]
    [a] = _audit(panel.db)
    assert a.accion == "usuario.eliminar" and "telegram_id=1001" in a.detalle and "Odoo no se modificó" in a.detalle
    assert "555666" not in a.detalle and "luis@x.co" not in a.detalle  # sin datos personales en el detalle
    assert c.delete(f"/api/admin/usuarios/{uid}", headers=_h()).status_code == 404


# ────────────────────────────────────────────── estaciones ─────────────────────────────────────────────

CSV_OK = ("nombre;nit;latitud;longitud;ciudad;direccion\n"
          "EDS Uno;900;6,2442;-75,5812;Medellín;Cra 80\n"
          "EDS Dos;;4.711;-74.0721;Bogotá;\n")


def _estaciones(db) -> dict[str, ProveedorCombustible]:
    with db.session() as s:
        return {p.nombre: p for p in s.scalars(select(ProveedorCombustible))}


def test_csv_valido_vista_previa_y_carga(panel):
    c = panel.client
    p = c.post("/api/admin/estaciones/previsualizar", json={"csv": CSV_OK}, headers=_h()).json()
    assert (p["validas"], p["nuevos"], p["actualizados"], p["desactivados"], p["errores"]) == (2, 2, 0, 0, [])
    assert p["muestra"][0]["latitud"] == 6.2442 and _estaciones(panel.db) == {}  # la vista previa no escribe
    r = c.post("/api/admin/estaciones/cargar", json={"csv": CSV_OK}, headers=_h())
    assert r.status_code == 200 and (r.json()["nuevos"], r.json()["actualizados"]) == (2, 0)
    est = _estaciones(panel.db)
    assert (est["EDS Uno"].latitud, est["EDS Uno"].longitud, est["EDS Uno"].ciudad) == (6.2442, -75.5812, "Medellín")
    assert est["EDS Dos"].longitud == -74.0721 and est["EDS Dos"].activo
    # Segunda vez: todas actualizadas.
    p2 = c.post("/api/admin/estaciones/previsualizar", json={"csv": CSV_OK}, headers=_h()).json()
    assert (p2["nuevos"], p2["actualizados"]) == (0, 2)
    [a] = _audit(panel.db)
    assert a.accion == "estaciones.cargar" and "nuevos=2" in a.detalle


def test_csv_con_errores_por_fila(panel):
    csv_malo = "nombre;latitud;longitud\nBuena;6,1;-75,1\nMala;95;-75\nSin par;6;\n;;\nOtra;abc;1\n"
    c = panel.client
    p = c.post("/api/admin/estaciones/previsualizar", json={"csv": csv_malo}, headers=_h()).json()
    assert p["validas"] == 1 and [e["linea"] for e in p["errores"]] == [3, 4, 6]
    assert "fuera de rango" in p["errores"][0]["mensaje"]
    # Sin reemplazar se cargan solo las válidas.
    r = c.post("/api/admin/estaciones/cargar", json={"csv": csv_malo}, headers=_h()).json()
    assert r["nuevos"] == 1 and r["omitidas_por_error"] == 3 and list(_estaciones(panel.db)) == ["Buena"]
    # Sin la columna nombre -> 422; sin filas válidas -> 422.
    assert c.post("/api/admin/estaciones/previsualizar", json={"csv": "razon;nit\nX;1\n"}, headers=_h()).status_code == 422
    assert c.post("/api/admin/estaciones/cargar", json={"csv": "nombre;latitud;longitud\nX;99;1\n"},
                  headers=_h()).status_code == 422


def test_csv_con_coma_decimal_y_separador_coma(panel):
    c = panel.client
    csv_coma = 'nombre,latitud,longitud\nEDS Norte,"6,25","-75,5"\nEDS Sur,5.5,-74.1\n'
    r = c.post("/api/admin/estaciones/cargar", json={"csv": csv_coma}, headers=_h())
    assert r.status_code == 200 and r.json()["nuevos"] == 2
    est = _estaciones(panel.db)
    assert (est["EDS Norte"].latitud, est["EDS Norte"].longitud) == (6.25, -75.5)
    assert (est["EDS Sur"].latitud, est["EDS Sur"].longitud) == (5.5, -74.1)


def test_csv_reemplazar_desactiva_las_que_no_vienen(panel):
    c = panel.client
    c.post("/api/admin/estaciones/cargar", json={"csv": CSV_OK}, headers=_h())
    nuevo = "nombre;nit\nEDS Uno;900\nEDS Tres;333\n"
    p = c.post("/api/admin/estaciones/previsualizar", json={"csv": nuevo, "reemplazar": True}, headers=_h()).json()
    assert (p["nuevos"], p["actualizados"], p["desactivados"]) == (1, 1, 1)
    r = c.post("/api/admin/estaciones/cargar", json={"csv": nuevo, "reemplazar": True}, headers=_h()).json()
    assert r["desactivados"] == 1
    est = _estaciones(panel.db)
    assert est["EDS Uno"].activo and est["EDS Tres"].activo and not est["EDS Dos"].activo
    assert est["EDS Uno"].latitud == 6.2442  # el CSV sin ubicación no la borra
    # Con reemplazar, un archivo con errores se rechaza: desactivaría de más.
    malo = "nombre;latitud;longitud\nEDS Uno;99;1\nEDS Tres;;\n"
    pm = c.post("/api/admin/estaciones/previsualizar", json={"csv": malo, "reemplazar": True}, headers=_h()).json()
    assert pm["bloquea_reemplazo"] is True
    assert c.post("/api/admin/estaciones/cargar", json={"csv": malo, "reemplazar": True},
                  headers=_h()).status_code == 422
    assert _estaciones(panel.db)["EDS Tres"].activo


def test_lista_y_activacion_de_estaciones(panel):
    c = panel.client
    c.post("/api/admin/estaciones/cargar", json={"csv": CSV_OK}, headers=_h())
    d = c.get("/api/admin/estaciones", headers=_h()).json()
    assert d["total"] == 2 and d["kpis"] == {"activas": 2, "con_ubicacion": 2}
    uno = next(s for s in d["items"] if s["nombre"] == "EDS Uno")
    assert uno["latitud"] == 6.2442 and uno["ciudad"] == "Medellín" and uno["activo"] is True
    assert c.post(f"/api/admin/estaciones/{uno['id']}/activo", json={"activo": False}, headers=_h()).status_code == 200
    assert [s["nombre"] for s in c.get("/api/admin/estaciones", params={"estado": "inactiva"}, headers=_h()).json()["items"]] == ["EDS Uno"]
    assert [s["nombre"] for s in c.get("/api/admin/estaciones", params={"q": "bogot"}, headers=_h()).json()["items"]] == ["EDS Dos"]
    assert c.get("/api/admin/estaciones", params={"estado": "x"}, headers=_h()).status_code == 422
    # La Mini App de combustible deja de ofrecerla.
    from app.combustible import service as combustible_service

    assert [p["nombre"] for p in combustible_service.form_full(panel.db)["proveedores"]] == ["EDS Dos"]
    assert c.post(f"/api/admin/estaciones/{uno['id']}/activo", json={"activo": True}, headers=_h()).status_code == 200
    assert c.post("/api/admin/estaciones/9999/activo", json={"activo": True}, headers=_h()).status_code == 404
    assert [a.accion for a in _audit(panel.db)] == ["estaciones.cargar", "estacion.desactivar", "estacion.activar"]


# ───────────────────────────────────────────── parámetros (.env) ─────────────────────────────────────────

def _put(panel, cambios):
    return panel.client.put("/api/admin/parametros", json={"cambios": cambios}, headers=_h())


def test_get_parametros_solo_lista_blanca_y_no_expone_secretos(panel):
    r = panel.client.get("/api/admin/parametros", headers=_h())
    assert r.status_code == 200 and "docker compose restart app" in r.json()["aviso"]
    items = {i["clave"]: i for i in r.json()["items"]}
    assert set(items) == set(envfile.PARAMETROS)
    for prohibido in ("TELEGRAM_BOT_TOKEN", "ADMIN_BOT_TOKEN", "ADMIN_TELEGRAM_IDS", "DATABASE_URL", "POSTGRES_PASSWORD",
                      "ODOO_PASSWORD", "ODOO_API_PASSWORD", "ODOO_URL", "WEBAPP_BASE_URL"):
        assert prohibido not in items
    assert items["LOG_LEVEL"]["valor"] == "INFO" and items["PREOP_MAX_ATTEMPTS"]["valor"] == "8"  # sin el comentario
    assert items["PREOP_MAX_ATTEMPTS"]["tipo"] == "int" and items["GPS_FLOTA_TIMEOUT_SECONDS"]["tipo"] == "float"
    secreto = items["GPS_FLOTA_API_KEY"]
    assert secreto["secreto"] and secreto["configurado"] is True and "valor" not in secreto
    assert "clave-super-secreta" not in r.text and "SECRETO-TELEGRAM" not in r.text
    # Clave que no está en el archivo: se muestra el valor por defecto de Settings.
    assert items["POLL_INTERVAL_SECONDS"]["valor"] == "15" and items["POLL_INTERVAL_SECONDS"]["en_archivo"] is False


def test_put_parametros_respalda_y_conserva_comentarios_y_orden(panel):
    r = _put(panel, {"LOG_LEVEL": "debug", "PREOP_MAX_ATTEMPTS": "12", "POLL_INTERVAL_SECONDS": 20})
    assert r.status_code == 200
    body = r.json()
    assert body["cambiados"] == ["LOG_LEVEL", "PREOP_MAX_ATTEMPTS", "POLL_INTERVAL_SECONDS"]
    assert "docker compose restart app" in body["aviso"]
    texto = panel.env.read_text(encoding="utf-8")
    esperado = ENV_INICIAL.replace("LOG_LEVEL=INFO", "LOG_LEVEL=DEBUG").replace(
        "PREOP_MAX_ATTEMPTS=8   # intentos", "PREOP_MAX_ATTEMPTS=12") + "POLL_INTERVAL_SECONDS=20\n"
    assert texto == esperado
    assert "# Entorno" in texto and "# Cola de envío a Odoo" in texto and "TELEGRAM_BOT_TOKEN=1234:SECRETO-TELEGRAM" in texto
    # Respaldo con el contenido original en run/env-backups/.env.<marca>.
    respaldos = list((panel.env.parent / "run" / "env-backups").glob(".env.*"))
    assert [p.name for p in respaldos] == [body["respaldo"]]
    assert respaldos[0].read_text(encoding="utf-8") == ENV_INICIAL
    # Un valor igual al actual no cambia nada ni crea otro respaldo.
    r2 = _put(panel, {"LOG_LEVEL": "DEBUG"}).json()
    assert r2["cambiados"] == [] and r2["respaldo"] is None
    assert len(list((panel.env.parent / "run" / "env-backups").glob(".env.*"))) == 1


def test_put_parametros_escribe_en_el_mismo_archivo_sin_rename(panel):
    inodo = panel.env.stat().st_ino
    assert _put(panel, {"LOG_LEVEL": "WARNING"}).status_code == 200
    assert panel.env.stat().st_ino == inodo  # mismo archivo (bind mount de un solo archivo)
    assert "LOG_LEVEL=WARNING\n" in panel.env.read_text(encoding="utf-8")


def test_put_parametros_respeta_saltos_de_linea_crlf(panel):
    panel.env.write_bytes(ENV_INICIAL.replace("\n", "\r\n").encode())
    assert _put(panel, {"LOG_LEVEL": "ERROR"}).status_code == 200
    datos = panel.env.read_bytes()
    assert b"LOG_LEVEL=ERROR\r\n" in datos and b"\n" not in datos.replace(b"\r\n", b"")


def test_put_parametros_rechaza_fuera_de_lista_y_tipos_invalidos(panel):
    antes = panel.env.read_text(encoding="utf-8")
    for cambios in (
        {"TELEGRAM_BOT_TOKEN": "x"}, {"ADMIN_TELEGRAM_IDS": "1"}, {"ADMIN_BOT_TOKEN": "x"}, {"DATABASE_URL": "sqlite:///y"},
        {"POSTGRES_PASSWORD": "x"}, {"ODOO_PASSWORD": "x"}, {"ODOO_URL": "http://x"}, {"WEBAPP_BASE_URL": "https://x"},
        {"log_level": "INFO"},  # la clave se compara exacta
        {"PREOP_MAX_ATTEMPTS": "muchos"}, {"PREOP_MAX_ATTEMPTS": "1.5"}, {"PREOP_MAX_ATTEMPTS": "0"},
        {"PREOP_MAX_ATTEMPTS": "-3"}, {"GPS_FLOTA_TIMEOUT_SECONDS": "10,5"}, {"GPS_FLOTA_TIMEOUT_SECONDS": "abc"},
        {"LOG_LEVEL": "VERBOSE"}, {"GPS_FLOTA_URL": "ftp://x"}, {"ODOO_API_COMPANY_IDS": "1;2"},
        {"PRUEBA_RUTA_ACUERDOS": "A\nTELEGRAM_BOT_TOKEN=robado"}, {"PRUEBA_RUTA_ACUERDOS": 'A "B"'},
        {"PRUEBA_RUTA_ACUERDOS": "A $B"},
    ):
        r = _put(panel, cambios)
        assert r.status_code == 400, cambios
    assert panel.env.read_text(encoding="utf-8") == antes
    assert not (panel.env.parent / "run").exists()  # nada se respaldó
    assert _put(panel, {}).status_code == 422
    # Todo o nada: un valor bueno junto a uno malo no se escribe.
    assert _put(panel, {"LOG_LEVEL": "ERROR", "PREOP_MAX_ATTEMPTS": "x"}).status_code == 400
    assert panel.env.read_text(encoding="utf-8") == antes
    assert _audit(panel.db) == []


def test_put_parametros_valores_validos_de_cada_tipo(panel):
    r = _put(panel, {"GPS_FLOTA_TIMEOUT_SECONDS": "7.5", "GPS_FLOTA_URL": "https://gps.example.com/api",
                     "ODOO_API_COMPANY_IDS": "1, 2", "COMBUSTIBLE_GASTOS_PRODUCT_ID": "77",
                     "PRUEBA_RUTA_ACUERDOS": "MINERA DE COBRE QUEBRADONA;SERVICIOS HIDROGEOLÓGICOS INTEGRALES S.A.S."})
    assert r.status_code == 200 and len(r.json()["cambiados"]) == 5
    vals = envfile.valores_actuales(panel.env)
    assert vals["ODOO_API_COMPANY_IDS"] == "1,2" and vals["COMBUSTIBLE_GASTOS_PRODUCT_ID"] == "77"
    assert vals["PRUEBA_RUTA_ACUERDOS"].endswith("S.A.S.")
    assert _put(panel, {"GPS_FLOTA_URL": ""}).status_code == 200  # vacío = cotejo GPS desactivado


def test_secreto_se_escribe_pero_nunca_se_muestra_ni_se_audita(panel):
    r = _put(panel, {"GPS_FLOTA_API_KEY": "nueva-clave-NO-VER", "LOG_LEVEL": "ERROR"})
    assert r.status_code == 200 and "nueva-clave-NO-VER" not in r.text
    assert "GPS_FLOTA_API_KEY=nueva-clave-NO-VER\n" in panel.env.read_text(encoding="utf-8")
    g = panel.client.get("/api/admin/parametros", headers=_h())
    assert "nueva-clave-NO-VER" not in g.text and "clave-super-secreta" not in g.text
    aud = panel.client.get("/api/admin/auditoria", headers=_h())
    assert "nueva-clave-NO-VER" not in aud.text and "ERROR" not in aud.text.replace("parametro.cambiar", "")
    filas = _audit(panel.db)
    assert [(a.accion, a.objetivo) for a in filas] == [("parametro.cambiar", "GPS_FLOTA_API_KEY"),
                                                        ("parametro.cambiar", "LOG_LEVEL")]
    assert "clave-super-secreta" not in repr([a.detalle for a in filas])
    # Vaciar el secreto.
    assert _put(panel, {"GPS_FLOTA_API_KEY": ""}).status_code == 200
    items = {i["clave"]: i for i in panel.client.get("/api/admin/parametros", headers=_h()).json()["items"]}
    assert items["GPS_FLOTA_API_KEY"]["configurado"] is False


def test_respaldos_conservan_los_ultimos_20(panel):
    for i in range(25):
        assert _put(panel, {"PREOP_MAX_ATTEMPTS": str(i + 20)}).status_code == 200
    carpeta = panel.env.parent / "run" / "env-backups"
    nombres = sorted(p.name for p in carpeta.glob(".env.*"))
    assert len(nombres) == envfile.BACKUPS_KEEP == 20
    # Se conservan los más recientes: el último respaldo es el estado previo al último cambio.
    assert (carpeta / nombres[-1]).read_text(encoding="utf-8").count("PREOP_MAX_ATTEMPTS=43") == 1


def test_parametros_sin_archivo_env(panel):
    panel.env.unlink()
    assert panel.client.get("/api/admin/parametros", headers=_h()).status_code == 503
    assert _put(panel, {"LOG_LEVEL": "INFO"}).status_code == 400


def test_la_lista_blanca_no_incluye_secretos_de_infraestructura():
    prohibidos = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "ADMIN_BOT_TOKEN", "ADMIN_TELEGRAM_IDS", "ADMIN_ENV_FILE",
                  "DATABASE_URL", "POSTGRES_PASSWORD", "ODOO_PASSWORD", "ODOO_API_PASSWORD", "ODOO_WRITE_PASSWORD"}
    assert not prohibidos & set(envfile.PARAMETROS)
    assert not any(c.startswith("ADMIN_") or c.endswith("_URL") and c != "GPS_FLOTA_URL" for c in envfile.PARAMETROS)
    assert [c for c, p in envfile.PARAMETROS.items() if p.secreto] == ["GPS_FLOTA_API_KEY"]
    for clave in envfile.PARAMETROS:  # cada clave existe en Settings
        assert clave.lower() in Settings.model_fields


# ─────────────────────────────────────────────── auditoría ───────────────────────────────────────────────

def test_auditoria_lista_quien_cuando_accion_objetivo(panel):
    _put(panel, {"LOG_LEVEL": "ERROR"})
    panel.client.post("/api/admin/estaciones/cargar", json={"csv": CSV_OK}, headers=_h())
    items = panel.client.get("/api/admin/auditoria", headers=_h()).json()["items"]
    assert [i["accion"] for i in items] == ["estaciones.cargar", "parametro.cambiar"]  # más reciente primero
    nuevo = items[1]
    assert nuevo["telegram_user_id"] == ADMIN_ID and nuevo["nombre"] == "Prueba" and nuevo["objetivo"] == "LOG_LEVEL"
    assert nuevo["fecha"] and "ERROR" not in (nuevo["detalle"] or "")
    assert panel.client.get("/api/admin/auditoria", params={"limite": 1}, headers=_h()).json()["items"][0]["accion"] == "estaciones.cargar"
    assert panel.client.get("/api/admin/auditoria", params={"limite": 0}, headers=_h()).status_code == 422


# ───────────────────────────────────────────────── bot ─────────────────────────────────────────────────

def _update(user_id):
    msg = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), message=msg), msg


def test_start_ofrece_el_portal_solo_a_administradores():
    start = admin_bot.make_start({ADMIN_ID}, "https://panel.example.com/")
    upd, msg = _update(ADMIN_ID)
    asyncio.run(start(upd, None))
    args, kwargs = msg.reply_text.call_args
    assert args[0] == "Selecciona un formulario:"
    boton = kwargs["reply_markup"].inline_keyboard[0][0]
    assert boton.text == "🖥️ PORTAL" and boton.web_app.url == "https://panel.example.com/"

    upd, msg = _update(42)
    asyncio.run(start(upd, None))
    texto = msg.reply_text.call_args.args[0]
    assert "solo para administradores" in texto and "42" in texto and "reply_markup" not in msg.reply_text.call_args.kwargs

    sin_url = admin_bot.make_start({ADMIN_ID}, "")
    upd, msg = _update(ADMIN_ID)
    asyncio.run(sin_url(upd, None))
    assert "ADMIN_WEBAPP_BASE_URL" in msg.reply_text.call_args.args[0]


def test_menu_del_bot_se_configura_como_web_app():
    application = SimpleNamespace(bot=SimpleNamespace(set_chat_menu_button=AsyncMock(), set_my_commands=AsyncMock()))
    asyncio.run(admin_bot.configure_menu(application, "https://panel.example.com/"))
    menu = application.bot.set_chat_menu_button.call_args.kwargs["menu_button"]
    assert menu.text == "PORTAL" and menu.web_app.url == "https://panel.example.com/"
    # Un fallo de Telegram no impide el arranque.
    application.bot.set_chat_menu_button.side_effect = RuntimeError("boom")
    asyncio.run(admin_bot.configure_menu(application, "https://panel.example.com/"))
