"""Vigilancias pendientes (ticket -> validador).

Sobrevive a reinicios del servicio. Cada fila indica qué validador escaneó un
ticket que aún no ha terminado de abordar, y el último estado notificado.

Backend configurable vía la URL de conexión (ver ``Settings.database_url``):
SQLite en desarrollo/pruebas (incluye ``:memory:`` y rutas de archivo sueltas,
por compatibilidad con llamadas directas como ``WatchStore(":memory:")``) y
PostgreSQL en Docker/producción. El mismo SQL (SQLAlchemy Core, sin ORM) sirve
para ambos motores; solo cambia el motor de conexión.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine, create_engine
from sqlalchemy.pool import StaticPool

from .ticket_state import rank

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS watches (
        ticket_id INTEGER NOT NULL,
        validator_chat_id BIGINT NOT NULL,
        ticket_number TEXT NOT NULL,
        last_status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (ticket_id, validator_chat_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS passenger_notices (
        ticket_id INTEGER PRIMARY KEY,
        last_status TEXT NOT NULL
    )
    """,
)


class Watch:
    __slots__ = ("ticket_id", "validator_chat_id", "ticket_number", "last_status", "expires_at")

    def __init__(self, ticket_id, validator_chat_id, ticket_number, last_status, expires_at):
        self.ticket_id = int(ticket_id)
        self.validator_chat_id = int(validator_chat_id)
        self.ticket_number = ticket_number
        self.last_status = last_status
        self.expires_at = expires_at  # datetime naive UTC


def _resolve_url(database_url: str) -> str:
    """Acepta una URL SQLAlchemy (``postgresql+psycopg2://...``) tal cual, o un
    atajo SQLite (``:memory:`` o una ruta de archivo) por compatibilidad hacia atrás."""
    if "://" in database_url:
        return database_url
    if database_url == ":memory:":
        return "sqlite:///:memory:"
    return f"sqlite:///{database_url}"


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///"):
        path = url.removeprefix("sqlite:///")
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        if path in ("", ":memory:"):
            # Una sola conexión compartida: una :memory: nueva por conexión perdería los datos.
            return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


class WatchStore:
    def __init__(self, database_url: str) -> None:
        self._engine = _make_engine(_resolve_url(database_url))
        with self._engine.begin() as conn:
            for statement in _SCHEMA:
                conn.execute(text(statement))

    @contextmanager
    def _conn(self) -> Iterator[Connection]:
        with self._engine.begin() as conn:
            yield conn

    def add(self, ticket_id: int, validator_chat_id: int, ticket_number: str,
            status: str, now: datetime, expires_at: datetime) -> bool:
        """Registra la vigilancia. Devuelve False si ya existía."""
        with self._conn() as conn:
            result = conn.execute(
                text(
                    "INSERT INTO watches "
                    "(ticket_id, validator_chat_id, ticket_number, last_status, created_at, expires_at) "
                    "VALUES (:ticket_id, :chat_id, :number, :status, :created, :expires) "
                    "ON CONFLICT (ticket_id, validator_chat_id) DO NOTHING"
                ),
                {
                    "ticket_id": int(ticket_id), "chat_id": int(validator_chat_id),
                    "number": ticket_number, "status": status,
                    "created": now.isoformat(timespec="seconds"),
                    "expires": expires_at.isoformat(timespec="seconds"),
                },
            )
            return result.rowcount == 1

    def all(self) -> list[Watch]:
        with self._conn() as conn:
            rows = conn.execute(
                text("SELECT ticket_id, validator_chat_id, ticket_number, last_status, expires_at FROM watches")
            ).fetchall()
        return [Watch(r[0], r[1], r[2], r[3], datetime.fromisoformat(r[4])) for r in rows]

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute(text("SELECT COUNT(*) FROM watches")).scalar_one()

    def update_status(self, ticket_id: int, validator_chat_id: int, status: str) -> None:
        # Nunca retrocede: el orden de progreso decide.
        with self._conn() as conn:
            row = conn.execute(
                text("SELECT last_status FROM watches WHERE ticket_id=:tid AND validator_chat_id=:cid"),
                {"tid": ticket_id, "cid": validator_chat_id},
            ).fetchone()
            if row and rank(status) > rank(row[0]):
                conn.execute(
                    text("UPDATE watches SET last_status=:status WHERE ticket_id=:tid AND validator_chat_id=:cid"),
                    {"status": status, "tid": ticket_id, "cid": validator_chat_id},
                )

    def remove(self, ticket_id: int, validator_chat_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                text("DELETE FROM watches WHERE ticket_id=:tid AND validator_chat_id=:cid"),
                {"tid": ticket_id, "cid": validator_chat_id},
            )

    def passenger_status(self, ticket_id: int) -> str | None:
        """Último estado de abordaje avisado al pasajero (None si nunca se avisó)."""
        with self._conn() as conn:
            row = conn.execute(
                text("SELECT last_status FROM passenger_notices WHERE ticket_id=:tid"), {"tid": int(ticket_id)}
            ).fetchone()
        return row[0] if row else None

    def set_passenger_status(self, ticket_id: int, status: str) -> None:
        # Nunca retrocede, igual que update_status.
        with self._conn() as conn:
            row = conn.execute(
                text("SELECT last_status FROM passenger_notices WHERE ticket_id=:tid"), {"tid": int(ticket_id)}
            ).fetchone()
            if row is None:
                conn.execute(
                    text("INSERT INTO passenger_notices (ticket_id, last_status) VALUES (:tid, :status)"),
                    {"tid": int(ticket_id), "status": status},
                )
            elif rank(status) > rank(row[0]):
                conn.execute(
                    text("UPDATE passenger_notices SET last_status=:status WHERE ticket_id=:tid"),
                    {"status": status, "tid": int(ticket_id)},
                )
