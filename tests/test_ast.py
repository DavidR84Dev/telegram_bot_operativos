"""Pruebas del AST (Análisis Seguro de Trabajo): catálogo, esquema, recepción, cola, conector y API."""
from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from app import formatting
from app.ast_trabajo import service
from app.ast_trabajo.catalogo import PREGUNTAS, con_alerta, form_definition
from app.ast_trabajo.router import AstServices
from app.ast_trabajo.router import router as ast_router
from app.ast_trabajo.schemas import AstIn
from app.ast_trabajo.worker import AstSender
from app.preop import etl
from app.preop.models import AnalisisSeguro, Extracto, SyncStatus, utc_now
from app.preop.odoo_writer import OdooPermanentError, OdooTransientError
from app.preop.service import SubmissionError, TelegramIdentity

from . import test_preop as _preop
from .helpers import BOT_TOKEN, make_init_data
from .test_preop import FakeObjects, FakeRest, TG_USER, _empleado, _extracto, _flota, _writer

# Mismas fixtures del preoperacional: BD en memoria con empleado, jefe, vehículo y extracto aprobado.
db = _preop.db
seeded = _preop.seeded

UUID = "1a2b3c4d-3c1d-4f7a-9a2b-1c2d3e4f5a6b"
IDENTITY = TelegramIdentity(TG_USER, "ana", "Ana C")
# Campos del modelo `ast` de Odoo (fields_get en qa2, 2026-11-25).
CAMPOS_ODOO = {"documentos", "descanso", "condiciones", "epp", "peligros", "pausas", "procedimientos",
               "aspectos", "conservacion", "medicamentos"}


def _respuestas(**over: str) -> dict[str, str]:
    base = {p.key: ("no" if p.key == "medicamentos" else "conforme") for p in PREGUNTAS}
    return base | over


def _payload(**over: Any) -> AstIn:
    data: dict[str, Any] = {"uuid": UUID, "respuestas": _respuestas()}
    data.update(over)
    return AstIn.model_validate(data)


# ─────────────────────────────────────── catálogo y esquema ───────────────────────────────────────

def test_catalogo_cubre_las_10_preguntas_de_odoo():
    assert {p.key for p in PREGUNTAS} == CAMPOS_ODOO
    assert all(p.obligatorio for p in PREGUNTAS)
    form = form_definition()
    assert [p["key"] for p in form["preguntas"]][:2] == ["documentos", "descanso"]
    med = next(p for p in form["preguntas"] if p["key"] == "medicamentos")
    assert [o["value"] for o in med["options"]] == ["si", "no"] and med["alerta"] == "si"


def test_alerta_es_no_conforme_o_medicamentos_si():
    assert con_alerta(_respuestas()) == []
    alertas = con_alerta(_respuestas(epp="no conforme", medicamentos="si"))
    assert [p.key for p in alertas] == ["epp", "medicamentos"]


def test_todas_las_preguntas_son_obligatorias():
    respuestas = _respuestas()
    del respuestas["descanso"]
    with pytest.raises(ValidationError, match="Descanso adecuado"):
        _payload(respuestas=respuestas)


@pytest.mark.parametrize("respuestas, error", [
    (_respuestas(epp="no aplica"), "Valor inválido"),
    (_respuestas(medicamentos="conforme"), "Valor inválido"),
    (_respuestas() | {"campo_nuevo": "conforme"}, "desconocidas"),
])
def test_esquema_rechaza_valores_fuera_del_catalogo(respuestas, error):
    with pytest.raises(ValidationError, match=error):
        _payload(respuestas=respuestas)


def test_medicamento_si_exige_nombre_y_no_lo_descarta():
    with pytest.raises(ValidationError, match="nombre del medicamento"):
        _payload(respuestas=_respuestas(medicamentos="si"))
    with pytest.raises(ValidationError, match="nombre del medicamento"):
        _payload(respuestas=_respuestas(medicamentos="si"), nombre_medicamento="  ")
    ok = _payload(respuestas=_respuestas(medicamentos="si"), nombre_medicamento=" Loratadina ")
    assert ok.nombre_medicamento == "Loratadina"
    assert _payload(nombre_medicamento="Loratadina").nombre_medicamento is None


