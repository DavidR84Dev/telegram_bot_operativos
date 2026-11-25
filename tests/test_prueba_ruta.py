"""Pruebas de la Prueba de Ruta: acceso por acuerdo, vehículo reservado, fotos, cola, conector, API y envío sin señal."""
from __future__ import annotations

import asyncio
import base64
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app import formatting
from app.config import Settings
from app.preop import etl, reservas
from app.preop import service as preop_service
from app.preop.models import (
    EstadoDiferido,
    PruebaRuta,
    PruebaRutaEnvioDiferido,
    PruebaRutaFoto,
    SyncStatus,
    utc_now,
)
from app.preop.odoo_writer import OdooPermanentError, OdooTransientError
from app.preop.service import SubmissionError, TelegramIdentity
from app.prueba_ruta import service
from app.prueba_ruta.acuerdos import (
    CONTRATO_NO_AUTORIZADO,
    acuerdos_sin_sincronizar,
    contrato_autorizado,
    normalizar_acuerdo,
    normalizar_lista,
)
from app.prueba_ruta.catalogo import PREGUNTAS, PROCEDIMIENTO, form_definition
from app.prueba_ruta.router import PruebaRutaServices
from app.prueba_ruta.router import router as prueba_ruta_router
from app.prueba_ruta.schemas import PruebaRutaIn
from app.prueba_ruta.worker import PruebaRutaSender

from . import test_preop as _preop
from .helpers import BOT_TOKEN, make_init_data
from .test_preop import JPEG, FakeObjects, FakeRest, TG_USER, _empleado, _extracto, _flota, _ids, _writer

db = _preop.db
seeded = _preop.seeded

UUID = "2b3c4d5e-3c1d-4f7a-9a2b-1c2d3e4f5a6b"
IDENTITY = TelegramIdentity(TG_USER, "ana", "Ana C")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
FOTO1 = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
FOTO2 = "data:image/png;base64," + base64.b64encode(PNG).decode()
AUTORIZADOS = normalizar_lista(Settings.model_fields["prueba_ruta_acuerdos"].default.split(";"))
CAMPOS_ODOO = {"direccion", "frenos", "suspencion", "pedales", "parqueo", "reductor"}


def _respuestas(**over: str) -> dict[str, str]:
    return {p.key: "no" for p in PREGUNTAS} | over


def _payload(**over: Any) -> PruebaRutaIn:
    data: dict[str, Any] = {"uuid": UUID, "respuestas": _respuestas(), "km1": FOTO1, "km2": FOTO2}
    data.update(over)
    return PruebaRutaIn.model_validate(data)


def _con_acuerdo(db, nombre: str = "[New] MINERA DE COBRE QUEBRADONA", jefe_pin: str = "999") -> None:
    """El contrato del empleado (id 55) es el acuerdo ``nombre`` en la tabla de acuerdos."""
    etl.sync_all(db, FakeRest({
        "/api/flota": [_flota(), _flota(11, "VAN 02/XYZ987")],
        "/api/empleados": [_empleado(), _empleado(101, pin=jefe_pin, responsable="", nombre="Jefe Uno")],
        "/api/extractos": [_extracto()],
        "/api/acuerdos": [{"id": 55, "nombre": nombre, "cliente": "X", "company_id": "Viacotur"}],
    }), [1])


def _reservar(db) -> int:
    """Preoperacional del conductor: reserva el vehículo de la jornada. Devuelve el id local del vehículo."""
    vehiculo, extracto = _ids(db)
    preop_service.submit(db, preop_service.TelegramIdentity(TG_USER, "ana", "Ana C"),
                         _preop._payload(vehiculo, extracto, uuid="9" * 8 + "-3c1d-4f7a-9a2b-1c2d3e4f5a6b"))
    return vehiculo


@pytest.fixture
def listo(seeded):
    """Conductor autorizado (Quebradona) con el vehículo reservado por su preoperacional."""
    _con_acuerdo(seeded)
    _reservar(seeded)
    return seeded


# ─────────────────────────────────────── catálogo y esquema ───────────────────────────────────────

def test_catalogo_coincide_con_odoo_y_la_referencia():
    assert {p.key for p in PREGUNTAS} == CAMPOS_ODOO and all(p.obligatorio for p in PREGUNTAS)
    assert len(PROCEDIMIENTO) == 13  # los 13 pasos del bot de referencia
    form = form_definition()
    assert [p["bloque"] for p in form["preguntas"]] == [1, 1, 1, 2, 2, 2]
    assert [f["key"] for f in form["fotos"]] == ["km1", "km2"] and all(f["obligatorio"] for f in form["fotos"])


