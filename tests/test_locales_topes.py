"""CSV BD del Panel Admin (locales autorizados), cotejo del gasto con los locales y topes por tipo de gasto."""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import csv_util
from app.admin import envfile
from app.config import Settings
from app.gastos_operativos import cotejo, locales, service
from app.gastos_operativos.catalogo import (
    TopesGastoError,
    clave_tipo,
    form_definition,
    parse_topes,
    texto_topes,
)
from app.gastos_operativos.router import GastosOperativosServices
from app.gastos_operativos.router import router as gastos_router
from app.gastos_operativos.schemas import GastosOperativosIn
from app.preop.db import Database
from app.preop.models import GastoOperativo, GastoOperativoLinea, LocalAutorizado, utc_now
from app.preop.service import SubmissionError

from . import test_admin as _admin
from . import test_gastos_operativos as _go
from .helpers import BOT_TOKEN, antedatar_reservas
from .test_admin import ADMIN_ID, _audit, _h
from .test_gastos_operativos import (
    IDENTITY,
    LAT,
    LON,
    TG_USER,
    TIPOS,
    _body,
    _diferido_body,
    _gasto,
    _headers,
    _recibir,
)

db = _go.db
seeded = _go.seeded
listo = _go.listo
panel = _admin.panel

CATEGORIAS = locales.categorias_de(TIPOS)


def _ahora_mas(**kw):
    return utc_now() + timedelta(**kw)


# ─────────────────────────────────────────── CSV de locales ───────────────────────────────────────────

CSV_OK = ("nombre;nit;categoria;latitud;longitud;ciudad;direccion\n"
          "Restaurante Uno;900;alimentacion;6,2442;-75,5812;Medellín;Cra 80\n"
          "Hotel Dos;;HOSPEDAJE;4.711;-74.0721;Bogotá;\n"
          "Todo Tres;777;;3,45;-76,53;Cali;\n")


def test_csv_valido_normaliza_categoria_sin_tildes_ni_mayusculas_y_acepta_coma_decimal():
    filas = locales.leer_csv(CSV_OK, CATEGORIAS)
    assert filas[0] == locales.FilaLocal("Restaurante Uno", 6.2442, -75.5812, "900", "Alimentación", "Medellín", "Cra 80")
    assert (filas[1].categoria, filas[1].latitud, filas[1].nit) == ("Hospedaje", 4.711, None)
    assert filas[2].categoria is None  # vacía: aplica a todos los tipos


def test_csv_separado_por_comas_con_decimales_entre_comillas():
    filas = locales.leer_csv('nombre,categoria,latitud,longitud\nRest,Alimentación,"6,25","-75,5"\nHotel,,5.5,-74.1\n',
                             CATEGORIAS)
    assert (filas[0].latitud, filas[0].longitud, filas[1].latitud) == (6.25, -75.5, 5.5)
    _, errores = locales.analizar_csv("nombre,latitud,longitud\nRest,6,25,-75\n", CATEGORIAS)
    assert "más columnas" in errores[0].mensaje


def test_csv_con_errores_por_fila():
    csv_malo = ("nombre;categoria;latitud;longitud\n"
                "Buena;Alimentación;6,1;-75,1\n"
                "Sin coordenadas;Alimentación;;\n"
                "Solo latitud;;6;\n"
                "Fuera de rango;;95;-75\n"
                "Categoría rara;Viáticos;6;-75\n"
                ";;;\n"
                "Texto;;abc;1\n")
    validas, errores = locales.analizar_csv(csv_malo, CATEGORIAS)
    assert [f.nombre for _, f in validas] == ["Buena"]
    assert [e.linea for e in errores] == [3, 4, 5, 6, 8]
    assert "obligatorias" in errores[0].mensaje and "obligatorias" in errores[1].mensaje
    assert "fuera de rango" in errores[2].mensaje
    assert "«Viáticos»" in errores[3].mensaje and "Alimentación" in errores[3].mensaje  # lista las válidas
    with pytest.raises(ValueError, match="nombre"):
        locales.analizar_csv("razon;nit\nX;1\n", CATEGORIAS)
    with pytest.raises(ValueError, match="Línea 3"):
        locales.leer_csv("nombre;latitud;longitud\nA;1;1\nB;;\n", CATEGORIAS)


