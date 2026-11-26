"""Pruebas del Pos-Operacional: esquema (km, fechas de Colombia a UTC, planilla), contexto, recepción, valores del
``create`` en ``pos.operacional``, dedupe, estados (Fault = fallido, red = reintento), envío sin señal, API y menú."""
from __future__ import annotations

import asyncio
import base64
import io
import xmlrpc.client
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from tenacity import wait_none

from app import formatting
from app import telegram_app as tga
from app.pos_operacional import service
from app.pos_operacional.catalogo import form_definition
from app.pos_operacional.router import PosOperacionalServices
from app.pos_operacional.router import router as pos_router
from app.pos_operacional.schemas import PosOperacionalIn
from app.pos_operacional.worker import PosOperacionalSender, listar
from app.preop import reservas
from app.preop.models import (
    EstadoDiferido,
    PosOperacional,
    PosOperacionalEnvioDiferido,
    ReservaVehiculo,
    SyncStatus,
    utc_now,
)
from app.preop.odoo_writer import OdooTransientError, OdooPermanentError

from . import test_combustible as _comb
from . import test_preop as _preop
from .helpers import BOT_TOKEN, antedatar_reservas, make_init_data
from .test_preop import TG_USER, FakeObjects, _writer

db = _preop.db
seeded = _preop.seeded
listo = _comb.listo  # preoperacional hecho: vehículo reservado (odómetro 1510, contrato 55)

IDENTITY = _comb.IDENTITY
UUID = "5c1d2e3f-3c1d-4f7a-9a2b-1c2d3e4f5a6b"
PDF = b"%PDF-1.4\n" + b"0" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
ENTRADA, SALIDA = "2026-11-26T08:00", "2026-11-26T17:30"  # hora de Colombia (UTC-5)