@pytest.mark.parametrize("cambio, error", [
    ({"respuestas": {k: v for k, v in _respuestas().items() if k != "parqueo"}}, "freno de parqueo"),
    ({"respuestas": _respuestas(frenos="conforme")}, "Valor inválido"),
    ({"km1": None}, "km1"),
    ({"km2": "data:image/gif;base64," + base64.b64encode(b"GIF89a" + b"\x00" * 30).decode()}, "JPEG o PNG"),
    ({"telegram_user_id": 1}, "telegram_user_id"),
])
def test_esquema_exige_preguntas_y_ambas_fotos(cambio, error):
    data = {"uuid": UUID, "respuestas": _respuestas(), "km1": FOTO1, "km2": FOTO2} | cambio
    data = {k: v for k, v in data.items() if v is not None}
    with pytest.raises(ValidationError, match=error):
        PruebaRutaIn.model_validate(data)


# ───────────────────────────────────────── acuerdos autorizados ─────────────────────────────────────

@pytest.mark.parametrize("nombre", [
    "MINERA DE COBRE QUEBRADONA", "minera de cobre quebradona", "[New] Minera de Cobre  Quebradona",
    "Servicios Hidrogeológicos Integrales S.A.S.", "SERVICIOS HIDROGEOLOGICOS INTEGRALES SAS",
])
def test_normalizacion_de_nombres_autorizados(nombre):
    assert normalizar_acuerdo(nombre) in AUTORIZADOS


def test_lista_configurable_con_n_acuerdos():
    settings = Settings(odoo_url="http://x", odoo_db="d", odoo_username="u", odoo_password="p",
                        telegram_bot_token="1:x", webapp_base_url="https://a", scanner_base_url="https://b",
                        prueba_ruta_acuerdos=" Minera de Cobre Quebradona ; Integral ;; Tercero S.A. ")
    assert settings.prueba_ruta_acuerdos_lista == ["Minera de Cobre Quebradona", "Integral", "Tercero S.A."]
    assert normalizar_lista(settings.prueba_ruta_acuerdos_lista) == {
        "MINERA DE COBRE QUEBRADONA", "INTEGRAL", "TERCERO SA"}


def test_contrato_autorizado_por_id_del_acuerdo_o_por_nombre(seeded):
    from app.preop.service import employee_for_telegram

    empleado = employee_for_telegram(seeded, TG_USER)
    with seeded.session() as s:
        assert contrato_autorizado(s, empleado, AUTORIZADOS) is None  # "Contrato Minera" no está en la lista
        assert contrato_autorizado(s, empleado, normalizar_lista(["contrato minera"])).odoo_id == 55
    _con_acuerdo(seeded)  # el id 55 es "[New] MINERA DE COBRE QUEBRADONA"
    with seeded.session() as s:
        autorizado = contrato_autorizado(s, empleado, AUTORIZADOS)
        assert autorizado is not None and autorizado.odoo_id == 55
        assert acuerdos_sin_sincronizar(s, AUTORIZADOS) == ["SERVICIOS HIDROGEOLOGICOS INTEGRALES SAS"]


def test_contrato_temporal_tambien_autoriza(seeded):
    etl.sync_all(seeded, FakeRest({
        "/api/flota": [_flota()],
        "/api/empleados": [_empleado(contrato_temporal_id=132,
                                     contrato_temporal="SERVICIOS HIDROGEOLÓGICOS INTEGRALES S.A.S.")],
        "/api/extractos": [_extracto()],
    }), [1])
    from app.preop.service import employee_for_telegram

    with seeded.session() as s:
        autorizado = contrato_autorizado(s, employee_for_telegram(seeded, TG_USER), AUTORIZADOS)
    assert autorizado is not None and autorizado.odoo_id == 132


# ───────────────────────────────────────── contexto y recepción ─────────────────────────────────────

def test_contexto_contrato_no_autorizado(seeded):
    from app.preop.service import employee_for_telegram

    ctx = service.form_context(seeded, employee_for_telegram(seeded, TG_USER), TG_USER, AUTORIZADOS)
    assert ctx["autorizado"] is False and ctx["mensaje"] == CONTRATO_NO_AUTORIZADO and ctx["vehiculo"] is None
    assert ctx["empleado"]["contrato"] == "Contrato Minera"


