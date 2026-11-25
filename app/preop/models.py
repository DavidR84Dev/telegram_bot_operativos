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
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Float,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
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


class Acuerdo(MasterMixin, Base):
    """Contrato (``agreement`` en Odoo): define el labrado mínimo de las llantas por tipo de vehículo."""

    __tablename__ = "acuerdos"

    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    cliente: Mapped[str | None] = mapped_column(String(255))
    labrado_camioneta: Mapped[float | None] = mapped_column(Numeric(6, 2, asdecimal=False))
    labrado_buses: Mapped[float | None] = mapped_column(Numeric(6, 2, asdecimal=False))


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


class AnalisisSeguro(TimestampMixin, Base):
    """Análisis Seguro de Trabajo (AST) diligenciado en la Mini App ⚠️ AST.

    Misma cola que ``preoperacional`` (``sync_status``, reintentos, idempotencia por
    ``uuid``) hacia el modelo ``ast`` de Odoo. No tiene vehículo: el contrato sale de los
    extractos aprobados del conductor (``extracto_id`` es solo trazabilidad local; el
    modelo ``ast`` de Odoo no tiene ese campo). Las preguntas y sus opciones están en
    ``app/ast_trabajo/catalogo.py``.
    """

    __tablename__ = "ast_trabajo"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    firma_nombre: Mapped[str] = mapped_column(String(255), nullable=False)

    empleado_id: Mapped[int] = mapped_column(ForeignKey("empleados.id", ondelete="RESTRICT"), nullable=False)
    extracto_id: Mapped[int | None] = mapped_column(ForeignKey("extractos.id", ondelete="SET NULL"))
    contrato_odoo_id: Mapped[int | None] = mapped_column(Integer)
    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    # {campo_odoo: "conforme" | "no conforme" | "si" | "no"} (validado contra el catálogo).
    respuestas: Mapped[dict[str, str]] = mapped_column(JsonType, nullable=False, default=dict)
    nombre_medicamento: Mapped[str | None] = mapped_column(String(255))
    observaciones: Mapped[str | None] = mapped_column(Text)
    # Respuestas con alerta ("no conforme" o medicamentos = "si"): Odoo abre plan de acción.
    no_conformes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    sync_status: Mapped[SyncStatus] = mapped_column(
        Enum(SyncStatus, name="ast_sync_status", native_enum=False, length=16,
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
    extracto: Mapped[Extracto | None] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_ast_attempts_no_negativo"),
        Index("ix_ast_cola", "sync_status", "next_attempt_at"),
        Index("ix_ast_empleado_fecha", "empleado_id", "fecha"),
    )


class PreopFoto(TimestampMixin, Base):
    """Fotos de evidencia de un preoperacional (hoy solo ``testigos`` -> ``testigos_img`` en Odoo).

    Tabla aparte: el binario no se carga al leer la cola y se pueden sumar tipos sin migrar.
    """

    __tablename__ = "preop_fotos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    preop_id: Mapped[int] = mapped_column(ForeignKey("preoperacional.id", ondelete="CASCADE"), nullable=False)
    tipo: Mapped[str] = mapped_column(String(32), nullable=False)
    mime: Mapped[str] = mapped_column(String(32), nullable=False)
    contenido: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    tamano: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (UniqueConstraint("preop_id", "tipo", name="uq_preop_fotos_tipo"),)


class EstadoDiferido(str, enum.Enum):
    ACEPTADO = "aceptado"  # llegó dentro de la ventana: se creó el preoperacional y sigue el flujo normal
    VENCIDO = "vencido"    # llegó fuera de la ventana: solo auditoría (no va a Odoo, no reserva, no notifica)


class PreopEnvioDiferido(TimestampMixin, Base):
    """Trazabilidad de los formularios guardados en el teléfono por falta de señal.

    Regla de negocio (2026-11-24): se reenvían como máximo 1 hora después de perder la
    señal (ver ``app/preop/diferido.py``). Tabla nueva (no columnas en ``preoperacional``)
    para no requerir migración. Horas:

    * ``apertura_at``: ``auth_date`` del initData con que se abrió el formulario (firmado
      por Telegram, no falsificable).
    * ``sin_senal_desde``: primer intento de envío fallido, según el reloj del teléfono.
    * ``recibido_at``: hora del servidor.
    """

    __tablename__ = "preop_envios_diferidos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    estado: Mapped[EstadoDiferido] = mapped_column(
        Enum(EstadoDiferido, name="preop_estado_diferido", native_enum=False, length=16,
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    motivo: Mapped[str | None] = mapped_column(Text)
    # Solo los aceptados tienen preoperacional; los vencidos guardan el formulario aquí.
    preop_id: Mapped[int | None] = mapped_column(ForeignKey("preoperacional.id", ondelete="SET NULL"))
    apertura_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sin_senal_desde: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recibido_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    # Segundos entre la pérdida de señal (teléfono) y la recepción (servidor).
    retraso_segundos: Mapped[int] = mapped_column(Integer, nullable=False)
    # Huella SHA-256 del formulario recibido (sin los datos de autenticación).
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Formulario completo de un envío vencido (sin la foto, que va en ``foto``).
    payload: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    foto: Mapped[bytes | None] = mapped_column(LargeBinary)

    __table_args__ = (Index("ix_preop_diferidos_estado_fecha", "estado", "recibido_at"),)


class AstEnvioDiferido(TimestampMixin, Base):
    """Trazabilidad de los AST guardados en el teléfono por falta de señal.

    Misma regla y mismas horas que ``PreopEnvioDiferido`` (ver ``app/preop/diferido.py``):
    se reenvían como máximo 1 hora después de perder la señal; fuera de la ventana quedan
    ``vencido`` (solo auditoría, con el formulario completo en ``payload``).
    """

    __tablename__ = "ast_envios_diferidos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    estado: Mapped[EstadoDiferido] = mapped_column(
        Enum(EstadoDiferido, name="ast_estado_diferido", native_enum=False, length=16,
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    motivo: Mapped[str | None] = mapped_column(Text)
    ast_id: Mapped[int | None] = mapped_column(ForeignKey("ast_trabajo.id", ondelete="SET NULL"))
    apertura_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sin_senal_desde: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recibido_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    retraso_segundos: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JsonType)

    __table_args__ = (Index("ix_ast_diferidos_estado_fecha", "estado", "recibido_at"),)


class PruebaRuta(TimestampMixin, Base):
    """Prueba de Ruta de la inspección preoperacional (Mini App 🗺️ Prueba de Ruta).

    Misma cola que ``preoperacional`` y ``ast_trabajo`` hacia el modelo ``prueba.ruta`` de
    Odoo. Solo para conductores cuyo contrato es un acuerdo autorizado
    (``PRUEBA_RUTA_ACUERDOS``). El vehículo es el reservado a su nombre por el
    preoperacional de la jornada (``preop_id`` es ese preoperacional, trazabilidad local).
    Las fotos del odómetro van en ``prueba_ruta_fotos``.
    """

    __tablename__ = "prueba_ruta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    firma_nombre: Mapped[str] = mapped_column(String(255), nullable=False)

    empleado_id: Mapped[int] = mapped_column(ForeignKey("empleados.id", ondelete="RESTRICT"), nullable=False)
    vehiculo_id: Mapped[int] = mapped_column(ForeignKey("flota.id", ondelete="RESTRICT"), nullable=False)
    preop_id: Mapped[int | None] = mapped_column(ForeignKey("preoperacional.id", ondelete="SET NULL"))
    # Acuerdo autorizado del empleado con el que se habilitó (id de `agreement` y nombre).
    contrato_odoo_id: Mapped[int | None] = mapped_column(Integer)
    contrato: Mapped[str | None] = mapped_column(String(255))
    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    # {campo_odoo: "si" | "no"} (validado contra app/prueba_ruta/catalogo.py). "si" = novedad.
    respuestas: Mapped[dict[str, str]] = mapped_column(JsonType, nullable=False, default=dict)
    observaciones: Mapped[str | None] = mapped_column(Text)
    no_conformes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    sync_status: Mapped[SyncStatus] = mapped_column(
        Enum(SyncStatus, name="prueba_ruta_sync_status", native_enum=False, length=16,
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

    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_prueba_ruta_attempts_no_negativo"),
        Index("ix_prueba_ruta_cola", "sync_status", "next_attempt_at"),
        Index("ix_prueba_ruta_vehiculo_fecha", "vehiculo_id", "fecha"),
    )


class PruebaRutaFoto(TimestampMixin, Base):
    """Fotos del odómetro de una prueba de ruta: ``km1`` (inicial) y ``km2`` (final), campos binarios de Odoo."""

    __tablename__ = "prueba_ruta_fotos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prueba_id: Mapped[int] = mapped_column(ForeignKey("prueba_ruta.id", ondelete="CASCADE"), nullable=False)
    tipo: Mapped[str] = mapped_column(String(16), nullable=False)
    mime: Mapped[str] = mapped_column(String(32), nullable=False)
    contenido: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    tamano: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (UniqueConstraint("prueba_id", "tipo", name="uq_prueba_ruta_fotos_tipo"),)


class PruebaRutaEnvioDiferido(TimestampMixin, Base):
    """Trazabilidad de las pruebas de ruta guardadas en el teléfono por falta de señal.

    Misma regla que ``PreopEnvioDiferido`` (``app/preop/diferido.py``). Un envío vencido
    guarda el formulario en ``payload`` (sin fotos) y las fotos en ``foto_km1``/``foto_km2``.
    """

    __tablename__ = "prueba_ruta_envios_diferidos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    estado: Mapped[EstadoDiferido] = mapped_column(
        Enum(EstadoDiferido, name="prueba_ruta_estado_diferido", native_enum=False, length=16,
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    motivo: Mapped[str | None] = mapped_column(Text)
    prueba_id: Mapped[int | None] = mapped_column(ForeignKey("prueba_ruta.id", ondelete="SET NULL"))
    apertura_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sin_senal_desde: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recibido_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    retraso_segundos: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    foto_km1: Mapped[bytes | None] = mapped_column(LargeBinary)
    foto_km2: Mapped[bytes | None] = mapped_column(LargeBinary)

    __table_args__ = (Index("ix_prueba_ruta_diferidos_estado_fecha", "estado", "recibido_at"),)


class Ubicacion(TimestampMixin, Base):
    """Ubicación compartida desde el chat (botón 📍 Enviar ubicación).

    Se asocia al preoperacional que el mismo usuario envió en los últimos minutos
    (``service.UBICACION_VENTANA``); si no hay ninguno, queda suelta.
    """

    __tablename__ = "ubicaciones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    empleado_id: Mapped[int | None] = mapped_column(ForeignKey("empleados.id", ondelete="SET NULL"))
    preop_id: Mapped[int | None] = mapped_column(ForeignKey("preoperacional.id", ondelete="SET NULL"))
    latitud: Mapped[float] = mapped_column(Float, nullable=False)
    longitud: Mapped[float] = mapped_column(Float, nullable=False)
    precision_m: Mapped[float | None] = mapped_column(Float)
    recibido_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        CheckConstraint("latitud BETWEEN -90 AND 90", name="ck_ubicaciones_latitud"),
        CheckConstraint("longitud BETWEEN -180 AND 180", name="ck_ubicaciones_longitud"),
        Index("ix_ubicaciones_usuario_fecha", "telegram_user_id", "recibido_at"),
    )


class ReservaVehiculo(TimestampMixin, Base):
    """Tabla temporal: vehículo reservado al conductor que hizo su preoperacional.

    Un extracto puede tener varios conductores, pero solo uno puede tener el vehículo
    a la vez: la reserva se crea al enviar el preoperacional y se borra cuando ese
    conductor toca 🔴 Finalizar (fin de su jornada) o a las ``reservas.RESERVA_HORAS``
    de creada. ``vehiculo_id`` y ``telegram_user_id`` son únicos: una placa tiene un solo
    conductor y un conductor tiene un solo vehículo a la vez. Al liberarse, la fila pasa
    a ``reservas_historial``.
    """

    __tablename__ = "reservas_vehiculo"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehiculo_id: Mapped[int] = mapped_column(ForeignKey("flota.id", ondelete="CASCADE"), nullable=False)
    empleado_id: Mapped[int] = mapped_column(ForeignKey("empleados.id", ondelete="CASCADE"), nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    extracto_id: Mapped[int | None] = mapped_column(ForeignKey("extractos.id", ondelete="SET NULL"))
    # Último preoperacional del conductor con este vehículo (puede hacer varios en el día).
    preop_id: Mapped[int | None] = mapped_column(ForeignKey("preoperacional.id", ondelete="SET NULL"))
    fecha_jornada: Mapped[date] = mapped_column(Date, nullable=False)  # fecha local (Colombia) de inicio
    reservado_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    ultimo_preop_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    vehiculo: Mapped[Flota] = relationship(lazy="joined")
    empleado: Mapped[Empleado] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("vehiculo_id", name="uq_reservas_vehiculo_vehiculo"),
        UniqueConstraint("telegram_user_id", name="uq_reservas_vehiculo_conductor"),
    )


class ReservaHistorial(TimestampMixin, Base):
    """Histórico permanente de jornadas: cada reserva liberada (finalizar, vencida o manual)."""

    __tablename__ = "reservas_historial"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehiculo_id: Mapped[int | None] = mapped_column(ForeignKey("flota.id", ondelete="SET NULL"))
    placa: Mapped[str] = mapped_column(String(255), nullable=False)
    empleado_id: Mapped[int | None] = mapped_column(ForeignKey("empleados.id", ondelete="SET NULL"))
    conductor: Mapped[str] = mapped_column(String(255), nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    extracto_id: Mapped[int | None] = mapped_column(ForeignKey("extractos.id", ondelete="SET NULL"))
    ultimo_preop_id: Mapped[int | None] = mapped_column(ForeignKey("preoperacional.id", ondelete="SET NULL"))
    preoperacionales: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fecha_jornada: Mapped[date] = mapped_column(Date, nullable=False)
    reservado_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ultimo_preop_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    liberado_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    motivo: Mapped[str] = mapped_column(String(16), nullable=False)  # finalizar | vencida | manual

    __table_args__ = (
        CheckConstraint("motivo IN ('finalizar', 'vencida', 'manual')", name="ck_reservas_historial_motivo"),
        Index("ix_reservas_historial_conductor_fecha", "telegram_user_id", "reservado_at"),
        Index("ix_reservas_historial_vehiculo_fecha", "vehiculo_id", "reservado_at"),
    )


class RegistroConductor(TimestampMixin, Base):
    """Registro de conductores nuevos (opción 📝 Registro del bot).

    Una fila por usuario de Telegram con los datos que escribió (nombre, documento y
    correo). Se valida contra ``/api/empleados`` por número de documento y, si el
    empleado existe, su ``hr.employee.pin`` se fija al id de Telegram con
    ``/api/empleados/register``. Si un intento falla (no existe en Odoo, error de red),
    la misma fila se actualiza en el siguiente intento (``intentos``). Con
    ``estado = 'registrado'`` el usuario ya no tiene acceso a la opción de registro.
    """

    __tablename__ = "registros_conductor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    nombre_completo: Mapped[str] = mapped_column(String(255), nullable=False)
    documento: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    correo: Mapped[str] = mapped_column(String(255), nullable=False)
    # pendiente | registrado | no_encontrado | pin_ocupado | error
    estado: Mapped[str] = mapped_column(String(16), nullable=False, default="pendiente")
    empleado_odoo_id: Mapped[int | None] = mapped_column(Integer)
    intentos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_log: Mapped[str | None] = mapped_column(Text)
    registrado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("estado IN ('pendiente', 'registrado', 'no_encontrado', 'pin_ocupado', 'error')",
                        name="ck_registros_conductor_estado"),
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
