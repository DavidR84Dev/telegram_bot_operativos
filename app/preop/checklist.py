"""Catálogo de ítems del preoperacional (fuente única de verdad).

Cada ítem usa como ``key`` el **nombre técnico del campo en Odoo** (modelo
``preoperacional``, módulos ``preoperacional`` + ``preoperacional_inherit``),
de modo que el mapeo hacia XML-RPC es directo. El mismo catálogo:

* valida el payload de la Mini App (``schemas.PreoperacionalIn``),
* se publica en ``GET /api/preop/form`` para que la Mini App dibuje el formulario,
* decide qué campos se envían a Odoo (``odoo_writer.build_odoo_values``).

Si Odoo agrega un campo nuevo, basta con añadirlo aquí; mientras tanto puede
llegar en ``extra`` (JSONB) sin romper nada.
"""
from __future__ import annotations

from dataclasses import dataclass

CONFORME = "conforme"
NO_CONFORME = "no conforme"
NO_APLICA = "no aplica"

OPC_BASICA: tuple[str, ...] = (CONFORME, NO_CONFORME)
OPC_CON_NA: tuple[str, ...] = (CONFORME, NO_CONFORME, NO_APLICA)

# Tipos de control, como el formulario de referencia (@viacoturform_bot):
# * CASILLA: marcada = "conforme"; sin marcar = "no conforme" (así se envía a Odoo).
# * RADIO: el conductor elige entre las opciones; sin elegir no se envía (queda vacío).
CASILLA = "casilla"
RADIO = "radio"


@dataclass(frozen=True)
class ChecklistItem:
    key: str
    tab: str
    section: str
    label: str
    # Qué hace que el ítem sea "no conforme" (tomado del ``string`` del campo en Odoo).
    criterio: str = ""
    # Solo para RADIO; una CASILLA siempre resulta en conforme / no conforme.
    options: tuple[str, ...] = OPC_BASICA
    control: str = CASILLA


@dataclass(frozen=True)
class MeasureItem:
    key: str
    label: str
    unit: str
    max_value: float


@dataclass(frozen=True)
class Llanta:
    """Una posición de llanta: su campo de labrado (mm) y de presión (psi) en Odoo."""

    codigo: str
    nombre: str
    posicion: str
    labrado: str
    presion: str
    interna: bool = False


# Pestañas del paso 2, en el orden del formulario de referencia (@viacoturform_bot).
TAB_DOCUMENTOS = "Documentos"
TAB_LLANTAS = "Rines/Llantas"
TAB_EQUIPO = "Equipo"
TAB_VEHICULO = "Vehículo"
TABS: tuple[str, ...] = (TAB_DOCUMENTOS, TAB_LLANTAS, TAB_EQUIPO, TAB_VEHICULO)

DOCUMENTOS = "Documentos del vehículo"
LLANTAS = "Rines y llantas"
SEGURIDAD = "Equipo de prevención y seguridad"
EXTINTORES = "Extintores"
CABINA = "Chasis y cabina"
NIVELES = "Niveles, sistemas de dirección, motor y frenos"
ELECTRICO = "Sistema eléctrico"

_DOC_INEXISTENTE = "Documento inexistente"
_DOC_VENCIDO = "Documento vencido o inexistente"
_SIN_DANADO = "Sin existencia, dañado"
_FALLAS = "Con fallas, dañado"
_NIVEL_BAJO = "Nivel bajo"


def _items(tab: str, section: str, *rows: tuple) -> tuple[ChecklistItem, ...]:
    """Filas ``(key, label, criterio="", opciones=None)``: sin opciones es CASILLA; con opciones, RADIO."""
    items = []
    for key, label, *rest in rows:
        criterio = rest[0] if rest else ""
        options = rest[1] if len(rest) > 1 else None
        items.append(ChecklistItem(key, tab, section, label, criterio, options or OPC_BASICA,
                                   RADIO if options else CASILLA))
    return tuple(items)


