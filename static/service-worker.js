// Version hochbumpen damit Mobile-Caches mit altem Stand invalidiert
// werden (PWAs auf iOS/Android cachen sonst sehr aggressiv).
const CACHE_NAME = 'camp-doku-shell-v3';
const SHELL_ASSETS = [
  '/static/style.css',
  '/static/favicon.png',
  '/static/manifest.webmanifest'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  // Alle alten Caches komplett löschen — sicherer Reset bei jeder neuen Version
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
    )).then(() => self.clients.claim())
  );
});

// Network-first für /static/: damit der Server immer die aktuelle
// Version liefert, Cache nur als Fallback (Offline).
self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  // Nur Same-Origin /static/* — alles andere (Login, App-Pages, APIs)
  // unangetastet durch den Browser handeln lassen.
  if (url.origin !== location.origin || !url.pathname.startsWith('/static/')) {
    return;
  }
  event.respondWith(
    fetch(request).then(response => {
      // Erfolgreich vom Server: cachen und ausliefern
      if (response && response.status === 200) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(request, copy))
          .catch(() => {});
      }
      return response;
    }).catch(() => caches.match(request))
  );
});