def _b64(datos: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(datos).decode()


PDF_B64 = _b64(PDF, "application/pdf")


def _form(**over: Any) -> PosOperacionalIn:
    data: dict[str, Any] = {
        "uuid": UUID, "km_inicial": 1510, "km_final": "1600,5", "entrada": ENTRADA, "salida": SALIDA,
        "planilla": PDF_B64, "planilla_nombre": "Planilla 26-11.pdf", "observaciones": "Sin novedades",
    }
    data.update(over)
    return PosOperacionalIn.model_validate(data)


def _recibir(db, **over: Any) -> int:
    return service.submit(db, IDENTITY, _form(**over))[0].id


def _queue(db, **over: Any) -> int:
    pid = _recibir(db, **over)
    assert service.claim_batch(db, 10, 300) == [pid]
    return pid


class FakeWriter:
    """Writer de prueba: búsqueda de duplicados y creación de ``pos.operacional``."""

    def __init__(self, existente: int | None = None, error: Exception | None = None) -> None:
        self.existente, self.error = existente, error
        self.busquedas: list[dict] = []
        self.creados: list[tuple[str, dict, bool]] = []

    def buscar_pos_operacional(self, vals):
        self.busquedas.append(vals)
        return self.existente

    def create_pos_operacional(self, ref, vals, submitted_at, uncertain):
        if self.error is not None:
            raise self.error
        self.creados.append((ref, vals, uncertain))
        return 501, "POS-501", False


# ──────────────────────────────────────────── esquema ────────────────────────────────────────────

def test_fechas_de_colombia_se_guardan_en_utc():
    data = _form()
    assert data.entrada == datetime(2026, 11, 26, 13, 0, tzinfo=UTC)
    assert data.salida == datetime(2026, 11, 26, 22, 30, tzinfo=UTC)
    # Con zona explícita se respeta: 08:00-05:00 y 13:00Z son el mismo instante.
    con_zona = _form(entrada="2026-11-26T08:00:00-05:00", salida="2026-11-26T22:30:00Z")
    assert con_zona.entrada == data.entrada and con_zona.salida == data.salida


def test_kilometros_con_coma_se_normalizan_a_punto_y_se_aceptan_enteros():
    data = _form(km_inicial="1510,5", km_final=1600)
    assert (data.km_inicial, data.km_final) == (1510.5, 1600.0)
    assert _form(km_inicial=1510, km_final=1510).km_final == 1510.0  # igual al inicial es válido


@pytest.mark.parametrize("cambio, error", [
    ({"km_inicial": 1510, "km_final": 1509.9}, "KM final no puede ser menor"),
    ({"km_final": "1,2,3"}, "km_final"),
    ({"km_final": "abc"}, "km_final"),
    ({"km_final": True}, "km_final"),
    ({"km_inicial": -1}, "km_inicial"),
    ({"km_final": 10**8}, "km_final"),
    ({"salida": ENTRADA}, "posterior a la entrada"),
    ({"salida": "2026-11-26T07:59"}, "posterior a la entrada"),
    ({"salida": "2026-11-27T08:01"}, "más de 24 horas"),
    ({"entrada": "no es fecha"}, "entrada"),
    ({"telegram_user_id": 1}, "telegram_user_id"),
    ({"contrato_odoo_id": 5}, "contrato_odoo_id"),  # el contrato sale del servidor
    ({"observaciones": "x" * 1001}, "observaciones"),
])
def test_esquema_rechaza(cambio, error):
    with pytest.raises(ValidationError, match=error):
        _form(**cambio)


def test_rango_de_24_horas_exactas_es_valido():
    assert _form(salida="2026-11-27T08:00").salida - _form().entrada == timedelta(hours=24)


@pytest.mark.parametrize("planilla, mensaje", [
    (_b64(b"GIF89a" + b"0" * 40, "image/gif"), "PDF o una foto"),
    (_b64(b"hola mundo, no soy un PDF", "text/plain"), "PDF o una foto"),
    (_b64(b"\x00\x00\x00\x18ftypheic" + b"0" * 40, "image/heic"), "HEIC"),
    ("@@@@@@@@@@@@@@@@@@@@", "base64"),
    (_b64(b"%PDF-" + b"0" * (3 * 1024 * 1024 + 10), "application/pdf"), "planilla"),
])
def test_planilla_invalida(planilla, mensaje):
    with pytest.raises(ValidationError, match=mensaje):
        _form(planilla=planilla)


def test_planilla_es_obligatoria():
    with pytest.raises(ValidationError, match="planilla"):
        PosOperacionalIn.model_validate({"uuid": UUID, "km_inicial": 1, "km_final": 2, "entrada": ENTRADA,
                                         "salida": SALIDA})


# ───────────────────────────────── contexto: vehículo, contrato, km y horas ─────────────────────────────────

def test_contexto_precarga_km_inicial_entrada_contrato_y_vehiculo(listo):
    from app.preop.service import employee_for_telegram

    empleado = employee_for_telegram(listo, TG_USER)
    reservado = utc_now() - timedelta(hours=3, minutes=20)
    with listo.session() as s:
        s.scalar(select(ReservaVehiculo)).reservado_at = reservado
    ahora = utc_now()
    ctx = service.form_context(listo, empleado, TG_USER, now=ahora)
    assert ctx["vehiculo"]["placa"] == "ABC123" and ctx["contrato"] == {"id": 55, "nombre": "Contrato Minera"}
    assert ctx["km_inicial"] == 1510.0  # odómetro del último preoperacional de la jornada
    colombia = lambda d: (d.astimezone(UTC) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M")  # noqa: E731
    assert ctx["entrada"] == colombia(reservado) and ctx["salida"] == colombia(ahora)
    assert ctx["empleado"]["nombre"] == "Ana Conductora"


def test_contrato_es_solo_el_del_preoperacional(listo):
    """Regla 2026-11-26: agreement_id = contrato del preoperacional; sin él no se deduce de otras fuentes."""
    from app.preop.models import Preoperacional
    from app.preop.service import employee_for_telegram

    with listo.session() as s:
        for preop in s.scalars(select(Preoperacional)):
            preop.contrato_odoo_id = None
    ctx = service.form_context(listo, employee_for_telegram(listo, TG_USER), TG_USER)
    assert ctx["vehiculo"]["placa"] == "ABC123" and ctx["contrato"] is None
    pid = _queue(listo)
    writer = FakeWriter()
    service.send_one(listo, writer, pid, 5)
    assert "agreement_id" not in writer.creados[0][1]


def test_contexto_sin_reserva_vigente_se_bloquea(seeded):
    from app.preop.service import SinReservaVigenteError, employee_for_telegram

    with pytest.raises(SinReservaVigenteError) as exc:
        service.form_context(seeded, employee_for_telegram(seeded, TG_USER), TG_USER)
    assert exc.value.status_code == 409 and str(exc.value) == reservas.MSG_SIN_RESERVA


def test_form_definition_expone_los_limites():
    form = form_definition()
    assert form["rango_max_horas"] == 24 and form["planilla_max_bytes"] == 3 * 1024 * 1024 and form["km_max"] > 1e6


# ───────────────────────────────────────────── recepción ─────────────────────────────────────────────

def test_recepcion_guarda_pendiente_con_la_reserva_de_la_jornada(listo):
    recibido, duplicado = service.submit(listo, IDENTITY, _form())
    assert (duplicado, recibido.sync_status, recibido.placa) == (False, SyncStatus.PENDIENTE, "ABC123")
    with listo.session() as s:
        pos = s.get(PosOperacional, recibido.id)
        assert (pos.vehiculo.odoo_id, pos.contrato_odoo_id, pos.contrato) == (10, 55, "Contrato Minera")
        assert pos.check_in.replace(tzinfo=None) == datetime(2026, 11, 26, 13, 0)
        assert (pos.planilla_tipo, pos.planilla_tamano, pos.planilla_nombre) == ("pdf", len(PDF), "Planilla 26-11.pdf")
    again, duplicado = service.submit(listo, IDENTITY, _form())
    assert duplicado is True and again.id == recibido.id


def test_uuid_ajeno_responde_409_y_sin_empleado_403(listo):
    _recibir(listo)
    ajeno = service.TelegramIdentity(1, None, "Otro")
    with pytest.raises(service.SubmissionError) as exc:
        service.submit(listo, ajeno, _form())
    assert exc.value.status_code == 409
    with pytest.raises(service.SubmissionError) as exc:
        service.submit(listo, ajeno, _form(uuid="6c1d2e3f-3c1d-4f7a-9a2b-1c2d3e4f5a6b"))
    assert exc.value.status_code == 403


def test_registro_antiguo_sin_vehiculo_sale_sin_vehiculo_ni_contrato(listo):
    seeded = listo
    pid = _queue(seeded)
    with seeded.session() as s:  # registro previo al 2026-11-26 (cuando la reserva era opcional)
        pos = s.get(PosOperacional, pid)
        pos.vehiculo_id, pos.contrato_odoo_id, pos.contrato = None, None, None
    writer = FakeWriter()
    outcome = service.send_one(seeded, writer, pid, 5)
    assert outcome.status == SyncStatus.ENVIADO and outcome.placa is None
    vals = writer.creados[0][1]
    assert "vehicle_id" not in vals and "agreement_id" not in vals and vals["employee_id"] == 100


# ──────────────────────────────────────────── envío a Odoo ────────────────────────────────────────────

def test_valores_exactos_del_create(listo):
    pid = _queue(listo)
    writer = FakeWriter()
    outcome = service.send_one(listo, writer, pid, 5)
    assert (outcome.status, outcome.odoo_id, outcome.odoo_name, outcome.placa) == (
        SyncStatus.ENVIADO, 501, "POS-501", "ABC123")
    assert writer.busquedas == [writer.creados[0][1]]  # busca con los mismos valores antes de crear
    ref, vals, incierto = writer.creados[0]
    assert ref == UUID and incierto is False
    assert vals == {
        "employee_id": 100, "agreement_id": 55, "vehicle_id": 10,
        "km_start": 1510.0, "km_end": 1600.5,
        "check_in": "2026-11-26 13:00:00", "check_out": "2026-11-26 22:30:00",
        "attachment": base64.b64encode(PDF).decode(), "attachment_filename": "Planilla 26-11.pdf",
        "type_file": "pdf", "observations": "Sin novedades",
    }
    assert isinstance(vals["km_start"], float) and isinstance(vals["km_end"], float)
    # Lo que Odoo calcula o tiene su propio formulario no se envía.
    prohibidos = {"lunch_hour", "state", "km_traveled", "expenses", "gallons", "km_fuel", "company_id"}
    assert not prohibidos & set(vals) and not [k for k in vals if k.startswith(("feeding_", "lodging_", "tolls_",
                                                                               "others_", "fuel_"))]
    with listo.session() as s:
        pos = s.get(PosOperacional, pid)
        assert (pos.sync_status, pos.odoo_id, pos.odoo_name, pos.next_attempt_at) == (
            SyncStatus.ENVIADO, 501, "POS-501", None)


def _imagen(fmt: str) -> bytes:
    from PIL import Image

    salida = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 10, 10)).save(salida, fmt)
    return salida.getvalue()


