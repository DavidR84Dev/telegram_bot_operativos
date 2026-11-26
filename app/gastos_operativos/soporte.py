"""Soporte de un gasto: PDF (firma ``%PDF``) o foto JPG/PNG, que se convierte a PDF en el servidor.

Odoo recibe siempre un PDF (``type_file: "pdf"``). El formato se decide por la firma real del archivo, no por lo que
diga el cliente. HEIC/HEIF no se soporta (no hay librería en el servidor): la Mini App vuelve a codificar las fotos
como JPG antes de enviarlas y, si el teléfono no logra leerlas, pide JPG o PNG.
"""
from __future__ import annotations

import base64
import binascii
import io
import re

from .catalogo import SOPORTE_MAX_BYTES

# Una foto de celular de 48 MP son ~48 M píxeles; por encima es un archivo anómalo o una bomba de descompresión.
MAX_PIXELES = 60_000_000
MSG_FORMATO = "El soporte debe ser un PDF o una foto JPG o PNG"


def decodificar(valor: str) -> bytes:
    """Base64 del soporte (admite el prefijo ``data:...;base64,``)."""
    crudo = valor.split(",", 1)[1] if valor.startswith("data:") else valor
    try:
        datos = base64.b64decode(crudo, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("El soporte no es base64 válido") from exc
    if not datos:
        raise ValueError("El soporte está vacío")
    if len(datos) > SOPORTE_MAX_BYTES:
        raise ValueError(f"El soporte supera {SOPORTE_MAX_BYTES // (1024 * 1024)} MB")
    return datos


def formato(datos: bytes) -> str:
    """``pdf``, ``jpeg`` o ``png`` según la firma. Lanza ``ValueError`` si no es ninguno."""
    if datos.startswith(b"%PDF"):
        return "pdf"
    if datos.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if datos.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if datos[4:8] == b"ftyp" and datos[8:12] in (b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"heim", b"heis"):
        raise ValueError("Las fotos HEIC no se soportan: usa JPG o PNG (o un PDF)")
    raise ValueError(MSG_FORMATO)


def validar(valor: str) -> bytes:
    """Decodifica y comprueba el formato; devuelve los bytes originales (la conversión es aparte)."""
    datos = decodificar(valor)
    formato(datos)
    return datos


def a_pdf(datos: bytes) -> tuple[bytes, bool]:
    """PDF listo para Odoo y si se convirtió desde una foto. Un PDF pasa tal cual; una foto se vuelve un PDF de una
    página (se corrige la orientación EXIF y se aplana la transparencia sobre blanco).
    Lanza ``ValueError`` con un mensaje claro si la foto está dañada."""
    if formato(datos) == "pdf":
        return datos, False
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(datos)) as imagen:
            if imagen.width * imagen.height > MAX_PIXELES:
                raise ValueError("La foto es demasiado grande: toma una de menor resolución")
            imagen.load()
            foto = ImageOps.exif_transpose(imagen) or imagen
            if foto.mode in ("RGBA", "LA", "P"):
                rgba = foto.convert("RGBA")
                fondo = Image.new("RGB", rgba.size, (255, 255, 255))
                fondo.paste(rgba, mask=rgba.getchannel("A"))
                foto = fondo
            elif foto.mode != "RGB":
                foto = foto.convert("RGB")
            salida = io.BytesIO()
            foto.save(salida, "PDF", resolution=150.0)
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as exc:
        raise ValueError("No se pudo leer la foto del soporte: vuelve a tomarla o adjunta un PDF") from exc
    return salida.getvalue(), True


def nombre_pdf(nombre: str | None, numero: int) -> str:
    """Nombre del archivo que se envía a Odoo: el original sin ruta y con extensión ``.pdf``."""
    base = re.split(r"[\\/]", (nombre or "").strip())[-1]
    base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", base)
    base = re.sub(r"[^\w .()\-]", "_", base, flags=re.UNICODE).strip(" ._")[:80]
    return f"{base or f'soporte_{numero}'}.pdf"
