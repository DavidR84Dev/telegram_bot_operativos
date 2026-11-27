"""Pruebas de Correcciones Post y Gastos: lista en vivo desde Odoo, esquema, recepción, cola, payload de la corrección
de gasto (``hr.expense`` nuevo), escritura del Post-operacional sobre el mismo registro, descarte, errores, envío sin
señal y API. No exige reserva vigente (el botón del teclado se prueba en ``tests/test_handlers.py``)."""
from __future__ import annotations

import base64
import io
import xmlrpc.client
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select

from app import formatting
from app.combustible.service import GASTOS_PATH
from app.correcciones import catalogo as cat
from app.correcciones import service
from app.correcciones.router import CorreccionesServices
from app.correcciones.router import router as correcciones_router
from app.correcciones.schemas import CorreccionIn
from app.correcciones.worker import CorreccionesSender
from app.config import get_settings
from app.preop import reservas
from app.preop.models import Correccion, CorreccionEnvioDiferido, EstadoDiferido, ReservaVehiculo, SyncStatus, utc_now
from app.preop.odoo_writer import OdooPermanentError, OdooTransientError
from app.preop.service import SubmissionError
from app.preop.rest_client import OdooApiError, _RetryableHttpError

from . import test_combustible as _comb
from . import test_preop as _preop
from .helpers import BOT_TOKEN, make_init_data
from .test_preop import TG_USER, FakeObjects, _writer

db = _preop.db
seeded = _preop.seeded

IDENTITY = _comb.IDENTITY
UUID = "7a1b2c3d-3c1d-4f7a-9a2b-1c2d3e4f5a6c"
PDF = b"%PDF-1.4\n" + b"0" * 64
PDF_B64 = "data:application/pdf;base64," + base64.b64encode(PDF).decode()
JPG_B64 = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 64).decode()  # firma sin imagen


def _foto_b64(formato: str = "JPEG") -> str:
    """Foto real de 40x30 (JPEG o PNG) en base64, como la que manda la Mini App desde la cámara o la galería."""
    from PIL import Image

    salida = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 30, 30)).save(salida, formato)
    mime = "jpeg" if formato == "JPEG" else "png"
    return f"data:image/{mime};base64," + base64.b64encode(salida.getvalue()).decode()
EMPLEADO_ODOO = 100  # ``seeded``: Ana Conductora


def _hoy() -> str:
    return reservas.fecha_jornada().isoformat()


AYER = (reservas.fecha_jornada() - timedelta(days=1)).isoformat()
PASADO_MANANA = (reservas.fecha_jornada() + timedelta(days=1)).isoformat()


def _original_gasto(**over: Any) -> dict[str, Any]:
    return {"id": 700, "name": "ALIMENTACIÓN", "product_id": [1, "Meals"], "total_amount": 65000.0,
            "employee_id": [EMPLEADO_ODOO, "Ana Conductora"], "date": _hoy(), "description": "Almuerzo",
            "state_expenses": "refused", **over}


def _original_pos(**over: Any) -> dict[str, Any]:
    return {"id": 800, "name": "POS/0001", "employee_id": [EMPLEADO_ODOO, "Ana Conductora"],
            "agreement_id": [55, "Contrato Minera"], "vehicle_id": [10, "BUS 01/ABC123"],
            "check_in": f"{AYER} 12:00:00", "check_out": f"{AYER} 22:00:00", "km_start": 1000.0,
            "km_end": 1100.0, "state": "refused", "observations": "Sin novedad", "attachment_filename": "p.pdf",
            "corrections": "Falta el valor", "corrections_hr": False, **over}


class FakeOdoo:
    """Writer de prueba: lecturas y escrituras de las correcciones, con registro de lo que se pidió."""

    def __init__(self, gastos: list | None = None, pos: list | None = None, registros: dict | None = None,
                 existentes: list[Any] | None = None, write_error: Exception | None = None,
                 pos_rango: list | None = ()) -> None:  # type: ignore[assignment]
        self.gastos, self.pos = gastos, pos  # None = Odoo no responde
        self.pos_rango = pos_rango  # pos.operacional del empleado por rango de fechas (None = Odoo no responde)
        self.rangos: list[tuple[int, datetime, datetime]] = []
        self.busquedas_planilla: list[tuple[int, str, datetime]] = []
        self.existentes_planilla: list[Any] = []
        self.registros = registros if registros is not None else {}
        self.existentes = list(existentes or [])
        self.write_error = write_error
        self.escritos: list[tuple[int, dict]] = []
        self.busquedas: list[tuple[dict, list[int], datetime]] = []
        self.consultas: list[tuple[str, int]] = []
        self.desdes: list[Any] = []

    def gastos_rechazados(self, employee_odoo_id, desde=None):
        self.consultas.append(("gastos", employee_odoo_id))
        self.desdes.append(desde)
        return self.gastos

    def pos_pendientes(self, employee_odoo_id, desde=None):
        self.consultas.append(("pos", employee_odoo_id))
        self.desdes.append(desde)
        return self.pos

    def pos_en_rango(self, employee_odoo_id, inicio, fin):
        self.rangos.append((employee_odoo_id, inicio, fin))
        return None if self.pos_rango is None else list(self.pos_rango)

    def buscar_pos_planilla(self, employee_odoo_id, check_in, desde):
        self.busquedas_planilla.append((employee_odoo_id, check_in, desde))
        if not self.existentes_planilla:
            return None
        r = self.existentes_planilla.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def leer_registro(self, model, record_id, campos):
        r = self.registros.get((model, record_id))
        if isinstance(r, Exception):
            raise r
        return dict(r) if r is not None else None

    def escribir_pos_operacional(self, pos_id, vals):
        if self.write_error:
            raise self.write_error
        self.escritos.append((pos_id, vals))
        self.registros[("pos.operacional", pos_id)] = {**self.registros[("pos.operacional", pos_id)], **{
            k: v for k, v in vals.items() if k in ("state", "km_start", "km_end", "check_in", "check_out",
                                                    "observations")}}

    def buscar_gasto_corregido(self, vals, excluir_ids, desde):
        self.busquedas.append((vals, list(excluir_ids), desde))
        if not self.existentes:
            return None
        r = self.existentes.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeRest:
    """Cliente REST de prueba: una respuesta (o excepción) por POST; sin lista, ids 9001, 9002..."""

    def __init__(self, respuestas: list[Any] | None = None) -> None:
        self.respuestas = list(respuestas or [])
        self.posts: list[tuple[str, dict]] = []

    def post_json_once(self, path, body):
        self.posts.append((path, body))
        r = self.respuestas.pop(0) if self.respuestas else {"id": 9000 + len(self.posts)}
        if isinstance(r, Exception):
            raise r
        return r


def _odoo(**kw: Any) -> FakeOdoo:
    registros = {("hr.expense", 700): _original_gasto(), ("pos.operacional", 800): _original_pos()}
    registros.update(kw.pop("registros", {}))
    return FakeOdoo(registros=registros, **kw)


def _gasto_raw(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"tipo": "gasto", "uuid": UUID, "odoo_id": 700, "total": 70000, "fecha": _hoy(),
                            "descripcion": "El valor correcto es 70.000", "pdf": PDF_B64, "pdf_nombre": "factura.pdf"}
    data.update(over)
    return data


def _pos_raw(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"tipo": "pos", "uuid": UUID, "odoo_id": 800, "km_inicial": 1000, "km_final": 1120,
                            "entrada": f"{AYER}T07:30", "salida": f"{AYER}T17:30",
                            "observaciones": "Corregí el KM final", "planilla": None, "planilla_nombre": None}
    data.update(over)
    return data


def _in(raw: dict[str, Any]):
    return TypeAdapter(CorreccionIn).validate_python(raw)


def _recibir(database, odoo: FakeOdoo, raw: dict[str, Any]) -> int:
    return service.submit(database, odoo, IDENTITY, _in(raw))[0].id  # type: ignore[arg-type]


def _reclamar(database, correccion_id: int) -> None:
    assert service.claim_batch(database, 10, 300) == [correccion_id]


def _fila(database, correccion_id: int) -> Correccion:
    with database.session() as s:
        return s.get(Correccion, correccion_id)


# ────────────────────────────────────── lista en vivo desde Odoo ──────────────────────────────────────

def test_el_writer_lee_solo_rechazados_y_pendientes_del_empleado():
    """Las consultas XML-RPC son de solo lectura y filtran por empleado y estado."""
    llamadas: list[tuple] = []

    class Objetos(FakeObjects):
        def execute_kw(self, db, uid, pw, model, method, args, kwargs):
            llamadas.append((model, method, args, kwargs))
            return [{"id": 1}]

    w = _writer(Objetos())
    assert w.gastos_rechazados(100) == [{"id": 1}] and w.pos_pendientes(100) == [{"id": 1}]
    (m1, metodo1, args1, _), (m2, metodo2, args2, kw2) = llamadas
    assert (m1, metodo1) == ("hr.expense", "search_read")
    assert args1[0] == [["employee_id", "=", 100], ["state_expenses", "in", ["refused"]]]
    assert (m2, metodo2) == ("pos.operacional", "search_read")
    assert args2[0] == [["employee_id", "=", 100], ["state", "in", ["refused", "refused by", "correct by"]]]
    assert "attachment" not in kw2["fields"]  # el binario de la planilla no se lee


