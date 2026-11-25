"""Prueba de Ruta de la inspección preoperacional: Mini App 🗺️ Prueba de Ruta, BD local y cola a Odoo.

Reutiliza la infraestructura del preoperacional (``app.preop``): tablas maestras,
validación de ``initData``, la reserva del vehículo de la jornada, la BD del bot, el
envío sin señal y el conector ``OdooWriter`` (método ``create_prueba_ruta``)::

    Mini App ──(initData HMAC)──▶ POST /api/prueba-ruta ──▶ prueba_ruta (pendiente) + prueba_ruta_fotos
    PruebaRutaSender ──(XML-RPC create)──▶ Odoo `prueba.ruta` ──▶ enviado | error | fallido

Acceso restringido: solo conductores cuyo contrato es un acuerdo de ``PRUEBA_RUTA_ACUERDOS``;
los demás ven "Contrato No Autorizado".
"""
