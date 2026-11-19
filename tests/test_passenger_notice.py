"""Aviso de abordaje al pasajero: sondeo, reintentos, estado por ticket y cliente HTTP."""
import asyncio
import json
import logging
from datetime import timedelta

import httpx

from app.passenger_notifier import SENT, PassengerNotifier
from app.watch_store import WatchStore
from app.watcher import Watcher
from tests.test_watcher import EXP, NOW, ticket


def _run(coro):
    return asyncio.run(coro)


class FakePassenger:
    """Simula el bot de pasajeros: devuelve ``reasons`` en orden y luego SENT."""

    def __init__(self, reasons=()):
        self.reasons = list(reasons)
        self.calls = []

    async def __call__(self, ticket_id, status):
        self.calls.append((ticket_id, status))
        return self.reasons.pop(0) if self.reasons else SENT


def _watcher(store, states, passenger, validator_sent=None):
    async def notify(chat_id, t):
        if validator_sent is not None:
            validator_sent.append((chat_id, t["status"]))

    return Watcher(store, lambda ids: {i: states[i] for i in ids if i in states},
                   notify, 15, clock=lambda: NOW, notify_passenger=passenger)


def _linked(t, **kw):
    return dict(t, id_telegram="999", **kw)


def test_pasajero_avisado_una_vez_con_dos_validadores(tmp_path):
    store = WatchStore(str(tmp_path / "w.sqlite3"))
    store.add(10, 555, "T1", "enviado", NOW, EXP)
    store.add(10, 666, "T1", "enviado", NOW, EXP)
    states = {10: _linked(ticket("abordado1"))}
    passenger = FakePassenger()
    w = _watcher(store, states, passenger)
    _run(w.poll_once())
    _run(w.poll_once())
    assert passenger.calls == [(10, "abordado1")]
    assert store.passenger_status(10) == "abordado1"
    assert store.count() == 0


def test_fallo_del_pasajero_retiene_vigilancia_y_reintenta(tmp_path):
    store = WatchStore(str(tmp_path / "w.sqlite3"))
    store.add(10, 555, "T1", "enviado", NOW, EXP)
    states = {10: _linked(ticket("abordado1"))}
    passenger = FakePassenger(reasons=["send_failed"])
    validator = []
    w = _watcher(store, states, passenger, validator)
    _run(w.poll_once())
    # El validador ya fue avisado, pero la vigilancia se conserva por el pasajero pendiente.
    assert validator == [(555, "abordado1")]
    assert store.count() == 1 and store.passenger_status(10) is None
    _run(w.poll_once())
    assert store.count() == 0 and store.passenger_status(10) == "abordado1"
    assert passenger.calls == [(10, "abordado1"), (10, "abordado1")]
    assert validator == [(555, "abordado1")]  # sin duplicar al validador


def test_fallo_registra_log_una_sola_vez(tmp_path, caplog):
    store = WatchStore(str(tmp_path / "w.sqlite3"))
    store.add(10, 555, "T1", "enviado", NOW, EXP)
    states = {10: _linked(ticket("abordado1"))}
    passenger = FakePassenger(reasons=["send_failed", "send_failed"])
    w = _watcher(store, states, passenger)
    with caplog.at_level(logging.WARNING, logger="app.watcher"):
        _run(w.poll_once())
        _run(w.poll_once())
    assert caplog.text.count("Aviso al pasajero pendiente") == 1
    assert "motivo=send_failed" in caplog.text


def test_pasajero_sin_telegram_no_se_reintenta(tmp_path, caplog):
    store = WatchStore(str(tmp_path / "w.sqlite3"))
    store.add(10, 555, "T1", "enviado", NOW, EXP)
    states = {10: ticket("abordado1")}  # sin id_telegram
    passenger = FakePassenger()
    w = _watcher(store, states, passenger)
    with caplog.at_level(logging.WARNING, logger="app.watcher"):
        _run(w.poll_once())
    assert passenger.calls == []
    assert store.count() == 0 and store.passenger_status(10) == "abordado1"
    assert "sin ID de Telegram" in caplog.text


def test_dos_rutas_avisan_cada_abordaje(tmp_path):
    store = WatchStore(str(tmp_path / "w.sqlite3"))
    store.add(10, 555, "T1", "enviado", NOW, EXP)
    states = {10: _linked(ticket("abordado1", r2=True))}
    passenger = FakePassenger()
    w = _watcher(store, states, passenger)
    _run(w.poll_once())
    states[10] = _linked(ticket("abordado2", r2=True, bol2=True))
    _run(w.poll_once())
    assert passenger.calls == [(10, "abordado1"), (10, "abordado2")]
    assert store.count() == 0


def test_vigilancia_caducada_sin_aviso_se_elimina_con_error(tmp_path, caplog):
    store = WatchStore(str(tmp_path / "w.sqlite3"))
    store.add(10, 555, "T1", "enviado", NOW - timedelta(hours=30), NOW - timedelta(hours=6))
    states = {10: _linked(ticket("abordado1"))}
    passenger = FakePassenger(reasons=["unreachable"])
    w = _watcher(store, states, passenger)
    with caplog.at_level(logging.ERROR, logger="app.watcher"):
        _run(w.poll_once())
    assert store.count() == 0
    assert "Vigilancia caducada sin aviso al pasajero" in caplog.text


def test_sin_notificador_no_cambia_el_comportamiento(tmp_path):
    store = WatchStore(str(tmp_path / "w.sqlite3"))
    store.add(10, 555, "T1", "enviado", NOW, EXP)
    states = {10: ticket("abordado1")}
    w = _watcher(store, states, None)
    _run(w.poll_once())
    assert store.count() == 0 and store.passenger_status(10) is None


def test_store_estado_pasajero_no_retrocede():
    store = WatchStore(":memory:")
    store.set_passenger_status(10, "abordado2")
    store.set_passenger_status(10, "abordado1")
    assert store.passenger_status(10) == "abordado2"
    assert store.passenger_status(99) is None


# --- Cliente HTTP hacia el bot de pasajeros (httpx.MockTransport) ---

def _notifier(handler, token="tok-int"):
    return PassengerNotifier("http://127.0.0.1:8010/", lambda: token,
                             transport=httpx.MockTransport(handler))


def test_cliente_envia_payload_y_token():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["json"] = request.read().decode()
        return httpx.Response(200, json={"sent": True})

    assert _notifier(handler).notify_boarding(10, "abordado1") == SENT
    assert seen["url"] == "http://127.0.0.1:8010/internal/notify-boarding"
    assert seen["auth"] == "Bearer tok-int"
    assert json.loads(seen["json"]) == {"ticket_id": 10, "status": "abordado1"}


def test_cliente_devuelve_motivo_del_bot():
    def handler(request):
        return httpx.Response(200, json={"sent": False, "reason": "send_failed"})

    assert _notifier(handler).notify_boarding(10, "abordado1") == "send_failed"


def test_cliente_errores_de_red_y_http():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    assert _notifier(boom).notify_boarding(10, "abordado1") == "unreachable"
    assert _notifier(lambda r: httpx.Response(401)).notify_boarding(10, "abordado1") == "http_401"


def test_cliente_sin_token_ni_url():
    def never(request):
        raise AssertionError("no debe llamarse")

    assert _notifier(never, token=None).notify_boarding(10, "abordado1") == "no_token"
    disabled = PassengerNotifier("", lambda: "x", transport=httpx.MockTransport(never))
    assert disabled.notify_boarding(10, "abordado1") == "disabled"
