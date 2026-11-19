from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app import scan_service
from app.access import AccessControl
from app.config import Settings
from app.main import app
from app.telegram_app import Services
from app.watch_store import WatchStore
from app.watcher import Watcher
from tests.helpers import BOT_TOKEN, TOKEN, make_init_data

QR = f"https://abc.trycloudflare.com/form/accept/{TOKEN}"


def _ticket(status="enviado"):
    return {"id": 779, "ticket_number": "ticket_B97C056E", "passenger_name": "PRUEBA", "status": status,
            "vehicle": "BUS 1", "route_origin": "ITAGUI", "route_destination": "MEDELLIN",
            "route_origin_2": "", "route_destination_2": "", "route_origin_bol": False,
            "route_origin_bol_2": False, "access_token": TOKEN, "token_expiration": "2099-01-01 00:00:00"}


def _client(monkeypatch, ticket=None, pin_ok=True):
    settings = Settings(odoo_url="http://127.0.0.1:1", odoo_db="t", odoo_username="u", odoo_password="p",
                        telegram_bot_token=BOT_TOKEN, webapp_base_url="https://tun.example.com/",
                        scanner_base_url="https://scan.example.com/", database_url=":memory:")
    store = WatchStore(":memory:")
    access = AccessControl(lambda uid: {"id": 1} if (pin_ok and uid == "7913476225") else None, 300)
    watcher = Watcher(store, lambda ids: {}, AsyncMock(), 15)
    monkeypatch.setattr(scan_service, "find_ticket_by_token", lambda odoo, t: ticket)
    app.state.services = Services(settings, None, access, store, watcher)
    watcher.ensure_running = lambda: None  # sin bucle de sondeo en la prueba HTTP
    return TestClient(app), store


def test_scanner_page_is_served():
    r = TestClient(app).get("/scanner")
    assert r.status_code == 200
    assert "showScanQrPopup" in r.text and "/api/scan" in r.text


def test_missing_init_data_is_401(monkeypatch):
    client, _ = _client(monkeypatch, _ticket())
    r = client.post("/api/scan", json={"qr_text": QR})
    assert r.status_code == 401


def test_forged_init_data_is_401(monkeypatch):
    client, _ = _client(monkeypatch, _ticket())
    r = client.post("/api/scan", json={"qr_text": QR},
                    headers={"X-Telegram-Init-Data": make_init_data(tamper=True)})
    assert r.status_code == 401


def test_unauthorized_employee_is_403(monkeypatch):
    client, store = _client(monkeypatch, _ticket(), pin_ok=False)
    r = client.post("/api/scan", json={"qr_text": QR}, headers={"X-Telegram-Init-Data": make_init_data()})
    assert r.status_code == 403 and store.count() == 0


def test_authorized_scan_returns_ticket_and_odoo_url(monkeypatch):
    client, store = _client(monkeypatch, _ticket())
    r = client.post("/api/scan", json={"qr_text": QR}, headers={"X-Telegram-Init-Data": make_init_data()})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "ok" and body["boardable"] and body["watching"]
    assert body["odoo_url"] == f"https://tun.example.com/form/accept/{TOKEN}"
    assert body["ticket"]["ticket_number"] == "ticket_B97C056E"
    assert "access_token" not in body["ticket"]
    assert body["ticket"]["status_label"] == "Enviado (válido)"
    assert store.count() == 1


def test_invalid_qr_is_reported_without_watch(monkeypatch):
    client, store = _client(monkeypatch, _ticket())
    r = client.post("/api/scan", json={"qr_text": "https://x.com/web/login"},
                    headers={"X-Telegram-Init-Data": make_init_data()})
    assert r.status_code == 200 and r.json()["status"] == "invalid_qr"
    assert store.count() == 0


def test_empty_qr_text_is_422(monkeypatch):
    client, _ = _client(monkeypatch, _ticket())
    r = client.post("/api/scan", json={"qr_text": ""}, headers={"X-Telegram-Init-Data": make_init_data()})
    assert r.status_code == 422