def test_el_writer_filtra_por_antiguedad_en_el_dominio_de_odoo():
    """Con ``desde`` el filtro de días viaja en el dominio (no se descarga todo para filtrar en Python)."""
    llamadas: list[tuple] = []

    class Objetos(FakeObjects):
        def execute_kw(self, db, uid, pw, model, method, args, kwargs):
            llamadas.append((model, args[0]))
            return []

    w = _writer(Objetos())
    desde = reservas.fecha_jornada() - timedelta(days=30)
    w.gastos_rechazados(100, desde)
    w.pos_pendientes(100, desde)
    (_, dom_gasto), (_, dom_pos) = llamadas
    assert ["date", ">=", desde.isoformat()] in dom_gasto
    inicio = f"{desde.isoformat()} 05:00:00"  # 00:00 de Colombia en UTC
    assert dom_pos[-5:] == ["|", ["check_in", ">=", inicio], "&", ["check_in", "=", False],
                            ["create_date", ">=", inicio]]
    assert ["employee_id", "=", 100] in dom_pos


def test_el_writer_devuelve_none_si_odoo_no_responde():
    class Caido(FakeObjects):
        def execute_kw(self, *a):
            raise xmlrpc.client.Fault(1, "AccessDenied")

    w = _writer(Caido())
    assert w.gastos_rechazados(100) is None and w.pos_pendientes(100) is None


def test_contexto_lista_lo_del_empleado_y_excluye_lo_ya_corregido(seeded):
    odoo = _odoo(gastos=[_original_gasto(), _original_gasto(id=701, name="PEAJES")],
                 pos=[_original_pos(), _original_pos(id=801, name="POS/0002", state="refused by")])
    empleado = _preop.service.employee_for_telegram(seeded, TG_USER)
    ctx = service.contexto(seeded, odoo, empleado)
    assert ctx["odoo_ok"] is True and odoo.consultas == [("gastos", EMPLEADO_ODOO), ("pos", EMPLEADO_ODOO)]
    desde = reservas.fecha_jornada() - timedelta(days=30)  # por defecto 30 días
    assert odoo.desdes == [desde, desde] and ctx["dias_maximos"] == 30 and ctx["desde"] == desde.isoformat()
    assert [g["id"] for g in ctx["gastos"]] == [700, 701] and ctx["gastos"][0]["tipo"] == "Meals"
    assert [p["estado_texto"] for p in ctx["pos"]] == ["PENDIENTE GASTOS", "PENDIENTE NÓMINA"]
    assert ctx["pos"][0]["entrada"] == f"{AYER}T07:00" and ctx["pos"][0]["salida"] == f"{AYER}T17:00"
    assert ctx["pos"][0]["vehiculo"] == "BUS 01/ABC123" and ctx["pos"][0]["revision"] == "Falta el valor"

    _recibir(seeded, odoo, _gasto_raw())
    _recibir(seeded, odoo, _pos_raw(uuid="1a1b2c3d-3c1d-4f7a-9a2b-1c2d3e4f5a6c"))
    ctx = service.contexto(seeded, odoo, empleado)
    assert [g["id"] for g in ctx["gastos"]] == [701] and [p["id"] for p in ctx["pos"]] == [801]


def test_una_correccion_fallida_o_descartada_no_oculta_el_registro(seeded):
    odoo = _odoo(gastos=[_original_gasto()], pos=[])
    cid = _recibir(seeded, odoo, _gasto_raw())
    empleado = _preop.service.employee_for_telegram(seeded, TG_USER)
    assert service.contexto(seeded, odoo, empleado)["gastos"] == []
    with seeded.session() as s:
        s.get(Correccion, cid).sync_status = SyncStatus.FALLIDO
    assert [g["id"] for g in service.contexto(seeded, odoo, empleado)["gastos"]] == [700]


def test_contexto_con_odoo_caido_da_un_mensaje_claro(seeded):
    empleado = _preop.service.employee_for_telegram(seeded, TG_USER)
    ctx = service.contexto(seeded, FakeOdoo(gastos=None, pos=[]), empleado)
    assert ctx["odoo_ok"] is False and "Odoo" in ctx["mensaje"] and ctx["gastos"] == [] and ctx["pos"] == []


# ───────────────────────────────────────────── esquema ─────────────────────────────────────────────

def test_esquema_gasto_normaliza_la_coma_y_conserva_el_pdf():
    d = _in(_gasto_raw(total="65000,5"))
    assert d.total == 65000.5 and d.pdf_nombre == "factura.pdf" and d.uuid == UUID


@pytest.mark.parametrize("cambio, error", [
    ({"pdf": "aGVsbG8gd29ybGQgbm8gZXMgcGRm"}, "PDF o una foto"),     # ni PDF ni JPG/PNG
    ({"pdf": "@@@@@@@@@@@@@@@@@@@@"}, "base64"),
    ({"descripcion": "   "}, "at least 1"),
    ({"total": 0}, "greater than 0"),
    ({"total": 10 ** 10}, "less than or equal"),
    ({"total": 10.123}, "2 decimales"),
    ({"total": True}, "número"),
    ({"fecha": (reservas.fecha_jornada() + timedelta(days=2)).isoformat()}, "futura"),
    ({"odoo_id": 0}, "greater than 0"),
    ({"tipo": "otro"}, "tipo"),
    ({"telegram_user_id": 1}, "telegram_user_id"),
])
def test_esquema_gasto_rechaza(cambio, error):
    with pytest.raises(ValidationError, match=error):
        _in(_gasto_raw(**cambio))


def test_esquema_gasto_exige_pdf_y_descripcion():
    for clave in ("pdf", "descripcion"):
        raw = _gasto_raw()
        del raw[clave]
        with pytest.raises(ValidationError, match=clave):
            _in(raw)


@pytest.mark.parametrize("cambio, error", [
    ({"observaciones": ""}, "at least 1"),
    ({"observaciones": None}, "valid string"),
    ({"km_final": 900}, "no puede ser menor"),
    ({"salida": f"{AYER}T07:30"}, "posterior"),
    ({"salida": f"{PASADO_MANANA}T07:30"}, "24 horas"),
    ({"planilla": "aGVsbG8gd29ybGQgbm8gZXMgcGRm"}, "PDF o una foto"),
    ({"km_inicial": -1}, "greater than or equal"),
])
def test_esquema_pos_rechaza(cambio, error):
    with pytest.raises(ValidationError, match=error):
        _in(_pos_raw(**cambio))


def test_esquema_pos_planilla_opcional_y_horas_en_utc():
    d = _in(_pos_raw(km_inicial="1000,5"))
    assert d.planilla is None and d.km_inicial == 1000.5
    assert d.entrada == datetime(2026, 11, 26, 12, 30, tzinfo=UTC)  # 07:30 de Colombia
    assert _in(_pos_raw(planilla=PDF_B64)).planilla == PDF_B64


# ───────────────────────────── fotos (se convierten a PDF) y ventana de antigüedad ─────────────────────────────

@pytest.mark.parametrize("formato", ["JPEG", "PNG"])
def test_gasto_con_foto_llega_a_odoo_como_pdf(seeded, formato):
    odoo = _odoo()
    cid = _recibir(seeded, odoo, _gasto_raw(pdf=_foto_b64(formato), pdf_nombre="IMG_0001.jpg"))
    c = _fila(seeded, cid)
    assert c.adjunto.startswith(b"%PDF") and c.adjunto_nombre == "IMG_0001.pdf"
    _reclamar(seeded, cid)
    rest = FakeRest()
    assert service.send_one(seeded, odoo, cid, 4, rest).status == SyncStatus.ENVIADO  # type: ignore[arg-type]
    body = rest.posts[0][1]
    assert body["type_file"] == "pdf" and body["attachment_filename"] == "IMG_0001.pdf"
    assert base64.b64decode(body["attachment"]).startswith(b"%PDF")


def test_planilla_de_pos_con_foto_llega_a_odoo_como_pdf(seeded):
    odoo = _odoo()
    cid = _recibir(seeded, odoo, _pos_raw(planilla=_foto_b64(), planilla_nombre="camara.jpeg"))
    _reclamar(seeded, cid)
    service.send_one(seeded, odoo, cid, 4)  # type: ignore[arg-type]
    vals = odoo.escritos[0][1]
    assert vals["type_file"] == "pdf" and vals["attachment_filename"] == "camara.pdf"
    assert base64.b64decode(vals["attachment"]).startswith(b"%PDF")


