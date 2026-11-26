"""Pruebas del registro de combustible: esquema, proveedores, vehículo reservado, kilometraje, cola, conector,
API y envío sin señal."""
from __future__ import annotations

import asyncio
import base64
import re
import httpx
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app import formatting
from app.combustible import gps, proveedores, service
from app.combustible.catalogo import PRODUCTO_GASTOS_ODOO_ID, TIPOS_COMBUSTIBLE, form_definition
from app.combustible.router import CombustibleServices
from app.combustible.router import router as combustible_router
from app.combustible.schemas import CombustibleIn
from app.combustible.worker import CombustibleSender
from app.preop import reservas
from app.preop import service as preop_service
from app.preop.models import (
    Combustible,
    CombustibleEnvioDiferido,
    CombustibleFactura,
    EstadoDiferido,
    ProveedorCombustible,
    SyncStatus,
    utc_now,
)
from app.preop.odoo_writer import OdooPermanentError, OdooTransientError
from app.preop.rest_client import OdooApiError, OdooRestClient, _RetryableHttpError
from app.preop.service import SubmissionError, TelegramIdentity

from . import test_preop as _preop
from .helpers import BOT_TOKEN, make_init_data
from .test_preop import TG_USER, FakeObjects, _ids, _writer

db = _preop.db
seeded = _preop.seeded

UUID = "3c4d5e6f-3c1d-4f7a-9a2b-1c2d3e4f5a6b"
IDENTITY = TelegramIdentity(TG_USER, "ana", "Ana C")
PDF = b"%PDF-1.4\n" + b"0" * 64
PDF_B64 = "data:application/pdf;base64," + base64.b64encode(PDF).decode()
ODOO_FUEL_TYPES = ["gasoline", "extra", "diesel", "electric", "gas", "glp", "diesel urea", "hydrogen"]


def _hoy() -> date:
    return reservas.fecha_jornada()


def _payload(**over: Any) -> CombustibleIn:
    data: dict[str, Any] = {
        "uuid": UUID, "para_gastos": False, "proveedor_texto": "Estación La 80",
        "fecha_transaccion": _hoy().isoformat(), "galones": "12,5", "precio_galon": "12000",
        "tipo_combustible": "diesel", "kilometraje": 1600,
    }
    data.update(over)
    return CombustibleIn.model_validate({k: v for k, v in data.items() if v is not None})


def _reservar(db) -> int:
    """Preoperacional del conductor (odómetro 1510): reserva el vehículo de la jornada."""
    vehiculo, extracto = _ids(db)
    preop_service.submit(db, IDENTITY, _preop._payload(vehiculo, extracto, uuid="8" * 8 + "-3c1d-4f7a-9a2b-1c2d3e4f5a6b"))
    return vehiculo


def _proveedor(db, nombre="Terpel La 80", nit="900123456", partner: int | None = 77) -> int:
    proveedores.cargar(db, [proveedores.FilaProveedor(nombre, nit, partner)])
    with db.session() as s:
        return s.scalar(select(ProveedorCombustible.id).where(ProveedorCombustible.nombre == nombre))


@pytest.fixture
def listo(seeded):
    """Conductor con el vehículo reservado por su preoperacional."""
    _reservar(seeded)
    return seeded


# ─────────────────────────────────────── catálogo y esquema ───────────────────────────────────────

def test_catalogo_coincide_con_odoo():
    assert [v for v, _ in TIPOS_COMBUSTIBLE] == ODOO_FUEL_TYPES
    form = form_definition()
    assert form["tipo_por_defecto"] == "diesel" and form["factura_obligatoria_en"] == ["gastos"]
    assert [m["para_gastos"] for m in form["modalidades"]] == [False, True]


def test_decimales_con_coma_y_total():
    data = _payload(galones="12,5", precio_galon="12000,50")
    assert (data.galones, data.precio_galon, data.total) == (12.5, 12000.5, 150006.25)


@pytest.mark.parametrize("cambio, error", [
    ({"proveedor_texto": None}, "proveedor"),
    ({"proveedor_id": 3}, "proveedor"),  # lista y texto a la vez
    ({"para_gastos": True}, "factura en PDF"),
    ({"factura": "data:application/pdf;base64," + base64.b64encode(b"\xff\xd8\xff" + b"0" * 40).decode()}, "PDF"),
    ({"tipo_combustible": "acpm"}, "Tipo de combustible"),
    ({"galones": 0}, "greater than 0"),
    ({"fecha_transaccion": (date.today() + timedelta(days=3)).isoformat()}, "futura"),
    ({"fecha_transaccion": (date.today() - timedelta(days=40)).isoformat()}, "30 días"),
    ({"telegram_user_id": 1}, "telegram_user_id"),
])
def test_esquema_rechaza(cambio, error):
    with pytest.raises(ValidationError, match=error):
        _payload(**cambio)


def test_factura_solo_obligatoria_en_gastos():
    assert _payload().factura is None
    con = _payload(para_gastos=True, factura=PDF_B64)
    assert con.factura_nombre == "factura.pdf"


# ───────────────────────────────────────────── proveedores ─────────────────────────────────────────────

def test_leer_csv_con_punto_y_coma_bom_y_nit():
    filas = proveedores.leer_csv("﻿Nombre;NIT;odoo_partner_id\nTerpel  La 80;900.123.456-7;77\n;;\nEDS Norte;;\n")
    assert filas == [proveedores.FilaProveedor("Terpel La 80", "9001234567", 77),
                     proveedores.FilaProveedor("EDS Norte", None, None)]
    assert proveedores.leer_csv("nombre,nit\nPrimax,800\n") == [proveedores.FilaProveedor("Primax", "800", None)]
    with pytest.raises(ValueError, match="Línea 2"):
        proveedores.leer_csv("nombre;odoo_partner_id\nX;abc\n")
    with pytest.raises(ValueError, match="nombre"):
        proveedores.leer_csv("razon;nit\nX;1\n")


def test_cargar_actualiza_por_nit_o_nombre_y_reemplaza(db):
    Fila = proveedores.FilaProveedor
    r1 = proveedores.cargar(db, [Fila("Terpel La 80", "900", 77), Fila("EDS Norte")])
    assert (r1.nuevos, r1.actualizados, r1.sin_contacto) == (2, 0, ("EDS Norte",))
    # Mismo NIT con otro nombre = el mismo proveedor; mismo nombre sin tildes = el mismo.
    r2 = proveedores.cargar(db, [Fila("TERPEL ESTACION 80", "900"), Fila("eds norte", None, 5)], reemplazar=True)
    assert (r2.nuevos, r2.actualizados, r2.desactivados, r2.sin_contacto) == (0, 2, 0, ())
    r3 = proveedores.cargar(db, [Fila("Primax")], reemplazar=True)
    assert (r3.nuevos, r3.desactivados) == (1, 2)
    with db.session() as s:
        assert [p.nombre for p in proveedores.listar_activos(s)] == ["Primax"]
        terpel = s.scalar(select(ProveedorCombustible).where(ProveedorCombustible.nit == "900"))
        assert terpel.nombre == "TERPEL ESTACION 80" and terpel.odoo_partner_id == 77 and not terpel.activo
    assert proveedores.desactivar(db, [999]) == 0


