/* Service worker do PouPou (PWA). Estratégia segura:
   - navegação (abrir o app): REDE primeiro, cai pro shell em cache quando offline;
   - estáticos do app (ícones/logos/manifest): cache primeiro (rápido, atualiza em 2º plano);
   - resto (API dinâmica: /cesta, /buscar, /mercados…): NÃO intercepta — vai direto pela rede
     (preços sempre frescos, sem quebrar auth). Também nunca mexe em origens externas (Supabase/CDN). */
const CACHE = "poupou-v5";
const SHELL = ["/", "/manifest.webmanifest", "/vendor/supabase.js",
               "/icons/icon-v2-192.png", "/icons/icon-v2-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // não mexe em Supabase/CDN/externos

  if (req.mode === "navigate") {
    // abre INSTANTÂNEO: serve o shell do cache e atualiza em 2º plano (stale-while-revalidate)
    e.respondWith(caches.match("/").then((cached) => {
      const net = fetch(req).then((res) => {
        const copy = res.clone(); caches.open(CACHE).then((c) => c.put("/", copy)); return res;
      }).catch(() => cached);
      return cached || net;
    }));
    return;
  }
  if (/^\/(icons|logos|vendor)\//.test(url.pathname) || url.pathname === "/manifest.webmanifest") {
    e.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }))
    );
    return;
  }
  // demais requisições (API dinâmica) seguem direto pela rede
});

/* ===== PUSH: recebe a notificação do servidor e mostra no celular (mesmo travado) ===== */
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) {}
  const titulo = d.title || "PouPou";
  e.waitUntil(self.registration.showNotification(titulo, {
    body: d.body || "",
    icon: d.icon || "/icons/icon-v2-192.png",
    badge: d.badge || "/icons/badge.png",
    data: { url: d.url || "/" },
    vibrate: [80, 40, 80],
    tag: d.tag || "poupou",
  }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
    for (const c of cs) { if ("focus" in c) { c.navigate && c.navigate(url); return c.focus(); } }
    if (clients.openWindow) return clients.openWindow(url);
  }));
});
