/* notifications.js
   Robust Push Subscription manager for Newton's Nerds.
   Drop into static/js/ and include from your template:
     <script src="{{ url_for('static', filename='js/notifications.js') }}"></script>

   Expects on the page:
   - <script type="application/json" id="vapidKeyData">"BASE64_VAPID"</script>
   - subscribe buttons with class .subscribe-btn and data-subject attributes

   Server endpoints used:
   - POST /save-subscription         (body: { subscription, subject, client_id })
   - GET  /subscriptions/check?endpoint=...
   - POST /subscriptions/delete      (body: { endpoint })
*/
(function () {
  'use strict';

  // ---- Helpers ----
  function log(...args) { console.debug('[push]', ...args); }
  function warn(...args) { console.warn('[push]', ...args); }
  function err(...args) { console.error('[push]', ...args); }

  function getJsonTextFromEl(id, defaultVal = '') {
    const el = document.getElementById(id);
    if (!el) return defaultVal;
    try { return JSON.parse(el.textContent || el.innerText || '""'); }
    catch (e) { console.warn('Failed to parse JSON from', id, e); return defaultVal; }
  }

  function urlBase64ToUint8Array(base64String) {
    if (!base64String) return null;
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  function setCookie(name, value, days = 365) {
    const maxAge = days * 24 * 60 * 60;
    const secure = location.protocol === 'https:';
    document.cookie = name + '=' + encodeURIComponent(value || '') +
      '; max-age=' + maxAge +
      '; path=/' +
      '; samesite=Lax' +
      (secure ? '; Secure' : '');
  }
  function getCookie(name) {
    const m = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return m ? decodeURIComponent(m.pop()) : null;
  }
  function deleteCookie(name) {
    document.cookie = name + '=; path=/; max-age=0; samesite=Lax';
  }

  // ---- State ----
  let registrationForPush = null;
  let currentSubscription = null; // PushSubscription or fallback plain object {endpoint: "..."}
  let serverSubjectsForEndpoint = []; // lowercased subjects that server says this endpoint has
  const vapidPublicKey = getJsonTextFromEl('vapidKeyData', '');

  // UI selectors
  const SUB_BTN_SELECTOR = '.subscribe-btn';

  // ---- UI helpers ----
  function updateSubscribeButtonsUI() {
    document.querySelectorAll(SUB_BTN_SELECTOR).forEach(btn => {
      const subjRaw = (btn.dataset.subject || '').trim();
      const subj = subjRaw.toLowerCase();
      const subscribed = currentSubscription && currentSubscription.endpoint && serverSubjectsForEndpoint.includes(subj);
      btn.disabled = false;
      btn.title = subscribed ? `Unsubscribe from ${subjRaw}` : `Subscribe to ${subjRaw}`;
      if (subscribed) {
        btn.textContent = 'Subscribed ✓';
        btn.classList.add('subscribed');
        btn.dataset.state = 'subscribed';
      } else {
        btn.textContent = '🔔 Subscribe';
        btn.classList.remove('subscribed');
        btn.dataset.state = 'none';
      }
    });
  }

  function showAlert(message) {
    try { alert(message); } catch (e) { console.log(message); }
  }

  // ---- Server APIs ----
  async function fetchServerSubjectsForEndpoint(endpoint) {
    if (!endpoint) return [];
    try {
      const resp = await fetch('/subscriptions/check?endpoint=' + encodeURIComponent(endpoint));
      if (!resp.ok) { warn('subscriptions/check failed', resp.status); return []; }
      const j = await resp.json();
      if (j && j.ok && Array.isArray(j.subjects)) {
        return j.subjects.map(s => (s || '').toString().toLowerCase());
      }
      return [];
    } catch (e) {
      warn('Error contacting server for subscriptions/check', e);
      return [];
    }
  }

  async function sendSubscriptionToServer(sub, subject) {
    try {
      const sObj = (typeof sub.toJSON === 'function') ? sub.toJSON() : sub;
      const resp = await fetch('/save-subscription', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ subscription: sObj, subject: subject, client_id: 'web' })
      });
      const body = await resp.json().catch(() => null);
      if (!resp.ok) {
        throw new Error((body && body.error) ? body.error : `Status ${resp.status}`);
      }
      return body;
    } catch (e) {
      err('Failed to save subscription on server', e);
      throw e;
    }
  }

  async function requestServerDeleteEndpoint(endpoint) {
    try {
      const resp = await fetch('/subscriptions/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ endpoint: endpoint })
      });
      if (resp.ok) return await resp.json().catch(() => ({ ok: true }));
      return { ok: false, status: resp.status };
    } catch (e) {
      warn('Failed requesting server to delete subscription', e);
      return { ok: false, error: e.message || String(e) };
    }
  }

  // ---- Subscription logic ----
  async function initServiceWorkerAndSubscription() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      document.querySelectorAll(SUB_BTN_SELECTOR).forEach(b => { b.disabled = true; b.title = 'Notifications not supported in this browser'; });
      warn('Push/ServiceWorker not available in this browser.');
      return;
    }
    try {
      registrationForPush = await navigator.serviceWorker.register('/service-worker.js', { scope: '/' });
      try { await registrationForPush.update(); } catch (e) { /* ignore */ }
      await navigator.serviceWorker.ready;

      // Listen for messages from the SW
      navigator.serviceWorker.addEventListener('message', (ev) => {
        const d = ev.data || {};
        if (d && d.type === 'SUBSCRIPTION_LOST') {
          warn('Service worker reported subscription lost.');
          // clear local state, ask server for current subjects again
          currentSubscription = null;
          serverSubjectsForEndpoint = [];
          updateSubscribeButtonsUI();
        }
      });

      try {
        currentSubscription = await registrationForPush.pushManager.getSubscription();
      } catch (e) {
        warn('pushManager.getSubscription failed', e);
        currentSubscription = null;
      }

      // If pushManager returned null, try reading cookie/localStorage fallback
      if (!currentSubscription || !currentSubscription.endpoint) {
        const savedEndpoint = getCookie('nn_subscription') || localStorage.getItem('subscriptionEndpoint');
        if (savedEndpoint) {
          currentSubscription = { endpoint: savedEndpoint };
        }
      }

      if (currentSubscription && currentSubscription.endpoint) {
        serverSubjectsForEndpoint = await fetchServerSubjectsForEndpoint(currentSubscription.endpoint);
      } else {
        serverSubjectsForEndpoint = [];
      }

      updateSubscribeButtonsUI();
      log('[subscribe] SW ready. subscription:', !!currentSubscription);
    } catch (err) {
      err('[subscribe] SW register/getSubscription failed:', err);
      document.querySelectorAll(SUB_BTN_SELECTOR).forEach(b => { b.disabled = true; b.title = 'Service worker registration failed'; });
    }
  }

  async function subscribeForSubject(subject, btn = null) {
    if (!registrationForPush) {
      showAlert("Service worker not ready — reload the page and try again.");
      return;
    }

    // ask for permission
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      showAlert('Please allow notifications to subscribe (check browser settings).');
      return;
    }

    try {
      let sub = await registrationForPush.pushManager.getSubscription();
      if (!sub) {
        if (!vapidPublicKey) {
          showAlert('Push not configured on the server (no VAPID public key).');
          warn('Missing VAPID public key value:', vapidPublicKey);
          return;
        }
        sub = await registrationForPush.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapidPublicKey)
        });
      }

      currentSubscription = sub;
      // Save to server
      let serverResp;
      try {
        serverResp = await sendSubscriptionToServer(sub, subject);
      } catch (e) {
        // network/server error: still keep local subscription and inform user
        try {
          const endpoint = (typeof sub.toJSON === 'function') ? sub.toJSON().endpoint : sub.endpoint;
          if (endpoint) { setCookie('nn_subscription', endpoint, 365); localStorage.setItem('subscriptionEndpoint', endpoint); }
        } catch (ignored) {}
        throw e;
      }

      // serverResp may contain subjects array
      if (serverResp && Array.isArray(serverResp.subjects)) {
        serverSubjectsForEndpoint = serverResp.subjects.map(s => (s || '').toString().toLowerCase());
      } else if (!serverSubjectsForEndpoint.includes(subject.toLowerCase())) {
        serverSubjectsForEndpoint.push(subject.toLowerCase());
      }

      try {
        const ep = ((typeof sub.toJSON === 'function') ? sub.toJSON().endpoint : sub.endpoint) || '';
        if (ep) { setCookie('nn_subscription', ep, 365); localStorage.setItem('subscriptionEndpoint', ep); }
      } catch (e) {}

      updateSubscribeButtonsUI();
      if (btn) { btn.textContent = 'Subscribed ✓'; btn.classList.add('subscribed'); }
      showAlert('Subscribed successfully ✓ (server confirmed).');
      log('subscribeForSubject done', subject, serverSubjectsForEndpoint);
    } catch (err) {
      err('subscribeForSubject error', err);
      if (err && err.name === 'NotAllowedError') {
        showAlert('Notifications are blocked for this site. Check your browser/site notification settings.');
      } else if (err && err.name === 'SecurityError') {
        showAlert('Push subscription failed due to security restrictions (must be HTTPS).');
      } else {
        showAlert("Subscription failed: " + (err && err.message ? err.message : String(err)));
      }
    }
  }

  async function unsubscribeEndpoint(btn = null) {
    if (!registrationForPush) {
      showAlert("Service worker not ready.");
      return;
    }
    try {
      const sub = await registrationForPush.pushManager.getSubscription();
      let endpointToDelete = null;
      if (!sub) {
        endpointToDelete = getCookie('nn_subscription') || localStorage.getItem('subscriptionEndpoint');
        if (!endpointToDelete) {
          showAlert('No active subscription to unsubscribe.');
          return;
        }
      } else {
        endpointToDelete = sub.endpoint;
      }

      if (!confirm('Unsubscribe this browser from all subjects? This will remove saved subscriptions stored on the server for this browser.')) {
        return;
      }

      if (sub) {
        try { const ok = await sub.unsubscribe(); if (!ok) warn("sub.unsubscribe returned false"); } catch (e) { warn("Error while calling sub.unsubscribe()", e); }
      }

      try {
        const resp = await requestServerDeleteEndpoint(endpointToDelete);
        if (resp && resp.ok) { serverSubjectsForEndpoint = []; updateSubscribeButtonsUI(); }
        else warn("Server delete returned non-ok", resp);
      } catch (e) { warn("Server-side deletion error", e); }

      deleteCookie('nn_subscription'); localStorage.removeItem('subscriptionEndpoint');
      currentSubscription = null; serverSubjectsForEndpoint = [];
      updateSubscribeButtonsUI();
      showAlert('Unsubscribed locally and requested server cleanup.');
    } catch (e) {
      err("Unsubscribe failed", e);
      showAlert('Unsubscribe failed (see console).');
    }
  }

  // ---- Event binding for subscribe buttons ----
  document.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest(SUB_BTN_SELECTOR);
    if (!btn) return;
    e.preventDefault();
    const subj = btn.dataset.subject || 'Other';
    const state = btn.dataset.state || 'none';
    if (state === 'subscribed') {
      unsubscribeEndpoint(btn);
    } else {
      subscribeForSubject(subj, btn);
    }
  });

  // Initialize UI disabled state, then init SW & subscription
  document.addEventListener('DOMContentLoaded', () => {
    // Disable until SW ready
    document.querySelectorAll(SUB_BTN_SELECTOR).forEach(b => { b.disabled = true; b.title = 'Subscription initializing…'; });
    initServiceWorkerAndSubscription().then(() => {
      // done during initServiceWorkerAndSubscription
      updateSubscribeButtonsUI();
    }).catch((e) => {
      warn('initServiceWorkerAndSubscription failed', e);
      document.querySelectorAll(SUB_BTN_SELECTOR).forEach(b => { b.disabled = true; b.title = 'Service worker failed to initialize'; });
    });
  });

  // Public functions for debugging from console (optional)
  window.NNPush = {
    subscribeForSubject,
    unsubscribeEndpoint,
    getCurrentSubscription: () => currentSubscription,
    getServerSubjectsForEndpoint: () => serverSubjectsForEndpoint
  };
})();