def test_resolver_contactos_solo_completa_los_que_faltan():
    filas = [proveedores.FilaProveedor("A", "1", 9), proveedores.FilaProveedor("B", "2")]
    vistos = []

    def resolver(nit, nombre):
        vistos.append((nit, nombre))
        return 42

    assert [f.odoo_partner_id for f in proveedores.resolver_contactos(filas, resolver)] == [9, 42]
    assert vistos == [("2", "B")]


def test_writer_busca_contacto_por_nit_y_luego_por_nombre():
    class Partners(FakeObjects):
        def execute_kw(self, db, uid, pw, model, method, args, kwargs):
            assert (model, method) == ("res.partner", "search")
            return [] if args[0][0][0] == "vat" else [15]

    assert _writer(Partners()).buscar_contacto("900", "Terpel") == 15


# ───────────────────────────────────────── contexto y recepción ─────────────────────────────────────

def test_contexto_sin_y_con_vehiculo_reservado(seeded):
    empleado = preop_service.employee_for_telegram(seeded, TG_USER)
    ctx = service.form_context(seeded, empleado, TG_USER)
    assert ctx["vehiculo"] is None and ctx["contrato"] is None and ctx["hoy"] == _hoy().isoformat()
    vehiculo = _reservar(seeded)
    ctx = service.form_context(seeded, empleado, TG_USER, odometro=lambda _id: 1400.0)
    assert ctx["vehiculo"]["id"] == vehiculo and ctx["vehiculo"]["placa"] == "ABC123"
    # Contrato del preoperacional (nombre desde el extracto) y el mayor odómetro conocido (el del preoperacional).
    assert ctx["contrato"] == "Contrato Minera" and ctx["ultimo_km"] == 1510


def test_form_incluye_proveedores_activos(seeded):
    _proveedor(seeded)
    form = service.form_full(seeded)
    assert [p["nombre"] for p in form["proveedores"]] == ["Terpel La 80"] and len(form["tipos"]) == 8


def test_csv_con_ubicacion_y_coma_decimal():
    filas = proveedores.leer_csv("nombre;nit;latitud;longitud;ciudad;direccion\n"
                                 "EDS Uno;1;6,2442;-75,5812;Medellín;Cra 80 # 30-10\nEDS Dos;2;4.711;-74.0721;;\n"
                                 "Sin ubicación;3;;;;\n")
    assert filas[0] == proveedores.FilaProveedor("EDS Uno", "1", None, 6.2442, -75.5812, "Medellín", "Cra 80 # 30-10")
    assert (filas[1].latitud, filas[1].longitud, filas[1].ciudad) == (4.711, -74.0721, None)
    assert filas[2].latitud is None and filas[2].longitud is None


def test_csv_ubicacion_invalida_da_error_por_fila():
    validas, errores = proveedores.analizar_csv(
        "nombre;latitud;longitud\nA;91;-75\nB;6;200\nC;abc;1\nD;6;\nE;6,1;-75,2\n")
    assert [f.nombre for _, f in validas] == ["E"]
    assert [e.linea for e in errores] == [2, 3, 4, 5]
    assert "fuera de rango" in errores[0].mensaje and "juntas" in errores[3].mensaje
    with pytest.raises(ValueError, match="Línea 2"):
        proveedores.leer_csv("nombre;latitud;longitud\nA;91;-75\n")
    # Coma decimal sin comillas en un CSV separado por comas: más columnas que encabezados.
    _, errores = proveedores.analizar_csv("nombre,latitud,longitud\nA,6,2442,-75,5812\n")
    assert errores and "más columnas" in errores[0].mensaje


def test_cargar_guarda_y_actualiza_ubicacion(db):
    Fila = proveedores.FilaProveedor
    proveedores.cargar(db, [Fila("Terpel", "900", None, 6.2, -75.5, "Medellín", "Cra 1")])
    proveedores.cargar(db, [Fila("Terpel", "900", None, None, None, None, "Cra 2")])  # sin ubicación: no la borra
    with db.session() as s:
        p = s.scalar(select(ProveedorCombustible))
        assert (p.latitud, p.longitud, p.ciudad, p.direccion) == (6.2, -75.5, "Medellín", "Cra 2")
    proveedores.cargar(db, [Fila("Terpel", "900", None, 7.0, -76.0)])
    with db.session() as s:
        assert s.scalar(select(ProveedorCombustible.latitud)) == 7.0


def test_form_incluye_ubicacion_de_proveedores(seeded):
    proveedores.cargar(seeded, [proveedores.FilaProveedor("Terpel La 80", "900", 77, 6.2, -75.5, "Medellín", "Cra 80")])
    [p] = service.form_full(seeded)["proveedores"]
    assert (p["latitud"], p["longitud"], p["ciudad"], p["direccion"]) == (6.2, -75.5, "Medellín", "Cra 80")


def test_database_agrega_columnas_de_ubicacion_a_tabla_vieja(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    url = f"sqlite:///{tmp_path / 'vieja.sqlite3'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE proveedores_combustible (id INTEGER PRIMARY KEY, nombre VARCHAR(255), "
                          "clave VARCHAR(255), nit VARCHAR(32), odoo_partner_id INTEGER, activo BOOLEAN, "
                          "created_at TIMESTAMP, updated_at TIMESTAMP)"))
    engine.dispose()
    from app.preop.db import Database

    nueva = Database(url)
    cols = {c["name"] for c in inspect(nueva.engine).get_columns("proveedores_combustible")}
    assert {"latitud", "longitud", "ciudad", "direccion"} <= cols


def test_submit_exige_el_preoperacional_de_la_jornada(seeded):
    with pytest.raises(SubmissionError, match="Preoperacional") as exc:
        service.submit(seeded, IDENTITY, _payload())
    assert exc.value.status_code == 409


def test_submit_rechaza_empleado_desconocido(listo):
    with pytest.raises(SubmissionError) as exc:
        service.submit(listo, TelegramIdentity(1, None, "X"), _payload())
    assert exc.value.status_code == 403


@pytest.mark.parametrize("km, odometro, ok", [
    (1510, None, True),      # igual al del preoperacional: se acepta (mayor o igual)
    (1509, None, False),     # menor al del preoperacional
    (1600, lambda _id: 1700.0, False),  # menor al último de Odoo
])
def test_kilometraje_mayor_o_igual_al_ultimo(listo, km, odometro, ok):
    if ok:
        assert service.submit(listo, IDENTITY, _payload(kilometraje=km), odometro)[0].kilometraje == km
    else:
        with pytest.raises(SubmissionError, match="mayor o igual"):
            service.submit(listo, IDENTITY, _payload(kilometraje=km), odometro)


def test_submit_rechaza_vehiculo_distinto_al_reservado(listo):
    with pytest.raises(SubmissionError, match="reservado") as exc:
        service.submit(listo, IDENTITY, _payload(vehiculo_id=9999))
    assert exc.value.status_code == 409


