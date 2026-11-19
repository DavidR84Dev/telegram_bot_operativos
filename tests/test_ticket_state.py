from datetime import datetime

import pytest

from app import ticket_state as ts

TOKEN = "QjZL-MkZbE_DHS5oSv8MKJv5HDpUyhlaYgAGoh8fVoc"
NOW = datetime(2026, 11, 7, 14, 0, 0)


@pytest.mark.parametrize("payload", [
    f"http://192.0.2.10:8079/form/accept/{TOKEN}",
    f"https://abc.trycloudflare.com/form/accept/{TOKEN}",
    f"  https://www.viacotur.com/form/accept/{TOKEN}  ",
])
def test_extract_token_ignores_host(payload):
    assert ts.extract_token(payload) == TOKEN


@pytest.mark.parametrize("payload", [
    "", "hola", "https://x.com/form/accept/corto", "https://x.com/otra/ruta/" + TOKEN + "/x",
    "https://x.com/web/login",
])
def test_extract_token_rejects_non_tickets(payload):
    assert ts.extract_token(payload) is None


def t(status="enviado", r2=False, bol2=False, exp="2026-11-08 10:00:00"):
    d = {"status": status, "token_expiration": exp, "route_origin_bol_2": bol2,
         "route_origin_2": "A" if r2 else "", "route_destination_2": "B" if r2 else ""}
    return d


def test_boardable_enviado():
    assert ts.is_boardable(t(), NOW)


def test_boardable_abordado1_only_with_pending_route_2():
    assert ts.is_boardable(t("abordado1", r2=True), NOW)
    assert not ts.is_boardable(t("abordado1", r2=True, bol2=True), NOW)
    assert not ts.is_boardable(t("abordado1"), NOW)


def test_not_boardable_expired_cancelled_solicitud_abordado2():
    assert not ts.is_boardable(t(exp="2026-11-07 10:00:00"), NOW)
    assert not ts.is_boardable(t("cancelado"), NOW)
    assert not ts.is_boardable(t("solicitud"), NOW)
    assert not ts.is_boardable(t("abordado2"), NOW)


def test_is_final():
    assert ts.is_final(t("abordado2"))
    assert ts.is_final(t("abordado1"))
    assert ts.is_final(t("cancelado"))
    assert not ts.is_final(t("abordado1", r2=True))
    assert not ts.is_final(t("enviado"))


def test_rank_orders_progress():
    assert ts.rank("enviado") < ts.rank("abordado1") < ts.rank("abordado2")
    assert ts.rank("cancelado") == -1
