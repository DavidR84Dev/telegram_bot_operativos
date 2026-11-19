from app.odoo_client import OdooClient


def test_client_exposes_only_read_operations():
    public = {name for name in dir(OdooClient) if not name.startswith("_")}
    assert public == {"search_read"}
