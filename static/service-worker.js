// Minimal service worker with offline fallback + push handling
const CACHE_NAME = 'newtons-nerds-v1';
const URLS_TO_CACHE = [
  '/',
  '/static/css/main.css',
  '/static/img/logo.png',
  '/questions'
];

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