CHECKLIST: tuple[ChecklistItem, ...] = (
    *_items(TAB_DOCUMENTOS, DOCUMENTOS,
        ("licencia_conduccion", "Licencia de conducción", _DOC_INEXISTENTE),
        ("licencia_transito", "Licencia de tránsito", _DOC_INEXISTENTE),
        ("soat", "SOAT", _DOC_VENCIDO),
        ("rtm", "Revisión técnico-mecánica", _DOC_VENCIDO),
        ("extracto", "Extracto de contrato", _DOC_VENCIDO),
        ("tarjeta_operacion", "Tarjeta de operación", _DOC_VENCIDO),
        ("cedula", "Cédula de ciudadanía"),
    ),
    *_items(TAB_LLANTAS, LLANTAS,
        ("rin_1", "Estado de los rines",
         "Falta de una o más tuercas, espárragos, tornillos o pernos; deformaciones o fisuras en los rines"),
        ("llanta_v", "Estado de las llantas",
         "Despegue o rotura en las bandas laterales; protuberancias, deformaciones, despegue "
         "o rotura en la banda de rodamiento"),
    ),
    *_items(TAB_EQUIPO, SEGURIDAD,
        ("conos", "Conos", "Sin existencia, dañado, solo una unidad"),
        ("cunas", "Cuñas", "Sin existencia, dañado, solo una unidad"),
        ("botiquin", "Botiquín de primeros auxilios", _SIN_DANADO),
        ("gato", "Gato", _SIN_DANADO),
        ("linterna", "Linterna", "Sin existencia, sin baterías o baterías en mal estado"),
        ("herramientas", "Herramientas básicas (alicate, destornilladores, llave de expansión y llaves fijas)",
         _SIN_DANADO),
        ("chaleco_reflectivo", "Chaleco reflectivo", _SIN_DANADO, OPC_BASICA),
        ("pico_pala", "Pico y pala", _SIN_DANADO, OPC_CON_NA),
        ("eslingas", "Eslingas", "Sin existencia, en mal estado", OPC_CON_NA),
        ("kit_derrames", "Kit control derrames químicos", _SIN_DANADO, OPC_BASICA),
        ("cables_de_arranque", "Cables de arranque", _SIN_DANADO, OPC_CON_NA),
        ("camaras", "Cámaras del vehículo (delantera y trasera)", "", OPC_CON_NA),
        ("barra", "Estado de la barra antivuelco", "Dañada, con fisuras o defectos", OPC_CON_NA),
        ("cruceta", "Cruceta", _SIN_DANADO),
        ("profundimetro", "Profundímetro"),
        ("medidor_aire", "Medidor de aire"),
        ("testigos", "Testigos del tablero", "Algún testigo de advertencia encendido"),
    ),
    *_items(TAB_VEHICULO, EXTINTORES,
        ("extintor_1", "Extintor N.º 1"),
        ("extintor_2", "Extintor N.º 2", "", OPC_CON_NA),
    ),
    *_items(TAB_VEHICULO, CABINA,
        ("orden_aseo", "Orden y aseo dentro y fuera del vehículo"),
        ("cinturones", "Cinturones de seguridad", _SIN_DANADO),
        ("suspension", "Suspensión – dirección", _FALLAS),
        ("espejos_retrovisores", "Espejos y retrovisores", _SIN_DANADO),
        ("vidrios", "Vidrios", _SIN_DANADO),
        ("espaldar", "Estado de inclinación del espaldar", "Dañado"),
        ("cojineria", "Estado de la cojinería del asiento", _SIN_DANADO),
        ("asiento", "Estado de fijación del asiento", "Dañado"),
    ),
    *_items(TAB_VEHICULO, NIVELES,
        ("nivel_frenos", "Nivel de líquido de frenos", _NIVEL_BAJO),
        ("nivel_aceite", "Nivel de aceite hidráulico", _NIVEL_BAJO),
        ("nivel_combustible", "Nivel de combustible", _NIVEL_BAJO),
        ("nivel_refrigerante", "Nivel de líquido refrigerante", _NIVEL_BAJO),
        ("nivel_aceite_motor", "Nivel de aceite de motor", _NIVEL_BAJO),
        ("frenos", "Frenos", _FALLAS),
        ("freno_mano", "Freno de mano", "Tensión baja"),
        ("correas", "Correas", _FALLAS),
    ),
    *_items(TAB_VEHICULO, ELECTRICO,
        ("switche", "Switche de encendido o starter", _FALLAS),
        ("pito", "Pito", _FALLAS),
        ("pito_reversa", "Pito de reversa", _SIN_DANADO),
        ("bateria", "Batería", _FALLAS),
        ("dispositivo_de_velocidad", "Dispositivo de velocidad", _SIN_DANADO),
        ("cierre_de_puertas", "Cierre de puertas", _FALLAS),
        ("aire_acondicionado", "Aire acondicionado", _FALLAS),
        ("velocimetro", "Velocímetro", _FALLAS),
        ("limpia_parabrisas", "Limpiaparabrisas", _FALLAS),
        ("luces_delanteras", "Luces delanteras, traseras, reversa y parada (stop)", _FALLAS),
        ("luces_direccionales", "Luces direccionales", _FALLAS),
    ),
)

