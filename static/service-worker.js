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