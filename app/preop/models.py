"""Modelos ORM (SQLAlchemy 2.0) del preoperacional y de las tablas maestras.

Decisiones:

* Las tablas maestras usan un id local (``id``) y la llave natural de Odoo
  (``odoo_id``, única) como objetivo del UPSERT. Nunca se borran filas: un
  registro que deja de llegar en una sincronización completa queda con
  ``activo = False`` (los preoperacionales históricos siguen apuntándole).
* ``extractos.vehiculo_id`` guarda el id de Odoo del vehículo, **sin llave
  foránea física**: un extracto puede referir un vehículo de otra compañía que
  no se sincroniza, y una FK rompería el UPSERT. La relación ORM es de solo
  lectura (``viewonly``).
* ``preoperacional`` sí tiene FK locales (empleado, vehículo, extracto): se
  crean contra filas maestras que existen en el momento del envío.
* Los JSON son ``JSONB`` en PostgreSQL y ``JSON`` en SQLite (pruebas), vía
  ``with_variant``. Los índices GIN solo se crean en PostgreSQL.
"""
from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, foreign, mapped_column, relationship

# JSONB en PostgreSQL; JSON genérico (texto) en SQLite.
JsonType = JSON().with_variant(JSONB(), "postgresql")


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now(), onupdate=utc_now
    )