def test_csv_en_windows_1252_se_decodifica():
    datos = "nombre;categoria;latitud;longitud;ciudad\nCafetería Ñandú;Alimentación;6,2;-75,5;Medellín\n".encode("cp1252")
    with pytest.raises(UnicodeDecodeError):
        datos.decode("utf-8")
    filas = locales.leer_csv(csv_util.decodificar(datos), CATEGORIAS)
    assert (filas[0].nombre, filas[0].categoria, filas[0].ciudad) == ("Cafetería Ñandú", "Alimentación", "Medellín")
    assert csv_util.decodificar("﻿a;b".encode("utf-8")) == "a;b"  # BOM de UTF-8


def test_cargar_actualiza_por_nit_o_por_nombre_y_categoria(seeded):
    resumen = locales.cargar(seeded, locales.leer_csv(CSV_OK, CATEGORIAS))
    assert (resumen.nuevos, resumen.actualizados, resumen.desactivados) == (3, 0, 0)
    assert locales.previsualizar(seeded, locales.leer_csv(CSV_OK, CATEGORIAS)) == {
        "nuevos": 0, "actualizados": 3, "desactivados": 0}
    otro = ("nombre;nit;categoria;latitud;longitud\n"
            "Restaurante Uno RENOMBRADO;900;Alimentación;6,3;-75,6\n"   # mismo NIT: se actualiza
            "HOTEL DOS;;Hospedaje;4,8;-74,1\n"                            # sin NIT: nombre + categoría
            "Hotel Dos;;Alimentación;4,9;-74,2\n")                        # otra categoría: es otro local
    r = locales.cargar(seeded, locales.leer_csv(otro, CATEGORIAS))
    assert (r.nuevos, r.actualizados) == (1, 2)
    with seeded.session() as s:
        todos = {loc.nombre: loc for loc in s.query(LocalAutorizado)}
    assert len(todos) == 4 and todos["Restaurante Uno RENOMBRADO"].latitud == 6.3
    assert todos["HOTEL DOS"].latitud == 4.8 and todos["Hotel Dos"].categoria == "Alimentación"


def test_previsualizar_cuenta_repetidas_del_archivo_como_actualizaciones(seeded):
    filas = locales.leer_csv("nombre;nit;latitud;longitud\nA;1;1;1\nA otra vez;1;2;2\nB;;3;3\nB;;4;4\n", CATEGORIAS)
    assert locales.previsualizar(seeded, filas) == {"nuevos": 2, "actualizados": 2, "desactivados": 0}
    assert locales.cargar(seeded, filas).nuevos == 2


# ───────────────────────────────────── endpoints del Panel Admin ─────────────────────────────────────

ENDPOINTS = [
    ("get", "/api/admin/locales"), ("post", "/api/admin/locales/previsualizar"), ("post", "/api/admin/locales/cargar"),
    ("post", "/api/admin/locales/1/activo"), ("get", "/api/admin/gastos-cotejo"),
]


@pytest.mark.parametrize(("metodo", "ruta"), ENDPOINTS)
def test_endpoints_de_locales_exigen_init_data_y_admin(panel, metodo, ruta):
    llamar = getattr(panel.client, metodo)
    assert llamar(ruta).status_code == 401
    assert llamar(ruta, headers={"X-Telegram-Init-Data": "x"}).status_code == 401
    assert llamar(ruta, headers=_h(user_id=TG_USER)).status_code == 403  # firma válida, no es admin


def _locales(db) -> dict[str, LocalAutorizado]:
    with db.session() as s:
        return {loc.nombre: loc for loc in s.query(LocalAutorizado)}


