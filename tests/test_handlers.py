import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import scan_service
from app import telegram_app as tga
from app.access import AccessControl
from app.config import Settings
from app.watch_store import WatchStore
from app.watcher import Watcher
from tests.helpers import TOKEN


def _settings():
    return Settings(odoo_url="http://127.0.0.1:1", odoo_db="t", odoo_username="u", odoo_password="p",
                    telegram_bot_token="1:x", webapp_base_url="https://tun.example.com/",
                    scanner_base_url="https://scan.example.com/", database_url=":memory:")


def _ctx(services):
    return SimpleNamespace(application=SimpleNamespace(bot_data={tga.SERVICES_KEY: services}))


def _update(user_id=7913476225, text=None, chat_id=555):
    msg = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=user_id),
                           effective_chat=SimpleNamespace(id=chat_id))


def _services(pin_ok=True):
    store = WatchStore(":memory:")
    access = AccessControl(lambda uid: {"id": 1} if (pin_ok and uid == "7913476225") else None, 300)
    watcher = Watcher(store, lambda ids: {}, AsyncMock(), 15)
    return tga.Services(_settings(), SimpleNamespace(), access, store, watcher)


def _ticket(status="enviado"):
    return {"id": 779, "ticket_number": "ticket_B97C056E", "passenger_name": "PRUEBA", "status": status,
            "vehicle": "", "route_origin": "ITAGUI", "route_destination": "MEDELLIN",
            "route_origin_2": "", "route_destination_2": "", "route_origin_bol": False,
            "route_origin_bol_2": False, "token_expiration": "2099-01-01 00:00:00"}


def test_unauthorized_user_is_rejected():
    svc = _services(pin_ok=False)
    upd = _update(user_id=111, text=f"https://x.com/form/accept/{TOKEN}")
    asyncio.run(tga.on_text(upd, _ctx(svc)))
    assert "no autorizado" in upd.message.reply_text.await_args.args[0].lower()
    assert svc.store.count() == 0


def test_pasted_url_shows_card_webapp_button_and_watch(monkeypatch):
    monkeypatch.setattr(scan_service, "find_ticket_by_token", lambda odoo, t: _ticket())
    svc = _services()
    upd = _update(text=f"http://192.0.2.10:8079/form/accept/{TOKEN}")
    asyncio.run(tga.on_text(upd, _ctx(svc)))
    button = upd.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.web_app.url == f"https://tun.example.com/form/accept/{TOKEN}"
    assert svc.store.count() == 1


def test_non_qr_text_points_to_scanner_button():
    svc = _services()
    upd = _update(text="hola")
    asyncio.run(tga.on_text(upd, _ctx(svc)))
    assert "Escanear QR" in upd.message.reply_text.await_args.args[0]


def test_main_keyboard_only_formularios_and_location_collapsible():
    kb = tga.main_keyboard()
    # Una sola fila: Formularios y Enviar ubicación lado a lado.
    assert [[b.text for b in row] for row in kb.keyboard] == [[tga.BTN_FORMULARIOS, tga.BTN_UBICACION]]
    # Botones de texto: las Mini Apps se abren con botones inline (initData).
    assert all(b.web_app is None for row in kb.keyboard for b in row)
    assert kb.keyboard[0][1].request_location is True
    # No persistente: Telegram lo pliega detrás de su botón de teclado (toggle-reply-markup).
    assert kb.is_persistent is False


def test_start_shows_welcome_and_keyboard():
    upd = _update(text="/start")
    asyncio.run(tga.cmd_start(upd, _ctx(_services())))
    call = upd.message.reply_text.await_args
    assert call.args[0].startswith("Bienvenido")
    assert call.kwargs["reply_markup"].keyboard[0][0].text == tga.BTN_FORMULARIOS


