from fastapi.testclient import TestClient

from app.main import app


def test_health():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_webhook_rejects_wrong_secret():
    with TestClient(app) as client:
        r = client.post("/telegram/webhook", json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": "mal"})
        assert r.status_code == 403


def test_webhook_rejects_malformed_json():
    with TestClient(app) as client:
        r = client.post("/telegram/webhook", content=b"{no json",
                        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret", "Content-Type": "application/json"})
        assert r.status_code == 400