def test_csv_vista_previa_carga_y_auditoria(panel):
    c = panel.client
    p = c.post("/api/admin/locales/previsualizar", json={"csv": CSV_OK}, headers=_h()).json()
    assert (p["validas"], p["nuevos"], p["actualizados"], p["errores"]) == (3, 3, 0, [])
    assert p["muestra"][0]["categoria"] == "Alimentación" and _locales(panel.db) == {}  # la vista previa no escribe
    r = c.post("/api/admin/locales/cargar", json={"csv": CSV_OK}, headers=_h())
    assert r.status_code == 200 and (r.json()["nuevos"], r.json()["actualizados"]) == (3, 0)
    todos = _locales(panel.db)
    assert (todos["Restaurante Uno"].latitud, todos["Restaurante Uno"].categoria) == (6.2442, "Alimentación")
    assert todos["Todo Tres"].categoria is None and todos["Todo Tres"].activo
    [a] = _audit(panel.db)
    assert a.accion == "locales.cargar" and a.telegram_user_id == ADMIN_ID and "nuevos=3" in a.detalle


def test_csv_con_errores_y_categoria_inexistente_por_endpoint(panel):
    c = panel.client
    malo = "nombre;categoria;latitud;longitud\nBueno;Alimentación;6,1;-75,1\nMalo;Viáticos;6;-75\nSin lat;;;\n"
    p = c.post("/api/admin/locales/previsualizar", json={"csv": malo}, headers=_h()).json()
    assert p["validas"] == 1 and [e["linea"] for e in p["errores"]] == [3, 4] and "Viáticos" in p["errores"][0]["mensaje"]
    r = c.post("/api/admin/locales/cargar", json={"csv": malo}, headers=_h()).json()
    assert r["nuevos"] == 1 and r["omitidas_por_error"] == 2 and list(_locales(panel.db)) == ["Bueno"]
    assert c.post("/api/admin/locales/previsualizar", json={"csv": "razon;nit\nX;1\n"}, headers=_h()).status_code == 422
    assert c.post("/api/admin/locales/cargar", json={"csv": "nombre;latitud;longitud\nX;99;1\n"},
                  headers=_h()).status_code == 422


def test_las_categorias_salen_de_los_tipos_del_env(panel):
    panel.env.write_text(panel.env.read_text(encoding="utf-8") + "GASTOS_OPERATIVOS_TIPOS=Viáticos:3;Otros:6\n",
                         encoding="utf-8")
    csv = "nombre;categoria;latitud;longitud\nA;viaticos;6;-75\nB;Alimentación;6;-75\n"
    p = panel.client.post("/api/admin/locales/previsualizar", json={"csv": csv}, headers=_h()).json()
    assert p["validas"] == 1 and p["muestra"][0]["categoria"] == "Viáticos" and len(p["errores"]) == 1
    assert panel.client.get("/api/admin/locales", headers=_h()).json()["categorias"] == ["Viáticos", "Otros"]


def test_reemplazar_desactiva_los_que_no_vienen_y_con_errores_se_rechaza(panel):
    c = panel.client
    c.post("/api/admin/locales/cargar", json={"csv": CSV_OK}, headers=_h())
    nuevo = "nombre;nit;latitud;longitud\nRestaurante Uno;900;6;-75\nNuevo Cuatro;444;5;-74\n"
    p = c.post("/api/admin/locales/previsualizar", json={"csv": nuevo, "reemplazar": True}, headers=_h()).json()
    assert (p["nuevos"], p["actualizados"], p["desactivados"]) == (1, 1, 2)
    r = c.post("/api/admin/locales/cargar", json={"csv": nuevo, "reemplazar": True}, headers=_h()).json()
    assert r["desactivados"] == 2
    todos = _locales(panel.db)
    assert todos["Restaurante Uno"].activo and todos["Nuevo Cuatro"].activo
    assert not todos["Hotel Dos"].activo and not todos["Todo Tres"].activo
    malo = "nombre;latitud;longitud\nRestaurante Uno;99;1\nNuevo Cuatro;5;-74\n"
    pm = c.post("/api/admin/locales/previsualizar", json={"csv": malo, "reemplazar": True}, headers=_h()).json()
    assert pm["bloquea_reemplazo"] is True
    assert c.post("/api/admin/locales/cargar", json={"csv": malo, "reemplazar": True}, headers=_h()).status_code == 422
    assert _locales(panel.db)["Nuevo Cuatro"].activo