def test_submit_proveedor_de_la_lista_y_factura_idempotente(listo):
    pid = _proveedor(listo)
    data = _payload(proveedor_texto=None, proveedor_id=pid, para_gastos=True, factura=PDF_B64, factura_nombre="f.pdf")
    first, dup1 = service.submit(listo, IDENTITY, data)
    second, dup2 = service.submit(listo, IDENTITY, data)
    assert (dup1, dup2) == (False, True) and first.id == second.id
    assert (first.proveedor_nombre, first.proveedor_odoo_id, first.contrato_odoo_id) == ("Terpel La 80", 77, 55)
    assert first.total == 150000 and first.preop_id is not None
    with listo.session() as s:
        factura = s.scalar(select(CombustibleFactura))
        assert (factura.contenido, factura.nombre_archivo) == (PDF, "f.pdf")


def test_submit_rechaza_proveedor_desactivado(listo):
    pid = _proveedor(listo)
    proveedores.desactivar(listo, [pid])
    with pytest.raises(SubmissionError, match="lista de aprobados"):
        service.submit(listo, IDENTITY, _payload(proveedor_texto=None, proveedor_id=pid))


def test_finalizar_la_jornada_bloquea_el_registro(listo):
    reservas.finalizar_jornada(listo, TG_USER)
    with pytest.raises(SubmissionError, match="Preoperacional"):
        service.submit(listo, IDENTITY, _payload())


# ────────────────────────────────────────────── cola ──────────────────────────────────────────────

class FakeWriter:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple] = []

    def create_combustible(self, ref, vals, submitted_at, uncertain):
        self.calls.append((ref, vals, uncertain))
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


LAT, LON = 6.25, -75.56


def _con_ubicacion(db, registro_id: int) -> None:
    with db.session() as s:
        row = s.get(Combustible, registro_id)
        row.latitud, row.longitud, row.ubicacion_at = LAT, LON, utc_now()


def _queue(db, **over) -> int:
    registro, _ = service.submit(db, IDENTITY, _payload(**over))
    _con_ubicacion(db, registro.id)  # sin ubicación el registro queda retenido
    assert service.claim_batch(db, 10, 300) == [registro.id]
    return registro.id


def test_envio_con_proveedor_escrito_a_mano_va_en_supplier(listo):
    registro_id = _queue(listo, ref_factura="FE-123")  # un cliente viejo aún puede mandarla
    writer = FakeWriter([(501, "BUS 01/ABC123", False)])
    outcome = service.send_one(listo, writer, registro_id, 8)
    assert outcome.status == SyncStatus.ENVIADO
    _ref, vals, uncertain = writer.calls[0]
    assert uncertain is False
    assert vals == {
        "employee_id": 100, "vehicle_id": 10, "company_id": 1, "date": _hoy().isoformat(),
        "is_fuel_expenses": False, "gallon": 12.5, "price_gallon": 12000.0, "amount": 150000.0,
        "fuel_type": "diesel", "km_tanking": 1600.0, "agreement_id": 55, "supplier": "Estación La 80",
        "lat_origin": LAT, "long_origin": LON,
    }
    assert "ref_invoice" not in vals
    text = formatting.combustible_result(outcome)
    assert "ABC123" in text and "$ 150 000" in text and "#501" in text
    with listo.session() as s:
        row = s.get(Combustible, registro_id)
        assert (row.sync_status, row.odoo_id) == (SyncStatus.ENVIADO, 501)


def test_envio_con_proveedor_de_la_lista_y_factura(listo):
    pid = _proveedor(listo)
    registro_id = _queue(listo, proveedor_texto=None, proveedor_id=pid, factura=PDF_B64)
    writer = FakeWriter([(502, None, False)])
    service.send_one(listo, writer, registro_id, 8)
    vals = writer.calls[0][1]
    assert vals["proveedor"] == 77 and "supplier" not in vals and vals["is_fuel_expenses"] is False
    assert base64.b64decode(vals["image_bill"]) == PDF and vals["fuel_attachment"] is True
    assert vals["attachment_filename"] == "factura.pdf"


def test_proveedor_de_la_lista_sin_contacto_va_como_texto(listo):
    pid = _proveedor(listo, nombre="EDS Norte", nit=None, partner=None)
    registro_id = _queue(listo, proveedor_texto=None, proveedor_id=pid)
    writer = FakeWriter([(503, None, False)])
    service.send_one(listo, writer, registro_id, 8)
    assert writer.calls[0][1]["supplier"] == "EDS Norte" and "proveedor" not in writer.calls[0][1]


def test_error_transitorio_reintenta_y_permanente_falla(listo):
    registro_id = _queue(listo)
    assert service.send_one(listo, FakeWriter([OdooTransientError("t")]), registro_id, 2).status == SyncStatus.ERROR
    assert service.claim_batch(listo, 10, 300, now=utc_now() + timedelta(hours=2)) == [registro_id]
    outcome = service.send_one(listo, FakeWriter([OdooPermanentError("ValidationError: x")]), registro_id, 8)
    assert outcome.status == SyncStatus.FALLIDO and "no pudo guardarse" in formatting.combustible_result(outcome)
    assert service.requeue_failed(listo) == 1


def test_colas_independientes(listo):
    from app.prueba_ruta import service as prueba_service

    _queue(listo)
    assert prueba_service.claim_batch(listo, 10, 300) == []


def test_sender_notifica(listo):
    _con_ubicacion(listo, service.submit(listo, IDENTITY, _payload())[0].id)
    notified = []

    async def notify(outcome):
        notified.append(outcome)

    sender = CombustibleSender(listo, FakeWriter([(1, None, False)]), 10, 8, 300, notify=notify)  # type: ignore[arg-type]
    outcomes = asyncio.run(sender.run_once())
    assert [o.status for o in outcomes] == [SyncStatus.ENVIADO] and notified == outcomes


def test_writer_crea_en_fuel_y_deduplica_por_empleado_vehiculo_y_km():
    objects = FakeObjects(natural_hit=None)
    calls = []
    original = objects.execute_kw

    def spy(db, uid, pw, model, method, args, kwargs):
        calls.append((model, method, args))
        return original(db, uid, pw, model, method, args, kwargs)

    objects.execute_kw = spy  # type: ignore[method-assign]
    vals = {"employee_id": 1, "vehicle_id": 2, "km_tanking": 1600.0}
    _writer(objects).create_combustible(UUID, vals, utc_now(), True)
    assert ("fuel", "create", [vals]) in calls
    [(_, _, search)] = [c for c in calls if c[1] == "search"]
    assert search[0][:3] == [("employee_id", "=", 1), ("vehicle_id", "=", 2), ("km_tanking", "=", 1600.0)]


# ─────────────────────────────────────────────── API ───────────────────────────────────────────────

@pytest.fixture
def api(seeded):
    app = FastAPI()
    app.include_router(combustible_router)
    woke: list[int] = []
    app.state.combustible = CombustibleServices(seeded, BOT_TOKEN, 3600, wake_worker=lambda: woke.append(1))
    return TestClient(app), woke


