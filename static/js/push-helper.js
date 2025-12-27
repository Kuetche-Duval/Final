// static/js/push-helper.js
// Responsible for: registering service worker, subscribing for push with VAPID key,
// sending subscription to server (/save-subscription), and utility helpers.
//
// Usage (example):
//   import or include this script in your page. Then call:
//     pushHelper.initPush({ subject: "Mathematics", clientId: "user-123" });

const pushHelper = (function () {
  const SW_PATH = '/service-worker.js';
  const VAPID_ENDPOINT = '/api/vapid';
  const SAVE_SUB_ENDPOINT = '/save-subscription';
  const SUBSCRIBE_OPTIONS = { userVisibleOnly: true };

  // Helper: base64 url -> Uint8Array (for vapid key)
  function urlBase64ToUint8Array(base64String) {
    // Pad base64 string
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  // Register service worker if supported
  async function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) {
      console.warn('Service workers unsupported in this browser.');
      return null;
    }
    try {
      const reg = await navigator.serviceWorker.register(SW_PATH, { scope: '/' });
      console.info('Service worker registered:', reg);
      return reg;
    } catch (err) {
      console.error('Service worker registration failed:', err);
      return null;
    }
  }

  // Get VAPID public key from server
  async function fetchVapidPublicKey() {
    try {
      const resp = await fetch(VAPID_ENDPOINT, { method: 'GET', credentials: 'same-origin' });
      if (!resp.ok) throw new Error(`Failed to fetch VAPID key: ${resp.status}`);
      const j = await resp.json();
      if (!j.ok || !j.publicKey) throw new Error('VAPID public key missing in response');
      return j.publicKey;
    } catch (err) {
      console.error('Error fetching VAPID key:', err);
      return null;
    }
  }

  // Subscribe to push with VAPID key
  async function subscribeToPush(registration, vapidPublicKey) {
    if (!registration || !registration.pushManager) {
      throw new Error('Invalid service worker registration for push manager.');
    }
    // request permission first
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      throw new Error('Notification permission not granted.');
    }

    const sub = await registration.pushManager.subscribe({
      ...SUBSCRIBE_OPTIONS,
      applicationServerKey: urlBase64ToUint8Array(vapidPublicKey)
    });

    return sub;
  }

  // Send subscription object to your server endpoint
  async function saveSubscriptionToServer(subscription, subject = 'Other', clientId = '') {
    try {
      const payload = { subscription, subject, client_id: clientId };
      const resp = await fetch(SAVE_SUB_ENDPOINT, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!resp.ok) {
        const txt = await resp.text();
        console.warn('save-subscription returned non-ok:', resp.status, txt);
      }
      return resp;
    } catch (err) {
      console.error('Failed saving subscription to server:', err);
      throw err;
    }
  }

  // Public init function: registers SW, subscribes, saves
  async function initPush(opts = {}) {
    const { subject = 'Other', clientId = '' } = opts;
    if (!('serviceWorker' in navigator)) {
      console.warn('Push not supported (no serviceWorker).');
      return { ok: false, reason: 'no-service-worker' };
    }
    try {
      const reg = await registerServiceWorker();
      if (!reg) return { ok: false, reason: 'sw-register-failed' };

      const vapidKey = await fetchVapidPublicKey();
      if (!vapidKey) return { ok: false, reason: 'no-vapid' };

      // Check if already subscribed
      let existing = await reg.pushManager.getSubscription();
      if (existing) {
        console.info('Already subscribed to push:', existing.endpoint);
        await saveSubscriptionToServer(existing, subject, clientId);
        return { ok: true, subscription: existing };
      }

      const sub = await subscribeToPush(reg, vapidKey);
      await saveSubscriptionToServer(sub, subject, clientId);
      console.info('Push subscription successful.');
      return { ok: true, subscription: sub };
    } catch (err) {
      console.error('initPush error:', err);
      return { ok: false, error: String(err) };
    }
  }

  // Optional unsubscribe helper (local + ask server to remove)
  async function unsubscribe() {
    if (!('serviceWorker' in navigator)) return { ok: false, reason: 'no-sw' };
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (!sub) return { ok: true, message: 'no-subscription' };
    try {
      // ask server to remove (best-effort)
      await fetch('/notifications/unsubscribe', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: sub.endpoint })
      }).catch(() => {});
      const ok = await sub.unsubscribe();
      return { ok };
    } catch (err) {
      return { ok: false, error: String(err) };
    }
  }

  // Expose a simple API
  return {
    initPush,
    unsubscribe,
    registerServiceWorker // exposed for advanced usage
  };
})();