def test_lista_busqueda_y_activacion_de_locales(panel):
    c = panel.client
    c.post("/api/admin/locales/cargar", json={"csv": CSV_OK}, headers=_h())
    d = c.get("/api/admin/locales", headers=_h()).json()
    assert d["total"] == 3 and d["kpis"] == {"activos": 3} and "Alimentación" in d["categorias"]
    uno = next(s for s in d["items"] if s["nombre"] == "Restaurante Uno")
    assert uno["latitud"] == 6.2442 and uno["categoria"] == "Alimentación" and uno["activo"] is True

    def nombres(**params):
        return [s["nombre"] for s in c.get("/api/admin/locales", params=params, headers=_h()).json()["items"]]

    assert nombres(q="bogot") == ["Hotel Dos"] and nombres(q="hospedaje") == ["Hotel Dos"]
    assert nombres(q="restaurante") == ["Restaurante Uno"]
    assert c.post(f"/api/admin/locales/{uno['id']}/activo", json={"activo": False}, headers=_h()).status_code == 200
    assert nombres(estado="inactiva") == ["Restaurante Uno"]
    assert c.get("/api/admin/locales", params={"estado": "x"}, headers=_h()).status_code == 422
    assert c.post(f"/api/admin/locales/{uno['id']}/activo", json={"activo": True}, headers=_h()).status_code == 200
    assert c.post("/api/admin/locales/9999/activo", json={"activo": True}, headers=_h()).status_code == 404
    assert [a.accion for a in _audit(panel.db)] == ["locales.cargar", "local.desactivar", "local.activar"]


def test_la_pagina_renombra_la_pestana_a_csv_bd_con_sus_dos_secciones(panel):
    page = panel.client.get("/admin").text
    assert "🗄️ CSV BD" in page and "⛽ Estaciones" not in page
    for marca in ("a) Estaciones de combustible", "b) Restaurantes y locales autorizados", "/api/admin/locales",
                  "/api/admin/gastos-cotejo", "locales_autorizados_ejemplo.csv", "GASTOS_OPERATIVOS_TOPES"):
        assert marca in page


# ───────────────────────────────────────── cotejo con los locales ─────────────────────────────────────────

def _cargar_locales(db, filas: list[tuple]) -> None:
    """(nombre, categoria, lat, lon[, activo])"""
    locales.cargar(db, [locales.FilaLocal(n, lat, lon, None, cat) for n, cat, lat, lon, *_ in filas])
    for n, _cat, _lat, _lon, *activo in filas:
        if activo and not activo[0]:
            with db.session() as s:
                s.query(LocalAutorizado).filter_by(nombre=n).one().activo = False


def _lineas(db, gid: int) -> list[GastoOperativoLinea]:
    with db.session() as s:
        return list(s.query(GastoOperativoLinea).filter_by(gasto_id=gid).order_by(GastoOperativoLinea.numero))


def _con_cotejo(db) -> int:
    gid = _recibir(db, gastos=[_gasto("Alimentación"), _gasto("Hospedaje", 90000), _gasto("Peajes", 8500)])
    assert service.asignar_ubicacion(db, TG_USER, LAT, LON, _ahora_mas(seconds=5)) is not None
    return gid


def test_cotejo_elige_el_local_mas_cercano_de_su_categoria(listo):
    _cargar_locales(listo, [
        ("Rest lejos", "Alimentación", 6.30, -75.56), ("Rest cerca", "Alimentación", 6.2501, -75.5601),
        ("Hotel cerca", "Hospedaje", 6.2505, -75.56), ("Hotel lejos", "Hospedaje", 6.40, -75.56),
        ("Rest inactivo", "Alimentación", 6.25, -75.56, False),  # el más cercano, pero inactivo: no cuenta
    ])
    lin = _lineas(listo, _con_cotejo(listo))
    assert [(ln.local_nombre, ln.local_cotejado_at is not None) for ln in lin] == [
        ("Rest cerca", True), ("Hotel cerca", True), (None, True)]  # Peajes: ningún local de su categoría
    assert 10 < lin[0].distancia_local_m < 20 and 50 < lin[1].distancia_local_m < 60
    assert lin[0].local_autorizado_id is not None and lin[2].local_autorizado_id is None
    assert lin[2].distancia_local_m is None