def test_contexto_autorizado_sin_y_con_vehiculo_reservado(seeded):
    from app.preop.service import employee_for_telegram

    _con_acuerdo(seeded)
    empleado = employee_for_telegram(seeded, TG_USER)
    ctx = service.form_context(seeded, empleado, TG_USER, AUTORIZADOS)
    assert ctx["autorizado"] is True and ctx["vehiculo"] is None
    vehiculo = _reservar(seeded)
    ctx = service.form_context(seeded, empleado, TG_USER, AUTORIZADOS)
    assert ctx["vehiculo"]["id"] == vehiculo and ctx["vehiculo"]["placa"]
    assert ctx["contrato"] == "Contrato Minera"


def test_submit_rechaza_contrato_no_autorizado(seeded):
    _reservar(seeded)
    with pytest.raises(SubmissionError, match=CONTRATO_NO_AUTORIZADO) as exc:
        service.submit(seeded, IDENTITY, _payload(), AUTORIZADOS)
    assert exc.value.status_code == 403


def test_submit_exige_el_preoperacional_de_la_jornada(seeded):
    _con_acuerdo(seeded)
    with pytest.raises(SubmissionError, match="Preoperacional") as exc:
        service.submit(seeded, IDENTITY, _payload(), AUTORIZADOS)
    assert exc.value.status_code == 409


def test_submit_rechaza_empleado_desconocido(listo):
    with pytest.raises(SubmissionError) as exc:
        service.submit(listo, TelegramIdentity(1, None, "X"), _payload(), AUTORIZADOS)
    assert exc.value.status_code == 403


def test_submit_guarda_fotos_y_es_idempotente(listo):
    first, dup1 = service.submit(listo, IDENTITY, _payload(respuestas=_respuestas(frenos="si")), AUTORIZADOS)
    second, dup2 = service.submit(listo, IDENTITY, _payload(), AUTORIZADOS)
    assert (dup1, dup2) == (False, True) and first.id == second.id
    assert first.no_conformes == 1 and first.contrato_odoo_id == 55 and first.preop_id is not None
    with listo.session() as s:
        fotos = {f.tipo: (f.mime, f.contenido) for f in s.scalars(select(PruebaRutaFoto))}
    assert fotos == {"km1": ("image/jpeg", JPEG), "km2": ("image/png", PNG)}


def test_submit_rechaza_vehiculo_distinto_al_reservado(listo):
    with pytest.raises(SubmissionError, match="reservado") as exc:
        service.submit(listo, IDENTITY, _payload(vehiculo_id=9999), AUTORIZADOS)
    assert exc.value.status_code == 409


def test_finalizar_la_jornada_bloquea_la_prueba(listo):
    reservas.finalizar_jornada(listo, TG_USER)
    with pytest.raises(SubmissionError, match="Preoperacional"):
        service.submit(listo, IDENTITY, _payload(), AUTORIZADOS)


# ────────────────────────────────────────────── cola ──────────────────────────────────────────────

class FakeWriter:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple] = []

    def create_prueba_ruta(self, ref, vals, submitted_at, uncertain):
        self.calls.append((ref, vals, uncertain))
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _queue(db, **over) -> int:
    prueba, _ = service.submit(db, IDENTITY, _payload(**over), AUTORIZADOS)
    assert service.claim_batch(db, 10, 300) == [prueba.id]
    return prueba.id


def test_envio_exitoso_arma_los_valores_de_odoo(listo):
    prueba_id = _queue(listo, observaciones="Sin ruidos")
    writer = FakeWriter([(99, "PRE-000009931", False)])
    outcome = service.send_one(listo, writer, prueba_id, 8)
    assert outcome.status == SyncStatus.ENVIADO and outcome.aviso_jefe is None
    _ref, vals, uncertain = writer.calls[0]
    assert uncertain is False
    assert vals["user_id"] == 100 and vals["vehicle_id"] == 10 and vals["jefe_inmediato"] == 101
    assert vals["company_id"] == 1 and vals["observaciones"] == "Sin ruidos"
    assert {k: vals[k] for k in CAMPOS_ODOO} == _respuestas()
    assert base64.b64decode(vals["km1"]) == JPEG and base64.b64decode(vals["km2"]) == PNG
    assert "state" not in vals and "plan_accion" not in vals and "contrato_id" not in vals
    with listo.session() as s:
        row = s.get(PruebaRuta, prueba_id)
        assert (row.sync_status, row.odoo_id, row.odoo_name) == (SyncStatus.ENVIADO, 99, "PRE-000009931")


