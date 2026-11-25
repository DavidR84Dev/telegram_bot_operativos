"""Análisis Seguro de Trabajo (AST): Mini App ⚠️ AST, persistencia local y cola hacia Odoo.

Reutiliza la infraestructura del preoperacional (``app.preop``): tablas maestras
(empleados, extractos), validación de ``initData``, la BD del bot y el conector de
escritura ``OdooWriter`` (método ``create_ast``)::

    Mini App ──(initData HMAC)──▶ POST /api/ast ──▶ ast_trabajo (pendiente)
    AstSender ──(XML-RPC create)──▶ Odoo `ast` ──▶ enviado (AST-xxxx) | error | fallido
"""
