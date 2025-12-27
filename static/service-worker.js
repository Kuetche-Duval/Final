// static/service-worker.js
const CACHE_NAME = 'nn-shell-v1';
const PRECACHE_URLS = [
  '/', 
  '/static/css/main.css',
  '/static/img/icon-192.png',
  '/static/img/icon-512.png',
  '/static/img/logo.png',
  '/static/js/push-helper.js',
  '/static/js/notifications.js'
];

// Install: cache app shell
self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(PRECACHE_URLS).catch(err => {
        console.warn('Some assets failed to cache on install', err);
      });
    })
  );
});

// Activate: cleanup old caches
self.addEventListener('activate', event => {
  const expectedCaches = [CACHE_NAME];
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.map(key => {
          if (!expectedCaches.includes(key)) {
            return caches.delete(key);
          }
        })
      )
    ).then(() => self.clients.claim())
  );
});

// Fetch: cache-first for shell, network-first for api, fallback to cache
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // Ignore non-GET
  if (req.method !== 'GET') return;

  // API network-first (for e.g. /notifications/pending)
  if (url.origin === location.origin && url.pathname.startsWith('/notifications')) {
    event.respondWith(
      fetch(req).then(resp => {
        // optionally cache a copy of responses
        return resp;
      }).catch(() => caches.match(req))
    );
    return;
  }

  // For all precached urls, cache-first
  event.respondWith(
    caches.match(req).then(cached => {
      if (cached) return cached;
      return fetch(req).then(networkResp => {
        // put a copy in runtime cache (optional)
        return caches.open(CACHE_NAME).then(cache => {
          try {
            cache.put(req, networkResp.clone());
          } catch (e) {
            // ignore opaque responses or failures
          }
          return networkResp;
        });
      }).catch(() => {
        // fallback HTML for navigation requests
        if (req.mode === 'navigate') {
          return caches.match('/');
        }
      });
    })
  );
});

// Push event: show notification
self.addEventListener('push', event => {
  try {
    const data = event.data ? event.data.json() : {};
    const title = data.title || 'Notification';
    const body = data.body || '';
    const icon = '/static/img/icon-192.png';
    const badge = '/static/img/icon-192.png';
    const url = data.url || '/';
    const options = {
      body,
      icon,
      badge,
      data: { url, raw: data },
      renotify: data.renotify || false,
      tag: data.tag || undefined
    };
    event.waitUntil(self.registration.showNotification(title, options));
  } catch (err) {
    console.error('push event error', err);
  }
});

// Notification click: navigate or focus client
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = event.notification.data && event.notification.data.url ? event.notification.data.url : '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      for (let client of windowClients) {
        // focus an open client
        if (client.url === url && 'focus' in client) {
          return client.focus();
        }
      }
      // otherwise open new window/tab
      if (clients.openWindow) {
        return clients.openWindow(url);
      }
    })
  );
});

// Push subscription change: notify clients to re-subscribe
self.addEventListener('pushsubscriptionchange', event => {
  console.warn('[sw] pushsubscriptionchange event', event);
  // Let pages know they should re-subscribe (we don't have the VAPID key here)
  event.waitUntil(
    self.clients.matchAll({ includeUncontrolled: true, type: 'window' }).then(clients => {
      for (const client of clients) {
        client.postMessage({ type: 'RE_SUBSCRIBE' });
      }
    })
  );
});

// Listen to messages from clients (optional)
self.addEventListener('message', (event) => {
  // small ping handler or cache-clearing commands if needed
  const data = event.data || {};
  if (data && data.type === 'CLEAR_RUNTIME_CACHE') {
    caches.open(CACHE_NAME).then(cache => cache.keys().then(keys => {
      return Promise.all(keys.map(k => cache.delete(k)));
    }));
  }
});