def _headers(user_id=TG_USER):
    return {"X-Telegram-Init-Data": make_init_data(user_id=user_id)}


def test_api_exige_init_data(api):
    client, _ = api
    assert client.get("/api/combustible/context").status_code == 401
    assert client.post("/api/combustible", json={}).status_code == 401
    assert client.post("/api/combustible", json={}, headers=_headers()).status_code == 422
    assert client.get("/api/combustible/context", headers=_headers(user_id=1)).status_code == 403


def test_api_envio_completo(api, seeded):
    client, woke = api
    _proveedor(seeded)
    ctx = client.get("/api/combustible/context", headers=_headers()).json()
    assert ctx["vehiculo"] is None
    body = _payload().model_dump(mode="json")
    assert client.post("/api/combustible", json=body, headers=_headers()).status_code == 409 and woke == []
    _reservar(seeded)
    proveedores.cargar(seeded, [proveedores.FilaProveedor("Terpel La 80", "900123456", 77, 6.2, -75.5, "Medellín", "Cra 80")])
    [prov] = client.get("/api/combustible/form", headers=_headers()).json()["proveedores"]
    assert prov["nombre"] == "Terpel La 80" and (prov["latitud"], prov["longitud"]) == (6.2, -75.5)
    assert (prov["ciudad"], prov["direccion"]) == ("Medellín", "Cra 80")
    first = client.post("/api/combustible", json=body, headers=_headers())
    assert first.status_code == 202 and first.json()["sync_status"] == "pendiente" and first.json()["total"] == 150000
    assert woke == [1]
    assert client.post("/api/combustible", json=body, headers=_headers()).json()["duplicado"] is True
    assert client.get(f"/api/combustible/{UUID}", headers=_headers()).json()["id"] == first.json()["id"]
    assert client.get(f"/api/combustible/{UUID}", headers=_headers(user_id=1)).status_code == 404


def test_api_kilometraje_menor_responde_422(api, seeded):
    client, woke = api
    _reservar(seeded)
    body = _payload(kilometraje=100).model_dump(mode="json")
    response = client.post("/api/combustible", json=body, headers=_headers())
    assert response.status_code == 422 and "mayor o igual" in response.json()["detail"] and woke == []


def test_pagina_sin_cache_logo_versionado_y_bandeja(api):
    client, _ = api
    page = client.get("/combustible")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
    assert re.findall(r'src="/static/viacotur-logo\.png\?v=([0-9a-f]{12})"', page.text)
    for marca in ('IDB_NAME = "combustible-offline"', "vaciarBandeja()", 'addEventListener("online"',
                  "Primero el Preoperacional", "normalizarDecimal", "application/pdf", "%PDF-"):
        assert marca in page.text
    assert "innerHTML" not in page.text and "toLocaleString" not in page.text


# ───────────────────── envío sin señal (misma regla del preoperacional: máx. 1 h) ─────────────────────

def _diferido_body(*, abierto_hace: timedelta, sin_senal_hace: timedelta, **over: Any) -> dict:
    body = _payload(**over).model_dump(mode="json")
    now = datetime.now(UTC)
    body["diferido"] = {
        "init_data_original": make_init_data(user_id=TG_USER, auth_date=int((now - abierto_hace).timestamp())),
        "sin_senal_desde": (now - sin_senal_hace).isoformat(),
    }
    return body


def test_api_diferido_en_ventana_y_trazabilidad(api, seeded):
    client, woke = api
    _reservar(seeded)
    body = _diferido_body(abierto_hace=timedelta(minutes=80), sin_senal_hace=timedelta(minutes=40))
    headers = {"X-Telegram-Init-Data": body["diferido"]["init_data_original"]}
    assert client.post("/api/combustible", json=body, headers=headers).status_code == 202 and woke == [1]
    with seeded.session() as s:
        registro = s.scalar(select(Combustible))
        traza = s.scalar(select(CombustibleEnvioDiferido))
        assert traza.estado == EstadoDiferido.ACEPTADO and traza.combustible_id == registro.id and traza.payload is None
    # Primer intento que sí llegó: el reenvío responde como duplicado.
    assert client.post("/api/combustible", json=body, headers=headers).json()["duplicado"] is True


def test_api_diferido_vencido_solo_auditoria_con_factura(api, seeded):
    client, woke = api
    _reservar(seeded)
    body = _diferido_body(abierto_hace=timedelta(hours=3), sin_senal_hace=timedelta(minutes=150),
                          para_gastos=True, factura=PDF_B64)
    assert client.post("/api/combustible", json=body, headers=_headers()).status_code == 410 and woke == []
    assert client.post("/api/combustible", json=body, headers=_headers()).status_code == 410
    with seeded.session() as s:
        assert s.scalar(select(func.count()).select_from(Combustible)) == 0
        traza = s.scalar(select(CombustibleEnvioDiferido))
        assert traza.estado == EstadoDiferido.VENCIDO and traza.factura == PDF
        assert "factura" not in traza.payload and traza.payload["proveedor_texto"] == "Estación La 80"


# ───────────────────── «Combustible para gastos» -> hr.expense por REST ─────────────────────

class FakeGastoWriter(FakeWriter):
    def __init__(self, existente: Any = None) -> None:
        super().__init__([])
        self.existente = existente
        self.busquedas: list[dict] = []

    def buscar_gasto_combustible(self, vals):
        self.busquedas.append(vals)
        if isinstance(self.existente, Exception):
            raise self.existente
        return self.existente


class FakeRest:
    def __init__(self, respuesta: Any = None) -> None:
        self.respuesta = {"id": 8001} if respuesta is None else respuesta
        self.posts: list[tuple[str, dict]] = []

    def post_json_once(self, path, body):
        self.posts.append((path, body))
        if isinstance(self.respuesta, Exception):
            raise self.respuesta
        return self.respuesta


def _gasto(db, **over) -> int:
    return _queue(db, para_gastos=True, factura=PDF_B64, observaciones="Tanqueo en ruta", **over)


def test_gasto_envia_el_json_exacto_a_gastos_register_y_no_toca_fuel(listo, monkeypatch):
    monkeypatch.setattr(service, "_product_id_gastos", lambda: PRODUCTO_GASTOS_ODOO_ID)  # sin depender del .env local
    registro_id = _gasto(listo, ref_factura="FE-9")
    writer, rest = FakeGastoWriter(), FakeRest({"id": 8001})
    outcome = service.send_one(listo, writer, registro_id, 8, rest)  # type: ignore[arg-type]
    assert outcome.status == SyncStatus.ENVIADO and outcome.odoo_id == 8001
    assert writer.calls == []  # nunca create_combustible
    [(path, body)] = rest.posts
    assert path == "/api/gastos/register"
    assert body == {
        "name": "COMBUSTIBLE", "product_id": 9629, "total_amount": 150000.0, "employee_id": 100,
        "description": "Tanqueo en ruta", "date": _hoy().isoformat(), "is_fuel_expenses": True,
        "gallon": 12.5, "fuel_type": "diesel", "km_tanking": 1600.0, "type_file": "pdf",
        "attachment_filename": "factura.pdf", "attachment": base64.b64encode(PDF).decode(),
        "ubicacion_gps_telegram": "6.250000,-75.560000", "ubicacion_gps_vehiculo": "GPS no disponible",
    }
    assert "lat_origin" not in body and "long_origin" not in body
    assert PRODUCTO_GASTOS_ODOO_ID == 9629
    assert isinstance(body["total_amount"], float) and isinstance(body["gallon"], float)
    assert writer.busquedas[0]["employee_id"] == 100
    with listo.session() as s:
        row = s.get(Combustible, registro_id)
        assert (row.sync_status, row.odoo_gasto_id, row.odoo_id) == (SyncStatus.ENVIADO, 8001, None)
        assert row.observaciones == "Tanqueo en ruta" and row.proveedor_nombre == "Estación La 80"


