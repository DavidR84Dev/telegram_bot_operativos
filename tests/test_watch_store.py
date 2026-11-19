from datetime import datetime, timedelta

from app.watch_store import WatchStore

NOW = datetime(2026, 11, 7, 14, 0, 0)


def test_add_is_idempotent_and_listed():
    s = WatchStore(":memory:")
    assert s.add(10, 555, "T1", "enviado", NOW, NOW + timedelta(hours=24))
    assert not s.add(10, 555, "T1", "enviado", NOW, NOW + timedelta(hours=24))
    rows = s.all()
    assert len(rows) == 1 and rows[0].ticket_id == 10 and rows[0].last_status == "enviado"
    assert s.count() == 1


def test_status_never_regresses():
    s = WatchStore(":memory:")
    s.add(10, 555, "T1", "abordado1", NOW, NOW + timedelta(hours=24))
    s.update_status(10, 555, "enviado")
    assert s.all()[0].last_status == "abordado1"
    s.update_status(10, 555, "abordado2")
    assert s.all()[0].last_status == "abordado2"


def test_remove_and_file_persistence(tmp_path):
    path = str(tmp_path / "w.sqlite3")
    s = WatchStore(path)
    s.add(10, 555, "T1", "enviado", NOW, NOW + timedelta(hours=24))
    s2 = WatchStore(path)  # simula reinicio del servicio
    assert s2.count() == 1
    s2.remove(10, 555)
    assert WatchStore(path).count() == 0