def test_formularios_lists_preoperacional_first():
    upd = _update(text=tga.BTN_FORMULARIOS)
    asyncio.run(tga.on_text(upd, _ctx(_services())))
    call = upd.message.reply_text.await_args
    assert call.args[0] == "📂 Formularios disponibles:"
    rows = call.kwargs["reply_markup"].inline_keyboard
    # Preoperacional -> AST -> Prueba de Ruta -> Combustible -> Gastos Operativos -> Escanear QR -> Finalizar.
    assert [row[0].text for row in rows] == [tga.BTN_PREOP, tga.BTN_AST, tga.BTN_PRUEBA_RUTA,
                                             tga.BTN_COMBUSTIBLE, tga.BTN_GASTOS, tga.BTN_ESCANEAR,
                                             tga.BTN_FINALIZAR]
    assert rows[0][0].web_app.url == "https://scan.example.com/preoperacional"
    assert rows[1][0].web_app.url == "https://scan.example.com/ast"
    assert rows[2][0].web_app.url == "https://scan.example.com/prueba-ruta"
    assert rows[3][0].web_app.url == "https://scan.example.com/combustible"
    assert rows[4][0].web_app.url == "https://scan.example.com/gastos-operativos"
    assert rows[5][0].web_app.url == "https://scan.example.com/scanner"
    assert rows[6][0].web_app is None and rows[6][0].callback_data == tga.CB_FINALIZAR_PEDIR


def test_combustible_command_opens_mini_app():
    upd = _update(text="/combustible")
    asyncio.run(tga.cmd_combustible(upd, _ctx(_services())))
    button = upd.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == tga.BTN_COMBUSTIBLE and button.web_app.url == "https://scan.example.com/combustible"
    assert "combustible" in [c.command for c in tga.BOT_COMMANDS]


def test_prueba_ruta_command_opens_mini_app():
    upd = _update(text="/prueba_ruta")
    asyncio.run(tga.cmd_prueba_ruta(upd, _ctx(_services())))
    button = upd.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == tga.BTN_PRUEBA_RUTA and button.web_app.url == "https://scan.example.com/prueba-ruta"
    assert "prueba_ruta" in [c.command for c in tga.BOT_COMMANDS]


def test_ast_command_opens_mini_app_and_is_registered():
    upd = _update(text="/ast")
    asyncio.run(tga.cmd_ast(upd, _ctx(_services())))
    button = upd.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == tga.BTN_AST and button.web_app.url == "https://scan.example.com/ast"
    assert "ast" in [c.command for c in tga.BOT_COMMANDS]
    assert tga.BTN_AST in tga.HELP_TEXT


def test_ast_command_requires_authorization():
    upd = _update(user_id=111, text="/ast")
    asyncio.run(tga.cmd_ast(upd, _ctx(_services())))
    assert "no autorizado" in upd.message.reply_text.await_args.args[0]


def test_scanner_button_opens_scanner():
    upd = _update(text=tga.BTN_ESCANEAR)
    asyncio.run(tga.on_text(upd, _ctx(_services())))
    button = upd.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.web_app.url == "https://scan.example.com/scanner"


def test_menu_button_requires_authorization():
    upd = _update(user_id=111, text=tga.BTN_FORMULARIOS)
    asyncio.run(tga.on_text(upd, _ctx(_services(pin_ok=False))))
    assert "no autorizado" in upd.message.reply_text.await_args.args[0].lower()


def test_location_is_saved_in_bot_database(monkeypatch):
    saved = []

    def fake_registrar(db, user_id, lat, lon, accuracy=None):
        saved.append((db, user_id, lat, lon, accuracy))
        return SimpleNamespace(id=1, preop_id=9, preop_nombre="PRE-0009")

    monkeypatch.setattr(tga.preop_service, "registrar_ubicacion", fake_registrar)
    monkeypatch.setattr(tga.gastos_service, "asignar_ubicacion_mas_antigua", lambda *a, **k: None)
    upd = _update()
    upd.message.location = SimpleNamespace(latitude=6.25, longitude=-75.56, horizontal_accuracy=8.0)
    ctx = _ctx(_services())
    ctx.application.bot_data[tga.PREOP_DB_KEY] = "db"
    asyncio.run(tga.on_location(upd, ctx))
    assert saved == [("db", 7913476225, 6.25, -75.56, 8.0)]
    assert "PRE-0009" in upd.message.reply_text.await_args.args[0]


