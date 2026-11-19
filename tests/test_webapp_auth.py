import time

import pytest

from app.webapp_auth import InitDataError, validate_init_data
from tests.helpers import BOT_TOKEN, make_init_data


def test_valid_signature_returns_user():
    assert validate_init_data(make_init_data(), BOT_TOKEN, 300) == {"user_id": "7913476225"}


def test_tampered_payload_is_rejected():
    with pytest.raises(InitDataError, match="firma"):
        validate_init_data(make_init_data(tamper=True), BOT_TOKEN, 300)


def test_wrong_bot_token_is_rejected():
    with pytest.raises(InitDataError, match="firma"):
        validate_init_data(make_init_data(), "999:OTRO", 300)


def test_expired_auth_date_is_rejected():
    old = int(time.time()) - 3600
    with pytest.raises(InitDataError, match="caducado"):
        validate_init_data(make_init_data(auth_date=old), BOT_TOKEN, 300)


def test_missing_or_unsigned_data_is_rejected():
    with pytest.raises(InitDataError):
        validate_init_data("", BOT_TOKEN, 300)
    with pytest.raises(InitDataError, match="sin firma"):
        validate_init_data("user=%7B%22id%22%3A1%7D&auth_date=1", BOT_TOKEN, 300)


def test_user_without_id_is_rejected():
    from urllib.parse import urlencode
    import hashlib
    import hmac
    fields = {"auth_date": str(int(time.time())), "user": "{}"}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(InitDataError, match="usuario"):
        validate_init_data(urlencode(fields), BOT_TOKEN, 300)
