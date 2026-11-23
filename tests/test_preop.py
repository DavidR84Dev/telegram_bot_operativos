"""Pruebas del módulo preoperacional: esquemas, ETL (UPSERT), cola de envío, conector XML-RPC y API."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import xmlrpc.client
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from tenacity import wait_none

from app.preop import etl, service
from app.preop.checklist import CHECKLIST, NO_CONFORME, count_no_conformes, form_definition, llantas_bajo_minimo
from app.preop.db import Database
from app import formatting
from app.preop.models import (
    Acuerdo,
    Base,
    Empleado,
    Extracto,
    Flota,
    Preoperacional,
    PreopFoto,
    SyncRun,
    SyncStatus,
    Ubicacion,
    utc_now,
)
from app.preop.odoo_writer import (
    OdooPermanentError,
    OdooTransientError,
    OdooWriter,
    build_odoo_values,
)
from app.preop.rest_client import OdooApiError, OdooRestClient
from app.preop.router import PreopServices
from app.preop.router import router as preop_router
from app.preop.schemas import EmpleadoApi, ExtractoApi, PreoperacionalIn
from app.preop.worker import PreopSender

from .helpers import BOT_TOKEN, make_init_data

TG_USER = 7913476225
UUID = "0b8f5a8e-3c1d-4f7a-9a2b-1c2d3e4f5a6b"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64  # firma JPEG válida
FOTO_B64 = base64.b64encode(JPEG).decode()


# ────────────────────────────────────────── fixtures ──────────────────────────────────────────

def _flota(odoo_id=10, nombre="BUS 01/ABC123", company=1, **extra):
    return {"id": odoo_id, "nombre": nombre, "matricula": "ABC123", "estado": "Activo", "company_id": "Viacotur",
            "numero_interno": "101", "parque_automotor": "propio", "odometer_post": 1500.5,
            "tipo_solicitud": "", **extra}


def _empleado(odoo_id=100, pin=str(TG_USER), responsable="Jefe Uno", **extra):
    return {"id": odoo_id, "nombre": "Ana Conductora", "departamento": 3, "puesto_trabajo": "Conductor",
            "codigo_pin": pin, "id_contract": 55, "contrato": "Contrato Minera", "contrato_temporal_id": None,
            "contrato_temporal": "", "responsable": responsable, "company_id": "Viacotur", **extra}


def _extracto(odoo_id=500, vehiculo: Any = "BUS 01/ABC123", **extra):
    now = utc_now()
    return {"id": odoo_id, "codigo": "EXT-500", "estado": "approved", "company_id": "Viacotur",
            "contratos_list": [{"id_contrato": 55, "nombre": "Contrato Minera"}],
            "vehiculo": vehiculo, "conductor": "Ana Conductora", "cel_conductor1": "3001112233",
            "responsable_cliente": {"name": "Cliente SA", "mobile": "3109998877"},
            "fecha_hora_inicio": (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
            "fecha_hora_fin": (now + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S"), **extra}


class FakeRest:
    def __init__(self, data: dict[str, list[dict]] | None = None, fail: set[str] | None = None) -> None:
        self.data = data or {}
        self.fail = fail or set()

    def fetch_all(self, path, company_id, page_size=500):
        if path in self.fail:
            raise OdooApiError(f"{path} caído")
        return [dict(item) for item in self.data.get(path, [])]


@pytest.fixture
def db():
    """SQLite en memoria por defecto. Para probar contra PostgreSQL real (JSONB, GIN, SKIP LOCKED):
    PREOP_TEST_DATABASE_URL=postgresql+psycopg2://user:pw@host:5432/db_desechable pytest tests/test_preop.py
    (¡la BD se vacía en cada prueba!)."""
    url = os.environ.get("PREOP_TEST_DATABASE_URL", "sqlite:///:memory:")
    if url.startswith("postgresql"):
        from sqlalchemy import create_engine

        engine = create_engine(url)
        Base.metadata.drop_all(engine)
        engine.dispose()
    return Database(url)


@pytest.fixture
def seeded(db):
    etl.sync_all(db, FakeRest({
        "/api/flota": [_flota(), _flota(11, "VAN 02/XYZ987", numero_interno="102"), _flota(12, "OTRA", company=1)],
        "/api/empleados": [_empleado(), _empleado(101, pin="999", responsable="", nombre="Jefe Uno")],
        "/api/extractos": [_extracto()],
    }), [1])
    return db


def _ids(db) -> tuple[int, int]:
    with db.session() as s:
        return (s.scalar(select(Flota.id).where(Flota.odoo_id == 10)),
                s.scalar(select(Extracto.id).where(Extracto.odoo_id == 500)))


def _payload(vehiculo_id: int | None, extracto_id: int, **over) -> PreoperacionalIn:
    data = {"uuid": UUID, "vehiculo_id": vehiculo_id, "extracto_id": extracto_id, "kilometraje": 1510,
            "checklist": {item.key: item.options[0] for item in CHECKLIST}, "llantas": {"llanta_1": 5.0},
            "testigos_img": f"data:image/jpeg;base64,{FOTO_B64}"}
    data.update(over)
    return PreoperacionalIn.model_validate(data)


IDENTITY = service.TelegramIdentity(TG_USER, "ana", "Ana C")


# ────────────────────────────────────────── esquemas ──────────────────────────────────────────

def test_empleado_maps_flat_and_nested_responsable():
    flat = EmpleadoApi.model_validate(_empleado()).to_row()
    assert flat["responsable"] == "Jefe Uno" and flat["responsable_mobile"] is None
    assert flat["departamento_id"] == 3 and flat["departamento"] is None
    nested = EmpleadoApi.model_validate(
        _empleado(responsable={"name": "Jefe Uno", "mobile": "3001234567"}, departamento={"id": 4, "name": "Ops"})
    ).to_row()
    assert nested["responsable_mobile"] == "3001234567"
    assert (nested["departamento_id"], nested["departamento"]) == (4, "Ops")


def test_extracto_maps_nested_vehicle_and_client_mobile():
    row = ExtractoApi.model_validate(_extracto(vehiculo={"id": 10, "name": "BUS 01"})).to_row()
    assert row["vehiculo_id"] == 10 and row["vehiculo"] == "BUS 01"
    assert row["responsable_cliente_mobile"] == "3109998877"
    assert row["fecha_hora_inicio"].tzinfo is not None
    assert row["contratos_list"] == [{"id_contrato": 55, "nombre": "Contrato Minera"}]


def test_preop_input_unchecked_box_is_no_conforme_and_unanswered_radio_is_empty():
    # Se permite enviar con ítems sin marcar (decisión del 2026-11-23); solo se alertan.
    checklist = _payload(1, 1, checklist={"soat": "conforme"}).checklist
    assert checklist["soat"] == "conforme"
    assert checklist["licencia_conduccion"] == NO_CONFORME  # casilla sin marcar
    assert "pico_pala" not in checklist and "camaras" not in checklist  # radios sin elegir
    with pytest.raises(ValueError, match="Valor inválido"):
        _payload(1, 1, checklist={"soat": "no aplica"})  # una casilla no tiene "no aplica"
    with pytest.raises(ValueError, match="Ítems desconocidos"):
        _payload(1, 1, checklist={"no_existe": "conforme"})
    bad = {item.key: item.options[0] for item in CHECKLIST} | {"soat": "regular"}
    with pytest.raises(ValueError, match="Valor inválido"):
        _payload(1, 1, checklist=bad)
    with pytest.raises(ValueError, match="fuera de rango"):
        _payload(1, 1, llantas={"llanta_1_pre": 900})
    with pytest.raises(ValueError):
        PreoperacionalIn.model_validate({**_payload(1, 1).model_dump(), "telegram_user_id": 1})  # extra=forbid


def test_count_no_conformes_matches_odoo_rules():
    checklist = {"soat": NO_CONFORME, "llanta_rep_pre_act": "no", "frenos": "conforme"}
    assert count_no_conformes(checklist, {"llanta_1": 1.2, "llanta_5": 1.9, "llanta_2": 0, "llanta_1_pre": 1}) == 3


def test_form_definition_lists_every_item():
    form = form_definition()
    assert sum(len(s["items"]) for s in form["sections"]) == len(CHECKLIST)
    # Pestañas del formulario de referencia, en orden, y cada ítem en una sola.
    assert [t["title"] for t in form["tabs"]] == ["Documentos", "Rines/Llantas", "Equipo", "Vehículo"]
    assert sum(len(s["items"]) for t in form["tabs"] for s in t["sections"]) == len(CHECKLIST)
    equipo = [i["key"] for s in form["tabs"][2]["sections"] for i in s["items"]]
    assert equipo[0] == "conos" and equipo[-1] == "testigos"
    items = {i["key"]: i for s in form["sections"] for i in s["items"]}
    assert items["soat"]["criterio"] == "Documento vencido o inexistente"
    # Controles como el formulario de referencia.
    radios = {k for k, i in items.items() if i["control"] == "radio"}
    assert radios == {"chaleco_reflectivo", "pico_pala", "eslingas", "kit_derrames", "cables_de_arranque",
                      "camaras", "barra", "testigos", "extintor_2"}
    assert items["camaras"]["options"] == ["conforme", "no conforme", "no aplica"]
    assert "camaras_new" not in items and "llanta_rep_pre_act" not in items


def test_form_definition_tires_cover_every_measure_once():
    form = form_definition()
    keys = [k for ll in form["llantas"] for k in (ll["labrado"], ll["presion"])]
    assert sorted(keys) == sorted(m["key"] for m in form["measures"])
    assert [ll["codigo"] for ll in form["llantas"] if ll["interna"]] == ["5", "6"]
    assert {ll["codigo"]: ll["labrado_minimo"] for ll in form["llantas"]}["5"] == 2.0


# ──────────────────────────────────────────── ETL ────────────────────────────────────────────

def test_upsert_inserts_then_updates_without_duplicates(seeded):
    etl.sync_all(seeded, FakeRest({
        "/api/flota": [_flota(odometer_post=2000), _flota(11, "VAN 02/XYZ987"), _flota(12, "OTRA")],
        "/api/empleados": [_empleado(), _empleado(101, pin="999", nombre="Jefe Uno")],
        "/api/extractos": [_extracto()],
    }), [1])
    with seeded.session() as s:
        assert s.scalar(select(func.count()).select_from(Flota)) == 3
        assert s.scalar(select(Flota.odometer_post).where(Flota.odoo_id == 10)) == 2000


def test_missing_rows_are_deactivated_but_not_on_empty_response(seeded):
    etl.sync_all(seeded, FakeRest({"/api/flota": [_flota()], "/api/empleados": [], "/api/extractos": [_extracto()]}),
                 [1])
    with seeded.session() as s:
        assert s.scalar(select(Flota.activo).where(Flota.odoo_id == 11)) is False
        # La API devolvió 0 empleados: protección, no se desactiva nadie.
        assert s.scalar(select(func.count()).select_from(Empleado).where(Empleado.activo.is_(True))) == 2


def test_failed_download_rolls_back_and_logs_run(seeded):
    results = etl.sync_all(seeded, FakeRest({"/api/empleados": [], "/api/extractos": []}, fail={"/api/flota"}), [1])
    assert results[0].error and "caído" in results[0].error
    with seeded.session() as s:
        assert s.scalar(select(func.count()).select_from(Flota).where(Flota.activo.is_(True))) == 3
        assert s.scalar(select(SyncRun.status).where(SyncRun.entidad == "flota").order_by(SyncRun.id.desc())) == "error"


def test_extracto_vehicle_resolved_by_unique_name(seeded):
    with seeded.session() as s:
        assert s.scalar(select(Extracto.vehiculo_id).where(Extracto.odoo_id == 500)) == 10


def test_invalid_records_are_skipped(db):
    result = etl.sync_entity(db, FakeRest({"/api/flota": [_flota(), {"id": 99, "nombre": ""}]}), etl.ENTITIES[0], 1)
    assert (result.upserts, result.invalidos) == (1, 1)


# ───────────────────────────────────────── REST client ────────────────────────────────────────

def _rest(handler) -> OdooRestClient:
    client = OdooRestClient("http://odoo.test", "bot", "pw", transport=httpx.MockTransport(handler))
    return client


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(OdooRestClient._get_page.retry, "wait", wait_none())  # type: ignore[attr-defined]
    monkeypatch.setattr(OdooRestClient._authenticate.retry, "wait", wait_none())  # type: ignore[attr-defined]


def test_rest_client_paginates_and_reauthenticates(no_wait):
    calls = {"auth": 0, "expired": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/authenticate":
            calls["auth"] += 1
            assert json.loads(request.content) == {"login": "bot", "password": "pw"}
            return httpx.Response(200, json={"token": f"t{calls['auth']}"})
        if calls["expired"]:
            calls["expired"] = False
            return httpx.Response(401, json={"detail": "expired"})
        page = int(request.url.params["page"])
        items = [{"id": i} for i in range(1, 3)] if page == 1 else [{"id": 3}]
        return httpx.Response(201, json={"total": 3, "items": items})

    assert [r["id"] for r in _rest(handler).fetch_all("/api/flota", 1, page_size=2)] == [1, 2, 3]
    assert calls["auth"] == 2


def test_rest_client_json_404_is_empty_but_html_404_is_error(no_wait):
    def json_404(request):
        if request.url.path == "/api/authenticate":
            return httpx.Response(200, json={"token": "t"})
        return httpx.Response(404, json={"detail": "Informacion no encontrada."})

    def html_404(request):
        if request.url.path == "/api/authenticate":
            return httpx.Response(200, json={"token": "t"})
        return httpx.Response(404, text="<html>Not Found</html>", headers={"content-type": "text/html"})

    assert _rest(json_404).fetch_all("/api/flota", 1) == []
    with pytest.raises(OdooApiError, match="no existe"):
        _rest(html_404).fetch_all("/api/flota", 1)


def test_rest_client_retries_5xx(no_wait):
    attempts = {"n": 0}

    def handler(request):
        if request.url.path == "/api/authenticate":
            return httpx.Response(200, json={"token": "t"})
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(502)
        return httpx.Response(201, json={"total": 1, "items": [{"id": 1}]})

    assert _rest(handler).fetch_all("/api/flota", 1) == [{"id": 1}]
    assert attempts["n"] == 3


def _jwt(exp: float) -> str:
    import base64

    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'HS256'})}.{b64({'uid': 2, 'exp': exp})}.firma"


def test_rest_client_renews_token_before_24h_expiry(no_wait):
    now = {"t": 1_000_000.0}
    issued: list[str] = []
    seen: list[str] = []

    def handler(request):
        if request.url.path == "/api/authenticate":
            issued.append(_jwt(now["t"] + 24 * 3600))
            return httpx.Response(200, json={"token": issued[-1]})
        seen.append(request.headers["Authorization"])
        return httpx.Response(201, json={"total": 1, "items": [{"id": 1}]})

    client = OdooRestClient("http://odoo.test", "bot", "pw", transport=httpx.MockTransport(handler),
                            clock=lambda: now["t"])
    client.fetch_all("/api/flota", 1)
    now["t"] += 23 * 3600            # dentro de la vigencia: reutiliza el token
    client.fetch_all("/api/flota", 1)
    now["t"] += 3600 - 5 * 60        # a 5 min de vencer (margen de 10): pide uno nuevo sin esperar el 401
    client.fetch_all("/api/flota", 1)
    assert len(issued) == 2
    assert seen == [f"Bearer {issued[0]}", f"Bearer {issued[0]}", f"Bearer {issued[1]}"]


# ─────────────────────────────────────── recepción / cola ──────────────────────────────────────

def test_submit_is_idempotent_by_uuid(seeded):
    vehiculo, extracto = _ids(seeded)
    first, dup1 = service.submit(seeded, IDENTITY, _payload(vehiculo, extracto))
    second, dup2 = service.submit(seeded, IDENTITY, _payload(vehiculo, extracto))
    assert (dup1, dup2) == (False, True) and first.id == second.id
    assert first.contrato_odoo_id == 55  # único contrato del extracto
    assert first.sync_status == SyncStatus.PENDIENTE


def test_submit_rejects_unknown_employee(seeded):
    vehiculo, extracto = _ids(seeded)
    with pytest.raises(service.SubmissionError) as exc:
        service.submit(seeded, service.TelegramIdentity(1, None, "X"), _payload(vehiculo, extracto))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("change", [
    {"estado": "draft"},                      # extracto no aprobado
    {"conductor": "Otro Conductor"},          # el empleado no es su conductor
    {"fecha_hora_fin": utc_now() - timedelta(days=3)},  # vencido
])
def test_submit_requires_own_approved_current_extracto(seeded, change):
    vehiculo, extracto = _ids(seeded)
    with seeded.session() as s:
        for key, value in change.items():
            setattr(s.get(Extracto, extracto), key, value)
    with pytest.raises(service.SubmissionError) as exc:
        service.submit(seeded, IDENTITY, _payload(vehiculo, extracto))
    assert exc.value.status_code == 403


def test_vehicle_comes_from_extracto_and_cannot_be_changed(seeded):
    vehiculo, extracto = _ids(seeded)
    with seeded.session() as s:
        otro = s.scalar(select(Flota.id).where(Flota.odoo_id == 11))
    with pytest.raises(service.SubmissionError, match="no corresponde al extracto"):
        service.submit(seeded, IDENTITY, _payload(otro, extracto))
    preop, _ = service.submit(seeded, IDENTITY, _payload(None, extracto))  # sin vehículo: se toma del extracto
    assert preop.vehiculo_id == vehiculo


def test_conductor_name_matches_without_accents_or_case(seeded):
    _, extracto = _ids(seeded)
    with seeded.session() as s:
        s.get(Extracto, extracto).segundo_conductor, s.get(Extracto, extracto).conductor = "  ANA  CONDUCTÓRA ", "Otro"
        empleado = s.scalar(select(Empleado).where(Empleado.odoo_id == 100))
        assert [e.id for e in service.extractos_del_conductor(s, empleado)] == [extracto]


def test_contract_required_when_extracto_has_several(seeded):
    vehiculo, extracto = _ids(seeded)
    with seeded.session() as s:
        s.get(Extracto, extracto).contratos_list = [{"id_contrato": 55, "nombre": "A"}, {"id_contrato": 56, "nombre": "B"}]
    with pytest.raises(service.SubmissionError, match="Selecciona el contrato"):
        service.submit(seeded, IDENTITY, _payload(vehiculo, extracto))
    preop, _ = service.submit(seeded, IDENTITY, _payload(vehiculo, extracto, contrato_odoo_id=56))
    assert preop.contrato_odoo_id == 56


@pytest.mark.parametrize("foto, error", [
    ("no-es-base64!!", "base64"),
    (base64.b64encode(b"GIF89a" + b"\x00" * 32).decode(), "JPEG o PNG"),
])
def test_photo_is_validated_by_real_signature(foto, error):
    with pytest.raises(ValueError, match=error):
        _payload(1, 1, testigos_img=foto)


def test_photo_is_stored_and_sent_as_testigos_img(seeded):
    preop_id = _queue_one(seeded)
    with seeded.session() as s:
        foto = s.scalar(select(PreopFoto).where(PreopFoto.preop_id == preop_id))
        assert (foto.tipo, foto.mime, foto.contenido) == ("testigos", "image/jpeg", JPEG)
    service.claim_batch(seeded, 10, 300)
    writer = FakeWriter([(77, "PRE-0077", False)])
    service.send_one(seeded, writer, preop_id, 8)
    assert writer.calls[0][1]["testigos_img"] == FOTO_B64


def test_submit_without_photo_and_with_unanswered_items(seeded):
    vehiculo, extracto = _ids(seeded)
    parcial = {item.key: item.options[0] for item in CHECKLIST if item.key not in {"soat", "pico_pala"}}
    preop, _ = service.submit(seeded, IDENTITY, _payload(
        vehiculo, extracto, checklist=parcial, testigos_img=None, observaciones="Rayón en puerta"))
    with seeded.session() as s:
        assert s.scalar(select(func.count()).select_from(PreopFoto).where(PreopFoto.preop_id == preop.id)) == 0
    service.claim_batch(seeded, 10, 300)
    writer = FakeWriter([(78, "PRE-0078", False)])
    service.send_one(seeded, writer, preop.id, 8)
    vals = writer.calls[0][1]
    assert "testigos_img" not in vals and "pico_pala" not in vals
    assert vals["soat"] == NO_CONFORME  # casilla sin marcar
    assert vals["observaciones"] == "Rayón en puerta"


def test_odometer_below_last_registered_is_rejected(seeded):
    vehiculo, extracto = _ids(seeded)  # último odómetro (Pos-Operacional) = 1500.5
    with pytest.raises(service.SubmissionError, match="mayor o igual"):
        service.submit(seeded, IDENTITY, _payload(vehiculo, extracto, kilometraje=1500))
    preop, _ = service.submit(seeded, IDENTITY, _payload(vehiculo, extracto, kilometraje=1500.5))
    assert preop.kilometraje == 1500.5
    # Varios preoperacionales del mismo conductor son válidos.
    otro, dup = service.submit(seeded, IDENTITY, _payload(vehiculo, extracto, uuid=UUID.replace("0b", "1c", 1)))
    assert not dup and otro.id != preop.id


def _sync_acuerdo(db, tipo: str, camioneta: float, buses: float) -> None:
    etl.sync_all(db, FakeRest({
        "/api/flota": [_flota(tipo_solicitud=tipo), _flota(11, "VAN 02/XYZ987"), _flota(12, "OTRA", company=1)],
        "/api/empleados": [_empleado(), _empleado(101, pin="999", responsable="", nombre="Jefe Uno")],
        "/api/extractos": [_extracto()],
        "/api/acuerdos": [{"id": 55, "nombre": "Contrato Minera", "cliente": "Minera",
                           "labrado_camioneta": camioneta, "labrado_buses": buses, "company_id": "Viacotur"}],
    }), [1])


@pytest.mark.parametrize("tipo, camioneta, buses, esperado", [
    ("bus", 2.0, 5.0, 5.0),
    ("buseta", 2.0, 5.0, 5.0),
    ("camioneta", 3.0, 5.0, 3.0),
    ("camioneta", 0.0, 5.0, None),  # el acuerdo no lo define: se usa el de Odoo
])
def test_context_tread_minimum_comes_from_agreement_and_vehicle_type(seeded, tipo, camioneta, buses, esperado):
    _sync_acuerdo(seeded, tipo, camioneta, buses)
    with seeded.session() as s:
        assert s.scalar(select(Acuerdo.labrado_buses).where(Acuerdo.odoo_id == 55)) == buses
        empleado = s.scalar(select(Empleado).where(Empleado.odoo_id == 100))
    [extracto] = service.form_context(seeded, empleado)["extractos"]
    assert extracto["contratos"][0]["labrado_minimo"] == esperado
    assert extracto["vehiculo"]["tipo"] == tipo


def test_tires_below_minimum_use_agreement_or_odoo_default():
    llantas = {"llanta_1": 4.0, "llanta_2": 6.0, "llanta_5": 1.9, "llanta_7": 0}
    assert llantas_bajo_minimo(llantas, 5.0) == ["Llanta N.º 1", "Llanta N.º 5"]
    assert llantas_bajo_minimo(llantas, None) == ["Llanta N.º 5"]  # Odoo: 2,0 mm en internas


def test_boss_is_alerted_on_findings_when_has_telegram_pin(seeded):
    _sync_acuerdo(seeded, "bus", 2.0, 5.0)
    vehiculo, extracto = _ids(seeded)
    checklist = {item.key: "conforme" for item in CHECKLIST} | {"frenos": NO_CONFORME}
    preop, _ = service.submit(seeded, IDENTITY, _payload(vehiculo, extracto, checklist=checklist,
                                                          llantas={"llanta_1": 4.0}))
    service.claim_batch(seeded, 10, 300)
    outcome = service.send_one(seeded, FakeWriter([(79, "PRE-0079", False)]), preop.id, 8)
    aviso = outcome.aviso_jefe
    assert aviso is not None and aviso.telegram_id == 999  # PIN del jefe = id de Telegram
    assert aviso.no_conformes == ("Frenos",) and aviso.llantas_bajas == ("Llanta N.º 1",)
    text = formatting.preop_aviso_jefe(aviso, outcome.odoo_name)
    assert "Ana Conductora" in text and "PRE-0079" in text and "Frenos" in text


def test_boss_not_alerted_without_findings(seeded):
    vehiculo, extracto = _ids(seeded)
    preop, _ = service.submit(seeded, IDENTITY, _payload(
        vehiculo, extracto, checklist={item.key: "conforme" for item in CHECKLIST}))
    service.claim_batch(seeded, 10, 300)
    assert service.send_one(seeded, FakeWriter([(80, "PRE-0080", False)]), preop.id, 8).aviso_jefe is None


def test_location_links_to_recent_preop_only(seeded):
    preop_id = _queue_one(seeded)
    reciente = service.registrar_ubicacion(seeded, TG_USER, 6.25, -75.56, 12.0)
    assert reciente.preop_id == preop_id
    tarde = service.registrar_ubicacion(seeded, TG_USER, 6.25, -75.56, now=utc_now() + timedelta(minutes=11))
    assert tarde.preop_id is None
    otro = service.registrar_ubicacion(seeded, 42, 6.0, -75.0)
    assert otro.preop_id is None
    with seeded.session() as s:
        filas = s.scalars(select(Ubicacion).order_by(Ubicacion.id)).all()
        assert [(u.telegram_user_id, u.empleado_id is not None) for u in filas] == [
            (TG_USER, True), (TG_USER, True), (42, False)]


def test_submit_rejects_contract_not_related(seeded):
    vehiculo, extracto = _ids(seeded)
    with pytest.raises(service.SubmissionError, match="contrato"):
        service.submit(seeded, IDENTITY, _payload(vehiculo, extracto, contrato_odoo_id=999))


class FakeWriter:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple] = []

    def create_preoperacional(self, ref, vals, submitted_at, uncertain):
        self.calls.append((ref, vals, uncertain))
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _queue_one(db) -> int:
    vehiculo, extracto = _ids(db)
    preop, _ = service.submit(db, IDENTITY, _payload(vehiculo, extracto))
    return preop.id


def test_send_success_stores_odoo_id(seeded):
    preop_id = _queue_one(seeded)
    assert service.claim_batch(seeded, 10, 300) == [preop_id]
    writer = FakeWriter([(77, "PRE-0077", False)])
    outcome = service.send_one(seeded, writer, preop_id, 8)
    assert outcome.status == SyncStatus.ENVIADO
    _ref, vals, uncertain = writer.calls[0]
    assert vals["employee_id"] == 100 and vals["vehicle_id"] == 10 and vals["extracto_id"] == 500
    assert vals["contrato_id"] == 55 and vals["immediate_boss_id"] == 101 and uncertain is False
    with seeded.session() as s:
        row = s.get(Preoperacional, preop_id)
        assert (row.sync_status, row.odoo_id, row.odoo_name, row.error_log) == (SyncStatus.ENVIADO, 77, "PRE-0077", None)
    assert service.claim_batch(seeded, 10, 300) == []


def test_transient_error_backs_off_then_fails_after_max_attempts(seeded):
    preop_id = _queue_one(seeded)
    service.claim_batch(seeded, 10, 300)
    outcome = service.send_one(seeded, FakeWriter([OdooTransientError("timeout")]), preop_id, 2)
    assert outcome.status == SyncStatus.ERROR
    assert service.claim_batch(seeded, 10, 300) == []  # aún en backoff
    later = utc_now() + timedelta(hours=2)
    assert service.claim_batch(seeded, 10, 300, now=later) == [preop_id]
    writer = FakeWriter([OdooTransientError("timeout")])
    assert service.send_one(seeded, writer, preop_id, 2).status == SyncStatus.FALLIDO
    assert writer.calls[0][2] is True  # segundo intento: resultado previo incierto
    assert service.requeue_failed(seeded) == 1
    assert service.claim_batch(seeded, 10, 300) == [preop_id]


def test_permanent_error_fails_immediately(seeded):
    preop_id = _queue_one(seeded)
    service.claim_batch(seeded, 10, 300)
    outcome = service.send_one(seeded, FakeWriter([OdooPermanentError("ValidationError: campo")]), preop_id, 8)
    assert outcome.status == SyncStatus.FALLIDO
    with seeded.session() as s:
        assert "ValidationError" in s.get(Preoperacional, preop_id).error_log


def test_stale_sending_lease_is_reclaimed(seeded):
    preop_id = _queue_one(seeded)
    service.claim_batch(seeded, 10, 300)
    assert service.claim_batch(seeded, 10, 300) == []
    assert service.claim_batch(seeded, 10, 300, now=utc_now() + timedelta(minutes=10)) == [preop_id]


def test_sender_notifies_final_states(seeded):
    _queue_one(seeded)
    notified = []

    async def notify(outcome):
        notified.append(outcome)

    sender = PreopSender(seeded, FakeWriter([(1, "PRE-1", False)]), 10, 8, 300, notify=notify)  # type: ignore[arg-type]
    outcomes = asyncio.run(sender.run_once())
    assert [o.status for o in outcomes] == [SyncStatus.ENVIADO] and notified[0].telegram_user_id == TG_USER


# ─────────────────────────────────────── conector XML-RPC ──────────────────────────────────────

class FakeObjects:
    def __init__(self, xmlid_hit=None, natural_hit=None, create_errors=()):
        self.xmlid_hit = xmlid_hit
        self.natural_hit = natural_hit
        self.create_errors = list(create_errors)
        self.created: list[dict] = []

    def execute_kw(self, db, uid, pw, model, method, args, kwargs):
        if model == "ir.model.data" and method == "search_read":
            return [{"res_id": self.xmlid_hit}] if self.xmlid_hit else []
        if model == "ir.model.data" and method == "create":
            raise xmlrpc.client.Fault(1, "AccessError: no puede crear ir.model.data")
        if method == "search":
            return [self.natural_hit] if self.natural_hit else []
        if method == "create":
            if self.create_errors:
                raise self.create_errors.pop(0)
            self.created.append(args[0])
            self.natural_hit = 900 + len(self.created)
            return self.natural_hit
        if method == "read":
            return [{"name": f"PRE-{args[0][0]}"}]
        raise AssertionError((model, method))


class FakeCommon:
    def authenticate(self, *args):
        return 2


def _writer(objects) -> OdooWriter:
    writer = OdooWriter("http://odoo.test", "db", "bot", "pw")
    writer._common = FakeCommon()  # type: ignore[assignment]
    writer._objects = objects  # type: ignore[assignment]
    return writer


VALS = {"employee_id": 1, "vehicle_id": 2, "odometer_actual": 10.0}


def test_writer_reuses_existing_xmlid():
    objects = FakeObjects(xmlid_hit=55)
    assert _writer(objects).create_preoperacional(UUID, VALS, utc_now(), False) == (55, "PRE-55", True)
    assert objects.created == []


def test_writer_retry_after_timeout_does_not_duplicate(monkeypatch):
    import app.preop.odoo_writer as ow

    monkeypatch.setattr(ow, "wait_exponential_jitter", lambda **_: wait_none())

    class CreatedThenTimeout(FakeObjects):
        def execute_kw(self, db, uid, pw, model, method, args, kwargs):
            if method == "create" and model == "preoperacional" and not self.created:
                super().execute_kw(db, uid, pw, model, method, args, kwargs)  # Odoo sí lo crea…
                raise TimeoutError("timed out")  # …pero la respuesta se pierde
            return super().execute_kw(db, uid, pw, model, method, args, kwargs)

    objects = CreatedThenTimeout()
    odoo_id, name, reused = _writer(objects).create_preoperacional(UUID, VALS, utc_now(), False)
    assert (odoo_id, reused) == (901, True) and len(objects.created) == 1


def test_writer_classifies_faults():
    with pytest.raises(OdooPermanentError, match="ValidationError"):
        _writer(FakeObjects(create_errors=[xmlrpc.client.Fault(1, "Traceback\nValidationError: dato")])
                ).create_preoperacional(UUID, VALS, utc_now(), False)


def test_build_odoo_values_only_sends_catalog_fields(seeded):
    vehiculo, extracto = _ids(seeded)
    preop, _ = service.submit(seeded, IDENTITY, _payload(vehiculo, extracto, extra={"campo_nuevo": "x"}))
    with seeded.session() as s:
        vals = build_odoo_values(s.get(Preoperacional, preop.id))
    assert "campo_nuevo" not in vals and vals["llanta_1"] == 5.0 and vals["soat"] == "conforme"
    assert vals["ref_iterna"] == "101" and vals["odometer_actual"] == 1510.0


# ─────────────────────────────────────────── API HTTP ──────────────────────────────────────────

@pytest.fixture
def api(seeded):
    app = FastAPI()
    app.include_router(preop_router)
    woke = []
    app.state.preop = PreopServices(seeded, BOT_TOKEN, 3600, wake_worker=lambda: woke.append(1))
    return TestClient(app), woke


def _headers(user_id=TG_USER):
    return {"X-Telegram-Init-Data": make_init_data(user_id=user_id)}


def test_api_requires_valid_init_data(api):
    client, _ = api
    assert client.get("/api/preop/context").status_code == 401
    forged = {"X-Telegram-Init-Data": make_init_data(tamper=True)}
    assert client.get("/api/preop/context", headers=forged).status_code == 401


def test_api_context_and_unknown_user(api):
    client, _ = api
    assert client.get("/api/preop/context", headers=_headers(user_id=1)).status_code == 403
    ctx = client.get("/api/preop/context", headers=_headers()).json()
    assert ctx["empleado"] == {"nombre": "Ana Conductora", "puesto_trabajo": "Conductor", "responsable": "Jefe Uno"}
    assert ctx["validado"] is True
    [extracto] = ctx["extractos"]
    assert extracto["vehiculo"]["odoo_id"] == 10 and extracto["vehiculo"]["matricula"] == "ABC123"
    assert extracto["contratos"] == [{"id_contrato": 55, "nombre": "Contrato Minera", "labrado_minimo": None}]


def test_api_context_not_validated_without_approved_extracto(api, seeded):
    client, _ = api
    with seeded.session() as s:
        s.scalar(select(Extracto)).estado = "pending"
    ctx = client.get("/api/preop/context", headers=_headers()).json()
    assert ctx["validado"] is False and ctx["extractos"] == []


def test_api_serves_logo(api):
    client, _ = api
    response = client.get("/static/viacotur-logo.png")
    assert response.status_code == 200 and response.content.startswith(b"\x89PNG")


def test_api_submit_accepts_and_wakes_worker(api, seeded):
    client, woke = api
    vehiculo, extracto = _ids(seeded)
    body = _payload(vehiculo, extracto).model_dump()
    first = client.post("/api/preop", json=body, headers=_headers())
    assert first.status_code == 202 and first.json()["sync_status"] == "pendiente" and woke == [1]
    again = client.post("/api/preop", json=body, headers=_headers())
    assert again.json()["duplicado"] is True and woke == [1]
    status = client.get(f"/api/preop/{UUID}", headers=_headers())
    assert status.json()["id"] == first.json()["id"]
    assert client.get(f"/api/preop/{UUID}", headers=_headers(user_id=1)).status_code == 404


def test_api_submit_validates_payload(api, seeded):
    client, _ = api
    vehiculo, extracto = _ids(seeded)
    body = _payload(vehiculo, extracto).model_dump()
    body["checklist"]["soat"] = "<script>"
    assert client.post("/api/preop", json=body, headers=_headers()).status_code == 422
