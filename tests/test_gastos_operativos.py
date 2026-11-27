"""Pruebas de Gastos Operativos: tipos configurables, esquema, soportes (foto -> PDF), recepción, cola, envío a
``hr.expense`` (un gasto por tarjeta), dedupe, ubicación obligatoria, GPS, envío sin señal, API y menú."""
from __future__ import annotations

import asyncio
import base64
import io
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import func, select

from app import formatting
from app import telegram_app as tga
from app.admin import envfile
from app.combustible import gps
from app.combustible import service as combustible_service
from app.gastos_operativos import service, soporte
from app.gastos_operativos.catalogo import TiposGastoError, form_definition, parse_tipos, texto_tipos
from app.gastos_operativos.router import GastosOperativosServices
from app.gastos_operativos.router import router as gastos_router
from app.gastos_operativos.schemas import GastosOperativosIn
from app.gastos_operativos.worker import GastosOperativosSender
from app.preop import reservas
from app.preop.models import (
    Combustible,
    EstadoDiferido,
    GastoOperativo,
    GastoOperativoEnvioDiferido,
    GastoOperativoLinea,
    GastoOperativoSoporte,
    SyncStatus,
    utc_now,
)
from app.preop.odoo_writer import OdooTransientError
from app.preop.rest_client import OdooApiError, _RetryableHttpError
from app.preop.service import SubmissionError

from . import test_combustible as _comb
from . import test_preop as _preop
from .helpers import BOT_TOKEN, antedatar_reservas, make_init_data
from .test_preop import TG_USER, FakeObjects, _writer

db = _preop.db
seeded = _preop.seeded
listo = _comb.listo  # preoperacional hecho: vehículo reservado

IDENTITY = _comb.IDENTITY
UUID = "7a1b2c3d-3c1d-4f7a-9a2b-1c2d3e4f5a6b"
PDF = b"%PDF-1.4\n" + b"0" * 64
PDF_B64 = "data:application/pdf;base64," + base64.b64encode(PDF).decode()
TEXTO_TIPOS = "Alimentación:1;Hospedaje:2;Peajes:6;Parqueadero:6;Lavado:6;Otros:6"
TIPOS = parse_tipos(TEXTO_TIPOS)
LAT, LON = 6.25, -75.56


def _hoy():
    return reservas.fecha_jornada()


def _imagen(formato: str = "PNG", modo: str = "RGB", size=(60, 40), **kw) -> bytes:
    buf = io.BytesIO()
    Image.new(modo, size, (200, 30, 30, 128) if modo == "RGBA" else (200, 30, 30)).save(buf, formato, **kw)
    return buf.getvalue()