def test_location_text_from_desktop_explains_how():
    upd = _update(text=tga.BTN_UBICACION)
    asyncio.run(tga.on_text(upd, _ctx(_services())))
    assert "celular" in upd.message.reply_text.await_args.args[0]


def test_scanner_inline_button_is_web_app():
    button = tga.scanner_inline("https://scan.example.com/").inline_keyboard[0][0]
    assert button.web_app.url == "https://scan.example.com/scanner"


def test_finalizar_asks_if_workday_is_over_with_two_options():
    upd = _update(text=tga.BTN_FINALIZAR)
    asyncio.run(tga.on_text(upd, _ctx(_services())))
    call = upd.message.reply_text.await_args
    assert call.args[0] == "¿Confirmas que ya terminaste tu jornada laboral?"
    botones = call.kwargs["reply_markup"].inline_keyboard[0]
    assert [(b.text, b.callback_data) for b in botones] == [
        ("✅ Sí, finalizar", tga.CB_FINALIZAR_SI), ("❌ Cancelar", tga.CB_FINALIZAR_NO)]


def test_finalizar_requires_authorization():
    upd = _update(user_id=111, text="/finalizar")
    asyncio.run(tga.cmd_finalizar(upd, _ctx(_services(pin_ok=False))))
    assert "no autorizado" in upd.message.reply_text.await_args.args[0].lower()


def _callback(data, user_id=7913476225):
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock())
    return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))


def test_finalizar_confirm_releases_only_own_reservations(monkeypatch):
    liberados = []

    def fake_finalizar(db, user_id):
        liberados.append((db, user_id))
        return SimpleNamespace(placas=("ABC123",), conductor="Ana", reservado_at=None, liberado_at=None,
                               preoperacionales=2, jefe_telegram_id=999)

    monkeypatch.setattr(tga.preop_reservas, "finalizar_jornada", fake_finalizar)
    ctx = _ctx(_services())
    ctx.application.bot_data[tga.PREOP_DB_KEY] = "db"
    ctx.bot = SimpleNamespace(send_message=AsyncMock())
    upd = _callback(tga.CB_FINALIZAR_SI, user_id=42)
    asyncio.run(tga.on_finalizar_confirm(upd, ctx))
    assert liberados == [("db", 42)]
    assert "ABC123" in upd.callback_query.edit_message_text.await_args.args[0]
    # Aviso al jefe inmediato por Telegram.
    aviso = ctx.bot.send_message.await_args.kwargs
    assert aviso["chat_id"] == 999 and "Jornada finalizada" in aviso["text"] and "Ana" in aviso["text"]


def test_finalizar_cancel_returns_to_forms_panel(monkeypatch):
    monkeypatch.setattr(tga.preop_reservas, "finalizar_jornada", lambda *a: (_ for _ in ()).throw(AssertionError))
    ctx = _ctx(_services())
    ctx.application.bot_data[tga.PREOP_DB_KEY] = "db"
    upd = _callback(tga.CB_FINALIZAR_NO)
    asyncio.run(tga.on_finalizar_confirm(upd, ctx))
    call = upd.callback_query.edit_message_text.await_args
    assert call.args[0] == "📂 Formularios disponibles:"
    assert call.kwargs["reply_markup"].inline_keyboard[0][0].text == tga.BTN_PREOP


