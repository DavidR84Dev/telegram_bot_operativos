"""``python -m app.admin``: servidor del Panel Admin (FastAPI + bot en polling)."""
import uvicorn

from ..config import get_settings
from .server import app


def main() -> None:  # pragma: no cover - arranque del proceso
    settings = get_settings()
    uvicorn.run(app, host=settings.admin_listen_host, port=settings.admin_listen_port)


if __name__ == "__main__":  # pragma: no cover
    main()