def test_gasto_sin_observaciones_envia_description_vacia(listo):
    registro_id = _queue(listo, para_gastos=True, factura=PDF_B64)
    rest = FakeRest()
    service.send_one(listo, FakeGastoWriter(), registro_id, 8, rest)  # type: ignore[arg-type]
    assert rest.posts[0][1]["description"] == ""


def test_observaciones_solo_en_gastos_y_normal_sin_ref_invoice(listo):
    assert _payload(observaciones="algo").observaciones is None
    assert _payload(para_gastos=True, factura=PDF_B64, observaciones="algo").observaciones == "algo"
    with pytest.raises(ValidationError):
        _payload(para_gastos=True, factura=PDF_B64, observaciones="x" * 501)
    registro_id = _queue(listo, observaciones="algo")
    writer = FakeWriter([(1, None, False)])
    service.send_one(listo, writer, registro_id, 8, FakeRest())  # type: ignore[arg-type]
    assert "ref_invoice" not in writer.calls[0][1] and "description" not in writer.calls[0][1]


def test_gasto_existente_no_se_vuelve_a_postear(listo):
    registro_id = _gasto(listo)
    rest = FakeRest()
    outcome = service.send_one(listo, FakeGastoWriter(existente=777), registro_id, 8, rest)  # type: ignore[arg-type]
    assert outcome.status == SyncStatus.ENVIADO and outcome.odoo_id == 777 and rest.posts == []


def test_busqueda_fallida_se_registra_y_se_continua(listo, caplog):
    registro_id = _gasto(listo)
    rest = FakeRest()
    writer = FakeGastoWriter(existente=OdooPermanentError("ValueError: Invalid field km_tanking"))
    with caplog.at_level("WARNING"):
        outcome = service.send_one(listo, writer, registro_id, 8, rest)  # type: ignore[arg-type]
    assert outcome.status == SyncStatus.ENVIADO and len(rest.posts) == 1
    assert "no se pudo verificar duplicados" in caplog.text


def test_busqueda_transitoria_no_postea_y_reintenta(listo):
    registro_id = _gasto(listo)
    rest = FakeRest()
    outcome = service.send_one(listo, FakeGastoWriter(existente=OdooTransientError("sin red")),
                               registro_id, 8, rest)  # type: ignore[arg-type]
    assert outcome.status == SyncStatus.ERROR and rest.posts == []


def test_gasto_4xx_es_definitivo_y_5xx_o_red_reintentable(listo):
    registro_id = _gasto(listo)
    out = service.send_one(listo, FakeGastoWriter(), registro_id, 8,
                           FakeRest(OdooApiError("/api/gastos/register respondió 422: x")))  # type: ignore[arg-type]
    assert out.status == SyncStatus.FALLIDO and "rechazó" in (out.error or "")
    assert service.requeue_failed(listo) == 1
    assert service.claim_batch(listo, 10, 300) == [registro_id]
    out = service.send_one(listo, FakeGastoWriter(), registro_id, 8,
                           FakeRest(_RetryableHttpError("respondió 503")))  # type: ignore[arg-type]
    assert out.status == SyncStatus.ERROR
    assert service.claim_batch(listo, 10, 300, now=utc_now() + timedelta(hours=2)) == [registro_id]
    out = service.send_one(listo, FakeGastoWriter(), registro_id, 8,
                           FakeRest(httpx.ConnectError("sin red")))  # type: ignore[arg-type]
    assert out.status == SyncStatus.ERROR


def test_gasto_sin_cliente_rest_queda_en_espera_sin_consumir_intentos(listo, caplog):
    registro_id = _gasto(listo)
    with caplog.at_level("ERROR"):
        out = service.send_one(listo, FakeGastoWriter(), registro_id, 1, None)
    assert out.status == SyncStatus.ERROR and "ODOO_API_LOGIN" in (out.error or "")
    assert "ODOO_API_LOGIN" in caplog.text
    with listo.session() as s:
        row = s.get(Combustible, registro_id)
        assert row.attempts == 0 and row.next_attempt_at is not None and row.sync_status == SyncStatus.ERROR


def test_gasto_sin_id_en_la_respuesta_se_marca_enviado(listo, caplog):
    registro_id = _gasto(listo)
    with caplog.at_level("WARNING"):
        out = service.send_one(listo, FakeGastoWriter(), registro_id, 8, FakeRest({"ok": True}))  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and out.odoo_id is None and "id reconocible" in caplog.text


def test_extraer_id_gasto_tolerante():
    f = service.extraer_id_gasto
    assert f({"id": 5}) == 5 and f({"expense_id": 6}) == 6 and f({"result": {"id": 7}}) == 7
    assert f({"data": {"id": 8}}) == 8 and f({"response": [{"id": 9}]}) == 9
    assert f({"id": True}) is None and f({"message": "ok"}) is None and f({"id": "x"}) is None


def test_sender_despacha_por_pestana(listo):
    _con_ubicacion(listo, service.submit(listo, IDENTITY, _payload(uuid="1" * 8 + UUID[8:]))[0].id)
    _con_ubicacion(listo, service.submit(listo, IDENTITY, _payload(para_gastos=True, factura=PDF_B64, kilometraje=1700))[0].id)
    writer, rest = FakeWriter([(1, None, False)]), FakeRest()
    sender = CombustibleSender(listo, writer, 10, 8, 300, rest_client=rest)  # type: ignore[arg-type]
    writer.buscar_gasto_combustible = lambda vals: None  # type: ignore[attr-defined]
    outcomes = asyncio.run(sender.run_once())
    assert [o.status for o in outcomes] == [SyncStatus.ENVIADO] * 2
    assert len(writer.calls) == 1 and writer.calls[0][1]["is_fuel_expenses"] is False
    assert len(rest.posts) == 1


def test_writer_busca_gasto_por_llave_natural():
    objects = FakeObjects(natural_hit=321)
    calls = []
    original = objects.execute_kw

    def spy(db, uid, pw, model, method, args, kwargs):
        calls.append((model, method, args))
        return original(db, uid, pw, model, method, args, kwargs)

    objects.execute_kw = spy  # type: ignore[method-assign]
    vals = {"employee_id": 1, "km_tanking": 1600.0, "date": "2026-11-03", "total_amount": 126200.0}
    assert _writer(objects).buscar_gasto_combustible(vals) == 321
    [(model, method, (dominio,))] = calls
    assert (model, method) == ("hr.expense", "search")
    assert dominio == [("employee_id", "=", 1), ("is_fuel_expenses", "=", True), ("km_tanking", "=", 1600.0),
                       ("date", "=", "2026-11-03"), ("total_amount", "=", 126200.0)]


