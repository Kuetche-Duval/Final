// static/js/push-helper.js
(() => {
  const vapidPublicKey = (window.NN_CONFIG && window.NN_CONFIG.vapidPublicKey) || "";
  function urlBase64ToUint8Array(base64String) {
    if (!base64String) return null;
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/\-/g, "+").replace(/_/g, "/");
    const rawData = atob(base64);
    const out = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) out[i] = rawData.charCodeAt(i);
    return out;
  }

  async function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) throw new Error("ServiceWorker not supported");
    const reg = await navigator.serviceWorker.register('/service-worker.js', { scope: '/' });
    await navigator.serviceWorker.ready;
    return reg;
  }

  async function getSubscription(reg) {
    try {
      return await reg.pushManager.getSubscription();
    } catch (e) {
      console.error("getSubscription failed", e);
      return null;
    }
  }

  async function subscribeForPush(subject) {
    try {
      const reg = await registerServiceWorker();
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        alert('Please allow notifications to subscribe.');
        return null;
      }
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidPublicKey)
      });
      // send subscription to server
      const body = { subscription: sub.toJSON ? sub.toJSON() : sub, subject: subject };
      const res = await fetch('/save-subscription', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const j = await res.json();
      if (!j || !j.ok) {
        throw new Error(j && j.error ? j.error : 'save-subscription failed');
      }
      return sub;
    } catch (e) {
      console.error('subscribeForPush', e);
      throw e;
    }
  }

  async function unsubscribeAll(endpoint) {
    try {
      // call server to delete
      await fetch('/subscriptions/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint })
      });
      // Also try local unsubscribe
      const reg = await registerServiceWorker();
      const s = await reg.pushManager.getSubscription();
      if (s) try { await s.unsubscribe(); } catch(e){ console.warn(e); }
      return true;
    } catch (e) {
      console.error('unsubscribeAll', e);
      return false;
    }
  }

  // set up UI handlers
  function initButtons() {
    document.querySelectorAll('.subscribe-btn').forEach(btn => {
      btn.addEventListener('click', async (ev) => {
        ev.preventDefault();
        const subj = btn.dataset.subject || 'Other';
        btn.disabled = true;
        try {
          // Check current subscription
          const reg = await registerServiceWorker();
          const cur = await getSubscription(reg);
          if (cur) {
            // check server if this endpoint already subscribed to this subj
            const ep = cur.endpoint;
            try {
              const r = await fetch('/subscriptions/check?endpoint=' + encodeURIComponent(ep));
              const j = await r.json();
              const subscribedSubjects = j && j.subjects ? j.subjects : [];
              if (subscribedSubjects.map(s=>s.toLowerCase()).includes(subj.toLowerCase())) {
                // already subscribed to subj -> unsubscribe entirely (simple approach)
                const ok = await unsubscribeAll(ep);
                if (ok) {
                  btn.classList.remove('subscribed');
                  btn.textContent = '🔔 Subscribe';
                }
                btn.disabled = false;
                return;
              }
            } catch (e) { /* ignore */ }
          }
          // else subscribe
          await subscribeForPush(subj);
          btn.classList.add('subscribed');
          btn.textContent = 'Subscribed ✓';
        } catch (err) {
          console.error(err);
          alert("Subscription failed: " + (err && err.message ? err.message : err));
        } finally {
          btn.disabled = false;
        }
      });
    });
  }

  // on load
  document.addEventListener('DOMContentLoaded', async () => {
    try {
      await registerServiceWorker();
    } catch (e) {
      console.warn('SW register failed', e);
    }
    initButtons();
  });

})();