@pytest.mark.parametrize("fmt, nombre, esperado", [
    ("JPEG", "foto.jpg", "foto.pdf"),
    ("PNG", "scan.png", "scan.pdf"),
    ("JPEG", None, "planilla.pdf"),
])
def test_foto_se_convierte_a_pdf_antes_de_odoo(listo, fmt, nombre, esperado):
    seeded = listo
    """Regla 2026-11-26: pos.operacional recibe la planilla solo en PDF."""
    pid = _queue(seeded, planilla=_b64(_imagen(fmt), "application/octet-stream"), planilla_nombre=nombre)
    writer = FakeWriter()
    service.send_one(seeded, writer, pid, 5)
    vals = writer.creados[0][1]
    assert vals["type_file"] == "pdf" and vals["attachment_filename"] == esperado
    assert base64.b64decode(vals["attachment"]).startswith(b"%PDF")


@pytest.mark.parametrize("nombre, esperado", [("factura.pdf", "factura.pdf"),
                                              ("C:\fakepath\mi planilla.PDF", "mi planilla.pdf")])
def test_pdf_viaja_tal_cual(listo, nombre, esperado):
    seeded = listo
    pid = _queue(seeded, planilla=_b64(PDF, "application/pdf"), planilla_nombre=nombre)
    writer = FakeWriter()
    service.send_one(seeded, writer, pid, 5)
    vals = writer.creados[0][1]
    assert vals["type_file"] == "pdf" and vals["attachment_filename"] == esperado
    assert base64.b64decode(vals["attachment"]) == PDF