def test_rest_post_json_once_no_reintenta_y_post_json_si():
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/authenticate":
            return httpx.Response(200, json={"token": "a.b.c"})
        hits["n"] += 1
        return httpx.Response(503) if hits["n"] < 3 else httpx.Response(200, json={"id": 1})

    client = OdooRestClient("http://odoo.test", "u", "p", transport=httpx.MockTransport(handler))
    with pytest.raises(_RetryableHttpError):
        client.post_json_once("/api/gastos/register", {})
    assert hits["n"] == 1


def test_columnas_nuevas_se_agregan_a_una_bd_existente(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.preop.db import Database

    url = f"sqlite:///{tmp_path / 'viejo.db'}"
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE combustible (id INTEGER PRIMARY KEY)"))
    eng.dispose()
    database = Database(url)
    columnas = {c["name"] for c in inspect(database.engine).get_columns("combustible")}
    assert {"observaciones", "odoo_gasto_id"} <= columnas


# ───────────────────────── ubicación obligatoria del tanqueo ─────────────────────────

def _recibir(db, uuid_: str = UUID, **over) -> int:
    return service.submit(db, IDENTITY, _payload(uuid=uuid_, **over))[0].id


def _ahora_mas(**kw):
    return utc_now() + timedelta(**kw)


def test_sin_ubicacion_queda_retenido_y_con_ubicacion_se_libera(listo):
    rid = _recibir(listo)
    assert service.claim_batch(listo, 10, 300) == []
    assert service.hay_ubicacion_pendiente(listo, TG_USER)
    res = service.asignar_ubicacion(listo, TG_USER, LAT, LON, _ahora_mas(seconds=5))
    assert res is not None and res.combustible_id == rid and res.pendientes == 0 and res.placa
    assert service.claim_batch(listo, 10, 300) == [rid]
    assert not service.hay_ubicacion_pendiente(listo, TG_USER)
    with listo.session() as s:
        assert s.get(Combustible, rid).ubicacion_at is not None


def test_ubicacion_va_al_mas_antiguo_y_avisa_de_los_pendientes(listo):
    primero = _recibir(listo)
    segundo = _recibir(listo, "2" * 8 + UUID[8:], kilometraje=1700)
    res = service.asignar_ubicacion(listo, TG_USER, LAT, LON, _ahora_mas(seconds=5))
    assert res.combustible_id == primero and res.pendientes == 1
    assert service.claim_batch(listo, 10, 300) == [primero]
    res = service.asignar_ubicacion(listo, TG_USER, 1.0, 2.0, _ahora_mas(seconds=6))
    assert res.combustible_id == segundo and res.pendientes == 0


def test_ubicacion_anterior_al_registro_se_ignora(listo):
    rid = _recibir(listo)
    assert service.asignar_ubicacion(listo, TG_USER, LAT, LON, _ahora_mas(minutes=-5)) is None
    assert service.asignar_ubicacion(listo, 999, LAT, LON, _ahora_mas(seconds=5)) is None
    assert service.claim_batch(listo, 10, 300) == []
    with listo.session() as s:
        assert s.get(Combustible, rid).latitud is None


def test_filas_viejas_sin_ubicacion_requerida_siguen_saliendo(listo):
    rid = _recibir(listo)
    with listo.session() as s:
        s.get(Combustible, rid).ubicacion_requerida = None
    assert service.claim_batch(listo, 10, 300) == [rid]
    with listo.session() as s:
        vals = service.build_odoo_values(s.get(Combustible, rid), None)
    assert "lat_origin" not in vals and "long_origin" not in vals


def test_gasto_sin_ubicacion_no_lleva_lat_long(listo):
    rid = _recibir(listo, para_gastos=True, factura=PDF_B64)
    with listo.session() as s:
        fac = s.scalar(select(CombustibleFactura).where(CombustibleFactura.combustible_id == rid))
        assert "lat_origin" not in service.build_gasto_payload(s.get(Combustible, rid), fac)


def test_recordatorios_a_los_10_y_30_minutos_sin_repetir(listo):
    rid = _recibir(listo)

    def numeros(**kw):
        return [r.numero for r in service.recordatorios_pendientes(listo, _ahora_mas(**kw))]

    assert numeros(minutes=9) == []
    assert numeros(minutes=11) == [1]
    assert numeros(minutes=12) == []
    assert numeros(minutes=29) == []
    assert numeros(minutes=31) == [2]
    assert numeros(minutes=500) == []
    with listo.session() as s:
        assert s.get(Combustible, rid).recordatorios_ubicacion == 2


def test_recordatorio_no_se_envia_si_ya_hay_ubicacion(listo):
    _con_ubicacion(listo, _recibir(listo))
    assert service.recordatorios_pendientes(listo, _ahora_mas(minutes=40)) == []


def test_sender_envia_recordatorios_y_tolera_fallos_de_telegram(listo):
    rid = _recibir(listo)
    sent = []

    async def remind(r):
        sent.append(r.numero)
        raise RuntimeError("telegram caído")

    sender = CombustibleSender(listo, FakeWriter([]), 10, 8, 300, remind=remind)  # type: ignore[arg-type]
    with listo.session() as s:
        s.get(Combustible, rid).fecha = utc_now() - timedelta(minutes=11)
    assert asyncio.run(sender.run_once()) == [] and sent == [1]
    assert asyncio.run(sender.run_once()) == [] and sent == [1]


def test_api_pide_la_ubicacion_al_aceptar_un_registro_nuevo(seeded):
    app = FastAPI()
    app.include_router(combustible_router)
    pedidos = []

    async def on_accepted(registro):
        # Como en main.py: el registro llega desligado de la sesión; la placa se lee aparte.
        assert service.placa_de(seeded, registro.id) == "ABC123"
        pedidos.append(registro.id)

    app.state.combustible = CombustibleServices(seeded, BOT_TOKEN, 3600, on_accepted=on_accepted)
    client = TestClient(app)
    _reservar(seeded)
    body = _payload().model_dump(mode="json")
    first = client.post("/api/combustible", json=body, headers=_headers())
    assert first.status_code == 202 and pedidos == [first.json()["id"]]
    assert client.post("/api/combustible", json=body, headers=_headers()).json()["duplicado"] is True
    assert len(pedidos) == 1  # el duplicado no vuelve a pedirla
    assert first.json()["sync_status"] == "pendiente"
    assert service.claim_batch(seeded, 10, 300) == []


def test_migracion_agrega_columnas_de_ubicacion(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.preop.db import Database

    url = f"sqlite:///{tmp_path / 'viejo2.db'}"
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE combustible (id INTEGER PRIMARY KEY)"))
    eng.dispose()
    cols = {c["name"] for c in inspect(Database(url).engine).get_columns("combustible")}
    assert {"latitud", "longitud", "ubicacion_at", "ubicacion_requerida", "recordatorios_ubicacion"} <= cols


# ───────────────────────── cotejo con el GPS de la flota ─────────────────────────

ITEM = {"vehiculo_id": "27407752", "timestamp": "2026-11-26T09:26:44-05:00",
        "coordenadas": {"lat": 6.68136055555, "long": -75.9113777778},
        "matricula": "Agrale/Ma 8.7 Euro V/EQX557", "velocidad": 29.0}


def test_placa_desde_matricula_y_normalizacion():
    assert gps.placa_desde_matricula("Agrale/Ma 8.7 Euro V/EQX557") == "EQX557"
    assert gps.placa_desde_matricula("eqx-557") == "EQX557"
    assert gps.placa_desde_matricula(None) == "" and gps.normalizar_placa(" eq x-557 ") == "EQX557"


def test_haversine_valor_conocido():
    assert gps.haversine_m(0, 0, 1, 0) == pytest.approx(111195, abs=5)
    assert gps.haversine_m(6.2, -75.5, 6.2, -75.5) == 0


def test_formatos_de_texto():
    assert gps.texto_telegram(6.167371, -75.641203) == "6.167371,-75.641203"
    pos = gps.PosicionGps(6.1675, -75.641, datetime(2026, 11, 26, 14, 4, 30, tzinfo=UTC))
    texto = gps.texto_vehiculo(pos, 6.167371, -75.641203)
    assert texto.startswith("6.167500,-75.641000 · 2026-11-26 09:04 · ") and texto.endswith(" m")
    assert re.fullmatch(r"6\.167500,-75\.641000 · 2026-11-26 09:04 · \d+ m", texto)
    assert gps.distancia_m(pos, 6.1675, -75.641) == 0
    assert gps.texto_vehiculo(None, 1, 2) == "GPS no disponible"


@pytest.mark.parametrize("cuerpo", [
    [ITEM], {"data": [ITEM]}, {"items": [ITEM]}, {"vehiculos": [ITEM]}, {"flota": [ITEM]},
    {"data": {"items": [ITEM]}},
])
def test_envolturas_aceptadas(cuerpo):
    pos = gps.buscar_posicion(cuerpo, "eqx 557")
    assert pos is not None and pos.latitud == pytest.approx(6.68136055555)


def test_varias_coincidencias_gana_la_mas_reciente_y_casos_raros():
    viejo = ITEM | {"timestamp": "2026-11-25T09:26:44-05:00", "coordenadas": {"lat": 1.0, "long": 2.0}}
    assert gps.buscar_posicion([viejo, ITEM], "EQX557").latitud == pytest.approx(6.68136055555)
    assert gps.buscar_posicion([ITEM], "ZZZ000") is None
    assert gps.buscar_posicion([ITEM], "") is None
    for raro in ({"ok": 1}, "texto", [None, 3], [ITEM | {"coordenadas": None}], [ITEM | {"timestamp": "ayer"}],
                 [ITEM | {"coordenadas": {"lat": "x", "long": 1}}], [ITEM | {"coordenadas": {"lat": 99, "long": 1}}]):
        assert gps.buscar_posicion(raro, "EQX557") is None


def _cliente(handler, **kw):
    return gps.FlotaGpsClient("http://gps.test/api/v1/external/flota", "SECRETA", 5,
                              transport=httpx.MockTransport(handler), **kw)


def test_cliente_envia_x_api_key_y_no_filtra_la_clave_en_logs(caplog):
    vistos = []

    def handler(request):
        vistos.append(request)
        return httpx.Response(200, json=[ITEM])

    with caplog.at_level("DEBUG"):
        assert _cliente(handler).consultar("EQX557") is not None
        assert _cliente(lambda r: httpx.Response(500)).consultar("EQX557") is None
    assert vistos[0].headers["x-api-key"] == "SECRETA" and str(vistos[0].url).endswith("/external/flota")
    assert "SECRETA" not in caplog.text


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(401), lambda r: httpx.Response(200, text="no json"),
    lambda r: httpx.Response(200, json={"x": 1}), lambda r: httpx.Response(200, json=[]),
])
def test_cliente_devuelve_none_ante_cualquier_fallo(handler):
    assert _cliente(handler).consultar("EQX557") is None


