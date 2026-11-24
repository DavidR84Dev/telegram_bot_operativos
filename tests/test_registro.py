"""Pruebas de 📝 Registro: validaciones, tabla registros_conductor, llamadas a Odoo y conversación."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from telegram.ext import ConversationHandler
from tenacity import wait_none

from app import telegram_app as tga
from app.access import AccessControl
from app.preop import etl, registro
from app.preop.db import Database
from app.preop.models import Empleado, RegistroConductor
from app.preop.rest_client import OdooApiError, OdooRestClient
from app.watch_store import WatchStore
from app.watcher import Watcher

from .test_handlers import _settings
from .test_preop import FakeRest, _empleado

NUEVO = 5550001
DOC = "1036123456"


class FakeApi:
    """Cliente REST falso: /api/empleados paginado y POST /api/empleados/register."""

    def __init__(self, empleados: list[dict], fail: bool = False) -> None:
        self.empleados = empleados
        self.fail = fail
        self.posts: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def iter_items(self, path, company_id, page_size=500):
        if self.fail:
            raise OdooApiError("API caída")
        assert path == "/api/empleados"
        yield from (dict(e) for e in self.empleados)

    def post_json(self, path, body):
        self.posts.append((path, body))
        return {"response": "El registro ha sido actualizado"}


def _datos(documento=DOC, uid=NUEVO) -> registro.DatosRegistro:
    return registro.DatosRegistro(uid, "nuevo", "Pedro Pérez Gómez", documento, "pedro@correo.com")


@pytest.fixture
def db():
    database = Database("sqlite:///:memory:")
    etl.sync_all(database, FakeRest({
        "/api/empleados": [_empleado(200, pin="", nombre="Pedro Perez Gomez", identificacion=DOC)],
    }), [1])
    return database


def _fila(db) -> RegistroConductor:
    with db.session() as s:
        fila = s.scalar(select(RegistroConductor).where(RegistroConductor.telegram_user_id == NUEVO))
        assert fila is not None
        return fila


# ──────────────────────────────────────── validaciones ────────────────────────────────────────

@pytest.mark.parametrize("texto, esperado", [
    ("  pedro   pérez ", "pedro pérez"), ("María-José O'Neil", "María-José O'Neil"),
    ("Pedro", None), ("Pedro 123", None), ("📋 Formularios", None), ("ab", None),
])
def test_limpiar_nombre(texto, esperado):
    assert registro.limpiar_nombre(texto) == esperado


@pytest.mark.parametrize("texto, esperado", [
    ("1.036.123.456", DOC), ("1036 123-456", DOC), ("12ab", None), ("1234", None), ("1" * 16, None),
])
def test_limpiar_documento(texto, esperado):
    assert registro.limpiar_documento(texto) == esperado


@pytest.mark.parametrize("texto, esperado", [
    (" Pedro@Correo.COM ", "pedro@correo.com"), ("pedro@correo", None), ("pedro correo.com", None),
])
def test_limpiar_correo(texto, esperado):
    assert registro.limpiar_correo(texto) == esperado


# ─────────────────────────────────────── proceso en Odoo ───────────────────────────────────────

def test_conductor_existente_queda_registrado_y_envia_pin(db):
    api = FakeApi([{"id": 7, "identificacion": "999"}, {"id": 200, "nombre": "Pedro Perez Gomez",
                                                         "identificacion": "1.036.123.456", "codigo_pin": ""}])
    resultado = registro.registrar_conductor(db, lambda: api, [1], _datos())
    assert resultado.estado == registro.REGISTRADO
    assert api.posts == [("/api/empleados/register", {"id": 200, "pin": str(NUEVO)})]
    fila = _fila(db)
    assert (fila.estado, fila.empleado_odoo_id, fila.documento, fila.correo, fila.telegram_username) == (
        "registrado", 200, DOC, "pedro@correo.com", "nuevo")
    assert fila.nombre_completo == "Pedro Pérez Gómez" and fila.registrado_at is not None
    # La copia local de empleados ya reconoce al conductor (el preoperacional no espera la sincronización).
    with db.session() as s:
        assert s.scalar(select(Empleado.codigo_pin).where(Empleado.odoo_id == 200)) == str(NUEVO)
    assert registro.ya_registrado(db, NUEVO) is True


def test_conductor_inexistente_no_llama_register(db):
    api = FakeApi([{"id": 7, "identificacion": "999"}])
    assert registro.registrar_conductor(db, lambda: api, [1], _datos()).estado == registro.NO_ENCONTRADO
    assert api.posts == []
    assert _fila(db).estado == "no_encontrado"
    assert registro.ya_registrado(db, NUEVO) is False


def test_no_sobrescribe_pin_de_otro_usuario(db):
    api = FakeApi([{"id": 200, "identificacion": DOC, "codigo_pin": "123456"}])
    assert registro.registrar_conductor(db, lambda: api, [1], _datos()).estado == registro.PIN_OCUPADO
    assert api.posts == []
    assert _fila(db).estado == "pin_ocupado"


def test_pin_ya_igual_no_reescribe_pero_queda_registrado(db):
    api = FakeApi([{"id": 200, "identificacion": DOC, "codigo_pin": str(NUEVO)}])
    assert registro.registrar_conductor(db, lambda: api, [1], _datos()).estado == registro.REGISTRADO
    assert api.posts == []


def test_error_de_odoo_permite_reintentar_en_la_misma_fila(db):
    assert registro.registrar_conductor(db, lambda: FakeApi([], fail=True), [1], _datos()).estado == registro.ERROR
    fila = _fila(db)
    assert fila.estado == "error" and "API caída" in (fila.error_log or "") and fila.intentos == 1
    api = FakeApi([{"id": 200, "identificacion": DOC}])
    assert registro.registrar_conductor(db, lambda: api, [1], _datos()).estado == registro.REGISTRADO
    fila = _fila(db)
    assert fila.intentos == 2 and fila.error_log is None
    with db.session() as s:
        assert len(s.scalars(select(RegistroConductor)).all()) == 1


def test_rest_client_post_json_con_token(monkeypatch):
    monkeypatch.setattr(OdooRestClient.post_json.retry, "wait", wait_none())  # type: ignore[attr-defined]
    vistos = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/authenticate":
            return httpx.Response(200, json={"token": "t"})
        vistos.append((request.method, request.url.path, request.headers["Authorization"],
                       json.loads(request.content)))
        if len(vistos) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"response": "ok"})

    client = OdooRestClient("http://odoo.test", "bot", "pw", transport=httpx.MockTransport(handler))
    assert client.post_json("/api/empleados/register", {"id": 1, "pin": "9"}) == {"response": "ok"}
    assert vistos[-1] == ("POST", "/api/empleados/register", "Bearer t", {"id": 1, "pin": "9"})


def test_rest_client_post_json_4xx_es_error():
    def handler(request):
        if request.url.path == "/api/authenticate":
            return httpx.Response(200, json={"token": "t"})
        return httpx.Response(400, json={"error": "Faltan parámetros"})

    client = OdooRestClient("http://odoo.test", "bot", "pw", transport=httpx.MockTransport(handler))
    with pytest.raises(OdooApiError):
        client.post_json("/api/empleados/register", {})


# ──────────────────────────────────────── conversación ────────────────────────────────────────

def _ctx(db, api, autorizados=()):
    store = WatchStore(":memory:")
    access = AccessControl(lambda uid: {"id": 1} if uid in autorizados else None, 300)
    services = tga.Services(_settings(), SimpleNamespace(), access, store, Watcher(store, lambda i: {}, AsyncMock(), 15))
    bot_data = {tga.SERVICES_KEY: services,
                tga.REGISTRO_KEY: tga.RegistroServices(db=db, client_factory=lambda: api, company_ids=[1])}
    return SimpleNamespace(application=SimpleNamespace(bot_data=bot_data), user_data={})


def _upd(text, uid=NUEVO):
    msg = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=uid, username="nuevo"),
                           effective_chat=SimpleNamespace(id=uid))


def _respuestas(upd) -> list[str]:
    return [call.args[0] for call in upd.message.reply_text.await_args_list]


def test_usuario_nuevo_ve_registro_en_lugar_del_menu(db):
    ctx = _ctx(db, FakeApi([]))
    for comando in (tga.cmd_start, tga.cmd_preop, tga.cmd_formularios):
        upd = _upd("/start")
        asyncio.run(comando(upd, ctx))
        call = upd.message.reply_text.await_args
        assert call.args[0] == tga.MSG_REG_INVITACION
        assert call.kwargs["reply_markup"].keyboard[0][0].text == tga.BTN_REGISTRO


def test_flujo_completo_registra_y_da_acceso(db):
    api = FakeApi([{"id": 200, "identificacion": DOC, "codigo_pin": ""}])
    ctx = _ctx(db, api)
    access = ctx.application.bot_data[tga.SERVICES_KEY].access
    assert access.is_authorized(str(NUEVO)) is False  # queda en caché como no autorizado

    pasos = [(tga.reg_inicio, tga.BTN_REGISTRO, tga.REG_NOMBRE),
             (tga.reg_nombre, "Pedro", tga.REG_NOMBRE),                 # falta el apellido
             (tga.reg_nombre, "Pedro Pérez Gómez", tga.REG_DOCUMENTO),
             (tga.reg_documento, "abc", tga.REG_DOCUMENTO),
             (tga.reg_documento, "1.036.123.456", tga.REG_CORREO),
             (tga.reg_correo, "pedro@", tga.REG_CORREO),
             (tga.reg_correo, "Pedro@Correo.com", ConversationHandler.END)]
    for handler, texto, estado in pasos:
        upd = _upd(texto)
        assert asyncio.run(handler(upd, ctx)) == estado, texto

    assert _respuestas(upd) == [tga.MSG_REG_VALIDANDO, tga.MSG_REG_EXITO]
    assert api.posts == [("/api/empleados/register", {"id": 200, "pin": str(NUEVO)})]
    fila = _fila(db)
    assert (fila.nombre_completo, fila.documento, fila.correo, fila.estado) == (
        "Pedro Pérez Gómez", DOC, "pedro@correo.com", "registrado")
    # Se descartó el "no autorizado" cacheado: la próxima consulta va de nuevo a Odoo.
    assert str(NUEVO) not in access._cache


def test_flujo_conductor_no_existe_muestra_mensaje_de_talento_humano(db):
    ctx = _ctx(db, FakeApi([]))
    ctx.user_data.update(reg_nombre="Pedro Pérez", reg_documento=DOC)
    upd = _upd("pedro@correo.com")
    assert asyncio.run(tga.reg_correo(upd, ctx)) == ConversationHandler.END
    assert _respuestas(upd)[-1] == ("Conductor no registrado en Odoo. Por favor comunicarse con el área "
                                    "de Talento humano")


def test_registro_solo_para_usuarios_nuevos(db):
    # Ya autorizado (PIN en Odoo): no entra al registro.
    ctx = _ctx(db, FakeApi([]), autorizados={str(NUEVO)})
    upd = _upd(tga.BTN_REGISTRO)
    assert asyncio.run(tga.reg_inicio(upd, ctx)) == ConversationHandler.END
    assert _respuestas(upd) == [tga.MSG_REG_YA_REGISTRADO]

    # Registrado en la tabla pero sin acceso en Odoo: tampoco puede registrarse de nuevo.
    registro.registrar_conductor(db, lambda: FakeApi([{"id": 200, "identificacion": DOC}]), [1], _datos())
    ctx = _ctx(db, FakeApi([]))
    upd = _upd(tga.BTN_REGISTRO)
    assert asyncio.run(tga.reg_inicio(upd, ctx)) == ConversationHandler.END
    assert _respuestas(upd) == [tga.MSG_REG_SIN_ACCESO]
    upd = _upd("/start")
    asyncio.run(tga.cmd_start(upd, ctx))
    assert _respuestas(upd) == [tga.MSG_REG_SIN_ACCESO]


def test_cancelar_registro(db):
    ctx = _ctx(db, FakeApi([]))
    ctx.user_data.update(reg_nombre="Pedro Pérez")
    upd = _upd("/cancelar")
    assert asyncio.run(tga.reg_cancelar(upd, ctx)) == ConversationHandler.END
    assert "cancelado" in _respuestas(upd)[0] and ctx.user_data == {}


def test_conversacion_va_antes_que_los_demas_handlers():
    app = tga.build_application(_settings(), SimpleNamespace(), WatchStore(":memory:"))
    primero = app.handlers[0][0]
    assert isinstance(primero, ConversationHandler) and primero.name == "registro"
    assert [c.command for c in tga.BOT_COMMANDS][:2] == ["start", "registro"]