def test_foto_danada_se_rechaza(seeded):
    with pytest.raises(service.SubmissionError):
        _queue(seeded, planilla=_b64(JPG, "image/jpeg"))


def test_observaciones_vacias_se_envian_en_blanco(listo):
    seeded = listo
    pid = _queue(seeded, observaciones="   ")
    writer = FakeWriter()
    service.send_one(seeded, writer, pid, 5)
    assert writer.creados[0][1]["observations"] == ""


def test_enviar_no_libera_la_reserva_del_vehiculo(listo):
    pid = _queue(listo)
    service.send_one(listo, FakeWriter(), pid, 5)
    assert reservas.reservas_de(listo, TG_USER) == ("ABC123",)


def test_dedupe_existente_no_crea_otro(listo):
    pid = _queue(listo)
    writer = FakeWriter(existente=777)
    outcome = service.send_one(listo, writer, pid, 5)
    assert (outcome.status, outcome.odoo_id) == (SyncStatus.ENVIADO, 777) and writer.creados == []


def test_dedupe_con_el_writer_real_busca_por_empleado_entrada_y_salida(listo):
    objects = _CapturaBusqueda(natural_hit=888)
    pid = _queue(listo)
    outcome = service.send_one(listo, _writer(objects), pid, 5)
    assert (outcome.status, outcome.odoo_id, objects.created) == (SyncStatus.ENVIADO, 888, [])
    modelo, dominio = objects.busquedas[0]
    assert modelo == "pos.operacional"
    assert dominio == [("employee_id", "=", 100), ("check_in", "=", "2026-11-26 13:00:00"),
                       ("check_out", "=", "2026-11-26 22:30:00")]


class _CapturaBusqueda(FakeObjects):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.busquedas: list[tuple[str, list]] = []

    def execute_kw(self, db, uid, pw, model, method, args, kwargs):
        if method == "search" and model == "pos.operacional":
            self.busquedas.append((model, args[0]))
        return super().execute_kw(db, uid, pw, model, method, args, kwargs)


def test_sin_duplicado_el_writer_real_crea_en_pos_operacional(listo):
    objects = FakeObjects()
    pid = _queue(listo)
    outcome = service.send_one(listo, _writer(objects), pid, 5)
    assert outcome.status == SyncStatus.ENVIADO and outcome.odoo_id == 901
    assert objects.created[0]["km_end"] == 1600.5 and objects.created[0]["type_file"] == "pdf"


