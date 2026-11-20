"""Validación de ``initData`` de Telegram Mini App (firma HMAC-SHA256 del bot).

Algoritmo oficial: secret = HMAC_SHA256(key="WebAppData", msg=bot_token);
hash = HMAC_SHA256(key=secret, msg=data_check_string), donde data_check_string
es "clave=valor" de todos los campos (sin ``hash``) ordenados y unidos con \\n.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InitDataError(Exception):
    """initData ausente, mal firmado, caducado o sin usuario."""


def validate_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int,
    now: float | None = None,
) -> dict[str, str]:
    """Devuelve ``{"user_id": ...}`` si la firma y la antigüedad son válidas."""
    user = validate_init_data_user(init_data, bot_token, max_age_seconds, now)
    return {"user_id": str(user["id"])}


def validate_init_data_user(
    init_data: str,
    bot_token: str,
    max_age_seconds: int,
    now: float | None = None,
) -> dict:
    """Como ``validate_init_data`` pero devuelve el objeto ``user`` completo de Telegram
    (``id``, ``first_name``, ``last_name``, ``username``...), ya verificado."""
    if not init_data:
        raise InitDataError("initData ausente")
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    received = data.pop("hash", None)
    if not received:
        raise InitDataError("initData sin firma")

    check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataError("firma inválida")

    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError as exc:
        raise InitDataError("auth_date inválido") from exc
    current = time.time() if now is None else now
    if auth_date <= 0 or current - auth_date > max_age_seconds or auth_date > current + 60:
        raise InitDataError("initData caducado")

    try:
        user = json.loads(data.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise InitDataError("usuario inválido") from exc
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise InitDataError("usuario inválido")
    return user