def test_esquema_no_acepta_identidad_en_el_cuerpo():
    with pytest.raises(ValidationError):
        _payload(telegram_user_id=1)


# ───────────────────────────────────────── contexto y recepción ─────────────────────────────────────

def test_contexto_lista_contratos_de_extractos_aprobados(seeded):
    from app.preop.service import employee_for_telegram

    empleado = employee_for_telegram(seeded, TG_USER)
    ctx = service.form_context(seeded, empleado)
    assert ctx["validado"] is True and ctx["empleado"]["responsable"] == "Jefe Uno"
    assert ctx["contratos"] == [{"id_contrato": 55, "nombre": "Contrato Minera"}]
    with seeded.session() as s:
        s.scalar(select(Extracto)).estado = "pending"
    assert service.form_context(seeded, empleado) == {**ctx, "validado": False, "contratos": []}


def test_contratos_de_varios_extractos_sin_repetir(seeded):
    etl.sync_all(seeded, FakeRest({
        "/api/flota": [_flota(), _flota(11, "VAN 02/XYZ987")],
        "/api/empleados": [_empleado(), _empleado(101, pin="999", responsable="", nombre="Jefe Uno")],
        "/api/extractos": [
            _extracto(),
            _extracto(501, vehiculo="VAN 02/XYZ987", contratos_list=[
                {"id_contrato": 55, "nombre": "Contrato Minera"}, {"id_contrato": 77, "nombre": "Colegio"}]),
        ],
    }), [1])
    from app.preop.service import employee_for_telegram

    ctx = service.form_context(seeded, employee_for_telegram(seeded, TG_USER))
    assert sorted(c["id_contrato"] for c in ctx["contratos"]) == [55, 77]
    with pytest.raises(SubmissionError, match="Selecciona el contrato"):
        service.submit(seeded, IDENTITY, _payload())
    ast, _ = service.submit(seeded, IDENTITY, _payload(contrato_odoo_id=77))
    assert ast.contrato_odoo_id == 77


def test_submit_es_idempotente_y_asigna_el_unico_contrato(seeded):
    first, dup1 = service.submit(seeded, IDENTITY, _payload())
    second, dup2 = service.submit(seeded, IDENTITY, _payload())
    assert (dup1, dup2) == (False, True) and first.id == second.id
    assert first.contrato_odoo_id == 55 and first.no_conformes == 0
    assert first.sync_status == SyncStatus.PENDIENTE
    with pytest.raises(SubmissionError) as exc:
        service.submit(seeded, TelegramIdentity(999, None, "Jefe"), _payload())
    assert exc.value.status_code == 409  # uuid de otro usuario


def test_submit_rechaza_empleado_desconocido_y_sin_extracto(seeded):
    with pytest.raises(SubmissionError) as exc:
        service.submit(seeded, TelegramIdentity(1, None, "X"), _payload())
    assert exc.value.status_code == 403
    with seeded.session() as s:
        s.scalar(select(Extracto)).estado = "pending"
    with pytest.raises(SubmissionError, match="extracto aprobado") as exc:
        service.submit(seeded, IDENTITY, _payload())
    assert exc.value.status_code == 403


def test_submit_rechaza_contrato_ajeno(seeded):
    with pytest.raises(SubmissionError, match="no corresponde"):
        service.submit(seeded, IDENTITY, _payload(contrato_odoo_id=999))


def test_submit_cuenta_alertas(seeded):
    ast, _ = service.submit(seeded, IDENTITY, _payload(
        respuestas=_respuestas(peligros="no conforme", medicamentos="si"), nombre_medicamento="Loratadina"))
    assert ast.no_conformes == 2 and ast.nombre_medicamento == "Loratadina"


# ────────────────────────────────────────────── cola ──────────────────────────────────────────────

class FakeWriter:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple] = []

    def create_ast(self, ref, vals, submitted_at, uncertain):
        self.calls.append((ref, vals, uncertain))
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _queue(db, **over) -> int:
    ast, _ = service.submit(db, IDENTITY, _payload(**over))
    assert service.claim_batch(db, 10, 300) == [ast.id]
    return ast.id


