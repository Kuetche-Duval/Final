
/* service-worker.js - robust push + fallback logic */
self.addEventListener('install', (ev) => {
  self.skipWaiting();
});
// Minimal service worker with offline fallback + push handling
const CACHE_NAME = 'newtons-nerds-v1';
const URLS_TO_CACHE = [
  '/',
  '/static/css/main.css',
  '/static/img/logo.png',
  '/questions'
];
self.addEventListener('activate', (ev) => {
  // take control asap
  ev.waitUntil(self.clients.claim());
});

// basic fetch handler (cache strategies can be added)
self.addEventListener('fetch', (ev) => {
  // default: let network handle it
});

// Listen for push events — showNotification with payload if present.
self.addEventListener('push', async (ev) => {
  try {
    let data = {};
    if (ev.data) {
      try { data = ev.data.json(); } catch (e) { data = { body: ev.data.text() }; }
    }
    const title = data.title || 'Newton\'s Nerds';
    const body = data.body || (data.preview && (data.preview.question || data.preview.attempt)) || 'New activity';
    const tag = data.tag || undefined;
    const url = (data.url && (typeof data.url === 'string')) ? data.url : '/questions';

    const options = {
      body: body,
      tag: tag,
      renotify: data.renotify === true,
      data: { url: url, payload: data },
      // add icon/badge if you have them in /static/img/
      icon: '/static/img/icon-192.png',
      badge: '/static/img/icon-192.png'
    };

    ev.waitUntil(self.registration.showNotification(title, options));
  } catch (err) {
    console.error('SW push handler error', err);
  }
});

// notification click
self.addEventListener('notificationclick', (ev) => {
  ev.notification.close();
  const dest = (ev.notification.data && ev.notification.data.url) || '/';
  ev.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(ws => {
    const first = ws.find(w => (new URL(w.url)).pathname === (new URL(dest, location).pathname));
    if (first) return first.focus();
    return clients.openWindow(dest);
  }));
});

// handle subscription change (best-effort)
self.addEventListener('pushsubscriptionchange', (ev) => {
  // Best effort: notify client pages to re-subscribe
  ev.waitUntil(clients.matchAll({ includeUncontrolled: true }).then(clients => {
    clients.forEach(c => {
      try { c.postMessage({ type: 'SUBSCRIPTION_LOST' }); } catch(e) {}
    });
  }));
});

=======
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(URLS_TO_CACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.map(k => {
      if(k !== CACHE_NAME) return caches.delete(k);
    }))).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  // simple cache-first for app shell, else network fallback
  event.respondWith(
    caches.match(event.request).then((r) => r || fetch(event.request).catch(() => caches.match('/')))
  );
});

// Push event: show notification and keep payload for client to fetch
self.addEventListener('push', (event) => {
  let data = { title: 'Newton\'s Nerds', body: 'You have a new message', url: '/' };
  try {
    if(event.data) data = event.data.json();
  } catch (e) {
    try { data = JSON.parse(event.data.text()); } catch (e) {}
  }

  const title = data.title || 'Newton\'s Nerds';
  const options = {
    body: data.body || '',
    data: { url: data.url || '/' },
    badge: '/static/img/logo.png',
    icon: '/static/img/logo.png'
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      for (let client of windowClients) {
        if (client.url === url && 'focus' in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});

// Optional: message handler so client can ask SW to fetch pending notifications
self.addEventListener('message', (event) => {
  if (!event.data) return;
  if (event.data.type === 'PING') {
    event.source.postMessage({ type: 'PONG' });
  }
});