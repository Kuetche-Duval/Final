// static/service-worker.js
self.addEventListener('install', event => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/\-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  return Uint8Array.from([...raw].map(ch => ch.charCodeAt(0)));
}

self.addEventListener('push', event => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    try { data = { body: event.data ? event.data.text() : '' }; } catch (e2) { data = { title: 'Notification', body: 'You have a message' }; }
  }
  const title = data.title || 'New notification';
  const options = {
    body: data.body || '',
    icon: '/static/img/icon-192.png',
    badge: '/static/img/icon-192.png',
    data: data.url || '/',
    tag: data.tag || undefined,
    renotify: !!data.renotify
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = event.notification.data || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      for (let client of windowClients) {
        if (client.url === url && 'focus' in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});

self.addEventListener('pushsubscriptionchange', event => {
  event.waitUntil((async () => {
    try {
      const r = await fetch('/api/vapid');
      const j = await r.json();
      const publicKey = j && j.publicKey;
      if (!publicKey) return console.warn('No public key returned, cannot re-subscribe');
      const applicationServerKey = urlBase64ToUint8Array(publicKey);
      const newSub = await self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey
      });
      await fetch('/save-subscription', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subject: 'Other', subscription: newSub.toJSON() })
      }).catch(e => console.error('Failed to post new subscription to server', e));
    } catch (e) {
      console.error('pushsubscriptionchange handler failed', e);
    }
  })());
});