def test_foto_ilegible_es_422_y_no_se_guarda(seeded):
    for raw in (_gasto_raw(pdf=JPG_B64), _pos_raw(planilla=JPG_B64)):  # firma JPG pero sin imagen
        with pytest.raises(SubmissionError) as exc:
            service.submit(seeded, _odoo(), IDENTITY, _in(raw))  # type: ignore[arg-type]
        assert exc.value.status_code == 422
    with seeded.session() as s:
        assert s.scalar(select(func.count()).select_from(Correccion)) == 0


def test_la_ventana_de_dias_sale_de_la_configuracion(monkeypatch):
    assert get_settings().correcciones_dias_maximos == 30 and cat.dias_maximos() == 30
    monkeypatch.setattr(get_settings(), "correcciones_dias_maximos", 90)
    assert cat.dias_maximos() == 90 and cat.form_definition()["dias_maximos"] == 90
    monkeypatch.setattr(get_settings(), "correcciones_dias_maximos", 9999)
    assert cat.dias_maximos() == 365  # tope razonable


def test_esquema_gasto_fecha_fuera_de_la_ventana_se_rechaza(monkeypatch):
    hoy = reservas.fecha_jornada()
    assert _in(_gasto_raw(fecha=(hoy - timedelta(days=30)).isoformat()))  # el último día cabe
    with pytest.raises(ValidationError, match="más de 30 días"):
        _in(_gasto_raw(fecha=(hoy - timedelta(days=31)).isoformat()))
    monkeypatch.setattr(get_settings(), "correcciones_dias_maximos", 60)
    assert _in(_gasto_raw(fecha=(hoy - timedelta(days=60)).isoformat()))
    with pytest.raises(ValidationError, match="más de 60 días"):
        _in(_gasto_raw(fecha=(hoy - timedelta(days=61)).isoformat()))


def test_recibir_rechaza_un_original_fuera_de_la_ventana(seeded):
    viejo = (reservas.fecha_jornada() - timedelta(days=45))
    gasto_viejo = _odoo(registros={("hr.expense", 700): _original_gasto(date=viejo.isoformat())})
    with pytest.raises(SubmissionError, match="30 días") as exc:
        service.submit(seeded, gasto_viejo, IDENTITY, _in(_gasto_raw()))  # type: ignore[arg-type]
    assert exc.value.status_code == 422
    pos_viejo = _odoo(registros={("pos.operacional", 800): _original_pos(check_in=f"{viejo} 12:00:00")})
    with pytest.raises(SubmissionError) as exc:
        service.submit(seeded, pos_viejo, IDENTITY, _in(_pos_raw()))  # type: ignore[arg-type]
    assert exc.value.status_code == 422
    # Sin check_in se usa create_date.
    sin_entrada = _odoo(registros={("pos.operacional", 800): _original_pos(
        check_in=False, create_date=f"{viejo} 12:00:00")})
    with pytest.raises(SubmissionError) as exc:
        service.submit(seeded, sin_entrada, IDENTITY, _in(_pos_raw()))  # type: ignore[arg-type]
    assert exc.value.status_code == 422
    reciente = _odoo(registros={("pos.operacional", 800): _original_pos(
        check_in=False, create_date=f"{AYER} 12:00:00")})
    assert service.submit(seeded, reciente, IDENTITY, _in(_pos_raw()))[1] is False  # type: ignore[arg-type]


# ───────────────────────────────────────────── recepción ─────────────────────────────────────────────

def test_recibir_guarda_los_datos_fijos_del_original_y_no_exige_reserva(seeded):
    with seeded.session() as s:
        assert s.scalar(select(func.count()).select_from(ReservaVehiculo)) == 0  # sin reserva vigente
    cid = _recibir(seeded, _odoo(), _gasto_raw())
    c = _fila(seeded, cid)
    assert c.tipo == "gasto" and c.odoo_id_original == 700 and c.referencia == "ALIMENTACIÓN"
    assert c.datos == {"name": "ALIMENTACIÓN", "product_id": 1, "total_amount": 70000.0, "date": _hoy()}
    assert c.adjunto == PDF and c.adjunto_nombre == "factura.pdf" and c.sync_status == SyncStatus.PENDIENTE
    cp = _fila(seeded, _recibir(seeded, _odoo(), _pos_raw(uuid="2a1b2c3d-3c1d-4f7a-9a2b-1c2d3e4f5a6c")))
    assert cp.tipo == "pos" and cp.estado_original == "refused" and cp.adjunto is None
    assert cp.datos["check_in"] == f"{AYER} 12:30:00" and cp.datos["km_end"] == 1120.0


def test_recibir_es_idempotente_por_uuid_y_rechaza_una_segunda_correccion(seeded):
    odoo = _odoo()
    recibida, dup = service.submit(seeded, odoo, IDENTITY, _in(_gasto_raw()))  # type: ignore[arg-type]
    assert dup is False
    again, dup = service.submit(seeded, odoo, IDENTITY, _in(_gasto_raw()))  # type: ignore[arg-type]
    assert dup is True and again.id == recibida.id
    with pytest.raises(service.SubmissionError, match="en proceso") as exc:
        service.submit(seeded, odoo, IDENTITY, _in(_gasto_raw(uuid="3a1b2c3d-3c1d-4f7a-9a2b-1c2d3e4f5a6c")))  # type: ignore[arg-type]
    assert exc.value.status_code == 409


@pytest.mark.parametrize("registro, codigo", [
    (_original_gasto(employee_id=[999, "Otro"]), 403),
    (_original_gasto(state_expenses="approved"), 409),
    (_original_gasto(state_expenses="correction"), 409),
    (None, 404),
    (OdooTransientError("Sin respuesta de Odoo"), 503),
])
def test_recibir_rechaza_un_gasto_ajeno_resuelto_o_inexistente(seeded, registro, codigo):
    odoo = _odoo(registros={("hr.expense", 700): registro})
    with pytest.raises(service.SubmissionError) as exc:
        service.submit(seeded, odoo, IDENTITY, _in(_gasto_raw()))  # type: ignore[arg-type]
    assert exc.value.status_code == codigo


def test_recibir_un_pos_que_ya_no_esta_pendiente_se_rechaza(seeded):
    odoo = _odoo(registros={("pos.operacional", 800): _original_pos(state="approved")})
    with pytest.raises(service.SubmissionError, match="ya no está pendiente") as exc:
        service.submit(seeded, odoo, IDENTITY, _in(_pos_raw()))  # type: ignore[arg-type]
    assert exc.value.status_code == 409


def test_recibir_sin_empleado_es_403(seeded):
    from app.preop.service import TelegramIdentity

    with pytest.raises(service.SubmissionError) as exc:
        service.submit(seeded, _odoo(), TelegramIdentity(1, None, "X"), _in(_gasto_raw()))  # type: ignore[arg-type]
    assert exc.value.status_code == 403


# ──────────────────────────────────── corrección de gasto: envío ────────────────────────────────────

def test_payload_exacto_de_la_correccion_de_gasto(seeded):
    odoo = _odoo()
    cid = _recibir(seeded, odoo, _gasto_raw())
    _reclamar(seeded, cid)
    rest = FakeRest()
    out = service.send_one(seeded, odoo, cid, 4, rest)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and out.odoo_id == 9001 and out.tipo == "gasto"
    assert rest.posts == [(GASTOS_PATH, {
        "name": "ALIMENTACIÓN", "product_id": 1, "total_amount": 70000.0, "employee_id": EMPLEADO_ODOO,
        "description": "El valor correcto es 70.000", "date": _hoy(), "state_expenses": "correction",
        "type_file": "pdf", "attachment_filename": "factura.pdf", "attachment": base64.b64encode(PDF).decode(),
    })]
    assert list(rest.posts[0][1]) == ["name", "product_id", "total_amount", "employee_id", "description", "date",
                                      "state_expenses", "type_file", "attachment_filename", "attachment"]
    assert isinstance(rest.posts[0][1]["total_amount"], float)
    assert _fila(seeded, cid).sync_status == SyncStatus.ENVIADO and _fila(seeded, cid).odoo_id == 9001


def test_dedupe_excluye_el_original_y_busca_solo_correcciones(seeded):
    """Con un gasto de corrección ya creado (reintento tras un timeout) no se postea de nuevo."""
    odoo = _odoo(existentes=[9555])
    cid = _recibir(seeded, odoo, _gasto_raw())
    _reclamar(seeded, cid)
    rest = FakeRest()
    out = service.send_one(seeded, odoo, cid, 4, rest)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and out.odoo_id == 9555 and rest.posts == []
    vals, excluir, desde = odoo.busquedas[0]
    assert excluir == [700]  # nunca confunde el gasto rechazado original con la corrección
    assert vals["state_expenses"] == "correction" and desde == _fila(seeded, cid).fecha


