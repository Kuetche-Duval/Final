/* static/service-worker.js
   Robust service worker for push notifications + basic background fetch of pending notifications.
   - Handles push events (payload is JSON)
   - Shows notifications with actions
   - Notification click focuses/opens client
   - Notifies all clients about incoming push via postMessage
   - Handles pushsubscriptionchange event to ask client to resubscribe
*/
'use strict';

const DEFAULT_ICON = '/static/icons/icon-192.svg';
const DEFAULT_BADGE = '/static/icons/icon-192.svg';
const SW_VERSION = 'nn-sw-v1';

self.addEventListener('install', (ev) => {
  // Activate immediately
  ev.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (ev) => {
  ev.waitUntil((async () => {
    try { await self.clients.claim(); } catch (e) { /* ignore */ }
  })());
});

function _showNotificationFromPayload(payload = {}) {
  const title = payload.title || "Newton's Nerds";
  const body = payload.body || (payload.preview && (payload.preview.question || payload.preview.attempt)) || "";
  const url = payload.url || '/questions';
  const tag = payload.tag || ('nn-' + (payload.subject || 'general'));
  const icon = payload.icon || DEFAULT_ICON;
  const badge = payload.badge || DEFAULT_BADGE;
  const data = {
    url,
    payload,
    ts: Date.now()
  };
  const options = {
    body,
    icon,
    badge,
    tag,
    renotify: !!payload.renotify,
    data,
    actions: [
      { action: 'open', title: 'Open', icon: icon },
      { action: 'dismiss', title: 'Dismiss' }
    ]
  };
  return self.registration.showNotification(title, options);
}

self.addEventListener('push', (event) => {
  // event.data should contain JSON (server sends JSON string)
  event.waitUntil((async () => {
    try {
      let payload = {};
      if (event.data) {
        try { payload = event.data.json(); } catch (e) {
          try { payload = JSON.parse(event.data.text()); } catch (e2) { payload = { body: event.data.text() }; }
        }
      } else {
        payload = { title: "Notification", body: "" };
      }

      // Show notification
      await _showNotificationFromPayload(payload);

      // Broadcast to open clients that a push arrived (useful to refresh UI)
      try {
        const clientsList = await self.clients.matchAll({ includeUncontrolled: true, type: 'window' });
        for (const client of clientsList) {
          client.postMessage({ type: 'PUSH_RECEIVED', payload });
        }
      } catch (e) { /* ignore */ }

    } catch (err) {
      console.error('ServiceWorker push handling failed', err);
    }
  })());
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const data = (event.notification && event.notification.data) || {};
  const url = (data && data.url) || '/questions';
  event.waitUntil((async () => {
    try {
      const allClients = await self.clients.matchAll({ includeUncontrolled: true, type: 'window' });
      // Focus an open client if possible
      for (const client of allClients) {
        const clientUrl = new URL(client.url || location.origin);
        const targetUrl = new URL(url, location.origin);
        // If same origin, focus it
        if (clientUrl.origin === targetUrl.origin) {
          try {
            await client.focus();
            // Ask client to navigate (post message)
            client.postMessage({ type: 'NOTIFICATION_CLICK', url });
            return;
          } catch (e) {
            // fallback to opening a new window
          }
        }
      }
      // No client found, open new window/tab
      await self.clients.openWindow(url);
    } catch (e) {
      console.error('notificationclick handler error', e);
    }
  })());
});

self.addEventListener('notificationclose', (event) => {
  // allow client to mark unread/dismissed etc.
  event.waitUntil((async () => {
    try {
      const clientsList = await self.clients.matchAll({ includeUncontrolled: true, type: 'window' });
      for (const client of clientsList) {
        client.postMessage({ type: 'NOTIFICATION_CLOSED', payload: event.notification && event.notification.data });
      }
    } catch (e) { /* ignore */ }
  })());
});

// If subscription changes (browser rotated keys, etc.) tell clients to resubscribe.
self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil((async () => {
    try {
      const clientsList = await self.clients.matchAll({ includeUncontrolled: true, type: 'window' });
      for (const client of clientsList) {
        client.postMessage({ type: 'SUBSCRIPTION_CHANGE' });
      }
    } catch (e) { /* ignore */ }
  })());
});

// Basic message handler so pages can ask the SW to fetch pending notifications
self.addEventListener('message', (event) => {
  const data = event.data || {};
  (async () => {
    try {
      if (data && data.type === 'FETCH_PENDING') {
        // Pages should call /notifications/pending themselves; SW can't reliably include cookies in cross-origin
        // But we try a fetch same-origin with credentials (useful if your cookies are set properly)
        try {
          const endpoint = data.endpoint ? `?endpoint=${encodeURIComponent(data.endpoint)}` : '';
          const resp = await fetch(`/notifications/pending${endpoint}`, { credentials: 'same-origin' });
          if (resp && resp.ok) {
            const j = await resp.json();
            if (j && j.items && Array.isArray(j.items)) {
              for (const item of j.items) {
                try { await _showNotificationFromPayload(item.data ? item.data : { title: item.title, body: item.body }); } catch (e) {}
              }
            }
          }
        } catch (e) { /* ignore */ }
      }
    } catch (e) {
      console.error('SW message handler error', e);
    }
  })();
});