def test_un_fault_de_validacion_deja_fallido_sin_reintento(listo):
    objects = FakeObjects(create_errors=[xmlrpc.client.Fault(2, "Traceback...\nValidationError: KM final inválido")])
    pid = _queue(listo)
    outcome = service.send_one(listo, _writer(objects), pid, 5)
    assert outcome.status == SyncStatus.FALLIDO and "KM final inválido" in outcome.error
    assert objects.created == []
    with listo.session() as s:
        pos = s.get(PosOperacional, pid)
        assert pos.sync_status == SyncStatus.FALLIDO and pos.next_attempt_at is None
        assert "Odoo rechazó el registro" in pos.error_log
    assert service.claim_batch(listo, 10, 300) == []  # no se reintenta solo


def test_un_error_de_red_se_reintenta_con_backoff(listo, monkeypatch):
    import app.preop.odoo_writer as ow

    monkeypatch.setattr(ow, "wait_exponential_jitter", lambda **_: wait_none())
    objects = FakeObjects(create_errors=[TimeoutError("timed out")] * 3)
    pid = _queue(listo)
    outcome = service.send_one(listo, _writer(objects), pid, 5)
    assert outcome.status == SyncStatus.ERROR and "Intento 1/5" in outcome.error
    with listo.session() as s:
        pos = s.get(PosOperacional, pid)
        assert pos.sync_status == SyncStatus.ERROR and pos.next_attempt_at.replace(tzinfo=UTC) > utc_now()
    assert service.claim_batch(listo, 10, 300) == []  # aún no toca
    assert service.claim_batch(listo, 10, 300, now=utc_now() + timedelta(hours=2)) == [pid]


def test_red_caida_agota_los_intentos_y_queda_fallido(listo):
    pid = _queue(listo)
    outcome = service.send_one(listo, FakeWriter(error=OdooTransientError("sin red")), pid, 1)
    assert outcome.status == SyncStatus.FALLIDO and "Intento 1/1" in outcome.error


def test_un_timeout_tras_crear_no_duplica_en_el_reintento(listo):
    pid = _queue(listo)
    primero = service.send_one(listo, FakeWriter(error=OdooTransientError("timeout")), pid, 5)
    assert primero.status == SyncStatus.ERROR
    assert service.claim_batch(listo, 10, 300, now=utc_now() + timedelta(hours=2)) == [pid]
    # Odoo sí lo había creado: el segundo intento lo encuentra por empleado + entrada + salida.
    writer = FakeWriter(existente=654)
    segundo = service.send_one(listo, writer, pid, 5)
    assert (segundo.status, segundo.odoo_id) == (SyncStatus.ENVIADO, 654) and writer.creados == []


def test_un_fallo_permanente_de_la_busqueda_no_impide_crear(listo, caplog):
    class BuscaMal(FakeWriter):
        def buscar_pos_operacional(self, vals):
            raise OdooPermanentError("campo inexistente")

    pid = _queue(listo)
    writer = BuscaMal()
    with caplog.at_level("WARNING"):
        outcome = service.send_one(listo, writer, pid, 5)
    assert outcome.status == SyncStatus.ENVIADO and len(writer.creados) == 1
    assert "no se pudo buscar un duplicado" in caplog.text


def test_requeue_devuelve_los_fallidos_a_pendiente(listo):
    pid = _queue(listo)
    service.send_one(listo, FakeWriter(error=OdooPermanentError("no")), pid, 5)
    assert service.requeue_failed(listo, [999]) == 0
    assert service.requeue_failed(listo) == 1
    assert service.claim_batch(listo, 10, 300) == [pid]
    with listo.session() as s:
        assert s.get(PosOperacional, pid).attempts == 2  # attempts=1 al reencolar: el siguiente busca antes de crear


def test_el_worker_envia_y_avisa_solo_el_resultado_final(listo):
    avisos: list[Any] = []

    async def notify(outcome):
        avisos.append(outcome)

    _recibir(listo)
    sender = PosOperacionalSender(listo, FakeWriter(), 60, 5, 300, notify=notify)
    outcomes = asyncio.run(sender.run_once())
    assert [o.status for o in outcomes] == [SyncStatus.ENVIADO] and avisos == outcomes
    assert asyncio.run(sender.run_once()) == []
    assert "#1" in listar(listo)[0] and "enviado" in listar(listo)[0]


# ───────────────────────────────────────── mensajes al conductor ─────────────────────────────────────────

def _outcome(**over: Any):
    base = dict(pos_id=7, telegram_user_id=TG_USER, status=SyncStatus.ENVIADO, odoo_id=501, odoo_name="POS-501",
                placa="ABC123")
    base.update(over)
    return service.PosOperacionalOutcome(**base)


