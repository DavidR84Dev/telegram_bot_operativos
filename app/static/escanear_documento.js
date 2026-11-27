/* Escanear documento: guía al conductor a la app nativa de escaneo del teléfono y conserva el borrador.
 *
 * Lo cargan los formularios que suben un documento (soporte, factura, planilla) y que antes tomaban la
 * foto con la cámara del formulario: gastos_operativos, pos_operacional y correcciones.
 * preoperacional y prueba_ruta NO lo usan: sus fotos (testigos, odómetro) siguen con la cámara.
 *
 * API (window.EscanearDocumento):
 *   abrir()                        hoja con los pasos según Telegram.WebApp.platform
 *   borrador(formulario, usuarioId) -> { guardar(datos), leer(), borrar() }
 *     Guarda en IndexedDB (clave usuario + formulario, caduca a las 2 h) lo diligenciado antes de salir a
 *     la otra app, porque Android puede destruir el WebView de Telegram. Todo en try/catch: si el
 *     almacenamiento falla, el formulario funciona igual (guardar devuelve false, leer devuelve null).
 */
(() => {
  "use strict";
  const tg = window.Telegram && window.Telegram.WebApp;
  // Apps de escaneo por plataforma: para agregar o quitar una, basta editar esta constante.
  //   id: clave para recordar la última usada; nombre/icono: lo que se ve en el botón; pasos: línea bajo el botón.
  //   android: paquete -> intent de lanzador (si no está instalada abre su ficha en Play Store).
  //   ios: url (esquema nativo, se navega) o tienda (página de App Store, se abre con openLink).
  const APPS_ESCANEO = {
    android: [
      { id: "drive", nombre: "Google Drive", icono: "☁️", paquete: "com.google.android.apps.docs",
        pasos: "➕ → Escanear, toma la foto y toca Guardar (PDF)." },
      { id: "adobe", nombre: "Adobe Scan", icono: "🅰️", paquete: "com.adobe.scan.android",
        pasos: "Abre directo en la cámara: escanea y toca Guardar PDF." },
      { id: "camscanner", nombre: "CamScanner", icono: "📷", paquete: "com.intsig.camscanner",
        pasos: "Cámara → escanea → Exportar PDF." },
    ],
    ios: [
      { id: "archivos", nombre: "Archivos", icono: "📁", url: "shareddocuments://",
        pasos: "⋯ (arriba a la derecha) → Escanear documentos → Guardar en una carpeta." },
      { id: "notas", nombre: "Notas", icono: "🗒️", url: "mobilenotes://",
        pasos: "Nueva nota → 📎/cámara → Escanear documentos → compartir/guardar como PDF en Archivos." },
      { id: "adobe", nombre: "Adobe Scan", icono: "🅰️", tienda: "https://apps.apple.com/app/id1199564834",
        pasos: "Abre en la cámara: escanea y toca Guardar PDF. La App Store muestra «Abrir» si ya la tienes." },
      { id: "camscanner", nombre: "CamScanner", icono: "📷", tienda: "https://apps.apple.com/app/id388627783",
        pasos: "Cámara → escanea → Exportar PDF. La App Store muestra «Abrir» si ya la tienes." },
    ],
  };
  const OTRA_APP = { id: "otra", nombre: "Otra app", icono: "🔎",
    pasos: "Abre tu app de escaneo, guarda el documento como PDF y vuelve aquí para subirlo con «Galería o PDF»." };
  const ULTIMA_KEY = "escanear-documento:ultima-app";
  const ESPERA_APERTURA_MS = 1500;

  // action=MAIN + LAUNCHER: sin ellos Android a menudo no resuelve el intent y no abre la app.
  const intentAndroid = (paquete) => "intent://#Intent;action=android.intent.action.MAIN;"
    + `category=android.intent.category.LAUNCHER;package=${paquete};`
    + `S.browser_fallback_url=${encodeURIComponent(`https://play.google.com/store/apps/details?id=${paquete}`)};end`;
  const leerUltima = () => { try { return localStorage.getItem(ULTIMA_KEY) || ""; } catch (_) { return ""; } };
  const guardarUltima = (id) => { try { localStorage.setItem(ULTIMA_KEY, id); } catch (_) { /* sin almacenamiento */ } };
  const BORRADOR_MAX_MS = 2 * 60 * 60 * 1000;
  const IDB_NAME = "escanear-documento", IDB_STORE = "borradores";

  const plataforma = () => String((tg && tg.platform) || "").toLowerCase();

  // ── Hoja con los pasos ───────────────────────────────────────────────────
  function estilos() {
    if (document.getElementById("escanear-doc-css")) return;
    const s = document.createElement("style");
    s.id = "escanear-doc-css";
    s.textContent = `
      .esc-fondo { position: fixed; inset: 0; z-index: 60; background: rgb(0 0 0 / 50%); display: flex; align-items: flex-end; justify-content: center; }
      .esc-hoja { width: 100%; max-width: 560px; background: var(--surface, #fff); color: var(--fg, #14172f); border-radius: 18px 18px 0 0;
        padding: 18px 16px calc(18px + env(safe-area-inset-bottom)); box-shadow: 0 -4px 20px rgb(0 0 0 / 25%); max-height: 90vh; overflow: auto; }
      .esc-hoja h2 { margin: 0 0 8px; font-size: 17px; color: var(--brand-2, #1f6fb8); }
      .esc-hoja p, .esc-hoja li { font-size: 14px; line-height: 1.45; }
      .esc-hoja ol { margin: 8px 0 12px; padding-left: 20px; }
      .esc-hoja .esc-nota { color: var(--hint, #6b7085); font-size: 13px; margin: 6px 0 0; }
      .esc-btn { width: 100%; min-height: 48px; border-radius: 12px; font: inherit; font-size: 16px; font-weight: 600; margin-top: 8px; }
      .esc-btn.primario { border: 0; background: var(--brand-2, #1f6fb8); color: #fff; }
      .esc-btn.secundario { border: 1px solid var(--brand-2, #1f6fb8); background: var(--surface, #fff); color: var(--brand-2, #1f6fb8); }`;
    document.head.append(s);
  }

  function nodo(tag, clase, texto) {
    const n = document.createElement(tag);
    if (clase) n.className = clase;
    if (texto !== undefined) n.textContent = texto;
    return n;
  }

  function abrir() {
    estilos();
    const plat = plataforma();
    const fondo = nodo("div", "esc-fondo");
    const hoja = nodo("div", "esc-hoja");
    hoja.setAttribute("role", "dialog");
    hoja.setAttribute("aria-modal", "true");
    hoja.append(nodo("h2", "", "📄 Abrir App para escanear"));
    const cerrar = () => { document.removeEventListener("visibilitychange", alOcultar); fondo.remove(); };
    let enSegundoPlano = false;
    const alOcultar = () => { if (document.hidden) enSegundoPlano = true; };

    const apps = APPS_ESCANEO[plat];
    if (apps) {
      const nota = nodo("p", "esc-nota", "");
      nota.hidden = true;
      const ultima = leerUltima();
      const orden = [...apps].sort((a, b) => (b.id === ultima) - (a.id === ultima));
      const lanzar = (app) => {
        guardarUltima(app.id);
        enSegundoPlano = false;
        document.addEventListener("visibilitychange", alOcultar);
        try {
          if (app.paquete) window.location.href = intentAndroid(app.paquete);
          else if (app.tienda) { if (tg && tg.openLink) tg.openLink(app.tienda); else window.open(app.tienda, "_blank"); }
          else window.location.href = app.url;
        } catch (_) { /* se siguen los pasos a mano */ }
        setTimeout(() => {
          if (!enSegundoPlano) {
            nota.textContent = `Si no se abrió ${app.nombre}, ábrela tú mismo desde tu teléfono y sigue los pasos.`;
            nota.hidden = false;
          }
        }, ESPERA_APERTURA_MS);
      };
      const fila = (app, accion) => {
        const b = nodo("button", `esc-btn ${app.id === ultima ? "primario" : "secundario"}`,
          `${app.icono} ${app.nombre}${app.id === ultima ? " · Última usada" : ""}`);
        b.type = "button";
        b.addEventListener("click", accion);
        hoja.append(b, nodo("p", "esc-nota", app.pasos));
      };
      for (const app of orden) fila(app, () => lanzar(app));
      fila(OTRA_APP, () => guardarUltima(OTRA_APP.id));
      hoja.append(nota);
      hoja.append(nodo("p", "esc-nota", "Al terminar, vuelve a Telegram y toca «Galería o PDF» para subir el PDF."));
      hoja.append(nodo("p", "esc-nota", "Lo que llevas diligenciado se conserva por 2 horas."));
    } else {
      hoja.append(nodo("p", "",
        "Escanea el documento con tu teléfono (app de escaneo o Drive/Archivos) y guárdalo como PDF. "
        + "Luego súbelo con «Galería o PDF»."));
    }

    const entendido = nodo("button", "esc-btn secundario", "Entendido");
    entendido.type = "button";
    entendido.addEventListener("click", cerrar);
    hoja.append(entendido);
    fondo.append(hoja);
    fondo.addEventListener("click", (ev) => { if (ev.target === fondo) cerrar(); });
    document.body.append(fondo);
    return cerrar;
  }

  // ── Borrador (IndexedDB) ─────────────────────────────────────────────────
  function abrirDb() {
    return new Promise((resolve, reject) => {
      if (!window.indexedDB) { reject(new Error("sin IndexedDB")); return; }
      const req = indexedDB.open(IDB_NAME, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(IDB_STORE);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function operar(modo, hacer) {
    const db = await abrirDb();
    try {
      return await new Promise((resolve, reject) => {
        const tx = db.transaction(IDB_STORE, modo);
        const req = hacer(tx.objectStore(IDB_STORE));
        tx.oncomplete = () => resolve(req ? req.result : undefined);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      });
    } finally {
      db.close();
    }
  }

  function borrador(formulario, usuarioId) {
    const clave = `${usuarioId === null || usuarioId === undefined ? "anon" : usuarioId}:${formulario}`;
    const api = {
      async guardar(datos) {
        try { await operar("readwrite", (s) => s.put({ guardado: Date.now(), datos }, clave)); return true; } catch (_) { return false; }
      },
      // Devuelve lo guardado (si no caducó) y lo borra: queda en el formulario, y volverá a guardarse al escanear.
      async leer() {
        try {
          const item = await operar("readonly", (s) => s.get(clave));
          if (!item) return null;
          await api.borrar();
          return Date.now() - item.guardado > BORRADOR_MAX_MS ? null : item.datos;
        } catch (_) { return null; }
      },
      async borrar() {
        try { await operar("readwrite", (s) => s.delete(clave)); } catch (_) { /* sin almacenamiento */ }
      },
    };
    return api;
  }

  window.EscanearDocumento = { abrir, borrador };
})();