def test_cliente_red_caida():
    def handler(request):
        raise httpx.ConnectError("sin red")

    assert _cliente(handler).consultar("EQX557") is None


def test_build_gps_client_segun_configuracion(caplog):
    from types import SimpleNamespace

    from pydantic import SecretStr

    base = {"gps_flota_url": "", "gps_flota_api_key": None, "gps_flota_timeout_seconds": 10}
    with caplog.at_level("WARNING"):
        assert gps.build_gps_client(SimpleNamespace(**base)) is None
        assert gps.build_gps_client(SimpleNamespace(**base | {"gps_flota_url": "http://x"})) is None
    assert "sin configurar" in caplog.text
    ok = SimpleNamespace(**base | {"gps_flota_url": "http://x", "gps_flota_api_key": SecretStr("k")})
    assert gps.build_gps_client(ok) is not None


class FakeGps:
    def __init__(self, resultado):
        self.resultado, self.llamadas = resultado, []

    def consultar(self, placa):
        self.llamadas.append(placa)
        if isinstance(self.resultado, Exception):
            raise self.resultado
        return self.resultado


def test_gasto_lleva_cotejo_gps_y_se_consulta_una_sola_vez(listo):
    registro_id = _gasto(listo)
    pos = gps.PosicionGps(6.2501, -75.5601, datetime(2026, 11, 26, 14, 4, tzinfo=UTC))
    fake = FakeGps(pos)
    rest = FakeRest(_RetryableHttpError("respondió 503"))  # fallo reintentable: la cola reintenta
    out = service.send_one(listo, FakeGastoWriter(), registro_id, 8, rest, fake)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ERROR and len(fake.llamadas) == 1
    assert service.claim_batch(listo, 10, 300, now=utc_now() + timedelta(hours=2)) == [registro_id]
    rest.respuesta = {"id": 1}
    out = service.send_one(listo, FakeGastoWriter(), registro_id, 8, rest, fake)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and len(fake.llamadas) == 1  # no se vuelve a consultar
    body = rest.posts[-1][1]
    assert body["ubicacion_gps_telegram"] == "6.250000,-75.560000"
    assert re.fullmatch(r"6\.250100,-75\.560100 · 2026-11-26 09:04 · \d+ m", body["ubicacion_gps_vehiculo"])
    assert "lat_origin" not in body and "long_origin" not in body
    with listo.session() as s:
        row = s.get(Combustible, registro_id)
        assert row.gps_distancia_m is not None and row.gps_at is not None and row.gps_latitud == 6.2501
    assert len(fake.llamadas) == 1