class MasterMixin(TimestampMixin):
    """Campos comunes de las tablas sincronizadas desde Odoo."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    odoo_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    # Nombre de la compañía tal como lo devuelve la API y el id con el que se consultó.
    company_id: Mapped[str | None] = mapped_column(String(255))
    company_odoo_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    # Última vez que la API devolvió el registro (se usa para desactivar los que desaparecen).
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    # Objeto original de la API (auditoría y campos no mapeados).
    raw: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)


class Empleado(MasterMixin, Base):
    __tablename__ = "empleados"

    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    # La API actual entrega el id del departamento (int); se acepta también nombre u objeto.
    departamento_id: Mapped[int | None] = mapped_column(Integer)
    departamento: Mapped[str | None] = mapped_column(String(255))
    puesto_trabajo: Mapped[str | None] = mapped_column(String(255))
    # hr.employee.pin = id de Telegram del empleado: identifica a quien abre la Mini App.
    codigo_pin: Mapped[str | None] = mapped_column(String(64), index=True)
    id_contract: Mapped[int | None] = mapped_column(Integer)
    contrato: Mapped[str | None] = mapped_column(String(255))
    contrato_temporal_id: Mapped[int | None] = mapped_column(Integer)
    contrato_temporal: Mapped[str | None] = mapped_column(String(255))
    responsable: Mapped[str | None] = mapped_column(String(255))
    responsable_mobile: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (Index("ix_empleados_nombre", "nombre"),)


class Flota(MasterMixin, Base):
    __tablename__ = "flota"

    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    matricula: Mapped[str | None] = mapped_column(String(32), index=True)
    estado: Mapped[str | None] = mapped_column(String(64))
    numero_interno: Mapped[str | None] = mapped_column(String(64), index=True)
    parque_automotor: Mapped[str | None] = mapped_column(String(64))
    # Numeric para no perder precisión; se lee como float.
    odometer_post: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False))
    tipo_solicitud: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (Index("ix_flota_nombre", "nombre"),)


class Extracto(MasterMixin, Base):
    __tablename__ = "extractos"

    codigo: Mapped[str | None] = mapped_column(String(64), index=True)
    conductor: Mapped[str | None] = mapped_column(String(255))
    cel_conductor1: Mapped[str | None] = mapped_column(String(32))
    estado_documentos_conductor1: Mapped[str | None] = mapped_column(String(64))
    segundo_conductor: Mapped[str | None] = mapped_column(String(255))
    cel_conductor2: Mapped[str | None] = mapped_column(String(32))
    estado_documentos_conductor2: Mapped[str | None] = mapped_column(String(64))
    tercer_conductor: Mapped[str | None] = mapped_column(String(255))
    cel_conductor3: Mapped[str | None] = mapped_column(String(32))
    estado_documentos_conductor3: Mapped[str | None] = mapped_column(String(64))
    # [{"id_contrato": int, "nombre": str}, ...] (ids de `agreement` en Odoo).
    contratos_list: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, nullable=False, default=list)
    estado: Mapped[str | None] = mapped_column(String(32))
    fecha_hora_inicio: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fecha_hora_fin: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responsable_cliente: Mapped[str | None] = mapped_column(String(255))
    responsable_cliente_mobile: Mapped[str | None] = mapped_column(String(32))
    vehiculo: Mapped[str | None] = mapped_column(String(255))
    # Id de Odoo (fleet.vehicle). Sin FK física: ver docstring del módulo.
    vehiculo_id: Mapped[int | None] = mapped_column(Integer, index=True)

    flota: Mapped[Flota | None] = relationship(
        primaryjoin=lambda: foreign(Extracto.vehiculo_id) == Flota.odoo_id,
        viewonly=True,
        lazy="joined",
    )

    __table_args__ = (
        # Búsqueda típica de la Mini App: extractos vigentes de un vehículo.
        Index("ix_extractos_vehiculo_vigencia", "vehiculo_id", "fecha_hora_inicio", "fecha_hora_fin"),
        Index("ix_extractos_estado", "estado"),
    )


class SyncStatus(str, enum.Enum):
    PENDIENTE = "pendiente"  # recibido, aún no enviado
    ENVIANDO = "enviando"    # tomado por el worker (lease: preop_sending_lease_seconds)
    ENVIADO = "enviado"      # creado en Odoo; odoo_id lleno
    ERROR = "error"          # fallo reintentable; next_attempt_at con backoff
    FALLIDO = "fallido"      # agotó reintentos o error permanente; requiere revisión manual


class Preoperacional(TimestampMixin, Base):
    __tablename__ = "preoperacional"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Llave de idempotencia generada por la Mini App: reenviar el mismo formulario no duplica.
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))

    # Firma / usuario: identidad validada con initData (nunca tomada del cuerpo de la petición).
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    firma_nombre: Mapped[str] = mapped_column(String(255), nullable=False)

    empleado_id: Mapped[int] = mapped_column(ForeignKey("empleados.id", ondelete="RESTRICT"), nullable=False)
    vehiculo_id: Mapped[int] = mapped_column(ForeignKey("flota.id", ondelete="RESTRICT"), nullable=False)
    extracto_id: Mapped[int | None] = mapped_column(ForeignKey("extractos.id", ondelete="SET NULL"))
    # Id de `agreement` en Odoo (del extracto o del contrato del empleado).
    contrato_odoo_id: Mapped[int | None] = mapped_column(Integer)

    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    kilometraje: Mapped[float] = mapped_column(Numeric(12, 1, asdecimal=False), nullable=False)
    numero_interno: Mapped[str | None] = mapped_column(String(64))
    observaciones: Mapped[str | None] = mapped_column(Text)

    # Ítems de inspección {campo_odoo: "conforme" | "no conforme" | "no aplica" | ...}.
    # Los campos válidos y sus opciones están en app/preop/checklist.py (validados con Pydantic).
    checklist: Mapped[dict[str, str]] = mapped_column(JsonType, nullable=False, default=dict)
    # Labrado (mm) y presión (psi) de llantas {campo_odoo: float}.
    llantas: Mapped[dict[str, float]] = mapped_column(JsonType, nullable=False, default=dict)
    # Atributos dinámicos adicionales (campos nuevos de Odoo aún no tipados).
    extra: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    # Resumen calculado al recibir (para filtrar sin abrir el JSON).
    no_conformes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Auditoría de sincronización con Odoo.
    sync_status: Mapped[SyncStatus] = mapped_column(
        Enum(SyncStatus, name="preop_sync_status", native_enum=False, length=16,
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=SyncStatus.PENDIENTE,
    )
    odoo_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    odoo_name: Mapped[str | None] = mapped_column(String(64))
    error_log: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    empleado: Mapped[Empleado] = relationship(lazy="joined")
    vehiculo: Mapped[Flota] = relationship(lazy="joined")
    extracto: Mapped[Extracto | None] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint("kilometraje >= 0", name="ck_preop_kilometraje_no_negativo"),
        CheckConstraint("attempts >= 0", name="ck_preop_attempts_no_negativo"),
        # Cola del worker: pendientes/errores por fecha de próximo intento.
        Index("ix_preop_cola", "sync_status", "next_attempt_at"),
        Index("ix_preop_vehiculo_fecha", "vehiculo_id", "fecha"),
        Index("ix_preop_checklist_gin", "checklist", postgresql_using="gin"),
    )


class SyncRun(Base):
    """Bitácora de cada sincronización maestra (una fila por entidad y compañía)."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entidad: Mapped[str] = mapped_column(String(32), nullable=False)
    company_odoo_id: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")  # running|ok|error
    recibidos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    upserts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalidos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    desactivados: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_log: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_sync_runs_entidad_inicio", "entidad", "started_at"),)
