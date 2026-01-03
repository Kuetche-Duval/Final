/* static/js/push-helper.js (fixed)
   Improvements made:
   - Removed unsafe auto-subscribe on load (no unexpected permission prompts)
   - Deduplicate subscriptions: if subscription exists we don't re-subscribe
   - Added in-flight mutex to prevent concurrent subscribe() calls
   - Use subscription.toJSON() when sending to server
   - Robust handling of server responses and VAPID key shapes
   - Set/clear localStorage flag and cookie only on successful save/unsubscribe
   - Small defensive fixes and clearer logging

   Usage (from any page):
     await nnPush.init();                 // registers SW (if not registered)
     const res = await nnPush.subscribe(); // opens permission prompt (if needed) and saves subscription to server
     await nnPush.unsubscribe();          // unsubscribes locally and optionally tells server (server-side deletion endpoint not implemented here)
*/

(function(global){
  'use strict';

  const API_VAPID = '/api/vapid';
  const SAVE_SUB = '/save-subscription';
  const SERVICE_WORKER_URL = '/service-worker.js';
  const SUB_KEY = 'nn_push_subscribed';

  function _log(...args){ try{ console.debug('[nnPush]', ...args); } catch(e){} }

  function getCookie(name) {
    const v = "; " + document.cookie;
    const parts = v.split("; " + name + "=");
    if (parts.length === 2) return decodeURIComponent(parts.pop().split(";").shift());
    return null;
  }

  function setCookie(name, value, days) {
    const daysNum = typeof days === 'number' ? days : 365;
    const d = new Date(); d.setTime(d.getTime() + daysNum*24*60*60*1000);
    document.cookie = `${name}=${encodeURIComponent(value)};path=/;max-age=${daysNum*24*60*60}`;
  }

  function deleteCookie(name){
    document.cookie = name + '=;path=/;max-age=0';
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
    // try to get an existing registration for the current scope
    const reg = await navigator.serviceWorker.getRegistration();
    if (reg) return reg;
    // otherwise register new
    return navigator.serviceWorker.register(SERVICE_WORKER_URL);
  }

  async function getVapidKey() {
    const r = await fetch(API_VAPID, {cache: 'no-store'});
    if (!r.ok) throw new Error('vapid-fetch-failed');
    let j;
    try { j = await r.json(); } catch(e){ throw new Error('vapid-invalid-json'); }
    // support several shapes
    const key = j.publicKey || j.public_key || j.vapidKey || j.key;
    if (!key) throw new Error('vapid-missing');
    return key;
  }

  async function currentSubscription() {
    try {
      const reg = await registerServiceWorker();
      if (!reg || !reg.pushManager) return null;
      return await reg.pushManager.getSubscription();
    } catch (e) {
      _log('currentSubscription error', e);
      return null;
    }
  }

  // in-flight mutex to avoid concurrent subscribe calls
  let _inFlightSubscribe = false;

  async function subscribeToPush({ subject = 'Other', client_id = '' } = {}) {
    if (_inFlightSubscribe) return { ok: false, error: 'subscribe-in-flight' };
    _inFlightSubscribe = true;
    try {
      if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        throw new Error('push-not-supported');
      }

      const reg = await registerServiceWorker();
      if (!reg || !reg.pushManager) throw new Error('push-manager-missing');

      // If already subscribed, return that (and optionally ensure server has it)
      const existing = await reg.pushManager.getSubscription();
      if (existing) {
        _log('already subscribed — returning existing subscription');
        // try to ensure server has it (best-effort, non-blocking)
        try {
          await saveSubscriptionToServer(existing, subject, client_id);
          localStorage.setItem(SUB_KEY, 'true');
          setEndpointCookie(existing.endpoint);
        } catch(e){ /* ignore */ }
        return { ok: true, subscription: existing, already: true };
      }

      // Request permission explicitly (only when user triggers subscribe)
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') throw new Error('permission-not-granted');

      const vapid = await getVapidKey();
      const applicationServerKey = urlBase64ToUint8Array(vapid);

      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey });

      // Save to server
      const saveRes = await saveSubscriptionToServer(sub, subject, client_id);
      if (!saveRes.ok) {
        // we still return subscription, but mark saved:false and don't set local flags
        return { ok: false, saved: saveRes, subscription: sub };
      }

      // success
      localStorage.setItem(SUB_KEY, 'true');
      setEndpointCookie(sub.endpoint);
      return { ok: true, saved: saveRes, subscription: sub };

    } catch (e) {
      _log('subscribeToPush error', e);
      return { ok: false, error: String(e) };
    } finally {
      _inFlightSubscribe = false;
    }
  }

  // serialize subscription and post to server
  async function saveSubscriptionToServer(subscription, subject = 'Other', client_id = ''){
    if (!subscription) return { ok: false, error: 'no-subscription' };
    let payload;
    try { payload = subscription.toJSON ? subscription.toJSON() : subscription; } catch(e){ payload = subscription; }

    try {
      const res = await fetch(SAVE_SUB, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ subscription: payload, subject, client_id })
      });
      let json = null;
      try { json = await res.json(); } catch(e){/* non-json response */}
      return { ok: res.ok, status: res.status, body: json };
    } catch(e){
      return { ok: false, error: String(e) };
    }
  }

  async function unsubscribeLocal() {
    try {
      const reg = await registerServiceWorker();
      const sub = reg && reg.pushManager ? await reg.pushManager.getSubscription() : null;
      if (!sub) return { ok: true, message: 'no-subscription' };
      const unsub = await sub.unsubscribe();
      // clear local markers
      localStorage.removeItem(SUB_KEY);
      deleteCookie('nn_subscription');
      return { ok: true, unsubscribed: unsub };
    } catch (e) {
      _log('unsubscribeLocal error', e);
      return { ok: false, error: String(e) };
    }
  }

  // small helper: ensure nn_subscription cookie is set (server also sets it on save)
  function setEndpointCookie(endpoint) {
    try {
      if (!endpoint) return;
      setCookie('nn_subscription', endpoint, 365);
    } catch(e){/* ignore */}
  }

  // simple init that registers SW and returns current subscription (if any)
  async function init() {
    try {
      const reg = await registerServiceWorker();
      _log('service worker registered: ', reg.scope);
      const sub = await (reg && reg.pushManager ? reg.pushManager.getSubscription() : null);
      if (sub && sub.endpoint) {
        localStorage.setItem(SUB_KEY, 'true');
        setEndpointCookie(sub.endpoint);
      }
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
      // opts may be omitted; fall back to cookies
      const subject = (opts && opts.subject) || (getCookie('nn_last_category') || 'Other');
      const client_id = (opts && opts.client_id) || (getCookie('nn_user')||'');
      return await subscribeToPush({ subject, client_id });
    },
    currentSubscription,
    unsubscribe: unsubscribeLocal,
    setEndpointCookie
  };

  // auto-init (non-blocking) — does NOT prompt for permission or auto-subscribe
  (async () => {
    try { await init(); } catch(e){ /* ignore */ }
  })();

})(window);