def test_finalizar_from_forms_panel_asks_confirmation():
    ctx = _ctx(_services())
    upd = _callback(tga.CB_FINALIZAR_PEDIR)
    asyncio.run(tga.on_finalizar_confirm(upd, ctx))
    call = upd.callback_query.edit_message_text.await_args
    assert call.args[0] == tga.FINALIZAR_PREGUNTA
    assert [b.callback_data for b in call.kwargs["reply_markup"].inline_keyboard[0]] == [
        tga.CB_FINALIZAR_SI, tga.CB_FINALIZAR_NO]


def test_finalizar_from_forms_panel_requires_authorization():
    ctx = _ctx(_services(pin_ok=False))
    upd = _callback(tga.CB_FINALIZAR_PEDIR, user_id=111)
    asyncio.run(tga.on_finalizar_confirm(upd, ctx))
    assert upd.callback_query.answer.await_args.kwargs["show_alert"] is True
    upd.callback_query.edit_message_text.assert_not_awaited()


def _location_update(**extra):
    from datetime import UTC, datetime

    upd = _update()
    upd.message.location = SimpleNamespace(latitude=6.25, longitude=-75.56, horizontal_accuracy=8.0)
    upd.message.date = datetime.now(UTC)
    for k, v in extra.items():
        setattr(upd.message, k, v)
    return upd


def test_location_is_assigned_to_pending_fuel_and_asks_again_if_more(monkeypatch):
    monkeypatch.setattr(tga.preop_service, "registrar_ubicacion",
                        lambda *a, **k: SimpleNamespace(id=1, preop_id=None, preop_nombre=None))
    calls = []

    def fake_asignar(db, user_id, lat, lon, date):
        calls.append((user_id, lat, lon))
        return SimpleNamespace(combustible_id=3, placa="ABC123", pendientes=1, origen="combustible")

    monkeypatch.setattr(tga.gastos_service, "asignar_ubicacion_mas_antigua", fake_asignar)
    upd = _location_update()
    ctx = _ctx(_services())
    ctx.application.bot_data[tga.PREOP_DB_KEY] = "db"
    asyncio.run(tga.on_location(upd, ctx))
    assert calls == [(7913476225, 6.25, -75.56)]
    text = upd.message.reply_text.await_args.args[0]
    assert "ABC123" in text and "más sin ubicación" in text
    assert upd.message.reply_text.await_args.kwargs["reply_markup"].keyboard[0][0].request_location is True


def test_forwarded_location_is_not_assigned_to_fuel(monkeypatch):
    monkeypatch.setattr(tga.preop_service, "registrar_ubicacion",
                        lambda *a, **k: SimpleNamespace(id=1, preop_id=None, preop_nombre=None))

    def boom(*a, **k):
        raise AssertionError("no debe asignarse")

    monkeypatch.setattr(tga.gastos_service, "asignar_ubicacion_mas_antigua", boom)
    upd = _location_update(forward_origin=object())
    ctx = _ctx(_services())
    ctx.application.bot_data[tga.PREOP_DB_KEY] = "db"
    asyncio.run(tga.on_location(upd, ctx))
    assert "Ubicación recibida" in upd.message.reply_text.await_args.args[0]


def test_location_wakes_fuel_worker(monkeypatch):
    monkeypatch.setattr(tga.preop_service, "registrar_ubicacion",
                        lambda *a, **k: SimpleNamespace(id=1, preop_id=None, preop_nombre=None))
    monkeypatch.setattr(tga.gastos_service, "asignar_ubicacion_mas_antigua",
                        lambda *a, **k: SimpleNamespace(combustible_id=3, placa="ABC123", pendientes=0, origen="combustible"))
    despertado = []
    upd = _location_update()
    ctx = _ctx(_services())
    ctx.application.bot_data[tga.PREOP_DB_KEY] = "db"
    ctx.application.bot_data[tga.COMBUSTIBLE_WAKE_KEY] = lambda: despertado.append(1)
    asyncio.run(tga.on_location(upd, ctx))
    assert despertado == [1]
