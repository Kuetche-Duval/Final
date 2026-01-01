/* static/js/push-helper.js
   Lightweight helper for registering service worker and subscribing to Push API.
   Usage (from any page):
     await nnPush.init();                 // registers SW (if not registered)
     const sub = await nnPush.subscribe(); // opens permission prompt and saves subscription to server
     await nnPush.unsubscribe();          // unsubscribes locally and optionally tells server (not implemented server-side)
*/

(function(global){
  const API_VAPID = '/api/vapid';
  const SAVE_SUB = '/save-subscription';
  const SERVICE_WORKER_URL = '/service-worker.js';

  function _log(...args){ try{ console.debug('[nnPush]', ...args); } catch(e){} }

  function getCookie(name) {
    const v = "; " + document.cookie;
    const parts = v.split("; " + name + "=");
    if (parts.length === 2) return decodeURIComponent(parts.pop().split(";").shift());
    return null;
  }

  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) outputArray[i] = rawData.charCodeAt(i);
    return outputArray;
  }

  async function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) throw new Error('service-worker-not-supported');
    const reg = await navigator.serviceWorker.getRegistration();
    if (reg) return reg;
    return navigator.serviceWorker.register(SERVICE_WORKER_URL);
  }

  async function getVapidKey() {
    const r = await fetch(API_VAPID);
    if (!r.ok) throw new Error('vapid-fetch-failed');
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'vapid-missing');
    return j.publicKey;
  }

  async function currentSubscription() {
    try {
      const reg = await registerServiceWorker();
      if (!reg || !reg.pushManager) return null;
      return await reg.pushManager.getSubscription();
    } catch (e) {
      return null;
    }
  }

  async function subscribeToPush({ subject = 'Other', client_id = '' } = {}) {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      throw new Error('push-not-supported');
    }
    const reg = await registerServiceWorker();
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') throw new Error('permission-not-granted');

    const vapid = await getVapidKey();
    const applicationServerKey = urlBase64ToUint8Array(vapid);

    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey });
    // Save to server
    const res = await fetch(SAVE_SUB, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ subscription: sub, subject, client_id })
    });
    const json = await res.json();
    return { ok: res.ok, saved: json, subscription: sub };
  }

  async function unsubscribeLocal() {
    const sub = await currentSubscription();
    if (!sub) return { ok: true, message: 'no-subscription' };
    try {
      const unsub = await sub.unsubscribe();
      return { ok: true, unsubscribed: unsub };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  }

  // small helper: ensure nn_subscription cookie is set (server also sets it on save)
  function setEndpointCookie(endpoint) {
    if (!endpoint) return;
    const days = 365;
    const d = new Date(); d.setTime(d.getTime() + days*24*60*60*1000);
    document.cookie = 'nn_subscription=' + encodeURIComponent(endpoint) + ';path=/;max-age=' + (days*24*60*60);
  }

  // simple init that registers SW and returns current subscription (if any)
  async function init() {
    try {
      const reg = await registerServiceWorker();
      _log('service worker registered: ', reg.scope);
      const sub = await (reg && reg.pushManager ? reg.pushManager.getSubscription() : null);
      if (sub && sub.endpoint) setEndpointCookie(sub.endpoint);
      return { registered: true, subscription: sub };
    } catch (e) {
      _log('init failed', e);
      return { registered: false, error: String(e) };
    }
  }

  // Expose API
  global.nnPush = {
    init,
    registerServiceWorker,
    getVapidKey,
    subscribe: async (opts) => {
      const res = await subscribeToPush(opts || { subject: (getCookie('nn_last_category') || 'Other'), client_id: (getCookie('nn_user')||'') });
      try { if (res.subscription && res.subscription.endpoint) setEndpointCookie(res.subscription.endpoint); } catch(e){}
      return res;
    },
    currentSubscription,
    unsubscribe: unsubscribeLocal,
    setEndpointCookie
  };

  // auto-init (non-blocking)
  (async () => {
    try { await init(); } catch(e){ /* ignore */ }
  })();

})(window);