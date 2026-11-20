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


def test_main_keyboard_formularios_scanner_location():
    kb = tga.main_keyboard()
    assert [[b.text for b in row] for row in kb.keyboard] == [
        [tga.BTN_FORMULARIOS], [tga.BTN_ESCANEAR], [tga.BTN_UBICACION]]
    # Botones de texto: las Mini Apps se abren con botones inline (initData).
    assert all(b.web_app is None for row in kb.keyboard for b in row)
    assert kb.keyboard[2][0].request_location is True


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
    assert rows[0][0].text == tga.BTN_PREOP
    assert rows[0][0].web_app.url == "https://scan.example.com/preoperacional"


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