# Llantas 1–4 siempre; 5 y 6 solo si el vehículo tiene llantas internas. En Odoo la
# repuesto es ``llanta_7`` (labrado) y ``llanta_rep_pre`` (presión).
LLANTAS_POSICIONES: tuple[Llanta, ...] = (
    Llanta("1", "Llanta N.º 1", "Delantera derecha", "llanta_1", "llanta_1_pre"),
    Llanta("2", "Llanta N.º 2", "Delantera izquierda", "llanta_2", "llanta_2_pre"),
    Llanta("3", "Llanta N.º 3", "Trasera derecha", "llanta_3", "llanta_3_pre"),
    Llanta("4", "Llanta N.º 4", "Trasera izquierda", "llanta_4", "llanta_4_pre"),
    Llanta("5", "Llanta N.º 5", "Interna derecha", "llanta_5", "llanta_5_pre", interna=True),
    Llanta("6", "Llanta N.º 6", "Interna izquierda", "llanta_6", "llanta_6_pre", interna=True),
    Llanta("R", "Llanta de repuesto", "Repuesto", "llanta_7", "llanta_rep_pre"),
)

LABRADO_MAX_MM = 30
PRESION_MAX_PSI = 200

LLANTAS_MEDIDAS: tuple[MeasureItem, ...] = (
    *(MeasureItem(ll.labrado, f"Labrado {ll.nombre.lower()}", "mm", LABRADO_MAX_MM) for ll in LLANTAS_POSICIONES),
    *(MeasureItem(ll.presion, f"Presión {ll.nombre.lower()}", "psi", PRESION_MAX_PSI) for ll in LLANTAS_POSICIONES),
)

CHECKLIST_BY_KEY: dict[str, ChecklistItem] = {item.key: item for item in CHECKLIST}
MEASURES_BY_KEY: dict[str, MeasureItem] = {item.key: item for item in LLANTAS_MEDIDAS}

# Mismos umbrales que preoperacional_inherit.create(): debajo, Odoo marca "no conforme".
LABRADO_MINIMO = {"llanta_5": 2.0, "llanta_6": 2.0}
LABRADO_MINIMO_GENERAL = 1.6

# fleet.vehicle.type (``tipo_solicitud`` en /api/flota) que usan el labrado de buses del
# acuerdo (agreement.labrado_buses); los demás tipos usan agreement.labrado_camioneta.
TIPOS_BUS = frozenset({"bus", "buseta", "minibus"})


