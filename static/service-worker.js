/* static/service-worker.js */
self.addEventListener('install', (ev) => {
  self.skipWaiting();
});

self.addEventListener('activate', (ev) => {
  self.clients.claim();
});

self.addEventListener('push', function(event) {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    try {
      payload = { title: 'Notification', body: event.data ? event.data.text() : 'New message' };
    } catch (err) {
      payload = { title: 'Notification', body: 'New message' };
    }
  }

  const title = payload.title || 'Notification';
  const options = {
    body: payload.body || '',
    tag: payload.tag || undefined,
    data: payload,
    renotify: !!payload.renotify,
    requireInteraction: false
  };

  event.waitUntil(
    self.registration.showNotification(title, options).then(() => {
      return self.clients.matchAll({ includeUncontrolled: true, type: 'window' }).then(clients => {
        for (const c of clients) {
          try { c.postMessage({ type: 'PUSH_RECEIVED', payload }); } catch(e) {}
        }
      });
    })
  );
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  let url = '/questions';
  if (event.notification.data && event.notification.data.url) url = event.notification.data.url;
  event.waitUntil(
    self.clients.matchAll({ type: 'window' }).then(function(clientList) {
      for (let i = 0; i < clientList.length; i++) {
        const client = clientList[i];
        if ('focus' in client) {
          client.focus();
          try { client.postMessage({ type: 'NOTIFICATION_CLICKED', payload: event.notification.data || {} }); } catch(e){}
          return client.navigate(url);
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});