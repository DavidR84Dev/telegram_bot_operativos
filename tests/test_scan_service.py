from datetime import datetime

from app import scan_service
from app.scan_service import scan_qr
from app.watch_store import WatchStore

NOW = datetime(2026, 11, 7, 14, 0, 0)
URL = "https://abc.trycloudflare.com/form/accept/QjZL-MkZbE_DHS5oSv8MKJv5HDpUyhlaYgAGoh8fVoc"


def _ticket(status="enviado", exp="2026-11-08 10:00:00"):
    return {"id": 779, "ticket_number": "T1", "passenger_name": "ANA", "status": status,
            "token_expiration": exp, "route_origin_2": "", "route_destination_2": "",
            "route_origin_bol_2": False}


def test_invalid_text_is_not_a_ticket():
    out = scan_qr(None, WatchStore(":memory:"), 24, "https://x.com/web/login", 555, NOW)
    assert out.status == "invalid_qr"


def test_unknown_token_is_not_found(monkeypatch):
    monkeypatch.setattr(scan_service, "find_ticket_by_token", lambda odoo, t: None)
    out = scan_qr(None, WatchStore(":memory:"), 24, URL, 555, NOW)
    assert out.status == "not_found"


def test_odoo_error_is_reported(monkeypatch):
    from app.odoo_client import OdooClientError

    def boom(odoo, t):
        raise OdooClientError("caído")
    monkeypatch.setattr(scan_service, "find_ticket_by_token", boom)
    assert scan_qr(None, WatchStore(":memory:"), 24, URL, 555, NOW).status == "odoo_error"


def test_boardable_ticket_is_watched(monkeypatch):
    monkeypatch.setattr(scan_service, "find_ticket_by_token", lambda odoo, t: _ticket())
    store = WatchStore(":memory:")
    out = scan_qr(None, store, 24, URL, 555, NOW)
    assert out.status == "ok" and out.boardable and out.watching
    assert store.count() == 1 and store.all()[0].ticket_id == 779


def test_boardable_without_chat_is_not_watched(monkeypatch):
    monkeypatch.setattr(scan_service, "find_ticket_by_token", lambda odoo, t: _ticket())
    store = WatchStore(":memory:")
    out = scan_qr(None, store, 24, URL, None, NOW)
    assert out.status == "ok" and out.boardable and not out.watching and store.count() == 0


def test_cancelled_or_expired_is_shown_but_not_watched(monkeypatch):
    store = WatchStore(":memory:")
    monkeypatch.setattr(scan_service, "find_ticket_by_token", lambda odoo, t: _ticket("cancelado"))
    out = scan_qr(None, store, 24, URL, 555, NOW)
    assert out.status == "ok" and not out.boardable and not out.watching
    monkeypatch.setattr(scan_service, "find_ticket_by_token",
                        lambda odoo, t: _ticket(exp="2026-11-07 09:00:00"))
    out = scan_qr(None, store, 24, URL, 555, NOW)
    assert not out.boardable and store.count() == 0
