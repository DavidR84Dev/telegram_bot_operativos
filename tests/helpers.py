import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

BOT_TOKEN = "123456:TEST"
TOKEN = "QjZL-MkZbE_DHS5oSv8MKJv5HDpUyhlaYgAGoh8fVoc"


def make_init_data(user_id=7913476225, auth_date=None, bot_token=BOT_TOKEN, tamper=False):
    """Construye initData firmado como lo hace Telegram (para pruebas)."""
    fields = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAH-test",
        "user": json.dumps({"id": user_id, "first_name": "Prueba"}, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields["user"] = json.dumps({"id": 111}, separators=(",", ":"))
    return urlencode(fields)


def antedatar_reservas(db, horas: float = 3) -> None:
    """Mueve las reservas vigentes ``horas`` atrás: un envío diferido solo vale si la reserva ya existía al abrir
    el formulario (regla del 2026-11-26), y los tests crean la reserva "ahora"."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.preop.models import ReservaVehiculo

    with db.session() as s:
        for r in s.scalars(select(ReservaVehiculo)):
            r.reservado_at = r.reservado_at - timedelta(hours=horas)
            r.ultimo_preop_at = r.ultimo_preop_at - timedelta(hours=horas)
