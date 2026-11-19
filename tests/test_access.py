from app.access import AccessControl
from app.odoo_client import OdooClientError


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_authorized_and_unauthorized():
    calls = []

    def lookup(uid):
        calls.append(uid)
        return {"id": 1, "name": "X"} if uid == "7913476225" else None

    access = AccessControl(lookup, ttl_seconds=300, clock=Clock())
    assert access.is_authorized("7913476225")
    assert not access.is_authorized("999")


def test_cache_avoids_repeated_odoo_calls_until_ttl():
    clock = Clock()
    calls = []
    access = AccessControl(lambda uid: calls.append(uid) or {"id": 1}, ttl_seconds=300, clock=clock)
    access.is_authorized("1")
    access.is_authorized("1")
    assert len(calls) == 1
    clock.now = 301
    access.is_authorized("1")
    assert len(calls) == 2


def test_negative_result_expires_sooner():
    clock = Clock()
    calls = []
    access = AccessControl(lambda uid: calls.append(uid) or None, ttl_seconds=300, clock=clock)
    access.is_authorized("2")
    clock.now = 61
    access.is_authorized("2")
    assert len(calls) == 2


def test_odoo_error_denies_and_is_not_cached():
    state = {"fail": True}

    def lookup(uid):
        if state["fail"]:
            raise OdooClientError("caído")
        return {"id": 1}

    access = AccessControl(lookup, ttl_seconds=300, clock=Clock())
    assert not access.is_authorized("1")
    state["fail"] = False
    assert access.is_authorized("1")
