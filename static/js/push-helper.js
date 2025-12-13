/* static/js/push-helper.js
   Tiny helper exposing functions:
     - registerServiceWorker()
     - getSubscription()
     - ensureSubscription(subject)  -> registers + saves subscription to server
     - unsubscribe(endpoint)
     - checkServerSubjects(endpoint)
   Works with endpoints:
     - GET /api/vapid
     - POST /save-subscription  (body { subscription, subject })
     - GET  /subscriptions/check?endpoint=...
     - POST /subscriptions/delete  (body { endpoint })
*/
export async function registerServiceWorker(swPath = '/service-worker.js') {
  if (!('serviceWorker' in navigator)) throw new Error('Service worker not supported');
  const reg = await navigator.serviceWorker.register(swPath, { scope: '/' });
  await navigator.serviceWorker.ready;
  return reg;
}

export async function getVapidPublicKey() {
  try {
    const r = await fetch('/api/vapid');
    if (!r.ok) return '';
    const j = await r.json();
    return j && j.publicKey ? j.publicKey : '';
  } catch (e) {
    return '';
  }
}

export function urlBase64ToUint8Array(base64String) {
  if (!base64String) return null;
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  const array = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) array[i] = rawData.charCodeAt(i);
  return array;
}

export async function getSubscription(reg = null) {
  try {
    const r = reg || await navigator.serviceWorker.ready;
    return await r.pushManager.getSubscription();
  } catch (e) {
    console.warn('getSubscription failed', e);
    return null;
  }
}

export async function subscribeUserToPush(subject = 'Other', reg = null, vapidPublicKey = '') {
  // returns server JSON or throws
  const swReg = reg || await navigator.serviceWorker.ready;
  let sub = await swReg.pushManager.getSubscription();
  if (!sub) {
    const key = vapidPublicKey || await getVapidPublicKey();
    const appServerKey = key ? urlBase64ToUint8Array(key) : null;
    sub = await swReg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: appServerKey });
  }
  const toSend = { subscription: (sub.toJSON ? sub.toJSON() : sub), subject };
  const res = await fetch('/save-subscription', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(toSend)
  });
  if (!res.ok) {
    throw new Error('Failed saving subscription on server');
  }
  const j = await res.json();
  return { server: j, subscription: sub };
}

export async function unsubscribeEndpoint(endpoint) {
  try {
    const res = await fetch('/subscriptions/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint })
    });
    return await res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function checkServerSubjects(endpoint) {
  try {
    const res = await fetch(`/subscriptions/check?endpoint=${encodeURIComponent(endpoint)}`);
    if (!res.ok) return [];
    const j = await res.json();
    return (j && j.subjects) ? j.subjects : [];
  } catch (e) {
    return [];
  }
}

// Helper to ask SW to fetch pending (optional)
export async function askSWToFetchPending(endpoint) {
  try {
    const reg = await navigator.serviceWorker.ready;
    if (navigator.serviceWorker.controller) {
      navigator.serviceWorker.controller.postMessage({ type: 'FETCH_PENDING', endpoint });
      return true;
    } else if (reg.active) {
      reg.active.postMessage({ type: 'FETCH_PENDING', endpoint });
      return true;
    }
    return false;
  } catch (e) {
    return false;
  }
}