def test_mensajes_recibido_registrado_y_fallido():
    recibido = formatting.pos_operacional_recibido("ABC123")
    assert "recibido" in recibido and "ABC123" in recibido and "Finalizar" in recibido and "no libera" in recibido
    assert "(" not in formatting.pos_operacional_recibido(None).split("</b>")[0]  # sin placa no hay paréntesis
    ok = formatting.pos_operacional_result(_outcome())
    assert "registrado en Odoo" in ok and "#501" in ok and "POS-501" in ok and "Finalizar" in ok and "ABC123" in ok
    mal = formatting.pos_operacional_result(_outcome(
        status=SyncStatus.FALLIDO, odoo_id=None, error="Odoo rechazó el registro: ValidationError: <km> inválido"))
    assert "no pudo guardarse" in mal and "ValidationError" in mal and "&lt;km&gt;" in mal and "#7" in mal


# ───────────────────────────────────────── API y envío sin señal ─────────────────────────────────────────

@pytest.fixture
def api(seeded):
    app = FastAPI()
    app.include_router(pos_router)
    woke: list[int] = []
    avisos: list[Any] = []

    async def on_accepted(recibido):
        avisos.append((recibido.id, recibido.placa))

    app.state.pos_operacional = PosOperacionalServices(
        seeded, BOT_TOKEN, 3600, wake_worker=lambda: woke.append(1), on_accepted=on_accepted)
    client = TestClient(app)
    client.woke, client.avisos = woke, avisos  # type: ignore[attr-defined]
    return client


def _headers(user_id=TG_USER):
    return {"X-Telegram-Init-Data": make_init_data(user_id=user_id)}


def _body(**over: Any) -> dict:
    return _form(**over).model_dump(mode="json", exclude_none=False)


def _diferido_body(*, abierto_hace: timedelta, sin_senal_hace: timedelta, **over: Any) -> dict:
    body = _body(**over)
    now = datetime.now(UTC)
    body["diferido"] = {
        "init_data_original": make_init_data(user_id=TG_USER, auth_date=int((now - abierto_hace).timestamp())),
        "sin_senal_desde": (now - sin_senal_hace).isoformat(),
    }
    return body


def test_api_exige_init_data_y_empleado(api):
    assert api.get("/api/pos-operacional/context").status_code == 401
    assert api.get("/api/pos-operacional/form").status_code == 401
    assert api.post("/api/pos-operacional", json={}).status_code == 401
    assert api.get(f"/api/pos-operacional/{UUID}").status_code == 401
    malo = {"X-Telegram-Init-Data": make_init_data(tamper=True)}
    assert api.get("/api/pos-operacional/context", headers=malo).status_code == 401
    assert api.post("/api/pos-operacional", json=_body(), headers=malo).status_code == 401
    # Firma válida pero sin PIN de empleado: 403.
    assert api.get("/api/pos-operacional/context", headers=_headers(user_id=1)).status_code == 403
    assert api.post("/api/pos-operacional", json=_body(), headers=_headers(user_id=1)).status_code == 403
    assert api.get(f"/api/pos-operacional/{UUID}", headers=_headers(user_id=1)).status_code == 404
    assert api.woke == [] and api.avisos == []


def test_api_sin_servicios_responde_503():
    app = FastAPI()
    app.include_router(pos_router)
    assert TestClient(app).get("/api/pos-operacional/form", headers=_headers()).status_code == 503


def test_api_contexto_envio_duplicado_y_estado(api, seeded):
    for r in (api.get("/api/pos-operacional/context", headers=_headers()),
              api.post("/api/pos-operacional", json=_body(), headers=_headers())):  # sin reserva: 409 con código
        assert r.status_code == 409 and r.json()["detail"] == reservas.MSG_SIN_RESERVA
        assert r.headers["x-error-code"] == reservas.CODIGO_SIN_RESERVA
    assert api.woke == [] and api.avisos == []
    _comb._reservar(seeded)
    ctx = api.get("/api/pos-operacional/context", headers=_headers()).json()
    assert ctx["vehiculo"]["placa"] == "ABC123" and ctx["km_inicial"] == 1510.0 and ctx["contrato"]["id"] == 55
    assert api.get("/api/pos-operacional/form", headers=_headers()).json()["rango_max_horas"] == 24

    primero = api.post("/api/pos-operacional", json=_body(), headers=_headers())
    assert primero.status_code == 202
    assert (primero.json()["sync_status"], primero.json()["duplicado"]) == ("pendiente", False)
    assert api.woke == [1] and api.avisos == [(primero.json()["id"], "ABC123")]
    otra = api.post("/api/pos-operacional", json=_body(), headers=_headers())
    assert otra.json()["duplicado"] is True and len(api.avisos) == 1  # el duplicado no repite el aviso
    assert api.get(f"/api/pos-operacional/{UUID}", headers=_headers()).json()["id"] == primero.json()["id"]
    assert api.get(f"/api/pos-operacional/{UUID}", headers=_headers(user_id=1)).status_code == 404


