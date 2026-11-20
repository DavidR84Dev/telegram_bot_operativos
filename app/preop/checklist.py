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


@dataclass(frozen=True)
class ChecklistItem:
    key: str
    section: str
    label: str
    options: tuple[str, ...] = OPC_BASICA


@dataclass(frozen=True)
class MeasureItem:
    key: str
    label: str
    unit: str
    max_value: float


DOCUMENTOS = "Documentos"
LLANTAS = "Rines y llantas"
SEGURIDAD = "Equipo de prevención y seguridad"
EXTINTORES = "Extintores"
CABINA = "Chasis y cabina"
NIVELES = "Niveles, dirección, motor y frenos"
ELECTRICO = "Sistema eléctrico"

CHECKLIST: tuple[ChecklistItem, ...] = (
    # Documentos
    ChecklistItem("licencia_conduccion", DOCUMENTOS, "Licencia de conducción"),
    ChecklistItem("licencia_transito", DOCUMENTOS, "Licencia de tránsito"),
    ChecklistItem("soat", DOCUMENTOS, "SOAT"),
    ChecklistItem("rtm", DOCUMENTOS, "Revisión técnico-mecánica", OPC_CON_NA),
    ChecklistItem("extracto", DOCUMENTOS, "Extracto de contrato", OPC_CON_NA),
    ChecklistItem("tarjeta_operacion", DOCUMENTOS, "Tarjeta de operación", OPC_CON_NA),
    ChecklistItem("cedula", DOCUMENTOS, "Cédula de ciudadanía"),
    # Rines y llantas
    ChecklistItem("rin_1", LLANTAS, "Estado de rines"),
    ChecklistItem("llanta_v", LLANTAS, "Estado de llantas"),
    ChecklistItem("llanta_rep_pre_act", LLANTAS, "Llanta de repuesto con presión de aire", ("si", "no")),
    # Equipo de prevención y seguridad
    ChecklistItem("conos", SEGURIDAD, "Conos"),
    ChecklistItem("cunas", SEGURIDAD, "Cuñas"),
    ChecklistItem("botiquin", SEGURIDAD, "Botiquín"),
    ChecklistItem("gato", SEGURIDAD, "Gato"),
    ChecklistItem("linterna", SEGURIDAD, "Linterna"),
    ChecklistItem("herramientas", SEGURIDAD, "Herramientas básicas"),
    ChecklistItem("chaleco_reflectivo", SEGURIDAD, "Chaleco reflectivo", OPC_CON_NA),
    ChecklistItem("barra", SEGURIDAD, "Barra antivuelco", OPC_CON_NA),
    ChecklistItem("pico_pala", SEGURIDAD, "Pico y pala", OPC_CON_NA),
    ChecklistItem("kit_derrames", SEGURIDAD, "Kit de derrames químicos", OPC_CON_NA),
    ChecklistItem("eslingas", SEGURIDAD, "Eslingas", OPC_CON_NA),
    ChecklistItem("cables_de_arranque", SEGURIDAD, "Cables de arranque", OPC_CON_NA),
    ChecklistItem("cruceta", SEGURIDAD, "Cruceta"),
    ChecklistItem("profundimetro", SEGURIDAD, "Profundímetro"),
    ChecklistItem("medidor_aire", SEGURIDAD, "Medidor de aire"),
    ChecklistItem("camaras", SEGURIDAD, "Cámaras del vehículo", OPC_CON_NA),
    ChecklistItem("camaras_new", SEGURIDAD, "¿Tiene cámaras?", ("s", "n")),
    # Extintores
    ChecklistItem("extintor_1", EXTINTORES, "Extintor 1"),
    ChecklistItem("extintor_2", EXTINTORES, "Extintor 2", OPC_CON_NA),
    # Chasis y cabina
    ChecklistItem("orden_aseo", CABINA, "Orden y aseo"),
    ChecklistItem("cinturones", CABINA, "Cinturones de seguridad"),
    ChecklistItem("suspension", CABINA, "Suspensión y dirección"),
    ChecklistItem("espejos_retrovisores", CABINA, "Espejos retrovisores"),
    ChecklistItem("vidrios", CABINA, "Vidrios"),
    ChecklistItem("cojineria", CABINA, "Cojinería del asiento"),
    ChecklistItem("espaldar", CABINA, "Espaldar e inclinación"),
    ChecklistItem("asiento", CABINA, "Fijación del asiento"),
    # Niveles, dirección, motor y frenos
    ChecklistItem("nivel_frenos", NIVELES, "Nivel líquido de frenos"),
    ChecklistItem("nivel_aceite", NIVELES, "Nivel aceite hidráulico"),
    ChecklistItem("nivel_combustible", NIVELES, "Nivel de combustible"),
    ChecklistItem("nivel_refrigerante", NIVELES, "Nivel de refrigerante"),
    ChecklistItem("nivel_aceite_motor", NIVELES, "Nivel aceite de motor"),
    ChecklistItem("frenos", NIVELES, "Frenos"),
    ChecklistItem("freno_mano", NIVELES, "Freno de mano"),
    ChecklistItem("correas", NIVELES, "Correas"),
    # Sistema eléctrico
    ChecklistItem("switche", ELECTRICO, "Switche de encendido"),
    ChecklistItem("pito", ELECTRICO, "Pito"),
    ChecklistItem("pito_reversa", ELECTRICO, "Pito de reversa", OPC_CON_NA),
    ChecklistItem("bateria", ELECTRICO, "Batería"),
    ChecklistItem("dispositivo_de_velocidad", ELECTRICO, "Dispositivo de velocidad", OPC_CON_NA),
    ChecklistItem("cierre_de_puertas", ELECTRICO, "Cierre de puertas"),
    ChecklistItem("aire_acondicionado", ELECTRICO, "Aire acondicionado"),
    ChecklistItem("velocimetro", ELECTRICO, "Velocímetro"),
    ChecklistItem("limpia_parabrisas", ELECTRICO, "Limpiaparabrisas"),
    ChecklistItem("luces_delanteras", ELECTRICO, "Luces delanteras"),
    ChecklistItem("luces_direccionales", ELECTRICO, "Luces direccionales"),
    ChecklistItem("testigos", ELECTRICO, "Testigos del tablero"),
)

