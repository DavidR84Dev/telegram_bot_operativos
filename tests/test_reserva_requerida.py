"""Regla del 2026-11-26: AST, Prueba de Ruta, Combustible, Gastos Operativos y Pos-Operacional exigen una reserva
vigente (preoperacional de la jornada, sin liberar con 🔴 Finalizar ni vencida). Mismo error en los cinco: 409 con
``X-Error-Code: sin_reserva_vigente``. El envío sin señal se acepta si la reserva estaba vigente al abrir el formulario.

Exención explícita: **Correcciones Post y Gastos** (``app/correcciones``) NO exige reserva vigente, porque corrige
registros de jornadas ya cerradas; por eso no está en ``FORMS``. Su prueba está en ``tests/test_correcciones.py``
(``test_recibir_guarda_los_datos_fijos_del_original_y_no_exige_reserva``)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

import pytest
from sqlalchemy import select

from app.ast_trabajo import service as ast_service
from app.combustible import service as comb_service
from app.gastos_operativos import service as gastos_service
from app.pos_operacional import service as pos_service
from app.preop import reservas
from app.preop import service as preop_service
from app.preop.models import ReservaHistorial, ReservaVehiculo, utc_now
from app.preop.service import SinReservaVigenteError, TiemposDiferido
from app.prueba_ruta import service as pr_service

from . import test_ast as _ast
from . import test_combustible as _comb
from . import test_gastos_operativos as _gastos
from . import test_pos_operacional as _pos
from . import test_preop as _preop
from . import test_prueba_ruta as _pr
from .test_preop import TG_USER

db = _preop.db
seeded = _preop.seeded

IDENTITY = _comb.IDENTITY


def _empleado(database):
    return preop_service.employee_for_telegram(database, TG_USER)


# Cada formulario: contexto de la Mini App, recepción normal y recepción diferida (sin señal).
FORMS: dict[str, dict[str, Callable[..., Any]]] = {
    "ast": {
        "context": lambda d: ast_service.form_context(d, _empleado(d), TG_USER),
        "submit": lambda d: ast_service.submit(d, _ast.IDENTITY, _ast._payload()),
        "diferido": lambda d, t: ast_service.submit_diferido(d, _ast.IDENTITY, _ast._payload(), t),
    },
    "prueba_ruta": {
        "context": lambda d: pr_service.form_context(d, _empleado(d), TG_USER, _pr.AUTORIZADOS),
        "submit": lambda d: pr_service.submit(d, _pr.IDENTITY, _pr._payload(), _pr.AUTORIZADOS),
        "diferido": lambda d, t: pr_service.submit_diferido(d, _pr.IDENTITY, _pr._payload(), t, _pr.AUTORIZADOS),
    },
    "combustible": {
        "context": lambda d: comb_service.form_context(d, _empleado(d), TG_USER),
        "submit": lambda d: comb_service.submit(d, IDENTITY, _comb._payload()),
        "diferido": lambda d, t: comb_service.submit_diferido(d, IDENTITY, _comb._payload(), t),
    },
    "gastos_operativos": {
        "context": lambda d: gastos_service.form_context(d, _empleado(d), TG_USER),
        "submit": lambda d: gastos_service.submit(d, IDENTITY, _gastos._form(), _gastos.TIPOS),
        "diferido": lambda d, t: gastos_service.submit_diferido(d, IDENTITY, _gastos._form(), t, _gastos.TIPOS),
    },
    "pos_operacional": {
        "context": lambda d: pos_service.form_context(d, _empleado(d), TG_USER),
        "submit": lambda d: pos_service.submit(d, IDENTITY, _pos._form()),
        "diferido": lambda d, t: pos_service.submit_diferido(d, IDENTITY, _pos._form(), t),
    },
}
NOMBRES = list(FORMS)


@pytest.fixture
def base(seeded):
    """BD sembrada con el acuerdo autorizado (Prueba de Ruta) y SIN reserva."""
    _pr._con_acuerdo(seeded)
    return seeded


def _reservar(database) -> None:
    _comb._reservar(database)


def _mover_reserva(database, *, horas: float, fecha_jornada=None) -> None:
    """Hace que la reserva se hubiera creado hace ``horas``."""
    with database.session() as s:
        r = s.scalar(select(ReservaVehiculo))
        r.reservado_at = utc_now() - timedelta(hours=horas)
        if fecha_jornada is not None:
            r.fecha_jornada = fecha_jornada


def _tiempos(apertura_hace: timedelta = timedelta(minutes=30)) -> TiemposDiferido:
    ahora = utc_now()
    return TiemposDiferido(apertura=ahora - apertura_hace, sin_senal_desde=ahora - timedelta(minutes=20), recibido=ahora)


def _sin_reserva(fn: Callable[[], Any]) -> None:
    with pytest.raises(SinReservaVigenteError) as exc:
        fn()
    assert exc.value.status_code == reservas.HTTP_SIN_RESERVA == 409
    assert exc.value.code == reservas.CODIGO_SIN_RESERVA and str(exc.value) == reservas.MSG_SIN_RESERVA


def test_mensaje_acordado():
    assert reservas.MSG_SIN_RESERVA == ("Para usar este formulario primero debes registrar el ✅ Preoperacional de hoy "
                                        "(vehículo reservado).")


@pytest.mark.parametrize("nombre", NOMBRES)
def test_sin_reserva_se_bloquea_el_contexto_y_el_envio(base, nombre):
    _sin_reserva(lambda: FORMS[nombre]["context"](base))
    _sin_reserva(lambda: FORMS[nombre]["submit"](base))


@pytest.mark.parametrize("nombre", NOMBRES)
def test_reserva_vencida_se_bloquea(base, nombre):
    _reservar(base)
    _mover_reserva(base, horas=reservas.RESERVA_HORAS + 1)
    _sin_reserva(lambda: FORMS[nombre]["context"](base))
    _sin_reserva(lambda: FORMS[nombre]["submit"](base))


@pytest.mark.parametrize("nombre", NOMBRES)
def test_reserva_liberada_con_finalizar_se_bloquea(base, nombre):
    _reservar(base)
    reservas.finalizar_jornada(base, TG_USER)
    _sin_reserva(lambda: FORMS[nombre]["context"](base))
    _sin_reserva(lambda: FORMS[nombre]["submit"](base))


@pytest.mark.parametrize("nombre", NOMBRES)
def test_reserva_vigente_de_ayer_se_permite_pasada_la_medianoche(base, nombre):
    """Turno nocturno: la jornada empezó ayer (fecha local) y sigue vigente (< 24 h)."""
    _reservar(base)
    _mover_reserva(base, horas=reservas.RESERVA_HORAS - 2, fecha_jornada=reservas.fecha_jornada() - timedelta(days=1))
    FORMS[nombre]["context"](base)
    FORMS[nombre]["submit"](base)


# ─────────────────────────────────────── envío sin señal ───────────────────────────────────────

@pytest.mark.parametrize("nombre", NOMBRES)
def test_diferido_se_acepta_si_la_reserva_estaba_vigente_al_abrir(base, nombre):
    """Reserva liberada (🔴 Finalizar) mientras el teléfono estaba sin señal: el formulario se abrió con ella vigente."""
    _reservar(base)
    _mover_reserva(base, horas=2)  # reservada antes de abrir el formulario
    tiempos = _tiempos()
    reservas.finalizar_jornada(base, TG_USER)  # liberada DESPUÉS de la apertura
    _sin_reserva(lambda: FORMS[nombre]["submit"](base))  # el envío normal (en línea) sí se rechaza
    FORMS[nombre]["diferido"](base, tiempos)


@pytest.mark.parametrize("nombre", NOMBRES)
def test_diferido_se_rechaza_si_la_reserva_ya_no_cubria_la_apertura(base, nombre):
    _reservar(base)
    _mover_reserva(base, horas=2)
    reservas.finalizar_jornada(base, TG_USER)
    with base.session() as s:  # liberada hace 45 min; el formulario se abrió hace 30 min, ya sin reserva
        h = s.scalar(select(ReservaHistorial))
        h.liberado_at = utc_now() - timedelta(minutes=45)
    _sin_reserva(lambda: FORMS[nombre]["diferido"](base, _tiempos(timedelta(minutes=30))))


@pytest.mark.parametrize("nombre", NOMBRES)
def test_diferido_sin_ninguna_reserva_se_rechaza(base, nombre):
    _sin_reserva(lambda: FORMS[nombre]["diferido"](base, _tiempos()))


def test_diferido_con_reserva_nueva_posterior_usa_la_jornada_de_la_apertura(base):
    """Abrió con la jornada A, finalizó y reservó de nuevo (B) antes de que llegara el envío: rige A, no B."""
    _reservar(base)
    _mover_reserva(base, horas=2)
    tiempos = _tiempos()
    reservas.finalizar_jornada(base, TG_USER)
    with base.session() as s:
        jornada_a = reservas._aware(s.scalar(select(ReservaHistorial)).reservado_at)
    vehiculo, extracto = _comb._ids(base)  # nueva reserva B (otro preoperacional), posterior a la apertura
    preop_service.submit(base, IDENTITY, _preop._payload(vehiculo, extracto, uuid="9" * 8 + "-3c1d-4f7a-9a2b-1c2d3e4f5a6b"))
    with base.session() as s:
        assert reservas.reservado_desde(s.scalar(select(ReservaVehiculo))) > tiempos.apertura
        elegida = preop_service.exigir_reserva(s, TG_USER, tiempos)
        assert reservas.reservado_desde(elegida) == jornada_a and elegida not in s


@pytest.mark.parametrize("nombre", NOMBRES)
def test_diferido_abierto_antes_de_cualquier_reserva_se_rechaza(base, nombre):
    """Formulario abierto sin reserva y preoperacional hecho después: no se acepta (la regla rige al abrir)."""
    tiempos = _tiempos()
    _reservar(base)  # reserva creada DESPUÉS de la apertura
    _sin_reserva(lambda: FORMS[nombre]["diferido"](base, tiempos))


def test_diferido_de_otro_conductor_no_aprovecha_la_reserva_ajena(base):
    _reservar(base)
    with base.session() as s:
        assert reservas.reserva_al_instante(s, TG_USER + 1, utc_now()) is None
        assert reservas.reserva_al_instante(s, TG_USER, utc_now() + timedelta(hours=30)) is None