def test_el_writer_busca_la_correccion_con_estado_fecha_y_sin_el_original():
    llamadas: list[tuple] = []

    class Objetos(FakeObjects):
        def execute_kw(self, db, uid, pw, model, method, args, kwargs):
            llamadas.append((model, method, args))
            return [4321]

    desde = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
    vals = {"employee_id": 100, "product_id": 1, "date": "2026-11-27", "total_amount": 70000.0}
    assert _writer(Objetos()).buscar_gasto_corregido(vals, [700], desde) == 4321
    modelo, metodo, args = llamadas[0]
    dominio = args[0]
    assert (modelo, metodo) == ("hr.expense", "search")
    assert ("state_expenses", "in", ["correction", "to_correction"]) in dominio
    assert ("id", "not in", [700]) in dominio and ("employee_id", "=", 100) in dominio
    assert ("create_date", ">=", "2026-11-27 14:58:00") in dominio  # recepción menos 2 min de holgura


def test_correccion_de_gasto_4xx_es_fallida_y_no_se_reintenta(seeded):
    odoo = _odoo()
    cid = _recibir(seeded, odoo, _gasto_raw())
    _reclamar(seeded, cid)
    out = service.send_one(seeded, odoo, cid, 4, FakeRest([OdooApiError("/api/gastos/register respondió 400")]))  # type: ignore[arg-type]
    assert out.status == SyncStatus.FALLIDO and "rechazó" in out.error and out.descartado is False
    assert _fila(seeded, cid).next_attempt_at is None
    assert service.requeue_failed(seeded) == 1  # una fallida normal sí se reencola


def test_correccion_de_gasto_5xx_o_red_se_reintenta_con_backoff_y_agota(seeded):
    odoo = _odoo()
    cid = _recibir(seeded, odoo, _gasto_raw())
    _reclamar(seeded, cid)
    out = service.send_one(seeded, odoo, cid, 4, FakeRest([_RetryableHttpError("503")]))  # type: ignore[arg-type]
    assert out.status == SyncStatus.ERROR and "Intento 1/4" in out.error
    assert _fila(seeded, cid).next_attempt_at is not None  # backoff
    with seeded.session() as s:
        s.get(Correccion, cid).attempts = 4
        s.get(Correccion, cid).sync_status = SyncStatus.ENVIANDO
    out = service.send_one(seeded, odoo, cid, 4, FakeRest([_RetryableHttpError("503")]))  # type: ignore[arg-type]
    assert out.status == SyncStatus.FALLIDO


def test_sin_cliente_rest_espera_sin_consumir_intentos(seeded):
    odoo = _odoo()
    cid = _recibir(seeded, odoo, _gasto_raw())
    _reclamar(seeded, cid)
    out = service.send_one(seeded, odoo, cid, 4, None)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ERROR and _fila(seeded, cid).attempts == 0


def test_busqueda_de_duplicado_transitoria_no_postea(seeded):
    odoo = _odoo(existentes=[OdooTransientError("sin respuesta")])
    cid = _recibir(seeded, odoo, _gasto_raw())
    _reclamar(seeded, cid)
    rest = FakeRest()
    out = service.send_one(seeded, odoo, cid, 4, rest)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ERROR and rest.posts == []


# ───────────────────────────────── corrección de Pos: escritura en el mismo registro ─────────────────────────────────

@pytest.mark.parametrize("estado, nuevo", [("refused", "correct"), ("refused by", "correct by")])
def test_pos_escribe_el_mismo_registro_con_el_estado_de_correccion(seeded, estado, nuevo):
    odoo = _odoo(registros={("pos.operacional", 800): _original_pos(state=estado)})
    cid = _recibir(seeded, odoo, _pos_raw())
    _reclamar(seeded, cid)
    out = service.send_one(seeded, odoo, cid, 4)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and out.odoo_id == 800 and out.tipo == "pos"
    bloque = service.bloque_observacion(_fila(seeded, cid))
    assert odoo.escritos == [(800, {
        "km_start": 1000.0, "km_end": 1120.0, "check_in": f"{AYER} 12:30:00", "check_out": f"{AYER} 22:30:00",
        "observations": f"Sin novedad\n\n{bloque}", "state": nuevo,
    })]  # sin planilla nueva no se toca la actual; las observaciones se agregan al final, no se reemplazan
    assert bloque.endswith("] Corregí el KM final") and bloque.startswith("[Corrección ")


def test_pos_con_planilla_nueva_la_envia_en_pdf(seeded):
    odoo = _odoo()
    cid = _recibir(seeded, odoo, _pos_raw(planilla=PDF_B64, planilla_nombre="planilla nueva.pdf"))
    _reclamar(seeded, cid)
    service.send_one(seeded, odoo, cid, 4)  # type: ignore[arg-type]
    vals = odoo.escritos[0][1]
    assert vals["attachment"] == base64.b64encode(PDF).decode() and vals["type_file"] == "pdf"
    assert vals["attachment_filename"] == "planilla nueva.pdf" and vals["state"] == "correct"


def test_pos_que_ya_no_esta_pendiente_se_descarta_con_motivo(seeded):
    odoo = _odoo()
    cid = _recibir(seeded, odoo, _pos_raw())
    odoo.registros[("pos.operacional", 800)] = _original_pos(state="approved")  # cambió entre el envío y la cola
    _reclamar(seeded, cid)
    out = service.send_one(seeded, odoo, cid, 4)  # type: ignore[arg-type]
    assert out.status == SyncStatus.FALLIDO and out.descartado is True and "APROBADO" in out.error
    assert odoo.escritos == []
    c = _fila(seeded, cid)
    assert c.descartado is True and c.next_attempt_at is None
    assert service.requeue_failed(seeded) == 0  # una descartada no se reencola
    texto = formatting.correccion_result(out)
    assert "no se aplicó" in texto and "Motivo" in texto


def test_pos_ya_corregido_con_los_mismos_valores_es_idempotente(seeded):
    odoo = _odoo()
    cid = _recibir(seeded, odoo, _pos_raw())
    odoo.registros[("pos.operacional", 800)] = _original_pos(
        state="correct", km_end=1120.0, check_in=f"{AYER} 12:30:00", check_out=f"{AYER} 22:30:00",
        observations=f"Sin novedad\n\n{service.bloque_observacion(_fila(seeded, cid))}")
    _reclamar(seeded, cid)
    out = service.send_one(seeded, odoo, cid, 4)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and out.descartado is False and odoo.escritos == []


def test_pos_observaciones_vacias_solo_llevan_el_bloque_nuevo(seeded):
    odoo = _odoo(registros={("pos.operacional", 800): _original_pos(observations=False)})
    cid = _recibir(seeded, odoo, _pos_raw())
    _reclamar(seeded, cid)
    service.send_one(seeded, odoo, cid, 4)  # type: ignore[arg-type]
    assert odoo.escritos[0][1]["observations"] == service.bloque_observacion(_fila(seeded, cid))


def test_pos_el_bloque_lleva_fecha_y_hora_de_colombia(seeded):
    cid = _recibir(seeded, _odoo(), _pos_raw())
    c = _fila(seeded, cid)
    esperado = service._hora_colombia(c.fecha).replace("T", " ")
    assert service.bloque_observacion(c) == f"[Corrección {esperado}] Corregí el KM final"


def test_pos_reintento_no_duplica_el_bloque_de_observaciones(seeded):
    """Si Odoo falla tras escribir (o el envío se reintenta), el bloque no se agrega dos veces."""
    odoo = _odoo()
    cid = _recibir(seeded, odoo, _pos_raw())
    _reclamar(seeded, cid)
    service.send_one(seeded, odoo, cid, 4)  # type: ignore[arg-type]
    bloque = service.bloque_observacion(_fila(seeded, cid))
    escrita = odoo.registros[("pos.operacional", 800)]["observations"]
    assert escrita.count(bloque) == 1 and escrita.startswith("Sin novedad")
    # Reintento tras un fallo de red al confirmar: Odoo ya está corregido -> no escribe otra vez.
    with seeded.session() as s:
        s.get(Correccion, cid).sync_status = SyncStatus.ENVIANDO
    assert service.send_one(seeded, odoo, cid, 4).status == SyncStatus.ENVIADO  # type: ignore[arg-type]
    assert len(odoo.escritos) == 1
    assert odoo.registros[("pos.operacional", 800)]["observations"].count(bloque) == 1
    # Y aunque el estado siga pendiente con el bloque ya presente, no lo repite.
    assert service.observaciones_finales(escrita, bloque) == escrita


def test_pos_corregido_con_otros_valores_se_descarta(seeded):
    odoo = _odoo(registros={("pos.operacional", 800): _original_pos(state="correct", km_end=1500.0)})
    cid = _recibir(seeded, _odoo(), _pos_raw())
    _reclamar(seeded, cid)
    assert service.send_one(seeded, odoo, cid, 4).descartado is True  # type: ignore[arg-type]


