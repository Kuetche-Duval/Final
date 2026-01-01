/* static/js/notifications.js
   Polls /notifications/pending and displays notifications on the page and via the service worker.
   - Poll interval: default 30s (adjustable)
   - On page focus / visibility change it triggers a fetch
   - After showing notifications, it acknowledges them to the server so they won't be re-shown
*/

(function(){
  const PENDING_URL = '/notifications/pending';
  const ACK_URL = '/notifications/ack';
  const POLL_SECONDS = 30;
  let pollTimer = null;

  function _log(...args){ try{ console.debug('[nnNotify]', ...args); } catch(e){} }

  function getCookie(name) {
    const v = "; " + document.cookie;
    const parts = v.split("; " + name + "=");
    if (parts.length === 2) return decodeURIComponent(parts.pop().split(";").shift());
    return null;
  }

  async function fetchPending() {
    try {
      const endpoint = getCookie('nn_subscription') || '';
      const url = endpoint ? `${PENDING_URL}?endpoint=${encodeURIComponent(endpoint)}` : PENDING_URL;
      const r = await fetch(url, { credentials: 'same-origin' });
      if (!r.ok) {
        _log('pending fetch failed', r.status);
        return [];
      }
      const j = await r.json();
      if (!j.ok) return [];
      return j.items || [];
    } catch (e) {
      _log('fetchPending error', e);
      return [];
    }
  }

  async function ackIds(ids) {
    if (!ids || !ids.length) return;
    try {
      const endpoint = getCookie('nn_subscription') || '';
      const r = await fetch(ACK_URL, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ endpoint, ids })
      });
      if (!r.ok) _log('ack failed', await r.text());
      return r.ok;
    } catch (e) {
      _log('ack error', e);
      return false;
    }
  }

  async function showNotifications(items) {
    if (!items || !items.length) return;
    // Try to use service worker to show notifications (better UX)
    try {
      if (navigator.serviceWorker && navigator.serviceWorker.controller) {
        // ask SW to show notifications via postMessage OR show from page using registration
        const reg = await navigator.serviceWorker.getRegistration();
        for (const it of items) {
          const payload = {
            title: it.title || 'Newton\'s Nerds',
            body: it.body || '',
            icon: (it.data && it.data.icon) || '/static/img/icon-192.png',
            badge: (it.data && it.data.badge) || '/static/img/icon-192.png',
            data: { id: it.id, subject: it.subject, url: (it.data && it.data.url) || '/'}
          };
          if (reg && reg.showNotification) {
            try {
              await reg.showNotification(payload.title, {
                body: payload.body,
                icon: payload.icon,
                badge: payload.badge,
                data: payload.data
              });
            } catch (e) {
              // fallback: postMessage to service worker
              try { navigator.serviceWorker.controller.postMessage({ type: 'show-notification', payload }); } catch(e2) {}
            }
          } else {
            // fallback: postMessage
            try { navigator.serviceWorker.controller.postMessage({ type: 'show-notification', payload }); } catch(e2) {}
          }
        }
      } else {
        // if no service worker controller, show an in-page toast / alert
        for (const it of items) {
          try {
            showInPageToast(it);
          } catch(e){}
        }
      }
    } catch (err) {
      _log('showNotifications error', err);
    } finally {
      // after attempting to show, ack
      const ids = items.map(i => i.id).filter(Boolean);
      if (ids.length) await ackIds(ids);
    }
  }

  // Simple in-page toast appended to body
  function showInPageToast(item) {
    try {
      const wrapId = 'nn-notify-wrap';
      let wrap = document.getElementById(wrapId);
      if (!wrap) {
        wrap = document.createElement('div');
        wrap.id = wrapId;
        wrap.style.position = 'fixed';
        wrap.style.right = '12px';
        wrap.style.bottom = '12px';
        wrap.style.zIndex = 999999;
        document.body.appendChild(wrap);
      }
      const el = document.createElement('div');
      el.className = 'card mb-2 shadow-sm';
      el.style.minWidth = '260px';
      el.innerHTML = `<div class="card-body p-2">
        <strong style="display:block">${escapeHtml(item.title || 'Notification')}</strong>
        <div style="font-size:0.95rem">${escapeHtml(item.body || '')}</div>
      </div>`;
      wrap.appendChild(el);
      setTimeout(()=> { try{ el.remove(); } catch(e){} }, 8000);
    } catch (e) { /* ignore */ }
  }

  function escapeHtml(s) {
    if (!s) return '';
    return s.replace(/[&<>"']/g, (m) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  }

  // Polling loop
  async function pollNow() {
    try {
      const items = await fetchPending();
      if (items && items.length) {
        await showNotifications(items);
      }
    } catch (e) {
      _log('poll error', e);
    }
  }

  function startPolling(seconds) {
    stopPolling();
    pollNow(); // immediate
    pollTimer = setInterval(pollNow, (seconds || POLL_SECONDS) * 1000);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // Visibility change => poll immediately
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) pollNow();
  });
  window.addEventListener('focus', () => { pollNow(); });

  // wire to window for debugging / manual control
  window.nnNotifications = {
    start: () => startPolling(POLL_SECONDS),
    stop: () => stopPolling(),
    pollNow,
    fetchPending
  };

  // auto start
  startPolling(POLL_SECONDS);
})();