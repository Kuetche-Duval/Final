/* service-worker.js - robust push + fallback logic */
self.addEventListener('install', (ev) => {
  self.skipWaiting();
});

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
