// notifications.js
//
// Helper: register service worker, subscribe/unsubscribe, fetch pending notifications, ack them.
// This works with the app endpoints: /save-subscription, /subscriptions/delete, /vapid_public_key, /notifications/pending, /notifications/ack

async function urlBase64ToUint8Array(base64String) {
    if (!base64String) return null;
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = window.atob(base64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; ++i) out[i] = raw.charCodeAt(i);
    return out;
  }
  
  async function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) {
      console.warn('Service workers not supported');
      return null;
    }
    try {
      const reg = await navigator.serviceWorker.register('/service-worker.js', { scope: '/' });
      await navigator.serviceWorker.ready;
      return reg;
    } catch (e) {
      console.error('Failed to register service worker', e);
      return null;
    }
  }
  
  async function subscribeForPush(user_id) {
    const reg = await registerServiceWorker();
    if (!reg) return null;
    try {
      let subscription = await reg.pushManager.getSubscription();
      if (!subscription) {
        // fetch VAPID public key from server
        let vapidKey = null;
        try {
          const r = await fetch('/vapid_public_key');
          if (r.ok) {
            const j = await r.json();
            vapidKey = j.vapid_public_key || null;
          }
        } catch (e) { console.warn('Could not fetch VAPID key', e); }
        if (!vapidKey) {
          console.error('No VAPID key available');
          return null;
        }
        subscription = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: await urlBase64ToUint8Array(vapidKey)
        });
      }
      // Persist to server
      await fetch('/save-subscription', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subscription: subscription.toJSON(), subject: 'Other', client_id: 'web', user_id: user_id })
      });
      // Save endpoint cookie + localStorage so UI can recover
      try {
        document.cookie = "nn_subscription=" + encodeURIComponent(subscription.endpoint) + "; Max-Age=31536000; Path=/; SameSite=Lax";
        localStorage.setItem("subscriptionEndpoint", subscription.endpoint);
      } catch (e) { console.warn('Could not set endpoint cookie/localStorage', e); }
      return subscription;
    } catch (e) {
      console.error('subscribeForPush failed', e);
      return null;
    }
  }
  
  async function unsubscribePush(user_id) {
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      let endpoint = null;
      if (sub) {
        endpoint = sub.endpoint;
        try {
          await sub.unsubscribe();
        } catch (e) {
          console.warn('Error unsubscribing local subscription', e);
        }
      } else {
        endpoint = decodeURIComponent((document.cookie.split('; ').find(row => row.startsWith('nn_subscription=')) || '').split('=')[1] || '');
        endpoint = endpoint || localStorage.getItem('subscriptionEndpoint');
      }
      if (endpoint) {
        await fetch('/subscriptions/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: endpoint })
        });
      }
      // Clear local markers
      document.cookie = "nn_subscription=; Max-Age=0; Path=/; SameSite=Lax";
      try { localStorage.removeItem('subscriptionEndpoint'); } catch(e) {}
      return true;
    } catch (e) {
      console.error('unsubscribePush failed', e);
      return false;
    }
  }
  
  async function fetchPendingNotifications(endpoint) {
    if (!endpoint) {
      const cookie = document.cookie.split('; ').find(row => row.startsWith('nn_subscription='));
      endpoint = cookie ? decodeURIComponent(cookie.split('=')[1]) : null;
      if (!endpoint) endpoint = localStorage.getItem('subscriptionEndpoint');
    }
    if (!endpoint) return [];
    try {
      const res = await fetch('/notifications/pending?endpoint=' + encodeURIComponent(endpoint));
      if (res.ok) {
        const data = await res.json();
        if (data && data.ok) return data.items || [];
      }
    } catch (e) {
      console.error('fetchPendingNotifications failed', e);
    }
    return [];
  }
  
  async function ackNotifications(endpoint, ids) {
    if (!endpoint || !Array.isArray(ids) || ids.length === 0) return false;
    try {
      const res = await fetch('/notifications/ack', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: endpoint, ids: ids })
      });
      if (res.ok) {
        const data = await res.json();
        return data.ok === true;
      }
    } catch (e) {
      console.error('ackNotifications failed', e);
    }
    return false;
  }
  