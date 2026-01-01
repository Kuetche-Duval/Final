// Minimal, safe service worker for Newton's Nerds
// - Handles basic push events and displays notifications
// - Avoids crashing when fetch handlers fail; always respond with a Response object.

self.addEventListener('install', (ev) => {
  self.skipWaiting();
});

self.addEventListener('activate', (ev) => {
  clients.claim();
});

// Very small push handler: show notification with data
self.addEventListener('push', function(event) {
  try {
    const payload = event.data ? event.data.json() : {};
    const title = payload.title || 'Newton\'s Nerds';
    const options = {
      body: payload.body || '',
      icon: payload.icon || '/static/img/icon-192.png',
      badge: payload.badge || '/static/img/icon-192.png',
      data: payload || {}
    };
    event.waitUntil(self.registration.showNotification(title, options));
  } catch (e) {
    // fallback: show generic notification
    event.waitUntil(self.registration.showNotification('Newton\'s Nerds', { body: 'You have a new message.' }));
  }
});

// Keep fetch handler defensive: never call respondWith with something that's not a Response
self.addEventListener('fetch', function(event) {
  const url = new URL(event.request.url);
  // Let navigation and site assets go through normally; for specific API endpoints we proxy safely
  if (url.pathname.startsWith('/notifications/pending')) {
    // Try to forward request; respond with a JSON 503 on failure to avoid TypeError in clients
    event.respondWith((async () => {
      try {
        const resp = await fetch(event.request);
        // ensure we return a proper Response
        return resp;
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: 'network-error' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' }
        });
      }
    })());
    return;
  }
  // default: let the request go to network (no caching here)
  // We do not call event.respondWith() for ordinary requests to keep things simple.
});
self.addEventListener('message', (ev) => {
  if(!ev.data) return;
  if (ev.data.type === 'show-notification') {
    const title = ev.data.title || 'Notification';
    self.registration.showNotification(title, { body: ev.data.body || '', data: ev.data.data || {} });
  }
});

self.addEventListener('push', (ev) => {
  try {
    const data = ev.data ? ev.data.json() : {};
    const title = data.title || 'New message';
    const opts = { body: data.body || '', data: data };
    ev.waitUntil(self.registration.showNotification(title, opts));
  } catch (e) {
    console.error('push event error', e);
  }
});