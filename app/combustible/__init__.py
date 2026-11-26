"""Registro de combustible: Mini App ⛽ Combustible, BD local y cola hacia el modelo ``fuel`` de Odoo.

Reutiliza la infraestructura del preoperacional (``app.preop``): tablas maestras,
validación de ``initData``, la reserva del vehículo de la jornada, el envío sin señal y el
conector ``OdooWriter`` (método ``create_combustible``)::

    Mini App ──(initData HMAC)──▶ POST /api/combustible ──▶ combustible (pendiente) + combustible_facturas
    CombustibleSender ──(XML-RPC create)──▶ Odoo `fuel` ──▶ enviado | error | fallido

El vehículo y el contrato salen del preoperacional de la jornada. Los proveedores aprobados
viven en ``proveedores_combustible`` (carga por CSV); uno que no esté en la lista se escribe a
mano y va a Odoo como texto (``supplier``).
"""
