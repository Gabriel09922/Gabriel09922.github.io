// Agroshow: guarda os arquivos do app para abrir rápido e recebe os avisos de preço.
const CACHE = "agroshow-v2";
const ARQUIVOS = ["./", "index.html", "firebase-config.js", "manifest.webmanifest", "icon-192.png", "icon-512.png"];

try {
  importScripts("https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js",
                "https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js",
                "firebase-config.js");
  if (self.FIREBASE_CONFIG && !/COLE/.test(self.FIREBASE_CONFIG.apiKey)) {
    firebase.initializeApp(self.FIREBASE_CONFIG);
    firebase.messaging(); // mostra sozinho os avisos que chegam com o app fechado
  }
} catch (e) { /* sem internet na instalação: o app funciona, só sem avisos até a próxima abertura */ }

self.addEventListener("install", (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ARQUIVOS)).then(() => self.skipWaiting())); });
self.addEventListener("activate", (e) => { e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window" }).then((cs) => cs.length ? cs[0].focus() : self.clients.openWindow("./")));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  e.respondWith(fetch(e.request).then((r) => { const copia = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copia)); return r; }).catch(() => caches.match(e.request).then((r) => r || caches.match("index.html"))));
});
