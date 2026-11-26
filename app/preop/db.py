"""Motor y sesiones SQLAlchemy del preoperacional (mismo DATABASE_URL que el resto del bot)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..watch_store import _make_engine, _resolve_url
from .models import Base


class Database:
    def __init__(self, database_url: str) -> None:
        self.engine: Engine = _make_engine(_resolve_url(database_url))
        if self.engine.dialect.name == "sqlite":
            # SQLite no aplica FK sin este PRAGMA (en PostgreSQL siempre se aplican).
            @event.listens_for(self.engine, "connect")
            def _fk_on(dbapi_conn, _record):  # pragma: no cover - trivial
                dbapi_conn.execute("PRAGMA foreign_keys=ON")

        # create_all es idempotente (CREATE TABLE/INDEX IF NOT EXISTS). Para cambios de
        # columnas en tablas existentes usar migraciones (Alembic), ver README.
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    # Columnas añadidas después de crear la tabla (create_all no altera tablas existentes).
    # (tabla, columna, tipo SQL); ADD COLUMN nullable es válido en SQLite y PostgreSQL.
    _NEW_COLUMNS = (
        ("combustible", "observaciones", "VARCHAR(500)"),
        ("combustible", "odoo_gasto_id", "INTEGER"),
        ("combustible", "latitud", "FLOAT"),
        ("combustible", "longitud", "FLOAT"),
        ("combustible", "ubicacion_at", "TIMESTAMP WITH TIME ZONE"),
        ("combustible", "ubicacion_requerida", "BOOLEAN"),
        ("combustible", "recordatorios_ubicacion", "INTEGER NOT NULL DEFAULT 0"),
        ("combustible", "gps_latitud", "FLOAT"),
        ("combustible", "gps_longitud", "FLOAT"),
        ("combustible", "gps_at", "TIMESTAMP WITH TIME ZONE"),
        ("combustible", "gps_distancia_m", "INTEGER"),
        ("combustible", "ubicacion_gps_vehiculo", "VARCHAR(255)"),
    )

    def _add_missing_columns(self) -> None:
        from sqlalchemy import inspect, text

        inspector = inspect(self.engine)
        for table, column, ddl in self._NEW_COLUMNS:
            if not inspector.has_table(table):
                continue
            if column in {c["name"] for c in inspector.get_columns(table)}:
                continue
            with self.engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Sesión transaccional: commit al salir sin error, rollback si hay excepción."""
        with self._sessions() as session, session.begin():
            yield session
