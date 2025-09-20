// static/service-worker.js
// Minimal, practical service worker for caching + push notifications.
//
// Notes:
// - This worker expects push event data to be JSON (title, body, url, tag, renotify, icon).
// - If your push sender sends plain text, the code will try to parse it as JSON and fall back to text.
//
// Keep this file under your project's static folder so templates can register it with:
// navigator.serviceWorker.register('{{ url_for("static", filename="service-worker.js") }}')

const CACHE_NAME = 'newtons-nerds-cache-v1';
const URLS_TO_CACHE = [
  '/',
  '/questions',
  '/coding',
  '/health',
  '/static/manifest.json',
  '/static/img/icon-192.png',
  '/static/img/icon-512.png'
];

// install: cache core assets
self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(URLS_TO_CACHE).catch(() => {/* ignore failures */}))
  );
});

// activate: remove old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.map(k => (k !== CACHE_NAME) ? caches.delete(k) : Promise.resolve())
    ))
  );
  self.clients.claim();
});

// fetch: cache-first for navigation & same-origin requests, network fallback otherwise
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // only handle GET requests (avoid interfering with POSTs)
  if (req.method !== 'GET') return;

  // For navigation requests, prefer network but fallback to cache (so page updates when online)
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).then(resp => {
        // put a copy in cache for offline fallback
        const copy = resp.clone();
        caches.open(CACHE_NAME).then(c => c.put(req, copy)).catch(()=>{});
        return resp;
      }).catch(() => caches.match(req).then(r => r || caches.match('/')))
    );
    return;
  }

  // For same-origin assets, try cache first, then network
  if (url.origin === location.origin) {
    event.respondWith(
      caches.match(req).then(cached => cached || fetch(req).then(resp => {
        // store in cache for next time
        try {
          const respClone = resp.clone();
          caches.open(CACHE_NAME).then(c => c.put(req, respClone)).catch(()=>{});
        } catch (e) {}
        return resp;
      }).catch(() => caches.match('/')))
    );
  }
  // else: let the browser handle cross-origin requests (e.g., cdn)
});

// Helper: parse push payload
async function _parsePushData(event) {
  if (!event.data) return null;
  try {
    // try JSON first
    return event.data.json();
  } catch (e) {
    try {
      // fallback to text -> treat as body
      const text = event.data.text();
      return { title: 'New message', body: await text };
    } catch (e2) {
      return null;
    }
  }
}

// push: show notification
self.addEventListener('push', event => {
  event.waitUntil((async () => {
    const payload = await _parsePushData(event) || {};
    const title = payload.title || payload.heading || 'Notification';
    const body = payload.body || payload.message || '';
    const url = (payload.url || '/');
    const tag = payload.tag || undefined;
    const renotify = !!payload.renotify;

    const icon = payload.icon || '/static/img/icon-192.png';
    const badge = payload.badge || '/static/img/icon-192.png';

    const options = {
      body,
      icon,
      badge,
      tag,
      renotify,
      data: {
        url,
        payload
      },
      // you can add actions here if you want (example commented)
      // actions: [{action: 'open', title: 'Open'}]
    };

    try {
      await self.registration.showNotification(title, options);
    } catch (err) {
      // fall back to postMessage (some browsers)
      console.error('showNotification failed', err);
    }
  })());
});

// notificationclick: focus or open client to the provided url
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification && event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      // Try to focus an existing open window/tab with same origin
      for (let i = 0; i < windowClients.length; i++) {
        const client = windowClients[i];
        try {
          const u = new URL(client.url);
          if (u.pathname === new URL(url, location.origin).pathname) {
            return client.focus();
          }
        } catch (e) {}
      }
      // Otherwise open a new window/tab
      return clients.openWindow(url);
    })
  );
});

// notificationclose: currently no-op, but could be used for analytics
self.addEventListener('notificationclose', event => {
  // event.notification.data may contain useful info
});