def test_novedades_avisan_al_jefe(listo):
    prueba_id = _queue(listo, respuestas=_respuestas(frenos="si", reductor="si"))
    outcome = service.send_one(listo, FakeWriter([(100, "PRE-100", False)]), prueba_id, 8)
    aviso = outcome.aviso_jefe
    assert aviso is not None and aviso.telegram_id == 999
    assert aviso.novedades == ("Ruido en los frenos delanteros", "Ruido en la suspensión tras el reductor")
    text = formatting.prueba_ruta_aviso_jefe(aviso, outcome.odoo_name)
    assert "Ana Conductora" in text and "PRE-100" in text and "ABC123" in text
    assert "jefe inmediato" in formatting.prueba_ruta_result(outcome)


def test_error_transitorio_reintenta_y_permanente_falla(listo):
    prueba_id = _queue(listo)
    assert service.send_one(listo, FakeWriter([OdooTransientError("t")]), prueba_id, 2).status == SyncStatus.ERROR
    assert service.claim_batch(listo, 10, 300, now=utc_now() + timedelta(hours=2)) == [prueba_id]
    outcome = service.send_one(listo, FakeWriter([OdooPermanentError("ValidationError: x")]), prueba_id, 8)
    assert outcome.status == SyncStatus.FALLIDO and "no pudo registrarse" in formatting.prueba_ruta_result(outcome)
    assert service.requeue_failed(listo) == 1


def test_colas_independientes(listo):
    from app.ast_trabajo import service as ast_service

    _queue(listo)
    assert ast_service.claim_batch(listo, 10, 300) == []


def test_sender_notifica(listo):
    service.submit(listo, IDENTITY, _payload(), AUTORIZADOS)
    notified = []

    async def notify(outcome):
        notified.append(outcome)

    sender = PruebaRutaSender(listo, FakeWriter([(1, "PRE-1", False)]), 10, 8, 300, notify=notify)  # type: ignore[arg-type]
    assert [o.status for o in asyncio.run(sender.run_once())] == [SyncStatus.ENVIADO]
    assert notified[0].telegram_user_id == TG_USER


def test_writer_crea_en_prueba_ruta_y_deduplica_por_empleado_y_vehiculo():
    objects = FakeObjects(natural_hit=None)
    calls = []
    original = objects.execute_kw

    def spy(db, uid, pw, model, method, args, kwargs):
        calls.append((model, method, args))
        return original(db, uid, pw, model, method, args, kwargs)

    objects.execute_kw = spy  # type: ignore[method-assign]
    vals = {"user_id": 1, "vehicle_id": 2}
    _writer(objects).create_prueba_ruta(UUID, vals, utc_now(), True)
    assert ("prueba.ruta", "create", [vals]) in calls
    [(_, _, search)] = [c for c in calls if c[1] == "search"]
    assert search[0][:2] == [("user_id", "=", 1), ("vehicle_id", "=", 2)]
    xmlid = next(a for m, meth, a in calls if m == "ir.model.data" and meth == "search_read")
    assert ("name", "=", "prueba_ruta_" + UUID.replace("-", "")) in xmlid[0]


# ─────────────────────────────────────────────── API ───────────────────────────────────────────────

@pytest.fixture
def api(seeded):
    app = FastAPI()
    app.include_router(prueba_ruta_router)
    woke: list[int] = []
    app.state.prueba_ruta = PruebaRutaServices(seeded, BOT_TOKEN, 3600, AUTORIZADOS, wake_worker=lambda: woke.append(1))
    return TestClient(app), woke


def _headers(user_id=TG_USER):
    return {"X-Telegram-Init-Data": make_init_data(user_id=user_id)}


def test_api_exige_init_data(api):
    client, _ = api
    assert client.get("/api/prueba-ruta/context").status_code == 401
    assert client.post("/api/prueba-ruta", json={}).status_code == 401
    assert client.post("/api/prueba-ruta", json={}, headers=_headers()).status_code == 422
    assert client.get("/api/prueba-ruta/context", headers=_headers(user_id=1)).status_code == 403


def test_api_contrato_no_autorizado(api, seeded):
    client, woke = api
    ctx = client.get("/api/prueba-ruta/context", headers=_headers()).json()
    assert ctx["autorizado"] is False and ctx["mensaje"] == "Contrato No Autorizado"
    _reservar(seeded)
    response = client.post("/api/prueba-ruta", json=_payload().model_dump(mode="json"), headers=_headers())
    assert response.status_code == 403 and response.json()["detail"] == "Contrato No Autorizado" and woke == []


