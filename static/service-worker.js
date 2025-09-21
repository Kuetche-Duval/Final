// static/service-worker.js
// Minimal, practical service worker for caching + push notifications.
// - sends message to clients on pushsubscriptionchange so page can react / re-subscribe
// - expects push event data to be JSON (title, body, url, tag, renotify, icon)
// - caches a small set of assets for offline fallback

const CACHE_NAME = 'newtons-nerds-cache-v1';
const URLS_TO_CACHE = [
  '/',
  '/questions',
  '/coding',
  '/health',
  '/static/manifest.json',
  '/static/img/icon-192.png',
  '/static/img/icon-512.png',
  '/static/img/logo.png'
];

// install: cache core assets
self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(URLS_TO_CACHE).catch(() => {/* ignore individual failures */}))
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

// fetch: network-first for navigation, cache-first for same-origin assets
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  if (req.method !== 'GET') return;

  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).then(resp => {
        // keep a copy
        const copy = resp.clone();
        caches.open(CACHE_NAME).then(c => c.put(req, copy)).catch(()=>{});
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

// helper: try parse push event data as JSON, fallback to text
async function _parsePushData(event) {
  if (!event.data) return null;
  try {
    return event.data.json();
  } catch (e) {
    try {
      const text = await event.data.text();
      return { title: 'Notification', body: text };
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

    const icon = payload.icon || '/static/img/logo.png';
    const badge = payload.badge || '/static/img/logo.png';

    const options = {
      body,
      icon,
      badge,
      tag,
      renotify,
      data: {
        url,
        payload
      }
    };

    try {
      await self.registration.showNotification(title, options);
    } catch (err) {
      // some browsers may restrict showNotification; still attempt to inform clients
      try {
        const all = await clients.matchAll({ includeUncontrolled: true });
        for (const c of all) {
          c.postMessage({ type: 'PUSH_RECEIVED', payload });
        }
      } catch (e) {}
    }
  })());
});

// notificationclick: focus or open client to the provided url
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification && event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      for (let i = 0; i < windowClients.length; i++) {
        const client = windowClients[i];
        try {
          const u = new URL(client.url);
          if (u.pathname === new URL(url, location.origin).pathname) {
            return client.focus();
          }
        } catch (e) {}
      }
      return clients.openWindow(url);
    })
  );
});

// notificationclose: post message (optional analytics hook)
self.addEventListener('notificationclose', event => {
  // placeholder: could postMessage to clients that user dismissed notification
});

// pushsubscriptionchange: notify clients so page can re-subscribe or show a hint
self.addEventListener('pushsubscriptionchange', event => {
  // Don't attempt to subscribe here (we lack applicationServerKey). Instead notify page(s).
  event.waitUntil((async () => {
    try {
      const all = await clients.matchAll({ includeUncontrolled: true });
      for (const c of all) {
        c.postMessage({ type: 'SUBSCRIPTION_LOST', reason: 'pushsubscriptionchange' });
      }
    } catch (e) {
      // ignore
    }
  })());
});
