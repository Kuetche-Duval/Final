// service-worker.js (improved)
'use strict';

self.addEventListener('push', function(event) {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    try {
      payload = { title: "Newton's Nerds", body: event.data ? event.data.text() : 'You have a new notification' };
    } catch (e2) {
      payload = { title: "Newton's Nerds", body: 'You have a new notification' };
    }
  }

  const title = payload.title || "Newton's Nerds";
  const options = {
    body: payload.body || '',
    data: payload.data || { url: payload.url || '/' },
    tag: payload.tag || 'nn-notification',
    renotify: payload.renotify === true,
    timestamp: Date.now()
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

// When notification clicked, try to focus an existing client or open root
self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  const urlToOpen = (event.notification.data && event.notification.data.url) ? event.notification.data.url : '/';
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
    for (var i = 0; i < clientList.length; i++) {
      var client = clientList[i];
      if (client.url && 'focus' in client) {
        // focus if same origin and path is same root
        client.focus();
        client.postMessage({ type: 'NOTIFICATION_CLICKED', payload: event.notification.data || {} });
        return client.focus();
      }
    }
    if (clients.openWindow) {
      return clients.openWindow(urlToOpen);
    }
  }));
});

// If subscription changes, notify pages so they may re-subscribe or save new endpoint
self.addEventListener('pushsubscriptionchange', function(event) {
  console.warn('[sw] pushsubscriptionchange event', event);
  event.waitUntil((async () => {
    // Notify clients to handle resubscribe (page will fetch /vapid_public_key to resubscribe)
    const allClients = await clients.matchAll({ includeUncontrolled: true });
    for (const client of allClients) {
      client.postMessage({ type: 'SUBSCRIPTION_LOST' });
    }
  })());
});