@pytest.mark.parametrize("gps_cliente", [None, FakeGps(None), FakeGps(RuntimeError("boom"))])
def test_gps_no_disponible_no_retiene_el_gasto(listo, gps_cliente):
    registro_id = _gasto(listo)
    rest = FakeRest()
    out = service.send_one(listo, FakeGastoWriter(), registro_id, 8, rest, gps_cliente)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO
    assert rest.posts[0][1]["ubicacion_gps_vehiculo"] == "GPS no disponible"
    with listo.session() as s:
        row = s.get(Combustible, registro_id)
        assert row.ubicacion_gps_vehiculo == "GPS no disponible" and row.gps_distancia_m is None


def test_fuel_sigue_con_lat_origin_y_no_consulta_gps(listo):
    registro_id = _queue(listo)
    writer, fake = FakeWriter([(1, None, False)]), FakeGps(None)
    service.send_one(listo, writer, registro_id, 8, None, fake)  # type: ignore[arg-type]
    vals = writer.calls[0][1]
    assert vals["lat_origin"] == LAT and vals["long_origin"] == LON
    assert "ubicacion_gps_telegram" not in vals and fake.llamadas == []


def test_migracion_agrega_columnas_gps(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.preop.db import Database

    url = f"sqlite:///{tmp_path / 'viejo3.db'}"
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE combustible (id INTEGER PRIMARY KEY)"))
    eng.dispose()
    cols = {c["name"] for c in inspect(Database(url).engine).get_columns("combustible")}
    assert {"gps_latitud", "gps_longitud", "gps_at", "gps_distancia_m", "ubicacion_gps_vehiculo"} <= cols


def test_product_id_de_gastos_sale_de_la_configuracion(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("COMBUSTIBLE_GASTOS_PRODUCT_ID", "77")
    get_settings.cache_clear()
    try:
        assert service._product_id_gastos() == 77
    finally:
        get_settings.cache_clear()


# ─────────────────────── kilometraje mínimo: tanqueos previos y regla estricta ───────────────────────

def _previo(db, km: float = 12600.0, **over) -> int:
    """Tanqueo previo del mismo vehículo (queda como pendiente local)."""
    return _recibir(db, UUID[:-1] + "9", kilometraje=km, **over)


@pytest.mark.parametrize("gastos", [False, True])
def test_km_debe_ser_estrictamente_mayor_que_el_tanqueo_local_previo(listo, gastos):
    extra = {"para_gastos": True, "factura": PDF_B64} if gastos else {}
    _previo(listo)
    for km in (12560, 12600):
        with pytest.raises(SubmissionError, match="mayor que") as exc:
            service.submit(listo, IDENTITY, _payload(kilometraje=km, **extra))
        assert exc.value.status_code == 422
    assert service.submit(listo, IDENTITY, _payload(kilometraje=12601, **extra))[0].kilometraje == 12601


def test_tanqueo_local_fallido_no_cuenta(listo):
    previo = _previo(listo)
    with listo.session() as s:
        s.get(Combustible, previo).sync_status = SyncStatus.FALLIDO
    assert service.submit(listo, IDENTITY, _payload(kilometraje=1600))[1] is False


@pytest.mark.parametrize("gastos", [False, True])
def test_km_de_fuel_en_odoo_cuenta_en_ambas_pestanas(listo, gastos):
    extra = {"para_gastos": True, "factura": PDF_B64} if gastos else {}
    with pytest.raises(SubmissionError, match="mayor que"):
        service.submit(listo, IDENTITY, _payload(kilometraje=2000, **extra), None, km_fuel=lambda _v: 2000.0)
    assert service.submit(listo, IDENTITY, _payload(kilometraje=2001, **extra), None,
                          km_fuel=lambda _v: 2000.0)[0].kilometraje == 2001


def test_km_de_gastos_del_empleado_solo_cuenta_en_la_pestana_gastos(listo):
    vistos = []

    def km_gastos(empleado_odoo_id):
        vistos.append(empleado_odoo_id)
        return 5000.0

    # pestaña Combustible: los hr.expense no cuentan
    assert service.submit(listo, IDENTITY, _payload(kilometraje=1600), None, km_gastos=km_gastos)[1] is False
    with pytest.raises(SubmissionError, match="mayor que"):
        service.submit(listo, IDENTITY, _payload(uuid="4" * 8 + UUID[8:], kilometraje=5000, para_gastos=True,
                                                   factura=PDF_B64), None, km_gastos=km_gastos)
    assert vistos and set(vistos) == {100}


def test_odometro_y_preop_siguen_aceptando_igual_pero_el_tanqueo_exige_mayor(listo):
    assert service.submit(listo, IDENTITY, _payload(kilometraje=1510))[0].kilometraje == 1510  # igual al preop
    with pytest.raises(SubmissionError, match="mayor que"):  # y ahora el tanqueo previo (1510) es estricto
        service.submit(listo, IDENTITY, _payload(uuid="5" * 8 + UUID[8:], kilometraje=1510))


def test_reenvio_del_mismo_uuid_no_falla_por_su_propio_km(listo):
    primero, _ = service.submit(listo, IDENTITY, _payload(kilometraje=1700))
    again, duplicado = service.submit(listo, IDENTITY, _payload(kilometraje=1700))
    assert duplicado is True and again.id == primero.id


def test_contexto_devuelve_limites_por_pestana(listo, caplog):
    _previo(listo, 12600.0)
    with listo.session() as s:
        from app.preop.models import Empleado
        empleado = s.scalar(select(Empleado).where(Empleado.codigo_pin == str(TG_USER)))
    ctx = service.form_context(listo, empleado, TG_USER, None, lambda _v: None, lambda _e: 13000.0)
    assert ctx["ultimo_km"] == 12600 and ctx["ultimo_km_estricto"] is True
    assert ctx["ultimo_km_gastos"] == 13000 and ctx["ultimo_km_gastos_estricto"] is True
    sin_lecturas = service.form_context(listo, empleado, TG_USER)
    assert sin_lecturas["ultimo_km_gastos"] == 12600


def test_lecturas_de_km_en_odoo_toleran_fallos_y_filtran_bien(caplog):
    objects = FakeObjects()
    visto = []

    def execute(db, uid, pw, model, method, args, kwargs):
        visto.append((model, args[0], kwargs))
        return [{"km_tanking": 777.0}]

    objects.execute_kw = execute  # type: ignore[method-assign]
    w = _writer(objects)
    assert w.ultimo_km_fuel(10) == 777.0 and w.ultimo_km_gastos(100) == 777.0
    assert visto[0][:2] == ("fuel", [["vehicle_id", "=", 10]])
    assert visto[1][:2] == ("hr.expense", [["employee_id", "=", 100], ["is_fuel_expenses", "=", True]])
    assert visto[1][2]["order"] == "km_tanking desc" and visto[1][2]["limit"] == 1

    def falla(db, uid, pw, model, method, args, kwargs):
        raise TimeoutError("timed out")

    objects.execute_kw = falla  # type: ignore[method-assign]
    with caplog.at_level("WARNING"):
        assert w.ultimo_km_fuel(10) is None and w.ultimo_km_gastos(100) is None
    assert "No se pudo leer el último km" in caplog.text
