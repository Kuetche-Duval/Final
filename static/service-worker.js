// static/service-worker.js

self.addEventListener('install', event => {
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  clients.claim();
});

self.addEventListener('push', event => {
  let payload = { title: 'New notification', body: '', icon: '/static/icons/icon-192.svg', data: { url: '/' } };
  try {
    if (event.data) {
      payload = event.data.json();
      // ensure minimal fields
      payload.title = payload.title || 'Notification';
      payload.body = payload.body || '';
      payload.icon = payload.icon || '/static/icons/icon-192.svg';
      payload.data = payload.data || { url: '/' };
    }
  } catch (e) {
    try {
      payload.body = event.data.text();
    } catch (err) {}
  }

  const options = {
    body: payload.body,
    icon: payload.icon,
    badge: payload.badge || payload.icon,
    data: payload.data,
    tag: payload.tag || undefined,
    renotify: payload.renotify || false,
    requireInteraction: payload.requireInteraction || false
  };

  event.waitUntil(self.registration.showNotification(payload.title, options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      for (let client of windowClients) {
        if (client.url === url && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(url);
      }
    })
  );
});