def test_pos_errores_de_odoo(seeded):
    odoo = _odoo(write_error=OdooTransientError("sin respuesta"))
    cid = _recibir(seeded, odoo, _pos_raw())
    _reclamar(seeded, cid)
    assert service.send_one(seeded, odoo, cid, 4).status == SyncStatus.ERROR  # type: ignore[arg-type]
    odoo.write_error = OdooPermanentError("ValidationError: dato")
    with seeded.session() as s:
        s.get(Correccion, cid).sync_status = SyncStatus.ENVIANDO
    out = service.send_one(seeded, odoo, cid, 4)  # type: ignore[arg-type]
    assert out.status == SyncStatus.FALLIDO and "rechazó" in out.error and out.descartado is False


# ───────────────────────────────────────────── worker y avisos ─────────────────────────────────────────────

def test_el_worker_envia_y_avisa_solo_el_resultado_final(seeded):
    import asyncio

    odoo = _odoo()
    _recibir(seeded, odoo, _gasto_raw())
    _recibir(seeded, odoo, _pos_raw(uuid="4a1b2c3d-3c1d-4f7a-9a2b-1c2d3e4f5a6c"))
    avisos: list[Any] = []

    async def notify(outcome):
        avisos.append(outcome)

    sender = CorreccionesSender(seeded, odoo, 60, 4, 300, notify=notify, rest_client=FakeRest())  # type: ignore[arg-type]
    outcomes = asyncio.run(sender.run_once())
    assert sorted(o.tipo for o in outcomes) == ["gasto", "pos"] and len(avisos) == 2
    assert all(o.status == SyncStatus.ENVIADO for o in avisos)
    textos = [formatting.correccion_result(o) for o in avisos]
    assert any("Corrección de gasto enviada" in t for t in textos) and any("Post-operacional" in t for t in textos)
    assert "recibida" in formatting.correccion_recibida("gasto", "<X>") and "&lt;X&gt;" in formatting.correccion_recibida("gasto", "<X>")


def test_aviso_de_fallo_definitivo(seeded):
    odoo = _odoo()
    cid = _recibir(seeded, odoo, _gasto_raw())
    _reclamar(seeded, cid)
    out = service.send_one(seeded, odoo, cid, 4, FakeRest([OdooApiError("400 mal")]))  # type: ignore[arg-type]
    texto = formatting.correccion_result(out)
    assert "no pudo guardarse" in texto and f"#{cid}" in texto


# ────────────────────────────────────── API, autenticación y envío sin señal ──────────────────────────────────────

@pytest.fixture
def api(seeded):
    app = FastAPI()
    app.include_router(correcciones_router)
    woke: list[int] = []
    avisos: list[Any] = []

    async def on_accepted(recibida):
        avisos.append((recibida.id, recibida.tipo))

    odoo = _odoo(gastos=[_original_gasto()], pos=[_original_pos()])
    app.state.correcciones = CorreccionesServices(
        seeded, odoo, BOT_TOKEN, 3600, wake_worker=lambda: woke.append(1), on_accepted=on_accepted)  # type: ignore[arg-type]
    client = TestClient(app)
    client.woke, client.avisos, client.odoo = woke, avisos, odoo  # type: ignore[attr-defined]
    return client


def _headers(user_id=TG_USER):
    return {"X-Telegram-Init-Data": make_init_data(user_id=user_id)}


def _diferido(raw: dict, *, abierto_hace: timedelta, sin_senal_hace: timedelta) -> dict:
    now = datetime.now(UTC)
    return {**raw, "diferido": {
        "init_data_original": make_init_data(user_id=TG_USER, auth_date=int((now - abierto_hace).timestamp())),
        "sin_senal_desde": (now - sin_senal_hace).isoformat()}}


def test_api_exige_init_data_y_empleado(api):
    assert api.get("/api/correcciones/context").status_code == 401
    assert api.get("/api/correcciones/form").status_code == 401
    assert api.post("/api/correcciones", json={}).status_code == 401
    bad = {"X-Telegram-Init-Data": make_init_data(tamper=True)}
    assert api.get("/api/correcciones/context", headers=bad).status_code == 401
    assert api.post("/api/correcciones", json=_gasto_raw(), headers=bad).status_code == 401
    assert api.post("/api/correcciones", json={}, headers=_headers()).status_code == 422
    assert api.get("/api/correcciones/context", headers=_headers(user_id=1)).status_code == 403
    assert api.post("/api/correcciones", json=_gasto_raw(), headers=_headers(user_id=1)).status_code == 403
    assert api.get(f"/api/correcciones/{UUID}", headers=_headers(user_id=1)).status_code == 404


def test_api_contexto_y_form(api):
    ctx = api.get("/api/correcciones/context", headers=_headers())
    assert ctx.status_code == 200 and ctx.json()["odoo_ok"] is True
    assert [g["id"] for g in ctx.json()["gastos"]] == [700] and [p["id"] for p in ctx.json()["pos"]] == [800]
    assert ctx.json()["hoy"] == _hoy()
    form = api.get("/api/correcciones/form", headers=_headers()).json()
    assert form["pdf_max_bytes"] == 3 * 1024 * 1024 and form["texto_max"] == 1000
    api.odoo.gastos = None  # type: ignore[attr-defined]
    caido = api.get("/api/correcciones/context", headers=_headers())
    assert caido.status_code == 200 and caido.json()["odoo_ok"] is False


def test_api_foto_se_acepta_y_fecha_fuera_de_ventana_es_422(api):
    foto = api.post("/api/correcciones", json=_gasto_raw(pdf=_foto_b64(), pdf_nombre="f.jpg"), headers=_headers())
    assert foto.status_code == 202
    vieja = (reservas.fecha_jornada() - timedelta(days=31)).isoformat()
    r = api.post("/api/correcciones", json=_gasto_raw(uuid="6a1b2c3d-3c1d-4f7a-9a2b-1c2d3e4f5a6c", fecha=vieja),
                 headers=_headers())
    assert r.status_code == 422 and "30 días" in r.text
    form = api.get("/api/correcciones/form", headers=_headers()).json()
    assert form["dias_maximos"] == 30
    assert api.get("/api/correcciones/context", headers=_headers()).json()["dias_maximos"] == 30


def test_api_recibe_responde_202_y_es_idempotente(api):
    first = api.post("/api/correcciones", json=_gasto_raw(), headers=_headers())
    assert first.status_code == 202 and first.json()["tipo"] == "gasto" and first.json()["sync_status"] == "pendiente"
    assert api.woke == [1] and api.avisos == [(first.json()["id"], "gasto")]
    again = api.post("/api/correcciones", json=_gasto_raw(), headers=_headers())
    assert again.json()["duplicado"] is True and len(api.avisos) == 1
    estado = api.get(f"/api/correcciones/{UUID}", headers=_headers())
    assert estado.status_code == 200 and estado.json()["uuid"] == UUID
    pos = api.post("/api/correcciones", json=_pos_raw(uuid="5a1b2c3d-3c1d-4f7a-9a2b-1c2d3e4f5a6c"), headers=_headers())
    assert pos.status_code == 202 and pos.json()["tipo"] == "pos"


def test_api_validacion_y_estados_de_odoo(api):
    sin_pdf = _gasto_raw()
    del sin_pdf["pdf"]
    r = api.post("/api/correcciones", json=sin_pdf, headers=_headers())
    assert r.status_code == 422
    assert api.post("/api/correcciones", json=_pos_raw(observaciones=""), headers=_headers()).status_code == 422
    api.odoo.registros[("hr.expense", 700)] = _original_gasto(state_expenses="approved")  # type: ignore[attr-defined]
    assert api.post("/api/correcciones", json=_gasto_raw(), headers=_headers()).status_code == 409
    api.odoo.registros[("hr.expense", 700)] = OdooTransientError("sin respuesta")  # type: ignore[attr-defined]
    assert api.post("/api/correcciones", json=_gasto_raw(), headers=_headers()).status_code == 503


def test_diferido_en_ventana_flujo_normal_y_trazabilidad(api, seeded):
    body = _diferido(_gasto_raw(), abierto_hace=timedelta(minutes=80), sin_senal_hace=timedelta(minutes=40))
    headers = {"X-Telegram-Init-Data": body["diferido"]["init_data_original"]}
    first = api.post("/api/correcciones", json=body, headers=headers)
    assert first.status_code == 202 and api.woke == [1] and api.avisos == [(first.json()["id"], "gasto")]
    with seeded.session() as s:
        c = s.scalar(select(Correccion))
        traza = s.scalar(select(CorreccionEnvioDiferido))
        assert traza.estado == EstadoDiferido.ACEPTADO and traza.correccion_id == c.id and traza.payload is None
    again = api.post("/api/correcciones", json=body, headers=headers)
    assert again.json()["duplicado"] is True and len(api.avisos) == 1


