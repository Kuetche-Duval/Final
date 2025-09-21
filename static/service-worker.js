// static/service-worker.js
// Robust service worker for caching + push notifications.
// Ensure this file is served at /service-worker.js (registered with scope '/')

const CACHE_NAME = 'newtons-nerds-cache-v2';
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
      .catch(e => console.error('[sw] cache install failed', e))
  );
  console.log('[sw] install');
});

// activate: remove old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.map(k => (k !== CACHE_NAME) ? caches.delete(k) : Promise.resolve())
    ))
  );
  self.clients.claim();
  console.log('[sw] activate');
});

// fetch: network-first for navigation to get updates quickly, cache-first for same-origin assets
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  if (req.method !== 'GET') return;

  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).then(resp => {
        try {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then(c => c.put(req, copy)).catch(()=>{});
        } catch (e) {}
        return resp;
      }).catch(() => caches.match(req).then(r => r || caches.match('/')))
    );
    return;
  }

  if (url.origin === location.origin) {
    event.respondWith(
      caches.match(req).then(cached => cached || fetch(req).then(resp => {
        try {
          const respClone = resp.clone();
          caches.open(CACHE_NAME).then(c => c.put(req, respClone)).catch(()=>{});
        } catch (e) {}
        return resp;
      }).catch(() => caches.match('/')))
    );
  }
});

// Helper: parse push payload
async function _parsePushData(event) {
  if (!event.data) return null;
  try {
    return event.data.json();
  } catch (e) {
    try {
      const text = await event.data.text();
      return { title: 'New message', body: text };
    } catch (e2) {
      return null;
    }
  }
}

// push: show notification
self.addEventListener('push', event => {
  event.waitUntil((async () => {
    const payload = await _parsePushData(event) || {};
    const title = payload.title || payload.heading || 'Newton\'s Nerds';
    const body = payload.body || payload.message || '';
    const url = (payload.url || '/questions');
    const tag = payload.tag || undefined;
    const renotify = !!payload.renotify;

    const icon = payload.icon || '/static/img/logo.png';
    const badge = payload.badge || '/static/img/logo.png';

    const options = {
      body,
      icon,
      badge,
      tag,
      renotify,
      data: { url, payload },
    };

    try {
      await self.registration.showNotification(title, options);
      console.log('[sw] showNotification', title, options);
    } catch (err) {
      console.error('[sw] showNotification failed', err);
    }
  })());
});

// notificationclick: focus or open client to the provided url
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification && event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      for (const client of windowClients) {
        try {
          const clientUrl = new URL(client.url, location.origin);
          const targetUrl = new URL(url, location.origin);
          if (clientUrl.pathname === targetUrl.pathname) {
            return client.focus();
          }
        } catch (e) { /* ignore */ }
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});

// optional notificationclose
self.addEventListener('notificationclose', event => {
  // could post analytics
});

// handle subscriptionchange - notify clients so they can re-subscribe if needed
self.addEventListener('pushsubscriptionchange', function(event) {
  console.log('[sw] pushsubscriptionchange', event);
  event.waitUntil(
    self.clients.matchAll({ includeUncontrolled: true }).then(clientsArr => {
      for (const c of clientsArr) {
        try {
          c.postMessage({ type: 'SUBSCRIPTION_LOST' });
        } catch (e) {
          console.debug('[sw] could not message client', e);
        }
      }
    })
  );
});

// message handler for debugging
self.addEventListener('message', function(evt) {
  console.log('[sw] msg from page:', evt.data);
});