def test_cotejo_usa_la_categoria_vacia_como_comodin(listo):
    _cargar_locales(listo, [("Rest Alim", "Alimentación", 6.2501, -75.5601), ("Todo", None, 6.25005, -75.56),
                            ("Hotel", "Hospedaje", 6.2505, -75.56)])
    lin = _lineas(listo, _con_cotejo(listo))
    # El comodín (≈5 m) gana a su propia categoría (≈15 m) y vale para Peajes, que no tiene locales propios.
    assert [ln.local_nombre for ln in lin] == ["Todo", "Todo", "Todo"]


def test_cotejo_sin_locales_deja_null_y_marca_cotejada(listo):
    lin = _lineas(listo, _con_cotejo(listo))
    assert all(ln.local_autorizado_id is None and ln.local_nombre is None and ln.distancia_local_m is None
               and ln.local_cotejado_at is not None for ln in lin)


def test_cotejo_no_se_repite_ni_corre_sin_ubicacion(listo):
    _cargar_locales(listo, [("Rest", "Alimentación", 6.2501, -75.5601)])
    gid = _recibir(listo)
    assert cotejo.cotejar_envio(listo, gid) == 0  # sin ubicación todavía
    assert all(ln.local_cotejado_at is None for ln in _lineas(listo, gid))
    service.asignar_ubicacion(listo, TG_USER, LAT, LON, _ahora_mas(seconds=5))
    antes = _lineas(listo, gid)[0].local_cotejado_at
    assert cotejo.cotejar_envio(listo, gid) == 0 and _lineas(listo, gid)[0].local_cotejado_at == antes


def test_un_fallo_del_cotejo_no_frena_la_ubicacion_ni_el_envio(listo, monkeypatch, caplog):
    gid = _recibir(listo)

    def boom(*_a, **_k):
        raise RuntimeError("BD del cotejo caída")

    monkeypatch.setattr(cotejo, "cotejar_envio", boom)
    res = service.asignar_ubicacion_mas_antigua(listo, TG_USER, LAT, LON, _ahora_mas(seconds=5))
    assert res is not None and res.combustible_id == gid and res.origen == "gastos"
    assert "no se pudo cotejar" in caplog.text and "BD del cotejo caída" in caplog.text
    assert service.claim_batch(listo, 10, 300) == [gid]  # el envío sigue su camino a Odoo
    rest = _go.FakeRest()
    out = service.send_one(listo, _go.FakeBuscador(), gid, 8, rest)  # type: ignore[arg-type]
    assert out.status.value == "enviado" and len(rest.posts) == 1
    assert _lineas(listo, gid)[0].local_cotejado_at is None
    assert not any("local" in k for k in rest.posts[0][1])  # el cotejo no va a Odoo


def test_gastos_cotejo_endpoint_filtra_por_fecha_y_categoria(panel, listo):
    # ``panel`` y ``listo`` comparten la misma BD de la prueba.
    _cargar_locales(listo, [("Rest cerca", "Alimentación", 6.2501, -75.5601)])
    _con_cotejo(listo)
    c = panel.client
    d = c.get("/api/admin/gastos-cotejo", headers=_h()).json()
    assert d["total"] == 3 and {i["tipo"] for i in d["items"]} == {"Alimentación", "Hospedaje", "Peajes"}
    alim = next(i for i in d["items"] if i["tipo"] == "Alimentación")
    assert alim["local"] == "Rest cerca" and alim["cotejado"] is True and 10 < alim["distancia_m"] < 20
    assert (alim["latitud"], alim["longitud"]) == (LAT, LON) and alim["valor"] == 65000.0
    assert next(i for i in d["items"] if i["tipo"] == "Peajes")["local"] is None
    solo = c.get("/api/admin/gastos-cotejo", params={"categoria": "alimentación"}, headers=_h()).json()
    assert [i["tipo"] for i in solo["items"]] == ["Alimentación"]
    hoy = _go._hoy().isoformat()
    assert c.get("/api/admin/gastos-cotejo", params={"desde": hoy, "hasta": hoy}, headers=_h()).json()["total"] == 3
    assert c.get("/api/admin/gastos-cotejo", params={"desde": "2999-01-01"}, headers=_h()).json()["total"] == 0
    assert c.get("/api/admin/gastos-cotejo", params={"desde": "ayer"}, headers=_h()).status_code == 422