def test_diferido_vencido_responde_410_y_queda_solo_como_auditoria(api, seeded):
    body = _diferido(_gasto_raw(), abierto_hace=timedelta(hours=3), sin_senal_hace=timedelta(minutes=150))
    assert api.post("/api/correcciones", json=body, headers=_headers()).status_code == 410
    assert api.post("/api/correcciones", json=body, headers=_headers()).status_code == 410
    assert api.woke == [] and api.avisos == []
    with seeded.session() as s:
        assert s.scalar(select(func.count()).select_from(Correccion)) == 0
        traza = s.scalar(select(CorreccionEnvioDiferido))
        assert traza.estado == EstadoDiferido.VENCIDO and "min después de perder la señal" in traza.motivo
        assert traza.payload["pdf"] == PDF_B64 and "diferido" not in traza.payload


def test_pagina_y_servicios_ausentes():
    app = FastAPI()
    app.include_router(correcciones_router)
    client = TestClient(app)
    assert client.get("/api/correcciones/form", headers=_headers()).status_code == 503
    html = client.get("/correcciones")
    assert html.status_code == 200 and "Correcciones Post y Gastos" in html.text
    assert "/api/correcciones" in html.text and "indexedDB" in html.text  # bandeja sin señal


# ═══════════════════════ Post en «Corrección Nómina» (correct by): listado y nueva corrección ═══════════════════════

def _empleado(database):
    return _preop.service.employee_for_telegram(database, TG_USER)


def test_pos_correct_by_se_lista_y_se_corrige_sobre_el_mismo_registro(seeded):
    odoo = _odoo(gastos=[], pos=[_original_pos(state="correct by", corrections_hr="Revisar horas")],
                 registros={("pos.operacional", 800): _original_pos(state="correct by")})
    ctx = service.contexto(seeded, odoo, _empleado(seeded))
    assert [p["estado_texto"] for p in ctx["pos"]] == ["CORRECCIÓN NÓMINA"]
    cid = _recibir(seeded, odoo, _pos_raw())
    _reclamar(seeded, cid)
    out = service.send_one(seeded, odoo, cid, 4)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and out.odoo_id == 800
    assert [(i, v["state"]) for i, v in odoo.escritos] == [(800, "correct by")]  # sigue en «correct by»
    bloque = service.bloque_observacion(_fila(seeded, cid))
    assert odoo.escritos[0][1]["observations"] == f"Sin novedad\n\n{bloque}"


def test_pos_correct_by_reintento_no_duplica_el_bloque(seeded):
    """En «correct by» (pendiente y destino a la vez) la idempotencia se evalúa antes de decidir."""
    odoo = _odoo(registros={("pos.operacional", 800): _original_pos(state="correct by")})
    cid = _recibir(seeded, odoo, _pos_raw())
    _reclamar(seeded, cid)
    service.send_one(seeded, odoo, cid, 4)  # type: ignore[arg-type]
    bloque = service.bloque_observacion(_fila(seeded, cid))
    with seeded.session() as s:
        s.get(Correccion, cid).sync_status = SyncStatus.ENVIANDO
    assert service.send_one(seeded, odoo, cid, 4).status == SyncStatus.ENVIADO  # type: ignore[arg-type]
    assert len(odoo.escritos) == 1
    assert odoo.registros[("pos.operacional", 800)]["observations"].count(bloque) == 1


def test_pos_con_otro_estado_se_descarta_aunque_antes_fuera_correct_by(seeded):
    odoo = _odoo(registros={("pos.operacional", 800): _original_pos(state="correct by")})
    cid = _recibir(seeded, odoo, _pos_raw())
    odoo.registros[("pos.operacional", 800)] = _original_pos(state="done")
    _reclamar(seeded, cid)
    assert service.send_one(seeded, odoo, cid, 4).descartado is True  # type: ignore[arg-type]


def _marcar_enviada(database, cid: int, hace: timedelta = timedelta(minutes=30)) -> datetime:
    with database.session() as s:
        c = s.get(Correccion, cid)
        c.sync_status, c.sent_at = SyncStatus.ENVIADO, utc_now() - hace
        return c.sent_at


def _odoo_fecha(valor: datetime) -> str:
    return valor.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def test_pos_enviado_se_oculta_solo_mientras_odoo_no_lo_modifique_despues(seeded):
    odoo = _odoo(gastos=[], pos=[])
    cid = _recibir(seeded, odoo, _pos_raw())
    empleado = _empleado(seeded)

    def ids(**raw: Any) -> list[int]:
        odoo.pos = [_original_pos(state="correct by", **raw)]
        return [p["id"] for p in service.contexto(seeded, odoo, empleado)["pos"]]

    assert ids() == []  # en curso (pendiente): oculto, aunque Odoo diga que se modificó después
    assert ids(write_date=_odoo_fecha(utc_now() + timedelta(hours=1))) == []
    enviada = _marcar_enviada(seeded, cid)
    assert ids() == []  # enviada y sin write_date: sigue oculta
    assert ids(write_date=_odoo_fecha(enviada + timedelta(seconds=30))) == []  # nuestro propio write (< 2 min)
    assert ids(write_date=_odoo_fecha(enviada + timedelta(minutes=2))) == []   # el límite no cuenta como posterior
    assert ids(write_date=_odoo_fecha(enviada + timedelta(minutes=5))) == [800]  # nómina pidió otra corrección


def test_segunda_correccion_409_en_curso_o_enviada_vigente_y_permitida_si_odoo_cambio(seeded):
    odoo = _odoo(registros={("pos.operacional", 800): _original_pos(state="correct by")})
    cid = _recibir(seeded, odoo, _pos_raw())
    otra = {"uuid": "2a1b2c3d-3c1d-4f7a-9a2b-1c2d3e4f5a6c"}
    with pytest.raises(SubmissionError) as e:
        _recibir(seeded, odoo, _pos_raw(**otra))
    assert e.value.status_code == 409  # en curso
    enviada = _marcar_enviada(seeded, cid)
    odoo.registros[("pos.operacional", 800)]["write_date"] = _odoo_fecha(enviada + timedelta(seconds=20))
    with pytest.raises(SubmissionError) as e:
        _recibir(seeded, odoo, _pos_raw(**otra))
    assert e.value.status_code == 409  # enviada y vigente
    odoo.registros[("pos.operacional", 800)]["write_date"] = _odoo_fecha(enviada + timedelta(minutes=10))
    assert _recibir(seeded, odoo, _pos_raw(**otra)) != cid  # Odoo lo modificó después: se puede corregir de nuevo


def test_un_gasto_enviado_sigue_oculto(seeded):
    odoo = _odoo(gastos=[_original_gasto()], pos=[])
    cid = _recibir(seeded, odoo, _gasto_raw())
    _marcar_enviada(seeded, cid)
    assert service.contexto(seeded, odoo, _empleado(seeded))["gastos"] == []
    with pytest.raises(SubmissionError) as e:
        _recibir(seeded, odoo, _gasto_raw(uuid="2a1b2c3d-3c1d-4f7a-9a2b-1c2d3e4f5a6c"))
    assert e.value.status_code == 409


# ═════════════════════════════ Planillas no enviadas (POST /api/posoperacional/register) ═════════════════════════════

HOY_D = reservas.fecha_jornada()
DIA_A, DIA_B, DIA_C = (HOY_D - timedelta(days=n) for n in (1, 3, 5))
UUID_P = "6a1b2c3d-3c1d-4f7a-9a2b-1c2d3e4f5a6c"


def _col(dia, hora: str) -> datetime:
    """Hora de Colombia de ``dia`` como UTC con zona."""
    h, m = map(int, hora.split(":"))
    return datetime(dia.year, dia.month, dia.day, h, m, tzinfo=reservas.ZONA_JORNADA).astimezone(UTC)


def _sembrar_preop(database, fecha: datetime, **over: Any) -> None:
    from app.preop.models import Flota, Preoperacional

    with database.session() as s:
        emp = _empleado(database)
        veh = s.scalar(select(Flota).where(Flota.odoo_id == 10))
        s.add(Preoperacional(telegram_user_id=TG_USER, firma_nombre="Ana", empleado_id=emp.id, vehiculo_id=veh.id,
                             fecha=fecha, kilometraje=1000, **over))


def _post_local(database, check_in: datetime, estado: SyncStatus = SyncStatus.ENVIADO) -> None:
    from app.preop.models import PosOperacional

    with database.session() as s:
        s.add(PosOperacional(telegram_user_id=TG_USER, firma_nombre="Ana", empleado_id=_empleado(database).id,
                             km_inicial=1, km_final=2, check_in=check_in, check_out=check_in + timedelta(hours=8),
                             planilla=PDF, planilla_nombre="p.pdf", planilla_tipo="pdf", planilla_tamano=len(PDF),
                             planilla_sha256="0" * 64, sync_status=estado))