def es_bus(tipo_vehiculo: str | None) -> bool:
    return (tipo_vehiculo or "").strip().lower() in TIPOS_BUS


def labrado_minimo_acuerdo(tipo_vehiculo: str | None, labrado_camioneta: float | None,
                           labrado_buses: float | None) -> float | None:
    """Labrado mínimo del acuerdo según el tipo de vehículo; None si el acuerdo no lo define (0)."""
    valor = labrado_buses if es_bus(tipo_vehiculo) else labrado_camioneta
    return float(valor) if valor and valor > 0 else None


def labrado_minimo(key: str, minimo_acuerdo: float | None) -> float:
    """Mínimo para alertar una llanta: el del acuerdo o, si no lo define, el de Odoo."""
    return minimo_acuerdo if minimo_acuerdo else LABRADO_MINIMO.get(key, LABRADO_MINIMO_GENERAL)


def llantas_bajo_minimo(llantas: dict[str, float], minimo_acuerdo: float | None) -> list[str]:
    """Nombres de las llantas medidas cuyo labrado está por debajo del mínimo permitido."""
    return [
        ll.nombre for ll in LLANTAS_POSICIONES
        if 0 < llantas.get(ll.labrado, 0) < labrado_minimo(ll.labrado, minimo_acuerdo)
    ]


# Radios con "No aplica": obligatorios para pasar a Evidencia (y validados en el servidor).
OBLIGATORIOS: frozenset[str] = frozenset(
    item.key for item in CHECKLIST if item.control == RADIO and NO_APLICA in item.options
)


def completar_casillas(checklist: dict[str, str]) -> dict[str, str]:
    """Una casilla sin marcar es "no conforme" (patrón del formulario de referencia)."""
    return {**{item.key: NO_CONFORME for item in CHECKLIST if item.control == CASILLA}, **checklist}


def count_no_conformes(checklist: dict[str, str], llantas: dict[str, float]) -> int:
    """Cuenta hallazgos igual que lo hará Odoo, para mostrarlos al usuario antes del envío."""
    total = sum(1 for value in checklist.values() if value == NO_CONFORME)
    for key, value in llantas.items():
        if key.endswith("_pre") or value <= 0:
            continue  # presión, o labrado no medido
        if value < LABRADO_MINIMO.get(key, LABRADO_MINIMO_GENERAL):
            total += 1
    return total


def form_definition() -> dict[str, object]:
    """Estructura serializable para que la Mini App dibuje el formulario.

    ``tabs`` agrupa las secciones en las pestañas del paso 2; ``sections`` es la
    misma información sin agrupar (cada sección indica su ``tab``). El labrado
    mínimo de cada llanta es el de Odoo; el del acuerdo llega en ``/api/preop/context``.
    """
    sections: dict[str, dict[str, object]] = {}
    for item in CHECKLIST:
        sec = sections.setdefault(item.section, {"title": item.section, "tab": item.tab, "items": []})
        sec["items"].append(  # type: ignore[attr-defined]
            {"key": item.key, "label": item.label, "criterio": item.criterio, "control": item.control,
             "options": list(item.options), "obligatorio": item.key in OBLIGATORIOS}
        )
    secciones = list(sections.values())
    return {
        "tabs": [{"title": tab, "sections": [s for s in secciones if s["tab"] == tab]} for tab in TABS],
        "sections": secciones,
        "llantas": [
            {"codigo": ll.codigo, "nombre": ll.nombre, "posicion": ll.posicion,
             "labrado": ll.labrado, "presion": ll.presion, "interna": ll.interna,
             "labrado_minimo": LABRADO_MINIMO.get(ll.labrado, LABRADO_MINIMO_GENERAL)}
            for ll in LLANTAS_POSICIONES
        ],
        "measures": [
            {"key": m.key, "label": m.label, "unit": m.unit, "max": m.max_value} for m in LLANTAS_MEDIDAS
        ],
    }
