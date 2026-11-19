import asyncio
from datetime import datetime, timedelta

from app.watch_store import Watch, WatchStore
from app.watcher import Watcher, decide

NOW = datetime(2026, 11, 7, 14, 0, 0)
EXP = NOW + timedelta(hours=24)


def ticket(status="enviado", r2=False, bol2=False, exp="2026-11-08 10:00:00", number="T1"):
    return {"id": 10, "ticket_number": number, "passenger_name": "ANA", "status": status,
            "vehicle": "", "route_origin": "A", "route_destination": "B",
            "route_origin_2": "C" if r2 else "", "route_destination_2": "D" if r2 else "",
            "route_origin_bol": status != "enviado", "route_origin_bol_2": bol2,
            "access_token": "", "token_expiration": exp}


def watch(last="enviado"):
    return Watch(10, 555, "T1", last, EXP)


def test_decide_table():
    assert decide(watch(), None, NOW) == "remove"
    assert decide(watch(), ticket("cancelado"), NOW) == "remove"
    assert decide(watch(), ticket(exp="2026-11-07 09:00:00"), NOW) == "remove"
    assert decide(watch(), ticket("abordado1"), NOW) == "notify"
    assert decide(watch("abordado1"), ticket("abordado1"), NOW) == "remove"  # final ya notificado
    assert decide(watch(), ticket("enviado"), NOW) == "keep"
    assert decide(watch("abordado1"), ticket("abordado1", r2=True), NOW) == "keep"
    assert decide(watch("abordado1"), ticket("abordado2", r2=True, bol2=True), NOW) == "notify"


def _run(coro):
    return asyncio.run(coro)


def _setup(tmp_path, tickets_states):
    """tickets_states: dict id->ticket dict mutable (se cambia entre sondeos)."""
    store = WatchStore(str(tmp_path / "w.sqlite3"))
    store.add(10, 555, "T1", "enviado", NOW, EXP)
    sent = []

    async def notify(chat_id, t):
        sent.append((chat_id, t["status"]))

    def fetch(ids):
        return {i: tickets_states[i] for i in ids if i in tickets_states}

    w = Watcher(store, fetch, notify, interval_seconds=15, clock=lambda: NOW)
    return store, w, sent


def test_single_route_notifies_once_and_stops(tmp_path):
    states = {10: ticket("enviado")}
    store, w, sent = _setup(tmp_path, states)
    _run(w.poll_once())
    assert sent == [] and store.count() == 1
    states[10] = ticket("abordado1")
    _run(w.poll_once())
    assert sent == [(555, "abordado1")]
    assert store.count() == 0
    _run(w.poll_once())  # no duplica
    assert sent == [(555, "abordado1")]


def test_two_routes_notifies_each_leg(tmp_path):
    states = {10: ticket("enviado", r2=True)}
    store, w, sent = _setup(tmp_path, states)
    states[10] = ticket("abordado1", r2=True)
    _run(w.poll_once())
    assert sent == [(555, "abordado1")] and store.count() == 1
    _run(w.poll_once())
    assert sent == [(555, "abordado1")]  # sin duplicado
    states[10] = ticket("abordado2", r2=True, bol2=True)
    _run(w.poll_once())
    assert sent == [(555, "abordado1"), (555, "abordado2")] and store.count() == 0


def test_cancelled_removes_without_notifying(tmp_path):
    states = {10: ticket("cancelado")}
    store, w, sent = _setup(tmp_path, states)
    _run(w.poll_once())
    assert sent == [] and store.count() == 0


def test_notify_failure_keeps_watch_for_retry(tmp_path):
    store = WatchStore(str(tmp_path / "w.sqlite3"))
    store.add(10, 555, "T1", "enviado", NOW, EXP)
    states = {10: ticket("abordado1")}
    attempts = []

    async def flaky(chat_id, t):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("telegram caído")

    w = Watcher(store, lambda ids: {i: states[i] for i in ids}, flaky, 15, clock=lambda: NOW)
    _run(w.poll_once())
    assert store.count() == 1 and store.all()[0].last_status == "enviado"
    _run(w.poll_once())
    assert store.count() == 0 and len(attempts) == 2


def test_expired_watch_is_removed(tmp_path):
    store = WatchStore(str(tmp_path / "w.sqlite3"))
    store.add(10, 555, "T1", "enviado", NOW - timedelta(hours=30), NOW - timedelta(hours=6))
    w = Watcher(store, lambda ids: {10: ticket("enviado")}, lambda c, t: None, 15, clock=lambda: NOW)
    _run(w.poll_once())
    assert store.count() == 0
