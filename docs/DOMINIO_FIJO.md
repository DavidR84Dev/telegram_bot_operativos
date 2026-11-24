# Dirección fija para los Mini Apps: `operativos.viacotur.com`

Los Mini Apps (preoperacional y escáner) se abrían por túneles `*.trycloudflare.com`, cuya
dirección cambia en cada reinicio. Los botones de los mensajes viejos quedaban apuntando a
una dirección muerta ("No se ha podido encontrar la dirección IP del servidor").

Se usa el mismo camino que ya funciona para `bot.viacotur.com` y `odoo.viacotur.com`:
DNS en GoDaddy → IP pública → MikroTik (dst-nat 80/443) → nginx en el servidor → servicio local.

> Un túnel con nombre de Cloudflare no sirve aquí: `viacotur.com` tiene su DNS en GoDaddy
> (`pdns13/14.domaincontrol.com`), no en Cloudflare.

## Pasos

1. **DNS (GoDaddy):** registro `A`, nombre `operativos`, valor `181.205.45.154`, TTL 600.
   Comprobar: `dig +short @8.8.8.8 operativos.viacotur.com` debe devolver la IP.

2. **nginx** (en el servidor):

   ```bash
   sudo cp deploy/nginx-operativos.conf /etc/nginx/sites-available/nginx-operativos.conf
   sudo ln -s /etc/nginx/sites-available/nginx-operativos.conf /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   ```

3. **Certificado:** ampliar el existente (no se toca la configuración de nginx):

   ```bash
   sudo certbot certonly --webroot -w /var/www/html --cert-name bot.viacotur.com --expand \
     -d bot.viacotur.com -d odoo.viacotur.com -d operativos.viacotur.com
   sudo systemctl reload nginx
   ```

   La renovación automática ya existe (`snap.certbot.renew.timer`).

4. **Bot** (`.env`): dejar una sola línea `SCANNER_BASE_URL=https://operativos.viacotur.com`
   y apuntar `WEBAPP_BASE_URL=https://odoo.viacotur.com` (página `/form/accept/<token>` de Odoo,
   ya publicada por nginx). Luego `docker compose up -d`.

5. **Apagar el túnel temporal:** `pkill -f "cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8011"`.

## Verificación

```bash
curl -sI https://operativos.viacotur.com/preoperacional   # 200 (desde datos móviles o fuera de la LAN)
```

Desde la LAN de la oficina la IP pública puede no responder si el MikroTik no tiene
*hairpin NAT*: probar desde datos móviles.
