/* static/service-worker.js
   Simple service worker to show push notifications and respond to clicks.
   - Handles 'push' events (payload should be JSON string)
   - Handles 'notificationclick' to open / focus the app window
   - Supports messages from the page to fetch pending notifications and show them
*/

self.addEventListener('install', (evt) => {
  // Activate immediately so registration becomes active without reload
  self.skipWaiting();
});

self.addEventListener('activate', (evt) => {
  evt.waitUntil(clients.claim());
});

function safeParse(data) {
  try { return (typeof data === 'string') ? JSON.parse(data) : data; } catch(e) { return null; }
}

// Utility to show a notification (used by push and by page-requested display)
async function showNotif(payload) {
  try {
    const title = payload.title || 'Newton\'s Nerds';
    const options = {
      body: payload.body || '',
      icon: payload.icon || '/static/img/icon-192.png',
      badge: payload.badge || '/static/img/icon-192.png',
      data: payload.data || payload // keep entire payload for click handling
    };
    await self.registration.showNotification(title, options);
  } catch (err) {
    // swallow
  }
}

// Handle incoming push messages
self.addEventListener('push', (evt) => {
  let payload = null;
  if (evt.data) {
    payload = safeParse(evt.data.text());
  }
  // If no structured payload, try to create a simple one
  if (!payload) payload = { title: 'Notification', body: evt.data ? evt.data.text() : 'You have a new message.' };

  // Show notification
  evt.waitUntil((async () => {
    await showNotif(payload);

    // attempt to notify clients (pages) via postMessage
    try {
      const all = await clients.matchAll({ includeUncontrolled: true, type: 'window' });
      for (const c of all) {
        c.postMessage({ type: 'push-received', payload });
      }
    } catch (e) {}
  })());
});

self.addEventListener('notificationclick', (evt) => {
  const data = evt.notification && evt.notification.data ? evt.notification.data : {};
  evt.notification.close();
  const urlToOpen = (data && data.url) ? (new URL(data.url, self.location.origin)).href : '/';
  evt.waitUntil((async () => {
    // Try to focus an existing client
    const all = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      const url = new URL(c.url);
      if (url.origin === self.location.origin) {
        // focus and (optionally) navigate
        c.focus();
        try { c.postMessage({ type: 'notification-click', data }); } catch (e) {}
        return;
      }
    }
    // otherwise open a new window/tab
    await clients.openWindow(urlToOpen);
  })());
});

// Respond to messages from page
self.addEventListener('message', (evt) => {
  const data = evt.data || {};
  (async () => {
    if (!data || !data.type) return;
    if (data.type === 'show-notification') {
      await showNotif(data.payload || {});
      return;
    }
    if (data.type === 'fetch-and-show-pending') {
      // page triggered fetch-and-show-pending: the page itself will fetch /notifications/pending
      // we keep service worker lightweight: prefer page to call /notifications/pending and then ask SW to show
      return;
    }
  })();
});