def test_api_envio_completo(api, seeded):
    client, woke = api
    _con_acuerdo(seeded)
    _reservar(seeded)
    assert len(client.get("/api/prueba-ruta/form", headers=_headers()).json()["preguntas"]) == 6
    body = _payload().model_dump(mode="json")
    first = client.post("/api/prueba-ruta", json=body, headers=_headers())
    assert first.status_code == 202 and first.json()["sync_status"] == "pendiente" and woke == [1]
    assert client.post("/api/prueba-ruta", json=body, headers=_headers()).json()["duplicado"] is True
    assert client.get(f"/api/prueba-ruta/{UUID}", headers=_headers()).json()["id"] == first.json()["id"]
    assert client.get(f"/api/prueba-ruta/{UUID}", headers=_headers(user_id=1)).status_code == 404


def test_pagina_sin_cache_logo_versionado_y_bandeja(api):
    client, _ = api
    page = client.get("/prueba-ruta")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
    assert re.findall(r'src="/static/viacotur-logo\.png\?v=([0-9a-f]{12})"', page.text)
    for marca in ('IDB_NAME = "prueba-ruta-offline"', "vaciarBandeja()", 'addEventListener("online"',
                  "Contrato No Autorizado", "getUserMedia"):
        assert marca in page.text
    assert "innerHTML" not in page.text


# ───────────────────── envío sin señal (misma regla del preoperacional: máx. 1 h) ─────────────────────

def _diferido_body(*, abierto_hace: timedelta, sin_senal_hace: timedelta, user_id=TG_USER) -> dict:
    body = _payload().model_dump(mode="json")
    now = datetime.now(UTC)
    body["diferido"] = {
        "init_data_original": make_init_data(user_id=user_id, auth_date=int((now - abierto_hace).timestamp())),
        "sin_senal_desde": (now - sin_senal_hace).isoformat(),
    }
    return body


def test_api_diferido_en_ventana_y_trazabilidad(api, seeded):
    client, woke = api
    _con_acuerdo(seeded)
    _reservar(seeded)
    body = _diferido_body(abierto_hace=timedelta(minutes=80), sin_senal_hace=timedelta(minutes=40))
    headers = {"X-Telegram-Init-Data": body["diferido"]["init_data_original"]}
    assert client.post("/api/prueba-ruta", json=body, headers=headers).status_code == 202 and woke == [1]
    with seeded.session() as s:
        prueba = s.scalar(select(PruebaRuta))
        traza = s.scalar(select(PruebaRutaEnvioDiferido))
        assert traza.estado == EstadoDiferido.ACEPTADO and traza.prueba_id == prueba.id and traza.payload is None


def test_api_diferido_vencido_solo_auditoria_con_fotos(api, seeded):
    client, woke = api
    _con_acuerdo(seeded)
    _reservar(seeded)
    body = _diferido_body(abierto_hace=timedelta(hours=3), sin_senal_hace=timedelta(minutes=150))
    assert client.post("/api/prueba-ruta", json=body, headers=_headers()).status_code == 410 and woke == []
    with seeded.session() as s:
        assert s.scalar(select(func.count()).select_from(PruebaRuta)) == 0
        traza = s.scalar(select(PruebaRutaEnvioDiferido))
        assert traza.estado == EstadoDiferido.VENCIDO and traza.foto_km1 == JPEG and traza.foto_km2 == PNG
        assert "km1" not in traza.payload and traza.payload["respuestas"] == _respuestas()
    assert client.post("/api/prueba-ruta", json=body, headers=_headers()).status_code == 410


def test_api_diferido_mismo_usuario_y_duplicado(api, seeded):
    client, _ = api
    _con_acuerdo(seeded)
    _reservar(seeded)
    otro = _diferido_body(abierto_hace=timedelta(minutes=30), sin_senal_hace=timedelta(minutes=5), user_id=1)
    assert client.post("/api/prueba-ruta", json=otro, headers=_headers()).status_code == 403
    assert client.post("/api/prueba-ruta", json=_payload().model_dump(mode="json"), headers=_headers()).status_code == 202
    tarde = _diferido_body(abierto_hace=timedelta(hours=3), sin_senal_hace=timedelta(hours=2))
    response = client.post("/api/prueba-ruta", json=tarde, headers=_headers())
    assert response.status_code == 202 and response.json()["duplicado"] is True