def test_api_km_final_menor_o_salida_no_posterior_dan_422(api):
    r = api.post("/api/pos-operacional", json=_body() | {"km_final": 100}, headers=_headers())
    assert r.status_code == 422 and "KM final no puede ser menor" in r.text
    r = api.post("/api/pos-operacional", json=_body() | {"salida": ENTRADA}, headers=_headers())
    assert r.status_code == 422 and "posterior a la entrada" in r.text
    r = api.post("/api/pos-operacional", json=_body() | {"salida": "2026-11-26T07:00"}, headers=_headers())
    assert r.status_code == 422
    r = api.post("/api/pos-operacional", json=_body() | {"planilla": _b64(b"GIF89a" + b"0" * 40, "image/gif")},
                 headers=_headers())
    assert r.status_code == 422 and "PDF o una foto" in r.text
    assert api.woke == [] and api.avisos == []


def test_api_acepta_km_con_coma_y_las_horas_de_colombia(api, listo):
    seeded = listo
    body = _body() | {"km_inicial": "1510,5", "km_final": "1600,75", "entrada": "2026-11-26T08:00",
                      "salida": "2026-11-26T17:30"}
    assert api.post("/api/pos-operacional", json=body, headers=_headers()).status_code == 202
    with seeded.session() as s:
        pos = s.scalar(select(PosOperacional))
        assert (pos.km_inicial, pos.km_final) == (1510.5, 1600.75)
        assert pos.check_in.replace(tzinfo=None) == datetime(2026, 11, 26, 13, 0)
        assert pos.check_out.replace(tzinfo=None) == datetime(2026, 11, 26, 22, 30)


def test_api_aviso_fallido_de_recepcion_no_devuelve_error(listo):
    seeded = listo
    app = FastAPI()
    app.include_router(pos_router)

    async def boom(_):
        raise RuntimeError("telegram caído")

    app.state.pos_operacional = PosOperacionalServices(seeded, BOT_TOKEN, 3600, on_accepted=boom)
    assert TestClient(app).post("/api/pos-operacional", json=_body(), headers=_headers()).status_code == 202


def test_diferido_en_ventana_flujo_normal_y_trazabilidad(api, listo):
    seeded = listo
    antedatar_reservas(seeded)  # la reserva ya existía al abrir el formulario
    body = _diferido_body(abierto_hace=timedelta(minutes=80), sin_senal_hace=timedelta(minutes=40))
    headers = {"X-Telegram-Init-Data": body["diferido"]["init_data_original"]}
    first = api.post("/api/pos-operacional", json=body, headers=headers)
    assert first.status_code == 202 and api.woke == [1] and api.avisos == [(first.json()["id"], "ABC123")]
    with seeded.session() as s:
        pos = s.scalar(select(PosOperacional))
        traza = s.scalar(select(PosOperacionalEnvioDiferido))
        assert traza.estado == EstadoDiferido.ACEPTADO and traza.pos_id == pos.id and traza.payload is None
    # Primer intento que sí llegó: el reenvío con el mismo uuid responde como duplicado y no repite el aviso.
    again = api.post("/api/pos-operacional", json=body, headers=headers)
    assert again.json()["duplicado"] is True and len(api.avisos) == 1
    assert service.claim_batch(seeded, 10, 300) == [pos.id]  # sin ubicación que esperar: sale a Odoo


