"""Pruebas del registro de combustible: esquema, proveedores, vehículo reservado, kilometraje, cola, conector,
API y envío sin señal."""
from __future__ import annotations

import asyncio
import base64
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app import formatting
from app.combustible import proveedores, service
from app.combustible.catalogo import TIPOS_COMBUSTIBLE, form_definition
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


def _queue(db, **over) -> int:
    registro, _ = service.submit(db, IDENTITY, _payload(**over))
    assert service.claim_batch(db, 10, 300) == [registro.id]
    return registro.id


def test_envio_con_proveedor_escrito_a_mano_va_en_supplier(listo):
    registro_id = _queue(listo, ref_factura="FE-123")
    writer = FakeWriter([(501, "BUS 01/ABC123", False)])
    outcome = service.send_one(listo, writer, registro_id, 8)
    assert outcome.status == SyncStatus.ENVIADO
    _ref, vals, uncertain = writer.calls[0]
    assert uncertain is False
    assert vals == {
        "employee_id": 100, "vehicle_id": 10, "company_id": 1, "date": _hoy().isoformat(),
        "is_fuel_expenses": False, "gallon": 12.5, "price_gallon": 12000.0, "amount": 150000.0,
        "fuel_type": "diesel", "km_tanking": 1600.0, "agreement_id": 55, "supplier": "Estación La 80",
        "ref_invoice": "FE-123",
    }
    text = formatting.combustible_result(outcome)
    assert "ABC123" in text and "$ 150 000" in text and "#501" in text
    with listo.session() as s:
        row = s.get(Combustible, registro_id)
        assert (row.sync_status, row.odoo_id) == (SyncStatus.ENVIADO, 501)


def test_envio_con_proveedor_de_la_lista_y_factura(listo):
    pid = _proveedor(listo)
    registro_id = _queue(listo, proveedor_texto=None, proveedor_id=pid, para_gastos=True, factura=PDF_B64)
    writer = FakeWriter([(502, None, False)])
    service.send_one(listo, writer, registro_id, 8)
    vals = writer.calls[0][1]
    assert vals["proveedor"] == 77 and "supplier" not in vals and vals["is_fuel_expenses"] is True
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
    service.submit(listo, IDENTITY, _payload())
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
    assert client.get("/api/combustible/form", headers=_headers()).json()["proveedores"][0]["nombre"] == "Terpel La 80"
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
