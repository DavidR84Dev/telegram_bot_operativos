"""Preoperacional vehicular: tablas maestras sincronizadas desde la API REST de Odoo,
formulario de la Mini App, persistencia local y cola de envío a Odoo por XML-RPC.

Tubería::

    API REST Odoo ──(etl, UPSERT)──▶ empleados / extractos / flota
    Mini App ──(initData HMAC)──▶ POST /api/preop ──▶ preoperacional (pendiente)
    worker ──(XML-RPC create)──▶ Odoo `preoperacional` ──▶ enviado (odoo_id) | error (error_log)
"""