def test_las_columnas_nuevas_se_agregan_a_una_tabla_existente(tmp_path):
    ruta = tmp_path / "vieja.db"
    with create_engine(f"sqlite:///{ruta}").begin() as conn:
        conn.execute(text("CREATE TABLE gastos_operativos_lineas (id INTEGER PRIMARY KEY)"))
    db = Database(f"sqlite:///{ruta}")
    with db.engine.connect() as conn:
        columnas = {r[1] for r in conn.execute(text("PRAGMA table_info(gastos_operativos_lineas)"))}
    assert {"local_autorizado_id", "local_nombre", "distancia_local_m", "local_cotejado_at"} <= columnas


# ───────────────────────────────────────────── topes por tipo ─────────────────────────────────────────────

def test_clave_tipo_ignora_tildes_mayusculas_y_espacios():
    assert clave_tipo("  ALIMENTACIÓN ") == clave_tipo("alimentacion") == "alimentacion"


def test_parse_topes_normaliza_y_vacio_es_sin_topes():
    assert parse_topes("", TIPOS) == {} and parse_topes(None, TIPOS) == {} and parse_topes(" ; ", TIPOS) == {}
    topes = parse_topes(" alimentacion : 50000 ; HOSPEDAJE:150000;", TIPOS)
    assert topes == {"Alimentación": 50000, "Hospedaje": 150000}  # con el nombre canónico de los tipos
    assert texto_topes(topes) == "Alimentación:50000;Hospedaje:150000"
    assert parse_topes("Cualquiera:5") == {"Cualquiera": 5}  # sin tipos solo se valida el formato


@pytest.mark.parametrize("texto, mensaje", [
    ("Alimentación", "formato Nombre:valor"), (":5000", "formato Nombre:valor"),
    ("Alimentación:abc", "entero en COP"), ("Alimentación:0", "mayor que cero"), ("Alimentación:-5", "entero en COP"),
    ("Alimentación:50.000", "entero en COP"), ("Alimentación:5000,5", "entero en COP"),
    ("Viáticos:5000", "no existe en GASTOS_OPERATIVOS_TIPOS"),
    ("Alimentación:1;alimentacion:2", "repetido"),
])
def test_parse_topes_errores_con_mensaje_claro(texto, mensaje):
    with pytest.raises(TopesGastoError) as exc:
        parse_topes(texto, TIPOS)
    assert mensaje in str(exc.value) and "GASTOS_OPERATIVOS_TOPES" in str(exc.value)


def test_settings_por_defecto_sin_topes():
    assert Settings.model_fields["gastos_operativos_topes"].default == ""
    assert parse_topes(Settings.model_fields["gastos_operativos_topes"].default, TIPOS) == {}


def test_form_definition_lleva_el_tope_de_cada_tipo():
    form = form_definition(TIPOS, {"Alimentación": 50000})
    assert form["tipos"][0] == {"value": "Alimentación", "label": "Alimentación", "tope": 50000}
    assert form["tipos"][1]["tope"] is None and "product_id" not in str(form)
    assert all(t["tope"] is None for t in form_definition(TIPOS)["tipos"])


def _form_in(gastos: list[dict]) -> GastosOperativosIn:
    return _go._form(gastos)


def test_servidor_rechaza_con_un_mensaje_por_gasto_y_acepta_el_tope_exacto(listo):
    topes = {"Alimentación": 50000, "Hospedaje": 150000}
    ok = service.submit(listo, IDENTITY, _form_in([_gasto("Alimentación", 50000), _gasto("Hospedaje", 150000),
                                                    _gasto("Peajes", 9_000_000)]), TIPOS, topes=topes)
    assert ok[1] is False  # en el tope exacto pasa; un tipo sin tope no se limita
    malo = _go._form([_gasto("ALIMENTACIÓN", 50001), _gasto("Peajes", 1), _gasto("Hospedaje", 200000)],
                     uuid="8b1b2c3d-3c1d-4f7a-9a2b-1c2d3e4f5a6b")
    with pytest.raises(service.TopeExcedidoError) as exc:
        service.submit(listo, IDENTITY, malo, TIPOS, topes=topes)
    msg = str(exc.value)
    assert exc.value.status_code == 422 and isinstance(exc.value, SubmissionError)
    assert "Gasto #1 (Alimentación): $50.001 supera el máximo de $50.000" in msg
    assert "Gasto #3 (Hospedaje): $200.000 supera el máximo de $150.000" in msg and "Gasto #2" not in msg
    with listo.session() as s:
        assert s.query(GastoOperativo).count() == 1  # solo el aceptado; lo rechazado no deja nada