def test_envio_exitoso_arma_los_valores_de_odoo(seeded):
    ast_id = _queue(seeded, observaciones="Todo bien")
    writer = FakeWriter([(95, "AST-000009500", False)])
    outcome = service.send_one(seeded, writer, ast_id, 8)
    assert outcome.status == SyncStatus.ENVIADO and outcome.aviso_jefe is None
    ref, vals, uncertain = writer.calls[0]
    assert ref == UUID and uncertain is False
    assert vals["employee_id"] == 100 and vals["contrato_id"] == 55 and vals["immediate_boss_id"] == 101
    assert vals["company_id"] == 1 and vals["observaciones"] == "Todo bien"
    assert {k: vals[k] for k in CAMPOS_ODOO} == _respuestas() and vals["nombre_medicamento"] == ""
    assert "state" not in vals and "plan_accion" not in vals  # los calcula Odoo
    with seeded.session() as s:
        row = s.get(AnalisisSeguro, ast_id)
        assert (row.sync_status, row.odoo_id, row.odoo_name) == (SyncStatus.ENVIADO, 95, "AST-000009500")
    assert service.claim_batch(seeded, 10, 300) == []


def test_alertas_avisan_al_jefe_con_pin(seeded):
    ast_id = _queue(seeded, respuestas=_respuestas(descanso="no conforme", medicamentos="si"),
                    nombre_medicamento="Loratadina")
    writer = FakeWriter([(96, "AST-96", False)])
    outcome = service.send_one(seeded, writer, ast_id, 8)
    assert writer.calls[0][1]["nombre_medicamento"] == "Loratadina"
    aviso = outcome.aviso_jefe
    assert aviso is not None and aviso.telegram_id == 999
    assert aviso.alertas == ("Descanso adecuado", "Consumo de medicamentos") and aviso.contrato == "Contrato Minera"
    text = formatting.ast_aviso_jefe(aviso, outcome.odoo_name)
    assert "Ana Conductora" in text and "AST-96" in text and "Loratadina" in text
    assert "jefe inmediato" in formatting.ast_result(outcome)


def test_sin_pin_del_jefe_no_hay_aviso(seeded):
    etl.sync_all(seeded, FakeRest({
        "/api/flota": [_flota()],
        "/api/empleados": [_empleado(), _empleado(101, pin="", responsable="", nombre="Jefe Uno")],
        "/api/extractos": [_extracto()],
    }), [1])
    ast_id = _queue(seeded, respuestas=_respuestas(epp="no conforme"))
    assert service.send_one(seeded, FakeWriter([(97, "AST-97", False)]), ast_id, 8).aviso_jefe is None


def test_error_transitorio_reintenta_y_luego_falla(seeded):
    ast_id = _queue(seeded)
    assert service.send_one(seeded, FakeWriter([OdooTransientError("timeout")]), ast_id, 2).status == SyncStatus.ERROR
    assert service.claim_batch(seeded, 10, 300) == []  # en backoff
    assert service.claim_batch(seeded, 10, 300, now=utc_now() + timedelta(hours=2)) == [ast_id]
    writer = FakeWriter([OdooTransientError("timeout")])
    assert service.send_one(seeded, writer, ast_id, 2).status == SyncStatus.FALLIDO
    assert writer.calls[0][2] is True  # resultado previo incierto
    assert service.requeue_failed(seeded) == 1
    assert service.claim_batch(seeded, 10, 300) == [ast_id]


def test_error_permanente_falla_de_inmediato(seeded):
    ast_id = _queue(seeded)
    outcome = service.send_one(seeded, FakeWriter([OdooPermanentError("ValidationError: x")]), ast_id, 8)
    assert outcome.status == SyncStatus.FALLIDO and "no pudo registrarse" in formatting.ast_result(outcome)


def test_colas_de_preoperacional_y_ast_son_independientes(seeded):
    from app.preop import service as preop_service

    _queue(seeded)
    assert preop_service.claim_batch(seeded, 10, 300) == []


def test_sender_notifica_estados_finales(seeded):
    service.submit(seeded, IDENTITY, _payload())
    notified = []

    async def notify(outcome):
        notified.append(outcome)

    sender = AstSender(seeded, FakeWriter([(1, "AST-1", False)]), 10, 8, 300, notify=notify)  # type: ignore[arg-type]
    outcomes = asyncio.run(sender.run_once())
    assert [o.status for o in outcomes] == [SyncStatus.ENVIADO] and notified[0].telegram_user_id == TG_USER