def _b64(datos: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(datos).decode()


def _gasto(tipo: str = "Alimentación", valor: int | str = 65000, soporte_b64: str = PDF_B64, **over: Any) -> dict:
    data: dict[str, Any] = {"tipo": tipo, "valor": valor, "observacion": "Almuerzo en ruta", "soporte": soporte_b64,
                            "soporte_nombre": "factura.pdf"}
    data.update(over)
    return data


def _form(gastos: list[dict] | None = None, **over: Any) -> GastosOperativosIn:
    data: dict[str, Any] = {"uuid": UUID, "fecha_gasto": _hoy().isoformat(),
                            "gastos": gastos if gastos is not None else [_gasto()]}
    data.update(over)
    return GastosOperativosIn.model_validate(data)


def _recibir(db, uuid_: str = UUID, gastos: list[dict] | None = None, **over: Any) -> int:
    return service.submit(db, IDENTITY, _form(gastos, uuid=uuid_, **over), TIPOS)[0].id


def _ahora_mas(**kw):
    return utc_now() + timedelta(**kw)


def _con_ubicacion(db, gasto_id: int) -> None:
    with db.session() as s:
        row = s.get(GastoOperativo, gasto_id)
        row.latitud, row.longitud, row.ubicacion_at = LAT, LON, utc_now()


def _queue(db, gastos: list[dict] | None = None, **over: Any) -> int:
    gid = _recibir(db, gastos=gastos, **over)
    _con_ubicacion(db, gid)  # sin ubicación el envío queda retenido
    assert service.claim_batch(db, 10, 300) == [gid]
    return gid


class FakeBuscador:
    """Writer de prueba: solo la búsqueda de duplicados en hr.expense."""

    def __init__(self, existentes: list[Any] | None = None) -> None:
        self.existentes = list(existentes or [])
        self.busquedas: list[tuple[dict, set[int]]] = []

    def buscar_gasto_operativo(self, vals, excluir_ids=()):
        self.busquedas.append((vals, set(excluir_ids)))
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


# ───────────────────────────────────── tipos configurables ─────────────────────────────────────

def test_parse_tipos_normaliza_y_mantiene_el_orden():
    tipos = parse_tipos(" Alimentación : 1 ;Hospedaje:2; ;Otros  varios:6;")
    assert [(t.nombre, t.product_id) for t in tipos] == [("Alimentación", 1), ("Hospedaje", 2), ("Otros varios", 6)]
    assert texto_tipos(tipos) == "Alimentación:1;Hospedaje:2;Otros varios:6"
    assert [t.product_id for t in TIPOS] == [1, 2, 6, 6, 6, 6]


@pytest.mark.parametrize("texto, mensaje", [
    ("", "vacío"),
    (" ; ", "vacío"),
    ("Alimentación", "Nombre:product_id"),
    (":5", "Nombre:product_id"),
    ("Alimentación:uno", "debe ser un número entero"),
    ("Alimentación:0", "mayor que cero"),
    ("Alimentación:-3", "mayor que cero"),
    ("Alimentación:1;alimentación:2", "repetido"),
    ("X" * 61 + ":1", "supera"),
])
def test_parse_tipos_errores_con_mensaje_claro(texto, mensaje):
    with pytest.raises(TiposGastoError, match=mensaje) as exc:
        parse_tipos(texto)
    assert "GASTOS_OPERATIVOS_TIPOS" in str(exc.value)


def test_el_valor_por_defecto_de_settings_es_valido():
    from app.config import Settings

    assert parse_tipos(Settings.model_fields["gastos_operativos_tipos"].default)[0].nombre == "Alimentación"


def test_form_definition_no_expone_el_product_id():
    form = form_definition(TIPOS)
    assert [t["value"] for t in form["tipos"]] == [t.nombre for t in TIPOS]
    assert "product_id" not in str(form) and form["max_gastos"] == 10


def test_panel_admin_edita_y_valida_el_parametro():
    assert "GASTOS_OPERATIVOS_TIPOS" in envfile.PARAMETROS
    assert envfile.validar("GASTOS_OPERATIVOS_TIPOS", " Alimentación : 1 ; Otros:6 ") == "Alimentación:1;Otros:6"
    with pytest.raises(envfile.ParametroError, match="product_id"):
        envfile.validar("GASTOS_OPERATIVOS_TIPOS", "Alimentación:abc")
    with pytest.raises(envfile.ParametroError, match="no permitidos"):
        envfile.validar("GASTOS_OPERATIVOS_TIPOS", "Alimentación:1;'Otros':6")


# ──────────────────────────────────────── esquema ────────────────────────────────────────

def test_esquema_acepta_el_formulario_y_suma_el_total():
    data = _form([_gasto(valor=65000), _gasto("Peajes", "12500", observacion=None)])
    assert data.total == 77500 and data.gastos[1].valor == 12500 and data.gastos[1].observacion is None


@pytest.mark.parametrize("cambio, error", [
    ({"gastos": []}, "at least 1"),
    ({"gastos": [_gasto(valor=0)]}, "greater than 0"),
    ({"gastos": [_gasto(valor="65.000")]}, "entero"),
    ({"gastos": [_gasto(valor=1234.5)]}, "entero"),
    ({"gastos": [_gasto(valor=True)]}, "entero"),
    ({"gastos": [_gasto(valor=10**9)]}, "less than or equal"),
    ({"gastos": [_gasto(soporte_b64="aGVsbG8gd29ybGQgbm8gZXMgcGRm")]}, "PDF o una foto"),
    ({"gastos": [_gasto(soporte_b64="@@@@@@@@@@@@@@@@@@@@")]}, "base64"),
    ({"gastos": [_gasto(tipo="")]}, "at least 1"),
    ({"gastos": [_gasto()] * 11}, "at most 10"),
    ({"fecha_gasto": (_hoy() + timedelta(days=3)).isoformat()}, "futura"),
    ({"fecha_gasto": (_hoy() - timedelta(days=40)).isoformat()}, "30 días"),
    ({"telegram_user_id": 1}, "telegram_user_id"),
])
def test_esquema_rechaza(cambio, error):
    with pytest.raises(ValidationError, match=error):
        _form(**cambio)


def test_esquema_exige_soporte_y_rechaza_el_total_excesivo():
    gasto = _gasto()
    del gasto["soporte"]
    with pytest.raises(ValidationError, match="soporte"):
        _form([gasto])
    grande = _b64(b"%PDF-" + b"0" * (2 * 1024 * 1024), "application/pdf")
    with pytest.raises(ValidationError, match="en total"):
        _form([_gasto(soporte_b64=grande)] * 4)


# ───────────────────────────────── soporte: PDF y foto -> PDF ─────────────────────────────────

@pytest.mark.parametrize("formato, modo, kw", [("PNG", "RGB", {}), ("JPEG", "RGB", {}), ("PNG", "RGBA", {}),
                                               ("PNG", "P", {})])
def test_foto_se_convierte_a_pdf(formato, modo, kw):
    pdf, convertido = soporte.a_pdf(_imagen(formato, modo, **kw))
    assert convertido is True and pdf.startswith(b"%PDF-") and len(pdf) > 100


def test_pdf_pasa_tal_cual_y_no_cuenta_como_convertido():
    assert soporte.a_pdf(PDF) == (PDF, False)


def test_foto_con_orientacion_exif_se_endereza():
    img = Image.new("RGB", (60, 40), (10, 200, 10))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotada 90°
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    pdf, convertido = soporte.a_pdf(buf.getvalue())
    assert convertido and pdf.startswith(b"%PDF-")


@pytest.mark.parametrize("datos, mensaje", [
    (b"GIF89a" + b"0" * 40, "PDF o una foto"),
    (b"\x00\x00\x00\x18ftypheic" + b"0" * 40, "HEIC"),
    (b"hola mundo, no soy un PDF", "PDF o una foto"),
])
def test_formato_invalido_se_rechaza(datos, mensaje):
    with pytest.raises(ValueError, match=mensaje):
        soporte.formato(datos)


def test_foto_corrupta_con_firma_valida_se_rechaza_con_mensaje_claro():
    with pytest.raises(ValueError, match="No se pudo leer la foto"):
        soporte.a_pdf(b"\xff\xd8\xff" + b"basura" * 20)


def test_foto_enorme_en_pixeles_se_rechaza(monkeypatch):
    monkeypatch.setattr(soporte, "MAX_PIXELES", 100)
    with pytest.raises(ValueError, match="demasiado grande"):
        soporte.a_pdf(_imagen("PNG", size=(60, 40)))


@pytest.mark.parametrize("nombre, numero, esperado", [
    ("IMG_2031.JPG", 1, "IMG_2031.pdf"), ("C:\\fotos\\recibo peaje.png", 2, "recibo peaje.pdf"),
    ("../../etc/pas<sw>d", 3, "pas_sw_d.pdf"), (None, 4, "soporte_4.pdf"), ("", 5, "soporte_5.pdf"),
    ("factura.pdf", 1, "factura.pdf"),
])
def test_nombre_del_archivo_enviado_a_odoo_es_pdf_y_seguro(nombre, numero, esperado):
    assert soporte.nombre_pdf(nombre, numero) == esperado


def test_submit_guarda_la_foto_como_pdf(listo):
    foto = _b64(_imagen("JPEG"), "image/jpeg")
    gid = _recibir(listo, gastos=[_gasto(soporte_b64=foto, soporte_nombre="IMG_9.JPG"), _gasto("Peajes", 8000)])
    with listo.session() as s:
        sop = {ln.numero: s.scalar(select(GastoOperativoSoporte).where(GastoOperativoSoporte.linea_id == ln.id))
               for ln in s.get(GastoOperativo, gid).lineas}
    assert sop[1].contenido.startswith(b"%PDF-") and sop[1].convertido is True and sop[1].nombre_archivo == "IMG_9.pdf"
    assert sop[1].tamano == len(sop[1].contenido) and len(sop[1].sha256) == 64
    assert sop[2].contenido == PDF and sop[2].convertido is False


def test_submit_rechaza_la_foto_corrupta_indicando_el_gasto(listo):
    corrupta = _b64(b"\xff\xd8\xff" + b"basura" * 20, "image/jpeg")
    with pytest.raises(SubmissionError, match=r"Gasto #2: No se pudo leer la foto") as exc:
        _recibir(listo, gastos=[_gasto(), _gasto("Peajes", 8000, soporte_b64=corrupta)])
    assert exc.value.status_code == 422
    with listo.session() as s:
        assert s.scalar(select(func.count()).select_from(GastoOperativo)) == 0


# ────────────────────────────────────────── recepción ──────────────────────────────────────────

def test_submit_guarda_cabecera_lineas_y_es_idempotente(listo):
    recibido, dup = service.submit(listo, IDENTITY, _form([_gasto(), _gasto("Hospedaje", 120000, observacion=None)]),
                                   TIPOS)
    assert dup is False and recibido.gastos == 2 and recibido.total == 185000 and recibido.sync_status == SyncStatus.PENDIENTE
    with listo.session() as s:
        cab = s.get(GastoOperativo, recibido.id)
        assert cab.ubicacion_requerida is True and cab.latitud is None and cab.vehiculo_id is not None
        assert cab.fecha_gasto == _hoy() and cab.empleado.odoo_id == 100
        assert [(ln.numero, ln.tipo_nombre, ln.product_id, ln.valor, ln.observacion, ln.sync_status)
                for ln in cab.lineas] == [
            (1, "Alimentación", 1, 65000, "Almuerzo en ruta", SyncStatus.PENDIENTE),
            (2, "Hospedaje", 2, 120000, None, SyncStatus.PENDIENTE)]
    again, dup = service.submit(listo, IDENTITY, _form([_gasto()]), TIPOS)
    assert dup is True and again.id == recibido.id and again.gastos == 2


def test_submit_sin_reserva_vigente_se_rechaza_con_409(seeded):
    with pytest.raises(SubmissionError) as exc:
        service.submit(seeded, IDENTITY, _form(), TIPOS)
    assert exc.value.status_code == 409 and str(exc.value) == reservas.MSG_SIN_RESERVA
    with seeded.session() as s:
        assert s.scalar(select(func.count()).select_from(GastoOperativo)) == 0


def test_submit_rechaza_tipo_inexistente_y_empleado_desconocido(listo):
    with pytest.raises(SubmissionError, match="Gasto #2: el tipo «Viáticos» no existe") as exc:
        _recibir(listo, gastos=[_gasto(), _gasto("Viáticos")])
    assert exc.value.status_code == 422
    with pytest.raises(SubmissionError) as exc:
        service.submit(listo, service.TelegramIdentity(1, "x", "X"), _form(), TIPOS)
    assert exc.value.status_code == 403


def test_submit_resuelve_el_tipo_sin_importar_mayusculas(listo):
    gid = _recibir(listo, gastos=[_gasto("ALIMENTACIÓN")])
    with listo.session() as s:
        assert s.get(GastoOperativo, gid).lineas[0].tipo_nombre == "Alimentación"


def test_submit_uuid_ajeno_responde_409(listo):
    _recibir(listo)
    with pytest.raises(SubmissionError) as exc:
        service.submit(listo, service.TelegramIdentity(999, "x", "X"), _form(), TIPOS)
    assert exc.value.status_code == 409


def test_contexto_con_y_sin_vehiculo(seeded):
    empleado = _comb.preop_service.employee_for_telegram(seeded, TG_USER)
    with pytest.raises(_comb.preop_service.SinReservaVigenteError):
        service.form_context(seeded, empleado, TG_USER)
    _comb._reservar(seeded)
    ctx = service.form_context(seeded, empleado, TG_USER)
    assert ctx["vehiculo"]["placa"] == "ABC123" and ctx["hoy"] == _hoy().isoformat() and ctx["empleado"]["nombre"]


# ────────────────────────────────── payload: un hr.expense por tarjeta ──────────────────────────────────

def test_payload_exacto_por_linea_y_un_hr_expense_por_tarjeta(listo):
    gid = _queue(listo, gastos=[
        _gasto("Alimentación", 65000, observacion="Almuerzo en ruta"),
        _gasto("Hospedaje", 120000, observacion=None),
        _gasto("Peajes", 8500, observacion="Peaje Niquía")])
    writer, rest = FakeBuscador(), FakeRest()
    out = service.send_one(listo, writer, gid, 8, rest)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and (out.enviados, out.total_gastos, out.fallidos) == (3, 3, ())
    assert [p for p, _ in rest.posts] == ["/api/gastos/register"] * 3  # uno por tarjeta
    comunes = {"employee_id": 100, "date": _hoy().isoformat(), "is_fuel_expenses": False, "type_file": "pdf",
               "attachment_filename": "factura.pdf", "attachment": base64.b64encode(PDF).decode(),
               "ubicacion_gps_telegram": "6.250000,-75.560000", "ubicacion_gps_vehiculo": "GPS no disponible"}
    assert [b for _, b in rest.posts] == [
        {"name": "ALIMENTACIÓN", "product_id": 1, "total_amount": 65000.0, "description": "Almuerzo en ruta", **comunes},
        {"name": "HOSPEDAJE", "product_id": 2, "total_amount": 120000.0, "description": "", **comunes},
        {"name": "PEAJES", "product_id": 6, "total_amount": 8500.0, "description": "Peaje Niquía", **comunes},
    ]
    for _, body in rest.posts:  # nada de combustible: ni vacío ni de relleno
        assert not {"gallon", "fuel_type", "km_tanking", "lat_origin", "long_origin"} & set(body)
        assert isinstance(body["total_amount"], float)
    with listo.session() as s:
        cab = s.get(GastoOperativo, gid)
        assert cab.sync_status == SyncStatus.ENVIADO and cab.sent_at is not None and cab.next_attempt_at is None
        assert [(ln.sync_status, ln.odoo_gasto_id) for ln in cab.lineas] == [
            (SyncStatus.ENVIADO, 9001), (SyncStatus.ENVIADO, 9002), (SyncStatus.ENVIADO, 9003)]


def test_la_foto_viaja_como_pdf_con_type_file_pdf(listo):
    gid = _queue(listo, gastos=[_gasto(soporte_b64=_b64(_imagen("PNG"), "image/png"), soporte_nombre="recibo.png")])
    rest = FakeRest()
    service.send_one(listo, FakeBuscador(), gid, 8, rest)  # type: ignore[arg-type]
    body = rest.posts[0][1]
    assert body["type_file"] == "pdf" and body["attachment_filename"] == "recibo.pdf"
    assert base64.b64decode(body["attachment"]).startswith(b"%PDF-")


def test_nombre_en_mayusculas_y_product_id_salen_del_tipo_guardado(listo):
    gid = _queue(listo, gastos=[_gasto("Lavado", 30000)])
    rest = FakeRest()
    service.send_one(listo, FakeBuscador(), gid, 8, rest)  # type: ignore[arg-type]
    assert (rest.posts[0][1]["name"], rest.posts[0][1]["product_id"]) == ("LAVADO", 6)


# ─────────────────────────────────────────── dedupe ───────────────────────────────────────────

def test_dedupe_busca_antes_de_cada_post_y_no_postea_si_ya_existe(listo):
    gid = _queue(listo, gastos=[_gasto(), _gasto("Peajes", 8500)])
    writer, rest = FakeBuscador([777, None]), FakeRest()
    out = service.send_one(listo, writer, gid, 8, rest)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and len(rest.posts) == 1 and rest.posts[0][1]["name"] == "PEAJES"
    assert [v["product_id"] for v, _ in writer.busquedas] == [1, 6]
    with listo.session() as s:
        assert [ln.odoo_gasto_id for ln in s.get(GastoOperativo, gid).lineas] == [777, 9001]


def test_dos_tarjetas_iguales_no_colapsan_en_un_solo_gasto(listo):
    gid = _queue(listo, gastos=[_gasto("Peajes", 8500), _gasto("Peajes", 8500)])
    writer, rest = FakeBuscador(), FakeRest()
    service.send_one(listo, writer, gid, 8, rest)  # type: ignore[arg-type]
    assert len(rest.posts) == 2
    # La segunda búsqueda excluye el hr.expense que acaba de crear la primera.
    assert [excl for _, excl in writer.busquedas] == [set(), {9001}]


def test_busqueda_transitoria_no_postea_y_reintenta(listo):
    gid = _queue(listo)
    rest = FakeRest()
    out = service.send_one(listo, FakeBuscador([OdooTransientError("sin red")]), gid, 8, rest)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ERROR and rest.posts == []


def test_busqueda_fallida_permanente_se_registra_y_se_continua(listo, caplog):
    gid = _queue(listo)
    rest = FakeRest()
    with caplog.at_level("WARNING"):
        out = service.send_one(listo, FakeBuscador([_comb.OdooPermanentError("Invalid field")]), gid, 8,
                               rest)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and len(rest.posts) == 1
    assert "no se pudo verificar duplicados" in caplog.text


def test_writer_busca_el_gasto_por_empleado_fecha_valor_producto_y_no_combustible():
    objects = FakeObjects(natural_hit=321)
    calls = []
    original = objects.execute_kw

    def spy(db, uid, pw, model, method, args, kwargs):
        calls.append((model, method, args))
        return original(db, uid, pw, model, method, args, kwargs)

    objects.execute_kw = spy  # type: ignore[method-assign]
    vals = {"employee_id": 1, "date": "2026-11-03", "total_amount": 65000.0, "product_id": 1}
    assert _writer(objects).buscar_gasto_operativo(vals) == 321
    assert _writer(objects).buscar_gasto_operativo(vals, excluir_ids=[5, 6]) == 321
    (model, method, (dominio,)), (_, _, (con_excluidos,)) = calls
    assert (model, method) == ("hr.expense", "search")
    assert dominio == [("employee_id", "=", 1), ("is_fuel_expenses", "=", False), ("date", "=", "2026-11-03"),
                       ("total_amount", "=", 65000.0), ("product_id", "=", 1)]
    assert con_excluidos == [*dominio, ("id", "not in", [5, 6])]


# ───────────────────────────────── errores: 4xx fallido, 5xx reintento ─────────────────────────────────

def test_4xx_deja_la_linea_en_fallido_y_las_demas_se_envian(listo):
    gid = _queue(listo, gastos=[_gasto(), _gasto("Hospedaje", 120000), _gasto("Peajes", 8500)])
    rest = FakeRest([{"id": 11}, OdooApiError("/api/gastos/register respondió 422: producto inválido"), {"id": 13}])
    out = service.send_one(listo, FakeBuscador(), gid, 8, rest)  # type: ignore[arg-type]
    assert out.status == SyncStatus.FALLIDO and out.enviados == 2 and out.total_gastos == 3
    assert [(f.numero, f.tipo) for f in out.fallidos] == [(2, "Hospedaje")]
    with listo.session() as s:
        cab = s.get(GastoOperativo, gid)
        assert [ln.sync_status for ln in cab.lineas] == [SyncStatus.ENVIADO, SyncStatus.FALLIDO, SyncStatus.ENVIADO]
        assert "rechazó el gasto #2" in cab.lineas[1].error_log and cab.next_attempt_at is None
        assert cab.sync_status == SyncStatus.FALLIDO


def test_requeue_reintenta_solo_las_lineas_fallidas(listo):
    gid = _queue(listo, gastos=[_gasto(), _gasto("Hospedaje", 120000)])
    service.send_one(listo, FakeBuscador(), gid, 8,
                     FakeRest([{"id": 11}, OdooApiError("/api/gastos/register respondió 400: x")]))  # type: ignore[arg-type]
    assert service.requeue_failed(listo) == 1 and service.requeue_failed(listo) == 0
    assert service.claim_batch(listo, 10, 300) == [gid]
    rest = FakeRest()
    out = service.send_one(listo, FakeBuscador(), gid, 8, rest)  # type: ignore[arg-type]
    assert [b["name"] for _, b in rest.posts] == ["HOSPEDAJE"]  # la primera ya estaba enviada
    assert out.status == SyncStatus.ENVIADO and out.enviados == 2
    with listo.session() as s:
        assert [ln.odoo_gasto_id for ln in s.get(GastoOperativo, gid).lineas] == [11, 9001]


@pytest.mark.parametrize("error", [_RetryableHttpError("respondió 503"), httpx.ConnectError("sin red"),
                                   _RetryableHttpError("/api/gastos/register respondió 500: boom")])
def test_5xx_o_red_deja_la_linea_en_error_y_el_envio_se_reintenta_con_backoff(listo, error):
    gid = _queue(listo, gastos=[_gasto(), _gasto("Peajes", 8500)])
    out = service.send_one(listo, FakeBuscador(), gid, 8, FakeRest([{"id": 11}, error]))  # type: ignore[arg-type]
    assert out.status == SyncStatus.ERROR and out.enviados == 1 and out.fallidos == ()
    with listo.session() as s:
        cab = s.get(GastoOperativo, gid)
        assert [ln.sync_status for ln in cab.lineas] == [SyncStatus.ENVIADO, SyncStatus.ERROR]
        assert cab.next_attempt_at is not None and "Intento 1/8" in cab.lineas[1].error_log
    assert service.claim_batch(listo, 10, 300) == []  # aún en backoff
    assert service.claim_batch(listo, 10, 300, now=utc_now() + timedelta(hours=2)) == [gid]
    rest = FakeRest()
    out = service.send_one(listo, FakeBuscador(), gid, 8, rest)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and [b["name"] for _, b in rest.posts] == ["PEAJES"]


def test_5xx_al_agotar_los_intentos_queda_fallido(listo):
    gid = _queue(listo)
    out = service.send_one(listo, FakeBuscador(), gid, 1, FakeRest([_RetryableHttpError("respondió 503")]))  # type: ignore[arg-type]
    assert out.status == SyncStatus.FALLIDO and out.enviados == 0 and len(out.fallidos) == 1


def test_sin_cliente_rest_queda_en_espera_sin_consumir_intentos(listo, caplog):
    gid = _queue(listo)
    with caplog.at_level("ERROR"):
        out = service.send_one(listo, FakeBuscador(), gid, 1, None)
    assert out.status == SyncStatus.ERROR and "ODOO_API_LOGIN" in (out.error or "") and "ODOO_API_LOGIN" in caplog.text
    with listo.session() as s:
        cab = s.get(GastoOperativo, gid)
        assert cab.attempts == 0 and cab.next_attempt_at is not None
        assert [ln.sync_status for ln in cab.lineas] == [SyncStatus.PENDIENTE]


def test_respuesta_sin_id_marca_enviado(listo, caplog):
    gid = _queue(listo)
    with caplog.at_level("WARNING"):
        out = service.send_one(listo, FakeBuscador(), gid, 8, FakeRest([{"ok": True}]))  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and "id reconocible" in caplog.text


def test_linea_sin_soporte_falla_sin_postear(listo):
    gid = _queue(listo)
    with listo.session() as s:
        s.query(GastoOperativoSoporte).delete()
    rest = FakeRest()
    out = service.send_one(listo, FakeBuscador(), gid, 8, rest)  # type: ignore[arg-type]
    assert out.status == SyncStatus.FALLIDO and rest.posts == []


# ──────────────────────────────── ubicación obligatoria y recordatorios ────────────────────────────────

def test_sin_ubicacion_queda_retenido_y_con_ubicacion_se_libera(listo):
    gid = _recibir(listo)
    assert service.claim_batch(listo, 10, 300) == []
    res = service.asignar_ubicacion(listo, TG_USER, LAT, LON, _ahora_mas(seconds=5))
    assert res is not None and (res.combustible_id, res.origen, res.pendientes, res.placa) == (gid, "gastos", 0, "ABC123")
    assert service.claim_batch(listo, 10, 300) == [gid]
    with listo.session() as s:
        cab = s.get(GastoOperativo, gid)
        assert (cab.latitud, cab.longitud) == (LAT, LON) and cab.ubicacion_at is not None


def test_la_ubicacion_se_usa_en_todas_las_lineas(listo):
    gid = _recibir(listo, gastos=[_gasto(), _gasto("Hospedaje", 120000), _gasto("Peajes", 8500)])
    service.asignar_ubicacion(listo, TG_USER, LAT, LON, _ahora_mas(seconds=5))
    rest = FakeRest()
    assert service.claim_batch(listo, 10, 300) == [gid]
    service.send_one(listo, FakeBuscador(), gid, 8, rest)  # type: ignore[arg-type]
    assert {b["ubicacion_gps_telegram"] for _, b in rest.posts} == {"6.250000,-75.560000"} and len(rest.posts) == 3


def test_ubicacion_anterior_al_envio_o_de_otro_usuario_se_ignora(listo):
    _recibir(listo)
    assert service.asignar_ubicacion(listo, TG_USER, LAT, LON, _ahora_mas(minutes=-5)) is None
    assert service.asignar_ubicacion(listo, 999, LAT, LON, _ahora_mas(seconds=5)) is None
    assert service.claim_batch(listo, 10, 300) == []


def test_ubicacion_va_al_envio_mas_antiguo_y_avisa_de_los_pendientes(listo):
    primero = _recibir(listo)
    segundo = _recibir(listo, "2" * 8 + UUID[8:])
    res = service.asignar_ubicacion(listo, TG_USER, LAT, LON, _ahora_mas(seconds=5))
    assert res.combustible_id == primero and res.pendientes == 1
    res = service.asignar_ubicacion(listo, TG_USER, 1.0, 2.0, _ahora_mas(seconds=6))
    assert res.combustible_id == segundo and res.pendientes == 0


def test_recordatorios_a_los_10_y_30_minutos_sin_repetir(listo):
    gid = _recibir(listo)

    def numeros(**kw):
        return [(r.numero, r.origen, r.placa) for r in service.recordatorios_pendientes(listo, _ahora_mas(**kw))]

    assert numeros(minutes=9) == []
    assert numeros(minutes=11) == [(1, "gastos", "ABC123")]
    assert numeros(minutes=12) == [] and numeros(minutes=29) == []
    assert numeros(minutes=31) == [(2, "gastos", "ABC123")]
    assert numeros(minutes=500) == []
    with listo.session() as s:
        assert s.get(GastoOperativo, gid).recordatorios_ubicacion == 2


def test_no_hay_recordatorio_si_ya_hay_ubicacion(listo):
    _con_ubicacion(listo, _recibir(listo))
    assert service.recordatorios_pendientes(listo, _ahora_mas(minutes=40)) == []


def test_sender_envia_recordatorios_tolera_fallos_de_telegram_y_notifica(listo):
    gid = _recibir(listo)
    enviados, avisos = [], []

    async def remind(r):
        enviados.append((r.numero, r.origen))
        raise RuntimeError("telegram caído")

    async def notify(outcome):
        avisos.append(outcome)

    sender = GastosOperativosSender(listo, FakeBuscador(), 10, 8, 300, notify=notify, remind=remind,  # type: ignore[arg-type]
                                    rest_client=FakeRest())  # type: ignore[arg-type]
    with listo.session() as s:
        s.get(GastoOperativo, gid).fecha = utc_now() - timedelta(minutes=11)
    assert asyncio.run(sender.run_once()) == [] and enviados == [(1, "gastos")]
    assert asyncio.run(sender.run_once()) == [] and enviados == [(1, "gastos")]  # no repite
    _con_ubicacion(listo, gid)
    [outcome] = asyncio.run(sender.run_once())
    assert outcome.status == SyncStatus.ENVIADO and avisos == [outcome]


# ─────────────────────────────────────────── GPS de la flota ───────────────────────────────────────────

def test_gps_no_disponible_en_un_envio_antiguo_sin_vehiculo_y_no_retiene(listo):
    seeded = listo
    gid = _queue(seeded)
    with seeded.session() as s:  # envío previo al 2026-11-26 (cuando el vehículo era opcional): sin placa que consultar
        s.get(GastoOperativo, gid).vehiculo_id = None
    fake = _comb.FakeGps(gps.PosicionGps(6.2, -75.5, datetime(2026, 11, 26, 14, 4, tzinfo=UTC)))
    rest = FakeRest()
    out = service.send_one(seeded, FakeBuscador(), gid, 8, rest, fake)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and out.placa is None
    assert fake.llamadas == [] and rest.posts[0][1]["ubicacion_gps_vehiculo"] == "GPS no disponible"


@pytest.mark.parametrize("cliente", [None, _comb.FakeGps(None), _comb.FakeGps(RuntimeError("boom"))])
def test_gps_caido_o_sin_dato_envia_gps_no_disponible(listo, cliente):
    gid = _queue(listo)
    rest = FakeRest()
    out = service.send_one(listo, FakeBuscador(), gid, 8, rest, cliente)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and rest.posts[0][1]["ubicacion_gps_vehiculo"] == "GPS no disponible"


def test_cotejo_gps_con_la_placa_del_vehiculo_reservado_una_sola_vez(listo):
    gid = _queue(listo, gastos=[_gasto(), _gasto("Peajes", 8500)])
    fake = _comb.FakeGps(gps.PosicionGps(6.2501, -75.5601, datetime(2026, 11, 26, 14, 4, tzinfo=UTC)))
    rest = FakeRest([{"id": 1}, _RetryableHttpError("respondió 503")])
    out = service.send_one(listo, FakeBuscador(), gid, 8, rest, fake)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ERROR and fake.llamadas == ["ABC123"]
    assert service.claim_batch(listo, 10, 300, now=utc_now() + timedelta(hours=2)) == [gid]
    service.send_one(listo, FakeBuscador(), gid, 8, rest, fake)  # type: ignore[arg-type]
    assert fake.llamadas == ["ABC123"]  # no se vuelve a consultar en el reintento
    textos = {b["ubicacion_gps_vehiculo"] for _, b in rest.posts}
    assert len(textos) == 1 and next(iter(textos)).startswith("6.250100,-75.560100 · 2026-11-26 09:04 · ")
    with listo.session() as s:
        assert s.get(GastoOperativo, gid).gps_distancia_m is not None


def test_combustible_sigue_consultando_el_gps_con_su_placa(listo):
    """El refactor genérico de ``tomar_instantanea_gps`` no cambia a Combustible."""
    rid = _comb._gasto(listo)
    fake = _comb.FakeGps(gps.PosicionGps(6.2501, -75.5601, datetime(2026, 11, 26, 14, 4, tzinfo=UTC)))
    out = combustible_service.send_one(listo, _comb.FakeGastoWriter(), rid, 8, _comb.FakeRest(), fake)  # type: ignore[arg-type]
    assert out.status == SyncStatus.ENVIADO and fake.llamadas == ["ABC123"]


# ───────────────────────────── convivencia con Combustible (el más antiguo) ─────────────────────────────

def _tanqueo(db) -> int:
    registro, _ = combustible_service.submit(db, IDENTITY, _comb._payload())
    return registro.id


def _fechar(db, tanqueo_id: int, gasto_id: int, tanqueo_hace: timedelta, gasto_hace: timedelta) -> None:
    with db.session() as s:
        s.get(Combustible, tanqueo_id).fecha = utc_now() - tanqueo_hace
        s.get(GastoOperativo, gasto_id).fecha = utc_now() - gasto_hace


def test_la_ubicacion_va_al_pendiente_mas_antiguo_entre_combustible_y_gastos(listo):
    tanqueo, gasto = _tanqueo(listo), _recibir(listo)
    _fechar(listo, tanqueo, gasto, timedelta(minutes=8), timedelta(minutes=3))  # el tanqueo es más antiguo
    res = service.asignar_ubicacion_mas_antigua(listo, TG_USER, LAT, LON, _ahora_mas(seconds=5))
    assert (res.origen, res.combustible_id, res.pendientes) == ("combustible", tanqueo, 1)
    res = service.asignar_ubicacion_mas_antigua(listo, TG_USER, 1.0, 2.0, _ahora_mas(seconds=6))
    assert (res.origen, res.combustible_id, res.pendientes) == ("gastos", gasto, 0)
    assert service.asignar_ubicacion_mas_antigua(listo, TG_USER, 1.0, 2.0, _ahora_mas(seconds=7)) is None
    with listo.session() as s:
        assert (s.get(Combustible, tanqueo).latitud, s.get(GastoOperativo, gasto).latitud) == (LAT, 1.0)


def test_si_los_gastos_son_mas_antiguos_la_ubicacion_va_primero_a_ellos(listo):
    tanqueo, gasto = _tanqueo(listo), _recibir(listo)
    _fechar(listo, tanqueo, gasto, timedelta(minutes=3), timedelta(minutes=8))
    res = service.asignar_ubicacion_mas_antigua(listo, TG_USER, LAT, LON, _ahora_mas(seconds=5))
    assert (res.origen, res.combustible_id, res.pendientes) == ("gastos", gasto, 1)
    with listo.session() as s:
        assert s.get(Combustible, tanqueo).latitud is None
    assert service.claim_batch(listo, 10, 300) == [gasto]
    assert combustible_service.claim_batch(listo, 10, 300) == []  # el tanqueo sigue retenido
    res = service.asignar_ubicacion_mas_antigua(listo, TG_USER, 1.0, 2.0, _ahora_mas(seconds=6))
    assert (res.origen, res.combustible_id, res.pendientes) == ("combustible", tanqueo, 0)


def test_solo_cuenta_lo_recibido_antes_de_la_ubicacion(listo):
    tanqueo, gasto = _tanqueo(listo), _recibir(listo)
    _fechar(listo, tanqueo, gasto, timedelta(minutes=1), timedelta(minutes=8))
    # Ubicación enviada hace 5 min: el gasto (hace 8) la espera, el tanqueo (hace 1) todavía no existía.
    res = service.asignar_ubicacion_mas_antigua(listo, TG_USER, LAT, LON, _ahora_mas(minutes=-5))
    assert (res.origen, res.combustible_id, res.pendientes) == ("gastos", gasto, 1)
    assert service.asignar_ubicacion_mas_antigua(listo, TG_USER, LAT, LON, _ahora_mas(minutes=-6)) is None


def test_empate_gana_combustible_y_sin_pendientes_devuelve_none(listo):
    assert service.asignar_ubicacion_mas_antigua(listo, TG_USER, LAT, LON, _ahora_mas(seconds=5)) is None
    tanqueo, gasto = _tanqueo(listo), _recibir(listo)
    ahora = utc_now() - timedelta(minutes=2)
    with listo.session() as s:
        s.get(Combustible, tanqueo).fecha = ahora
        s.get(GastoOperativo, gasto).fecha = ahora
    assert service.asignar_ubicacion_mas_antigua(listo, TG_USER, LAT, LON, _ahora_mas(seconds=5)).origen == "combustible"


def test_on_location_despierta_la_cola_de_gastos_y_confirma(monkeypatch):
    from types import SimpleNamespace

    from .test_handlers import _ctx, _location_update, _services

    monkeypatch.setattr(tga.preop_service, "registrar_ubicacion",
                        lambda *a, **k: SimpleNamespace(id=1, preop_id=None, preop_nombre=None))
    monkeypatch.setattr(tga.gastos_service, "asignar_ubicacion_mas_antigua",
                        lambda *a, **k: SimpleNamespace(combustible_id=3, placa="ABC123", pendientes=1, origen="gastos"))
    despertados = []
    upd = _location_update()
    ctx = _ctx(_services())
    ctx.application.bot_data[tga.PREOP_DB_KEY] = "db"
    ctx.application.bot_data[tga.GASTOS_WAKE_KEY] = lambda: despertados.append("gastos")
    ctx.application.bot_data[tga.COMBUSTIBLE_WAKE_KEY] = lambda: despertados.append("combustible")
    asyncio.run(tga.on_location(upd, ctx))
    assert despertados == ["gastos"]
    text = upd.message.reply_text.await_args.args[0]
    assert "tus gastos operativos" in text and "ABC123" in text and "más sin ubicación" in text
    assert upd.message.reply_text.await_args.kwargs["reply_markup"].keyboard[0][0].request_location is True


# ─────────────────────────────────────── envío sin señal ───────────────────────────────────────

@pytest.fixture
def api(seeded):
    app = FastAPI()
    app.include_router(gastos_router)
    woke: list[int] = []
    pedidos: list[Any] = []

    async def on_accepted(recibido):
        assert service.placa_de(seeded, recibido.id) in ("ABC123", None)
        pedidos.append((recibido.id, recibido.gastos))

    app.state.gastos_operativos = GastosOperativosServices(
        seeded, BOT_TOKEN, 3600, tipos=TIPOS, wake_worker=lambda: woke.append(1), on_accepted=on_accepted)
    client = TestClient(app)
    client.woke, client.pedidos = woke, pedidos  # type: ignore[attr-defined]
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


def test_diferido_en_ventana_flujo_normal_y_trazabilidad(api, listo):
    seeded = listo
    antedatar_reservas(seeded)  # la reserva ya existía al abrir el formulario
    body = _diferido_body(abierto_hace=timedelta(minutes=80), sin_senal_hace=timedelta(minutes=40))
    headers = {"X-Telegram-Init-Data": body["diferido"]["init_data_original"]}
    first = api.post("/api/gastos-operativos", json=body, headers=headers)
    assert first.status_code == 202 and api.woke == [1] and api.pedidos == [(first.json()["id"], 1)]
    with seeded.session() as s:
        cab = s.scalar(select(GastoOperativo))
        traza = s.scalar(select(GastoOperativoEnvioDiferido))
        assert traza.estado == EstadoDiferido.ACEPTADO and traza.gasto_id == cab.id and traza.payload is None
        assert cab.ubicacion_requerida is True  # el reenvío también pide y retiene hasta la ubicación
    # Primer intento que sí llegó: el reenvío con el mismo uuid responde como duplicado y no repite el aviso.
    again = api.post("/api/gastos-operativos", json=body, headers=headers)
    assert again.json()["duplicado"] is True and len(api.pedidos) == 1


def test_diferido_vencido_solo_auditoria_conserva_el_formulario_y_el_soporte(api, seeded):
    body = _diferido_body(abierto_hace=timedelta(hours=3), sin_senal_hace=timedelta(minutes=150))
    assert api.post("/api/gastos-operativos", json=body, headers=_headers()).status_code == 410
    assert api.post("/api/gastos-operativos", json=body, headers=_headers()).status_code == 410
    assert api.woke == [] and api.pedidos == []
    with seeded.session() as s:
        assert s.scalar(select(func.count()).select_from(GastoOperativo)) == 0
        traza = s.scalar(select(GastoOperativoEnvioDiferido))
        assert traza.estado == EstadoDiferido.VENCIDO and "min después de perder la señal" in traza.motivo
        assert traza.payload["gastos"][0]["tipo"] == "Alimentación" and traza.payload["gastos"][0]["soporte"] == PDF_B64
        assert "diferido" not in traza.payload


# ──────────────────────────────────── API, autenticación y página ────────────────────────────────────

def test_api_exige_init_data_y_empleado(api):
    assert api.get("/api/gastos-operativos/context").status_code == 401
    assert api.get("/api/gastos-operativos/form").status_code == 401
    assert api.post("/api/gastos-operativos", json={}).status_code == 401
    bad = {"X-Telegram-Init-Data": make_init_data(tamper=True)}
    assert api.get("/api/gastos-operativos/context", headers=bad).status_code == 401
    assert api.post("/api/gastos-operativos", json={}, headers=_headers()).status_code == 422
    assert api.get("/api/gastos-operativos/context", headers=_headers(user_id=1)).status_code == 403
    assert api.post("/api/gastos-operativos", json=_body(), headers=_headers(user_id=1)).status_code == 403
    assert api.get(f"/api/gastos-operativos/{UUID}", headers=_headers(user_id=1)).status_code == 404


def test_api_sin_servicios_responde_503():
    client = TestClient(_app_sin_servicios())
    assert client.get("/api/gastos-operativos/form", headers=_headers()).status_code == 503


def _app_sin_servicios() -> FastAPI:
    app = FastAPI()
    app.include_router(gastos_router)
    return app


def test_api_envio_completo_con_y_sin_reserva(api, seeded):
    form = api.get("/api/gastos-operativos/form", headers=_headers()).json()
    assert [t["value"] for t in form["tipos"]] == [t.nombre for t in TIPOS] and "product_id" not in str(form)
    body = _body(gastos=[_gasto(), _gasto("Peajes", 8500, observacion=None)])
    for r in (api.get("/api/gastos-operativos/context", headers=_headers()),
              api.post("/api/gastos-operativos", json=body, headers=_headers())):  # sin reserva: 409 con código
        assert r.status_code == 409 and r.json()["detail"] == reservas.MSG_SIN_RESERVA
        assert r.headers["x-error-code"] == reservas.CODIGO_SIN_RESERVA
    assert api.woke == []
    _comb._reservar(seeded)
    ctx = api.get("/api/gastos-operativos/context", headers=_headers()).json()
    assert ctx["vehiculo"]["placa"] == "ABC123" and ctx["empleado"]["nombre"] and ctx["hoy"] == _hoy().isoformat()
    first = api.post("/api/gastos-operativos", json=body, headers=_headers())
    assert first.status_code == 202 and first.json()["sync_status"] == "pendiente"
    assert (first.json()["total"], first.json()["gastos"], first.json()["duplicado"]) == (73500, 2, False)
    assert api.woke == [1] and api.pedidos == [(first.json()["id"], 2)]
    assert service.claim_batch(seeded, 10, 300) == []  # retenido hasta la ubicación
    assert api.post("/api/gastos-operativos", json=body, headers=_headers()).json()["duplicado"] is True
    assert api.get(f"/api/gastos-operativos/{UUID}", headers=_headers()).json()["id"] == first.json()["id"]
    assert api.get(f"/api/gastos-operativos/{UUID}", headers=_headers(user_id=1)).status_code == 404


def test_api_rechaza_tipo_desconocido_y_foto_corrupta_con_422(api):
    r = api.post("/api/gastos-operativos", json=_body(gastos=[_gasto("Viáticos")]), headers=_headers())
    assert r.status_code == 422 and "Viáticos" in r.json()["detail"]
    corrupta = _b64(b"\xff\xd8\xff" + b"basura" * 20, "image/jpeg")
    r = api.post("/api/gastos-operativos", json=_body(gastos=[_gasto(soporte_b64=corrupta)]), headers=_headers())
    assert r.status_code == 422 and "foto" in r.json()["detail"] and api.woke == []


def test_api_aviso_fallido_de_ubicacion_no_devuelve_error(listo):
    seeded = listo
    app = _app_sin_servicios()

    async def boom(_):
        raise RuntimeError("telegram caído")

    app.state.gastos_operativos = GastosOperativosServices(seeded, BOT_TOKEN, 3600, tipos=TIPOS, on_accepted=boom)
    assert TestClient(app).post("/api/gastos-operativos", json=_body(), headers=_headers()).status_code == 202


def test_pagina_sin_cache_logo_versionado_y_marcas_del_formulario(api):
    page = api.get("/gastos-operativos")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
    import re

    assert re.findall(r'src="/static/viacotur-logo\.png\?v=([0-9a-f]{12})"', page.text)
    for marca in ('IDB_NAME = "gastos-operativos-offline"', "vaciarBandeja()", 'addEventListener("online"',
                  "+ Agregar gasto", "Total general", "Cancelar", "Enviar", "Gasto #", "Soporte (PDF o foto)",
                  "Primero el Preoperacional", "sin_reserva_vigente", "Abrir App para escanear", "/static/escanear_documento.js?v=", "application/pdf", "%PDF", "soloDigitos",
                  "/api/gastos-operativos"):
        assert marca in page.text
    assert "innerHTML" not in page.text and "toLocaleString" not in page.text
    assert "capture" not in page.text and "Tomar foto" not in page.text and "getUserMedia" not in page.text


# ─────────────────────────────────────────── menú del bot ───────────────────────────────────────────

def test_el_boton_va_justo_debajo_de_combustible_en_el_menu():
    etiquetas = [f.label for f in tga.FORMULARIOS]
    assert etiquetas.index(tga.BTN_GASTOS) == etiquetas.index(tga.BTN_COMBUSTIBLE) + 1
    assert tga.BTN_GASTOS == "💰 Gastos Operativos"
    settings = _services_settings()
    inline = tga.formularios_inline(settings).inline_keyboard
    textos = [fila[0].text for fila in inline]
    assert textos.index(tga.BTN_GASTOS) == textos.index(tga.BTN_COMBUSTIBLE) + 1
    boton = inline[textos.index(tga.BTN_GASTOS)][0]
    assert boton.web_app.url == "https://scan.example.com/gastos-operativos"
    assert "gastos" in [c.command for c in tga.BOT_COMMANDS]
    assert tga.BTN_GASTOS in tga.HELP_TEXT


def _services_settings():
    from .test_handlers import _services

    return _services().settings


def test_comando_gastos_abre_la_mini_app_con_las_instrucciones():
    from .test_handlers import _ctx, _services, _update

    upd = _update(text="/gastos")
    asyncio.run(tga.cmd_gastos(upd, _ctx(_services())))
    call = upd.message.reply_text.await_args
    assert "Instrucciones para Gastos Operativos" in call.args[0] and "10 minutos" in call.args[0]
    assert call.kwargs["reply_markup"].inline_keyboard[0][0].web_app.url == "https://scan.example.com/gastos-operativos"


# ───────────────────────────────────────── mensajes al conductor ─────────────────────────────────────────

def _outcome(**over: Any):
    base = dict(gasto_id=7, telegram_user_id=TG_USER, status=SyncStatus.ENVIADO, enviados=3, total_gastos=3,
                total=193500.0, placa="ABC123")
    base.update(over)
    return service.GastosOutcome(**base)


def test_mensaje_de_confirmacion_resultado_y_ubicacion():
    texto = formatting.gastos_result(_outcome())
    assert "3 gastos registrados en Odoo" in texto and "ABC123" in texto and "$ 193 500" in texto
    assert "1 gasto registrado en Odoo" in formatting.gastos_result(_outcome(enviados=1, total_gastos=1))
    parcial = formatting.gastos_result(_outcome(
        status=SyncStatus.FALLIDO, enviados=2, fallidos=(service.GastoFallido(2, "Hospedaje", 120000.0, "x"),)))
    assert "2 de 3 gastos registrados" in parcial and "Gasto #2 (Hospedaje, $ 120 000)" in parcial and "#7" in parcial
    assert "no es necesario diligenciarlos" in parcial
    ninguno = formatting.gastos_result(_outcome(
        status=SyncStatus.FALLIDO, enviados=0, total_gastos=1,
        fallidos=(service.GastoFallido(1, "Peajes", 8500.0),)))
    assert "registrados en Odoo." not in ninguno and "Gasto #1 (Peajes, $ 8 500)" in ninguno
    pedir = formatting.gastos_pedir_ubicacion("ABC123", 2)
    assert "2 gastos recibidos" in pedir and "ABC123" in pedir and "Enviar ubicación" in pedir and "10 minutos" in pedir
    assert "1 gasto recibido" in formatting.gastos_pedir_ubicacion(None, 1)
    assert "Falta la ubicación" in formatting.gastos_recordatorio_ubicacion("ABC123")
    assert "1 registro(s) más" in formatting.gastos_ubicacion_registrada("ABC123", 1)


# ─────────────────────────────────────────── cola: tablas ───────────────────────────────────────────

def test_filas_viejas_o_sin_marca_de_ubicacion_no_se_retienen(listo):
    gid = _recibir(listo)
    with listo.session() as s:
        s.get(GastoOperativo, gid).ubicacion_requerida = None
    assert service.claim_batch(listo, 10, 300) == [gid]


def test_borrar_el_envio_borra_lineas_y_soportes_en_cascada(listo):
    gid = _recibir(listo, gastos=[_gasto(), _gasto("Peajes", 8500)])
    with listo.session() as s:
        assert s.scalar(select(func.count()).select_from(GastoOperativoLinea)) == 2
        s.delete(s.get(GastoOperativo, gid))
    with listo.session() as s:
        assert s.scalar(select(func.count()).select_from(GastoOperativoLinea)) == 0
        assert s.scalar(select(func.count()).select_from(GastoOperativoSoporte)) == 0
