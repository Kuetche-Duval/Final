// Minimal, safe service-worker for Newton's Nerds
// Place at static/service-worker.js and serve at "/service-worker.js".

const CACHE_NAME = 'nn-static-v1';
const OFFLINE_HTML = '/offline.html'; // optional offline fallback (you can add a file)

self.addEventListener('install', (event) => {
  self.skipWaiting();
  // Optionally pre-cache assets:
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      // add essentials if you want, e.g. '/', '/static/css/main.css'
      return cache.addAll([
        // '/offline.html'
      ].filter(Boolean));
    })
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  // Always let non-HTTP(S) requests through
  if (!req.url.startsWith('http')) return;

  const url = new URL(req.url);

  // Bypass service worker for admin/api/notification endpoints and service-worker itself
  if (url.pathname === '/service-worker.js' ||
      url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/notifications') ||
      url.pathname.startsWith('/save-subscription') ||
      url.pathname.startsWith('/exercise-table') ) {
    event.respondWith(fetch(req));
    return;
  }

  // Don't interfere with POST/PUT/DELETE — just fetch them
  if (req.method !== 'GET') {
    event.respondWith(fetch(req));
    return;
  }

  // For navigation or GET resources, try network first then fallback to cache
  event.respondWith(
    fetch(req).then((networkResponse) => {
      // Ensure we only cache successful responses (optional)
      try {
        if (networkResponse && networkResponse.ok) {
          const clone = networkResponse.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(req, clone)).catch(()=>{});
        }
      } catch (e) { /* ignore */ }
      return networkResponse;
    }).catch(() => {
      // On network failure, try cache
      return caches.match(req).then(cached => {
        if (cached) return cached;
        // optionally return an offline fallback for navigations
        return caches.match(OFFLINE_HTML).then(fallback => fallback || new Response('Offline', {status: 503, statusText: 'Offline'}));
      });
    })
  );
});

// (Optional) push handler (safe no-op if not configured)
self.addEventListener('push', (event) => {
  try {
    const data = event.data ? event.data.json() : { title: 'Newton\'s Nerds', body: 'New notification' };
    const title = data.title || 'Newton\'s Nerds';
    const options = {
      body: data.body || '',
      icon: '/static/icons/icon-192.svg',
      badge: '/static/icons/icon-192.svg',
      data: data.url || '/'
    };
    event.waitUntil(self.registration.showNotification(title, options));
  } catch (e) {
    // swallow errors in SW context
  }
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data || '/';
  event.waitUntil(clients.matchAll({type:'window'}).then(clientList => {
    for (const c of clientList) {
      if (c.url === url && 'focus' in c) return c.focus();
    }
    if (clients.openWindow) return clients.openWindow(url);
  }));
});
