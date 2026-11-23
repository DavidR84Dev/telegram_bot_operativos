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