def test_diferido_vencido_solo_auditoria_conserva_el_formulario_y_la_planilla(api, seeded):
    body = _diferido_body(abierto_hace=timedelta(hours=3), sin_senal_hace=timedelta(minutes=150))
    assert api.post("/api/pos-operacional", json=body, headers=_headers()).status_code == 410
    assert api.post("/api/pos-operacional", json=body, headers=_headers()).status_code == 410
    assert api.woke == [] and api.avisos == []
    with seeded.session() as s:
        assert s.scalar(select(func.count()).select_from(PosOperacional)) == 0
        traza = s.scalar(select(PosOperacionalEnvioDiferido))
        assert traza.estado == EstadoDiferido.VENCIDO and "min después de perder la señal" in traza.motivo
        assert traza.payload["planilla"] == body["planilla"] and traza.payload["km_final"] == 1600.5
        assert "diferido" not in traza.payload


def test_diferido_con_firma_invalida_da_401(api):
    body = _diferido_body(abierto_hace=timedelta(minutes=10), sin_senal_hace=timedelta(minutes=5))
    body["diferido"]["init_data_original"] = make_init_data(tamper=True)
    headers = {"X-Telegram-Init-Data": make_init_data(tamper=True)}
    assert api.post("/api/pos-operacional", json=body, headers=headers).status_code == 401


def test_pagina_sin_cache_logo_versionado_y_marcas_del_formulario(api):
    page = api.get("/pos-operacional")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
    import re

    assert re.findall(r'src="/static/viacotur-logo\.png\?v=([0-9a-f]{12})"', page.text)
    for marca in ('IDB_NAME = "pos-operacional-offline"', "vaciarBandeja()", 'addEventListener("online"',
                  "Información del personal", "Contrato", "KM inicial", "KM final", "Entrada", "Salida",
                  "Planilla de servicios", "Observaciones", "Primero el Preoperacional", "sin_reserva_vigente", "getUserMedia", "abrirCamara",
                  "capturarFoto", "capture", "application/pdf", "%PDF", "normalizarDecimal", "parseNum", "fmtNum",
                  'inputmode="decimal"', "Finalizar", "/api/pos-operacional"):
        assert marca in page.text
    assert "innerHTML" not in page.text and "toLocaleString" not in page.text
    assert "location" not in page.text.lower()  # no se pide ubicación


# ─────────────────────────────────────────────── menú del bot ───────────────────────────────────────────────

def test_el_boton_va_justo_debajo_de_gastos_operativos_en_el_menu():
    etiquetas = [f.label for f in tga.FORMULARIOS]
    assert tga.BTN_POS_OPERACIONAL == "🧾 Pos-Operacional"
    assert etiquetas.index(tga.BTN_POS_OPERACIONAL) == etiquetas.index(tga.BTN_GASTOS) + 1
    from .test_handlers import _services

    inline = tga.formularios_inline(_services().settings).inline_keyboard
    textos = [fila[0].text for fila in inline]
    assert textos.index(tga.BTN_POS_OPERACIONAL) == textos.index(tga.BTN_GASTOS) + 1
    assert textos.index(tga.BTN_POS_OPERACIONAL) < textos.index(tga.BTN_ESCANEAR) < textos.index(tga.BTN_FINALIZAR)
    boton = inline[textos.index(tga.BTN_POS_OPERACIONAL)][0]
    assert boton.web_app.url == "https://scan.example.com/pos-operacional"
    assert "pos_operacional" in [c.command for c in tga.BOT_COMMANDS]
    assert tga.BTN_POS_OPERACIONAL in tga.HELP_TEXT


def test_comando_pos_operacional_abre_la_mini_app():
    from .test_handlers import _ctx, _services, _update

    upd = _update(text="/pos_operacional")
    asyncio.run(tga.cmd_pos_operacional(upd, _ctx(_services())))
    call = upd.message.reply_text.await_args
    assert "Finalizar" in call.args[0]
    assert call.kwargs["reply_markup"].inline_keyboard[0][0].web_app.url == "https://scan.example.com/pos-operacional"


def test_borrar_el_envio_deja_la_traza_sin_pos(listo):
    pid = _recibir(listo)
    with listo.session() as s:
        s.add(PosOperacionalEnvioDiferido(
            uuid=UUID, telegram_user_id=TG_USER, estado=EstadoDiferido.ACEPTADO, pos_id=pid,
            apertura_at=utc_now(), sin_senal_desde=utc_now(), retraso_segundos=0, payload_sha256="0" * 64))
    with listo.session() as s:
        s.delete(s.get(PosOperacional, pid))
    with listo.session() as s:
        assert s.scalar(select(PosOperacionalEnvioDiferido.pos_id)) is None