def _planilla_raw(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"tipo": "planilla", "uuid": UUID_P, "fecha": DIA_A.isoformat(),
                            "entrada": f"{DIA_A.isoformat()}T08:07", "observaciones": "No alcancé a enviarla",
                            "planilla": PDF_B64, "planilla_nombre": "planilla.pdf"}
    data.update(over)
    return data


def _jornadas(database, odoo: FakeOdoo) -> list[str]:
    return [j["fecha"] for j in service.contexto(database, odoo, _empleado(database))["planillas"]]


def test_jornadas_una_por_dia_con_el_primer_preoperacional_y_respetan_la_ventana(seeded):
    _sembrar_preop(seeded, _col(DIA_A, "09:30"))
    _sembrar_preop(seeded, _col(DIA_A, "06:45"))  # el primero del día es el que cuenta
    _sembrar_preop(seeded, _col(HOY_D - timedelta(days=40), "07:00"))  # fuera de la ventana de 30 días
    odoo = _odoo(gastos=[], pos=[])
    ctx = service.contexto(seeded, odoo, _empleado(seeded))
    assert len(ctx["planillas"]) == 1 and ctx["planillas_mensaje"] is None
    j = ctx["planillas"][0]
    assert j["fecha"] == DIA_A.isoformat() and j["entrada"] == f"{DIA_A.isoformat()}T06:45" and j["vehiculo"]
    assert len(odoo.rangos) == 1  # una sola consulta por rango para todos los días
    emp, inicio, fin = odoo.rangos[0]
    assert emp == EMPLEADO_ODOO and inicio == _col(HOY_D - timedelta(days=30), "00:00")
    assert fin == _col(HOY_D + timedelta(days=1), "00:00")


def test_jornadas_se_excluyen_por_odoo_post_local_y_carga_local(seeded):
    for dia in (DIA_A, DIA_B, DIA_C):
        _sembrar_preop(seeded, _col(dia, "07:00"))
    assert _jornadas(seeded, _odoo(gastos=[], pos=[])) == [DIA_A.isoformat(), DIA_B.isoformat(), DIA_C.isoformat()]
    # Odoo: un pos.operacional con check_in ese día de Colombia (o create_date si no tiene check_in).
    rango = [{"check_in": _odoo_fecha(_col(DIA_A, "20:00")), "create_date": "2020-01-01 00:00:00"},
             {"check_in": False, "create_date": _odoo_fecha(_col(DIA_B, "21:00"))}]
    assert _jornadas(seeded, _odoo(gastos=[], pos=[], pos_rango=rango)) == [DIA_C.isoformat()]
    # Post-operacional local del formulario normal: cuenta si no está fallido.
    _post_local(seeded, _col(DIA_C, "07:10"), SyncStatus.FALLIDO)
    assert _jornadas(seeded, _odoo(gastos=[], pos=[])) == [DIA_A.isoformat(), DIA_B.isoformat(), DIA_C.isoformat()]
    _post_local(seeded, _col(DIA_C, "07:10"), SyncStatus.PENDIENTE)
    _post_local(seeded, _col(DIA_B, "07:10"))
    assert _jornadas(seeded, _odoo(gastos=[], pos=[])) == [DIA_A.isoformat()]
    # Carga local de planilla (pendiente, enviando, error o enviada) de ese día.
    odoo = _odoo(gastos=[], pos=[])
    cid = _recibir(seeded, odoo, _planilla_raw())
    assert _jornadas(seeded, odoo) == []
    with seeded.session() as s:
        s.get(Correccion, cid).sync_status = SyncStatus.FALLIDO
    assert _jornadas(seeded, odoo) == [DIA_A.isoformat()]  # una fallida no oculta la jornada


def test_jornada_de_la_reserva_vigente_no_se_ofrece_y_sin_reserva_si(seeded):
    from app.preop.models import Flota

    ahora = utc_now()
    _sembrar_preop(seeded, ahora)
    hoy = reservas.fecha_jornada(ahora).isoformat()
    assert _jornadas(seeded, _odoo(gastos=[], pos=[])) == [hoy]  # sin reserva (ya finalizó) sí se ofrece
    with seeded.session() as s:
        veh = s.scalar(select(Flota.id).where(Flota.odoo_id == 10))
        s.add(ReservaVehiculo(vehiculo_id=veh, empleado_id=_empleado(seeded).id, telegram_user_id=TG_USER,
                              fecha_jornada=reservas.fecha_jornada(ahora), reservado_at=ahora, ultimo_preop_at=ahora))
    assert _jornadas(seeded, _odoo(gastos=[], pos=[])) == []


def test_si_odoo_no_responde_las_planillas_dan_mensaje_y_no_bloquean_lo_demas(seeded):
    _sembrar_preop(seeded, _col(DIA_A, "07:00"))
    ctx = service.contexto(seeded, _odoo(gastos=[_original_gasto()], pos=[], pos_rango=None), _empleado(seeded))
    assert ctx["odoo_ok"] is True and ctx["planillas"] == [] and "planillas" in ctx["planillas_mensaje"]
    assert [g["id"] for g in ctx["gastos"]] == [700]


def test_planilla_payload_exacto_con_check_in_en_utc(seeded):
    _sembrar_preop(seeded, _col(DIA_A, "07:00"))
    odoo = _odoo(gastos=[], pos=[])
    cid = _recibir(seeded, odoo, _planilla_raw())
    c = _fila(seeded, cid)
    assert c.tipo == "planilla" and c.referencia == f"Planilla {DIA_A.isoformat()}"
    assert c.datos["fecha"] == DIA_A.isoformat()
    _reclamar(seeded, cid)
    rest = FakeRest([{"id": 4779}])
    out = service.send_one(seeded, odoo, cid, 4, rest)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and out.odoo_id == 4779 and out.tipo == "planilla"
    assert rest.posts == [(cat.PLANILLA_PATH, {
        "corrections_hr": True, "employee_id": EMPLEADO_ODOO,
        "check_in": f"{DIA_A.isoformat()} 13:07:00",  # 08:07 de Colombia = 13:07 UTC
        "observations": "No alcancé a enviarla", "type_file": "pdf", "attachment_filename": "planilla.pdf",
        "attachment": base64.b64encode(PDF).decode(),
    })]
    assert cat.PLANILLA_PATH == "/api/posoperacional/register"
    assert list(rest.posts[0][1]) == ["corrections_hr", "employee_id", "check_in", "observations", "type_file",
                                      "attachment_filename", "attachment"]
    assert _fila(seeded, cid).odoo_id == 4779 and odoo.escritos == []  # crea uno nuevo: nunca escribe uno existente


def test_planilla_con_foto_llega_como_pdf_y_una_sola_carga_por_dia(seeded):
    _sembrar_preop(seeded, _col(DIA_A, "07:00"))
    odoo = _odoo(gastos=[], pos=[])
    cid = _recibir(seeded, odoo, _planilla_raw(planilla=_foto_b64(), planilla_nombre="IMG_1.jpg"))
    _reclamar(seeded, cid)
    rest = FakeRest()
    service.send_one(seeded, odoo, cid, 4, rest)  # type: ignore[arg-type]
    body = rest.posts[0][1]
    assert base64.b64decode(body["attachment"]).startswith(b"%PDF") and body["attachment_filename"] == "IMG_1.pdf"
    with pytest.raises(SubmissionError) as e:  # el día ya tiene una carga: 409
        _recibir(seeded, odoo, _planilla_raw(uuid="3a1b2c3d-3c1d-4f7a-9a2b-1c2d3e4f5a6c"))
    assert e.value.status_code == 409


def test_planilla_de_un_dia_sin_jornada_pendiente_es_409_y_con_odoo_caido_503(seeded):
    odoo = _odoo(gastos=[], pos=[])
    with pytest.raises(SubmissionError) as e:  # no hay preoperacional ese día
        _recibir(seeded, odoo, _planilla_raw())
    assert e.value.status_code == 409
    _sembrar_preop(seeded, _col(DIA_A, "07:00"))
    with pytest.raises(SubmissionError) as e:  # Odoo ya tiene un Post ese día
        _recibir(seeded, _odoo(pos_rango=[{"check_in": _odoo_fecha(_col(DIA_A, "12:00")), "create_date": False}]),
                 _planilla_raw())
    assert e.value.status_code == 409
    with pytest.raises(SubmissionError) as e:
        _recibir(seeded, _odoo(pos_rango=None), _planilla_raw())
    assert e.value.status_code == 503


def test_planilla_dedupe_busca_por_check_in_exacto_desde_la_recepcion(seeded):
    _sembrar_preop(seeded, _col(DIA_A, "07:00"))
    odoo = _odoo(gastos=[], pos=[])
    cid = _recibir(seeded, odoo, _planilla_raw())
    odoo.existentes_planilla = [4780]
    _reclamar(seeded, cid)
    rest = FakeRest()
    out = service.send_one(seeded, odoo, cid, 4, rest)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and out.odoo_id == 4780 and rest.posts == []
    assert odoo.busquedas_planilla == [(EMPLEADO_ODOO, f"{DIA_A.isoformat()} 13:07:00", _fila(seeded, cid).fecha)]