# ───────────────────────────────────────── conector XML-RPC ─────────────────────────────────────────

class AstObjects(FakeObjects):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.models: list[tuple[str, str, Any]] = []

    def execute_kw(self, db, uid, pw, model, method, args, kwargs):
        self.models.append((model, method, args))
        return super().execute_kw(db, uid, pw, model, method, args, kwargs)


def test_writer_crea_en_el_modelo_ast_con_external_id_propio():
    objects = AstObjects()
    odoo_id, name, reused = _writer(objects).create_ast(UUID, {"employee_id": 1}, utc_now(), False)
    assert reused is False and objects.created == [{"employee_id": 1}]
    assert ("ast", "create", [{"employee_id": 1}]) in objects.models
    xmlid = next(a for m, meth, a in objects.models if m == "ir.model.data" and meth == "search_read")
    assert ("name", "=", "ast_" + UUID.replace("-", "")) in xmlid[0] and ("model", "=", "ast") in xmlid[0]


def test_writer_ast_tras_timeout_busca_por_empleado():
    objects = AstObjects(natural_hit=321)
    assert _writer(objects).create_ast(UUID, {"employee_id": 1}, utc_now(), True)[0] == 321
    [(_, _, args)] = [m for m in objects.models if m[1] == "search"]
    domain = args[0]
    assert domain[0] == ("employee_id", "=", 1) and domain[1][0] == "create_date" and len(domain) == 2
    assert objects.created == []


# ─────────────────────────────────────────────── API ───────────────────────────────────────────────

@pytest.fixture
def api(seeded):
    app = FastAPI()
    app.include_router(ast_router)
    woke: list[int] = []
    app.state.ast = AstServices(seeded, BOT_TOKEN, 3600, wake_worker=lambda: woke.append(1))
    return TestClient(app), woke


def _headers(user_id=TG_USER):
    return {"X-Telegram-Init-Data": make_init_data(user_id=user_id)}


def test_api_exige_init_data_valido(api):
    client, _ = api
    assert client.get("/api/ast/context").status_code == 401
    assert client.get("/api/ast/form", headers={"X-Telegram-Init-Data": make_init_data(tamper=True)}).status_code == 401
    assert client.post("/api/ast", json={}).status_code == 401
    assert client.post("/api/ast", json={}, headers=_headers()).status_code == 422


def test_api_contexto_y_formulario(api):
    client, _ = api
    assert client.get("/api/ast/context", headers=_headers(user_id=1)).status_code == 403
    ctx = client.get("/api/ast/context", headers=_headers()).json()
    assert ctx["validado"] is True and ctx["contratos"] == [{"id_contrato": 55, "nombre": "Contrato Minera"}]
    assert len(client.get("/api/ast/form", headers=_headers()).json()["preguntas"]) == 10


def test_api_envio_despierta_worker_y_es_idempotente(api):
    client, woke = api
    body = _payload().model_dump()
    first = client.post("/api/ast", json=body, headers=_headers())
    assert first.status_code == 202 and first.json()["sync_status"] == "pendiente" and woke == [1]
    again = client.post("/api/ast", json=body, headers=_headers())
    assert again.json()["duplicado"] is True and woke == [1]
    assert client.get(f"/api/ast/{UUID}", headers=_headers()).json()["id"] == first.json()["id"]
    assert client.get(f"/api/ast/{UUID}", headers=_headers(user_id=1)).status_code == 404


def test_api_envio_sin_extracto_es_403(api, seeded):
    client, _ = api
    with seeded.session() as s:
        s.scalar(select(Extracto)).estado = "pending"
    response = client.post("/api/ast", json=_payload().model_dump(), headers=_headers())
    assert response.status_code == 403


def test_pagina_sin_cache_y_logo_versionado(api):
    client, _ = api
    page = client.get("/ast")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
    assert re.findall(r'src="/static/viacotur-logo\.png\?v=([0-9a-f]{12})"', page.text)
    assert "/api/ast/form" in page.text and "innerHTML" not in page.text
