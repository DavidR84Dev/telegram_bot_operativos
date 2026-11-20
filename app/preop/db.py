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
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Sesión transaccional: commit al salir sin error, rollback si hay excepción."""
        with self._sessions() as session, session.begin():
            yield session
