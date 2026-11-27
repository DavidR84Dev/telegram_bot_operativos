"""«Abrir App para escanear»: los formularios con documentos lo llevan; los de fotos de evidencia conservan la cámara."""
from __future__ import annotations

import hashlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.correcciones.router import router as correcciones_router
from app.gastos_operativos.router import router as gastos_router
from app.pos_operacional.router import router as pos_router
from app.preop.router import router as preop_router
from app.prueba_ruta.router import router as prueba_ruta_router

CON_ESCANEO = ["/gastos-operativos", "/Post-operacional", "/correcciones"]
CON_CAMARA = ["/preoperacional", "/prueba-ruta"]


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()
    for r in (preop_router, gastos_router, pos_router, correcciones_router, prueba_ruta_router):
        app.include_router(r)
    return TestClient(app)


@pytest.mark.parametrize("ruta", CON_ESCANEO)
def test_formularios_de_documentos_escanean_y_ya_no_usan_la_camara(client, ruta):
    page = client.get(ruta)
    assert page.status_code == 200
    assert "📄 Abrir App para escanear" in page.text and "Galería o PDF" in page.text
    assert "Tomar foto" not in page.text and "capture=" not in page.text and 'capture:' not in page.text
    assert "getUserMedia" not in page.text
    assert "EscanearDocumento" in page.text and "Recuperamos lo que llevabas" in page.text
    assert re.findall(r'src="/static/escanear_documento\.js\?v=([0-9a-f]{12})"', page.text)


@pytest.mark.parametrize("ruta", CON_CAMARA)
def test_formularios_de_fotos_de_evidencia_conservan_la_camara(client, ruta):
    page = client.get(ruta)
    assert page.status_code == 200
    assert "Tomar foto" in page.text and "capture=" in page.text
    assert "escanear_documento" not in page.text


def test_script_compartido_versionado_y_con_los_pasos_por_plataforma(client):
    page = client.get("/gastos-operativos")
    [version] = re.findall(r'/static/escanear_documento\.js\?v=([0-9a-f]{12})"', page.text)
    js = client.get(f"/static/escanear_documento.js?v={version}")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert hashlib.sha256(js.content).hexdigest()[:12] == version
    for marca in ("APPS_ESCANEO", "com.google.android.apps.docs", "com.adobe.scan.android", "com.intsig.camscanner",
                  "browser_fallback_url", "shareddocuments://", "mobilenotes://",
                  "https://apps.apple.com/app/id1199564834", "https://apps.apple.com/app/id388627783", "openLink",
                  "Otra app", "Abre tu app de escaneo, guarda el documento como PDF",
                  "ultima-app", "localStorage", "Última usada",
                  "Al terminar, vuelve a Telegram y toca «Galería o PDF» para subir el PDF",
                  "visibilitychange", "Galería o PDF", "Entendido", "indexedDB", "BORRADOR_MAX_MS"):
        assert marca in js.text