def _api_con_topes(db, topes, avisos: list | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(gastos_router)

    async def on_rejected(user_id, mensaje):
        avisos.append((user_id, mensaje))

    app.state.gastos_operativos = GastosOperativosServices(
        db, BOT_TOKEN, 3600, tipos=TIPOS, topes=topes, wake_worker=lambda: None,
        on_rejected=on_rejected if avisos is not None else None)
    return TestClient(app)


def test_api_form_trae_el_tope_y_el_post_responde_422(listo):
    seeded = listo
    api = _api_con_topes(seeded, {"Alimentación": 50000})
    form = api.get("/api/gastos-operativos/form", headers=_headers()).json()
    assert [(t["value"], t["tope"]) for t in form["tipos"]][:2] == [("Alimentación", 50000), ("Hospedaje", None)]
    r = api.post("/api/gastos-operativos", json=_body(gastos=[_gasto(valor=50001)]), headers=_headers())
    assert r.status_code == 422 and "supera el máximo de $50.000" in r.json()["detail"]
    assert api.post("/api/gastos-operativos", json=_body(gastos=[_gasto(valor=50000)]),
                    headers=_headers()).status_code == 202


def test_envio_diferido_se_valida_con_el_tope_vigente_al_recibir_y_se_avisa_por_el_chat(listo):
    seeded = listo
    antedatar_reservas(seeded)  # la reserva ya existía al abrir el formulario
    avisos: list = []
    api = _api_con_topes(seeded, {"Alimentación": 50000}, avisos)
    body = _diferido_body(abierto_hace=timedelta(minutes=80), sin_senal_hace=timedelta(minutes=40),
                          gastos=[_gasto(valor=65000)])  # el tope bajó mientras el teléfono estaba sin señal
    headers = {"X-Telegram-Init-Data": body["diferido"]["init_data_original"]}
    r = api.post("/api/gastos-operativos", json=body, headers=headers)
    assert r.status_code == 422 and "$65.000 supera el máximo de $50.000" in r.json()["detail"]
    assert len(avisos) == 1 and avisos[0][0] == TG_USER and "Gasto #1" in avisos[0][1]
    with seeded.session() as s:
        assert s.query(GastoOperativo).count() == 0
    # Un envío normal rechazado no avisa por el chat (el conductor está viendo el formulario).
    r = api.post("/api/gastos-operativos", json=_body(gastos=[_gasto(valor=65000)]), headers=_headers())
    assert r.status_code == 422 and len(avisos) == 1
    # Si el tope sigue vigente y el valor lo respeta, el diferido entra normal.
    ok = _diferido_body(abierto_hace=timedelta(minutes=80), sin_senal_hace=timedelta(minutes=40),
                        gastos=[_gasto(valor=45000)])
    assert api.post("/api/gastos-operativos", json=ok,
                    headers={"X-Telegram-Init-Data": ok["diferido"]["init_data_original"]}).status_code == 202


def test_fallo_al_avisar_del_rechazo_no_cambia_la_respuesta(seeded):
    app = FastAPI()
    app.include_router(gastos_router)

    async def boom(*_a):
        raise RuntimeError("telegram caído")

    app.state.gastos_operativos = GastosOperativosServices(seeded, BOT_TOKEN, 3600, tipos=TIPOS,
                                                           topes={"Alimentación": 1}, on_rejected=boom)
    body = _diferido_body(abierto_hace=timedelta(minutes=80), sin_senal_hace=timedelta(minutes=40))
    r = TestClient(app).post("/api/gastos-operativos", json=body,
                             headers={"X-Telegram-Init-Data": body["diferido"]["init_data_original"]})
    assert r.status_code == 422


def test_pagina_de_gastos_muestra_el_maximo_y_bloquea_el_envio(seeded):
    page = _api_con_topes(seeded, {}).get("/gastos-operativos").text
    for marca in ("Máximo ${pesos(tope)}", "excedeTope", "topeDe", "supera el máximo de"):
        assert marca in page


def test_mensaje_de_chat_del_rechazo_por_tope():
    from app import formatting

    texto = formatting.gastos_rechazado_por_tope("Gasto #1 (Alimentación): $65.000 supera el máximo de $50.000 por gasto")
    assert "no fueron aceptados" in texto and "$65.000" in texto and "diligéncialos de nuevo" in texto


# ───────────────────────────────────── parámetro del Panel Admin (.env) ─────────────────────────────────────

def test_parametro_de_topes_en_la_lista_blanca_y_su_validacion():
    assert "GASTOS_OPERATIVOS_TOPES" in envfile.PARAMETROS and not envfile.PARAMETROS["GASTOS_OPERATIVOS_TOPES"].secreto
    assert envfile.validar("GASTOS_OPERATIVOS_TOPES", " Alimentación : 50000 ; Hospedaje:150000 ") == \
        "Alimentación:50000;Hospedaje:150000"
    assert envfile.validar("GASTOS_OPERATIVOS_TOPES", "") == ""  # vacío = sin topes
    for malo in ("Alimentación:abc", "Alimentación:0", "Alimentación", "Alimentación:5;'Otros':6", "A:5;a:6"):
        with pytest.raises(envfile.ParametroError):
            envfile.validar("GASTOS_OPERATIVOS_TOPES", malo)


def _put(panel, cambios):
    return panel.client.put("/api/admin/parametros", json={"cambios": cambios}, headers=_h())


def test_put_topes_los_cruza_con_los_tipos_y_se_guarda_normalizado(panel):
    antes = panel.env.read_text(encoding="utf-8")
    r = _put(panel, {"GASTOS_OPERATIVOS_TOPES": "alimentacion:50000;Hospedaje:150000"})
    assert r.status_code == 200 and r.json()["cambiados"] == ["GASTOS_OPERATIVOS_TOPES"]
    assert envfile.valores_actuales(panel.env)["GASTOS_OPERATIVOS_TOPES"] == "Alimentación:50000;Hospedaje:150000"
    items = {i["clave"]: i for i in panel.client.get("/api/admin/parametros", headers=_h()).json()["items"]}
    assert items["GASTOS_OPERATIVOS_TOPES"]["valor"] == "Alimentación:50000;Hospedaje:150000"
    # Un tipo que no existe se rechaza y no se escribe nada más.
    guardado = panel.env.read_text(encoding="utf-8")
    assert _put(panel, {"GASTOS_OPERATIVOS_TOPES": "Viáticos:5000"}).status_code == 400
    assert panel.env.read_text(encoding="utf-8") == guardado and guardado != antes
    # Quitar un tipo que tiene tope también se rechaza; quitar el tope primero lo permite.
    assert _put(panel, {"GASTOS_OPERATIVOS_TIPOS": "Peajes:6;Otros:6"}).status_code == 400
    assert _put(panel, {"GASTOS_OPERATIVOS_TIPOS": "Peajes:6;Otros:6", "GASTOS_OPERATIVOS_TOPES": ""}).status_code == 200
    assert _put(panel, {"GASTOS_OPERATIVOS_TOPES": "Peajes:20000"}).status_code == 200  # contra los tipos nuevos
    assert [a.accion for a in _audit(panel.db)].count("parametro.cambiar") == 4


def test_env_example_documenta_los_topes_vacios():
    from pathlib import Path

    ejemplo = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert "\nGASTOS_OPERATIVOS_TOPES=\n" in ejemplo


def test_el_arranque_rechaza_topes_invalidos_con_mensaje_claro(monkeypatch):
    from app import main

    for texto, mensaje in (("Viáticos:5000", "no existe en GASTOS_OPERATIVOS_TIPOS"), ("Alimentación:0", "mayor que cero")):
        monkeypatch.setattr(main.settings, "gastos_operativos_topes", texto)
        with pytest.raises(RuntimeError, match="Configuración inválida: GASTOS_OPERATIVOS_TOPES") as exc:
            main._start_gastos_operativos(None, None, None, None)  # type: ignore[arg-type]
        assert mensaje in str(exc.value)
