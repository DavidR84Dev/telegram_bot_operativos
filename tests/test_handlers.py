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
    upd = _update(text=f"http://192.168.1.10:8079/form/accept/{TOKEN}")
    asyncio.run(tga.on_text(upd, _ctx(svc)))
    button = upd.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.web_app.url == f"https://tun.example.com/form/accept/{TOKEN}"
    assert svc.store.count() == 1


def test_non_qr_text_points_to_scanner_button():
    svc = _services()
    upd = _update(text="hola")
    asyncio.run(tga.on_text(upd, _ctx(svc)))
    assert "Escanear QR" in upd.message.reply_text.await_args.args[0]


def test_scanner_keyboard_is_plain_text_button():
    kb = tga.scanner_keyboard()
    assert kb.keyboard[0][0].text == tga.BTN_ESCANEAR
    assert kb.keyboard[0][0].web_app is None


def test_scanner_inline_button_is_web_app():
    button = tga.scanner_inline("https://scan.example.com/").inline_keyboard[0][0]
    assert button.web_app.url == "https://scan.example.com/scanner"