def test_el_writer_busca_la_planilla_creada_y_lee_el_rango():
    llamadas: list[tuple] = []

    class Objetos(FakeObjects):
        def execute_kw(self, db, uid, pw, model, method, args, kwargs):
            llamadas.append((model, method, args, kwargs))
            return [4321] if method == "search" else [{"id": 1}]

    w = _writer(Objetos())
    desde = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
    assert w.buscar_pos_planilla(100, "2026-11-01 13:07:00", desde) == 4321
    assert w.pos_en_rango(100, desde, desde + timedelta(days=1)) == [{"id": 1}]
    (m1, met1, a1, _), (m2, met2, a2, kw2) = llamadas
    assert (m1, met1) == ("pos.operacional", "search")
    assert a1[0] == [("employee_id", "=", 100), ("check_in", "=", "2026-11-01 13:07:00"),
                     ("create_date", ">=", "2026-11-27 14:58:00")]  # recepción menos 2 min
    assert (m2, met2) == ("pos.operacional", "search_read") and ["employee_id", "=", 100] in a2[0]
    assert ["check_in", ">=", "2026-11-27 15:00:00"] in a2[0] and ["create_date", "<", "2026-11-28 15:00:00"] in a2[0]
    assert "attachment" not in kw2["fields"]


def test_planilla_4xx_es_fallida_5xx_o_red_reintenta_y_sin_cliente_espera(seeded):
    _sembrar_preop(seeded, _col(DIA_A, "07:00"))
    odoo = _odoo(gastos=[], pos=[])
    cid = _recibir(seeded, odoo, _planilla_raw())
    _reclamar(seeded, cid)
    out = service.send_one(seeded, odoo, cid, 4, FakeRest([OdooApiError("/api/posoperacional/register respondió 400")]))  # type: ignore[arg-type]
    assert out.status == SyncStatus.FALLIDO and "rechazó la planilla" in out.error and out.descartado is False
    assert service.requeue_failed(seeded) == 1
    _reclamar(seeded, cid)
    out = service.send_one(seeded, odoo, cid, 4, FakeRest([_RetryableHttpError("503")]))  # type: ignore[arg-type]
    assert out.status == SyncStatus.ERROR and "Intento" in out.error and _fila(seeded, cid).next_attempt_at is not None
    with seeded.session() as s:
        s.get(Correccion, cid).sync_status = SyncStatus.ENVIANDO
    antes = _fila(seeded, cid).attempts
    out = service.send_one(seeded, odoo, cid, 4, None)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ERROR and "sin configurar" in out.error
    assert _fila(seeded, cid).attempts == antes - 1  # sin cliente no consume el intento


def test_planilla_con_busqueda_transitoria_no_postea(seeded):
    _sembrar_preop(seeded, _col(DIA_A, "07:00"))
    odoo = _odoo(gastos=[], pos=[])
    cid = _recibir(seeded, odoo, _planilla_raw())
    odoo.existentes_planilla = [OdooTransientError("sin respuesta")]
    _reclamar(seeded, cid)
    rest = FakeRest()
    assert service.send_one(seeded, odoo, cid, 4, rest).status == SyncStatus.ERROR  # type: ignore[arg-type]
    assert rest.posts == []


def test_el_pos_creado_por_la_planilla_no_se_ofrece_hasta_que_odoo_lo_modifique(seeded):
    _sembrar_preop(seeded, _col(DIA_A, "07:00"))
    odoo = _odoo(gastos=[], pos=[])
    cid = _recibir(seeded, odoo, _planilla_raw())
    _reclamar(seeded, cid)
    service.send_one(seeded, odoo, cid, 4, FakeRest([{"id": 4779}]))  # type: ignore[arg-type]
    sent = _fila(seeded, cid).sent_at
    odoo.pos = [_original_pos(id=4779, state="correct by", write_date=_odoo_fecha(utc_now()))]
    assert service.contexto(seeded, odoo, _empleado(seeded))["pos"] == []
    odoo.pos = [_original_pos(id=4779, state="correct by", write_date=_odoo_fecha(sent + timedelta(minutes=9)))]
    assert [p["id"] for p in service.contexto(seeded, odoo, _empleado(seeded))["pos"]] == [4779]


@pytest.mark.parametrize("cambio, error", [
    ({"planilla": None}, "valid string"),
    ({"observaciones": "  "}, "at least 1"),
    ({"entrada": f"{PASADO_MANANA}T08:00", "fecha": PASADO_MANANA}, "futura"),
    ({"entrada": f"{DIA_B.isoformat()}T08:00"}, "dentro del día"),                      # otro día
    ({"fecha": (HOY_D - timedelta(days=40)).isoformat(), "entrada": f"{HOY_D - timedelta(days=40)}T08:00"}, "antigüedad"),
    ({"planilla": "aGVsbG8gd29ybGQgbm8gZXMgcGRm"}, "PDF o una foto"),
    ({"odoo_id": 5}, "odoo_id"),
])
def test_esquema_planilla_rechaza(cambio, error):
    with pytest.raises(ValidationError, match=error):
        _in(_planilla_raw(**cambio))


def test_esquema_planilla_hora_futura_de_hoy_se_rechaza():
    futura = (datetime.now(reservas.ZONA_JORNADA) + timedelta(minutes=30)).replace(microsecond=0)
    if futura.date() != HOY_D:  # cerca de medianoche la hora futura cae mañana: ya lo cubre «fuera del día»
        pytest.skip("la media hora siguiente cae en otro día")
    with pytest.raises(ValidationError, match="futura"):
        _in(_planilla_raw(fecha=HOY_D.isoformat(), entrada=futura.strftime("%Y-%m-%dT%H:%M:%S")))


def test_esquema_planilla_entrada_en_utc():
    assert _in(_planilla_raw()).entrada == _col(DIA_A, "08:07")


def test_formatting_de_planilla():
    from app.correcciones.service import CorreccionOutcome

    assert "Carga de planilla recibida" in formatting.correccion_recibida("planilla", "Planilla 2026-11-01")
    ok = formatting.correccion_result(CorreccionOutcome(1, TG_USER, "planilla", SyncStatus.ENVIADO, "Planilla X", 4779))
    assert "Planilla enviada a Odoo" in ok and "Post-operacional nuevo" in ok
    mal = formatting.correccion_result(
        CorreccionOutcome(2, TG_USER, "planilla", SyncStatus.FALLIDO, "Planilla X", None, False, "boom"))
    assert "Tu planilla no pudo guardarse" in mal and "#2" in mal


def test_api_planillas_contexto_recepcion_y_errores(api, seeded):
    _sembrar_preop(seeded, _col(DIA_A, "07:00"))
    ctx = api.get("/api/correcciones/context", headers=_headers()).json()
    assert [j["fecha"] for j in ctx["planillas"]] == [DIA_A.isoformat()]
    assert ctx["planillas"][0]["entrada"] == f"{DIA_A.isoformat()}T07:00"
    assert api.post("/api/correcciones", json=_planilla_raw(planilla=None), headers=_headers()).status_code == 422
    assert api.post("/api/correcciones", json=_planilla_raw(observaciones=""), headers=_headers()).status_code == 422
    sin_dia = api.post("/api/correcciones", json=_planilla_raw(
        fecha=DIA_B.isoformat(), entrada=f"{DIA_B.isoformat()}T08:00"), headers=_headers())
    assert sin_dia.status_code == 409
    ok = api.post("/api/correcciones", json=_planilla_raw(), headers=_headers())
    assert ok.status_code == 202 and ok.json()["tipo"] == "planilla"
    assert api.avisos == [(ok.json()["id"], "planilla")]
    assert api.post("/api/correcciones", json=_planilla_raw(), headers=_headers()).json()["duplicado"] is True


def test_api_planilla_sin_senal_en_ventana_flujo_normal_y_vencida_410(api, seeded):
    _sembrar_preop(seeded, _col(DIA_A, "07:00"))
    vencida = api.post("/api/correcciones", json=_diferido(
        _planilla_raw(uuid="5a1b2c3d-3c1d-4f7a-9a2b-1c2d3e4f5a6c"),
        abierto_hace=timedelta(hours=3), sin_senal_hace=timedelta(hours=2)), headers=_headers())
    assert vencida.status_code == 410
    resp = api.post("/api/correcciones", json=_diferido(
        _planilla_raw(), abierto_hace=timedelta(minutes=20), sin_senal_hace=timedelta(minutes=10)), headers=_headers())
    assert resp.status_code == 202 and resp.json()["tipo"] == "planilla"
    with seeded.session() as s:
        estados = list(s.scalars(select(CorreccionEnvioDiferido.estado)))
    assert sorted(e.value for e in estados) == sorted([EstadoDiferido.VENCIDO.value, EstadoDiferido.ACEPTADO.value])
