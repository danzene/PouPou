/* Service worker do PouPou (PWA). Estratégia segura:
   - navegação (abrir o app): REDE primeiro, cai pro shell em cache quando offline;
   - estáticos do app (ícones/logos/manifest): cache primeiro (rápido, atualiza em 2º plano);
   - resto (API dinâmica: /cesta, /buscar, /mercados…): NÃO intercepta — vai direto pela rede
     (preços sempre frescos, sem quebrar auth). Também nunca mexe em origens externas (Supabase/CDN). */
const CACHE = "poupou-v3";
const SHELL = ["/", "/manifest.webmanifest", "/icons/icon-v2-192.png", "/icons/icon-v2-512.png"];

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
    e.respondWith(fetch(req).catch(() => caches.match("/")));
    return;
  }
  if (/^\/(icons|logos)\//.test(url.pathname) || url.pathname === "/manifest.webmanifest") {
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