_POSICIONES = (
    ("1", "N.º 1 delantera derecha"),
    ("2", "N.º 2 delantera izquierda"),
    ("3", "N.º 3 trasera derecha"),
    ("4", "N.º 4 trasera izquierda"),
    ("5", "N.º 5 interna derecha"),
    ("6", "N.º 6 interna izquierda"),
)

# Labrado (mm) y presión (psi). En Odoo la repuesto es llanta_7 (labrado) y llanta_rep_pre (presión).
LLANTAS_MEDIDAS: tuple[MeasureItem, ...] = (
    *(MeasureItem(f"llanta_{n}", f"Labrado {label}", "mm", 30) for n, label in _POSICIONES),
    MeasureItem("llanta_7", "Labrado repuesto", "mm", 30),
    *(MeasureItem(f"llanta_{n}_pre", f"Presión {label}", "psi", 200) for n, label in _POSICIONES),
    MeasureItem("llanta_rep_pre", "Presión repuesto", "psi", 200),
)

CHECKLIST_BY_KEY: dict[str, ChecklistItem] = {item.key: item for item in CHECKLIST}
MEASURES_BY_KEY: dict[str, MeasureItem] = {item.key: item for item in LLANTAS_MEDIDAS}

# Ítems sin opción "no conforme" (sí/no informativos): no cuentan como hallazgo.
_INFORMATIVOS = {"llanta_rep_pre_act", "camaras_new"}

# Mismos umbrales que preoperacional_inherit.create(): debajo, Odoo marca "no conforme".
LABRADO_MINIMO = {"llanta_5": 2.0, "llanta_6": 2.0}
LABRADO_MINIMO_GENERAL = 1.6


def count_no_conformes(checklist: dict[str, str], llantas: dict[str, float]) -> int:
    """Cuenta hallazgos igual que lo hará Odoo, para mostrarlos al usuario antes del envío."""
    total = sum(1 for key, value in checklist.items() if key not in _INFORMATIVOS and value == NO_CONFORME)
    for key, value in llantas.items():
        if key.endswith("_pre") or value <= 0:
            continue  # presión, o labrado no medido
        if value < LABRADO_MINIMO.get(key, LABRADO_MINIMO_GENERAL):
            total += 1
    return total


def form_definition() -> dict[str, object]:
    """Estructura serializable para que la Mini App dibuje el formulario."""
    sections: dict[str, list[dict[str, object]]] = {}
    for item in CHECKLIST:
        sections.setdefault(item.section, []).append(
            {"key": item.key, "label": item.label, "options": list(item.options)}
        )
    return {
        "sections": [{"title": title, "items": items} for title, items in sections.items()],
        "measures": [
            {"key": m.key, "label": m.label, "unit": m.unit, "max": m.max_value} for m in LLANTAS_MEDIDAS
        ],
    }
