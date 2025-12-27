// Poller for pending notifications (keeps behaviour but is defensive).
const siteNotifications = (function () {
  const PENDING_ENDPOINT = '/notifications/pending';
  const ACK_ENDPOINT = '/notifications/ack';
  const POLL_INTERVAL = 30 * 1000; // 30s
  const CONTAINER_ID = 'nn-notifications-container';

  async function fetchPending(endpoint) {
    const url = new URL(PENDING_ENDPOINT, window.location.origin);
    if (endpoint) url.searchParams.set('endpoint', endpoint);
    try {
      const resp = await fetch(url.toString(), { credentials: 'same-origin' });
      if (!resp.ok) throw new Error(`Server returned ${resp.status}`);
      const j = await resp.json();
      if (!j.ok) throw new Error('Server returned ok=false');
      return j.items || [];
    } catch (err) {
      console.error('fetchPending error:', err);
      return [];
    }
  }

  async function ackNotifications(endpoint, ids = []) {
    try {
      const resp = await fetch(ACK_ENDPOINT, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint, ids })
      });
      if (!resp.ok) {
        console.warn('ackNotifications non-ok:', resp.status);
      }
      return resp;
    } catch (err) {
      console.error('ackNotifications error:', err);
      throw err;
    }
  }

  function getContainer() {
    let c = document.getElementById(CONTAINER_ID);
    if (c) return c;
    c = document.createElement('div');
    c.id = CONTAINER_ID;
    Object.assign(c.style, {
      position: 'fixed',
      right: '18px',
      bottom: '18px',
      zIndex: 99999,
      maxWidth: '360px',
      display: 'flex',
      flexDirection: 'column',
      gap: '10px',
      alignItems: 'flex-end'
    });
    document.body.appendChild(c);
    return c;
  }

  function renderItem(item, container, onDismiss) {
    const card = document.createElement('div');
    card.className = 'card';
    Object.assign(card.style, {
      width: '100%',
      boxShadow: '0 6px 18px rgba(0,0,0,0.12)',
      padding: '12px',
      borderRadius: '12px',
      background: 'var(--card-bg, #fff)',
      color: 'var(--text, #111)'
    });

    const title = document.createElement('div');
    title.style.fontWeight = 700;
    title.style.marginBottom = '6px';
    title.textContent = item.title || 'Notification';

    const body = document.createElement('div');
    body.style.marginBottom = '10px';
    body.style.whiteSpace = 'pre-wrap';
    body.textContent = item.body || (item.data && item.data.announcement ? JSON.stringify(item.data || {}) : '');

    const actions = document.createElement('div');
    actions.style.display = 'flex';
    actions.style.justifyContent = 'flex-end';
    actions.style.gap = '8px';

    const openBtn = document.createElement('button');
    openBtn.className = 'btn btn-sm btn-primary';
    openBtn.textContent = 'Open';
    openBtn.addEventListener('click', () => {
      const url = (item.data && item.data.url) || item.url || '/';
      try { window.open(url, '_blank'); } catch (e) { window.location.href = url; }
      onDismiss(item.id);
    });

    const dismissBtn = document.createElement('button');
    dismissBtn.className = 'btn btn-sm btn-outline';
    dismissBtn.textContent = 'Dismiss';
    dismissBtn.addEventListener('click', () => onDismiss(item.id));

    actions.appendChild(openBtn);
    actions.appendChild(dismissBtn);

    card.appendChild(title);
    card.appendChild(body);
    card.appendChild(actions);

    container.appendChild(card);
  }

  let pollingHandle = null;
  async function pollAndRender(opts = {}) {
    const endpoint = opts.endpoint || '';
    const container = getContainer();
    const items = await fetchPending(endpoint);
    if (!items || items.length === 0) {
      return;
    }
    container.innerHTML = '';
    items.forEach(item => {
      const normalized = {
        id: item.id,
        title: item.title || (item.subject ? `New: ${item.subject}` : 'Notification'),
        body: item.body || (item.data && item.data.preview ? JSON.stringify(item.data.preview) : ''),
        data: item.data || {}
      };
      renderItem(normalized, container, async (nid) => {
        try {
          await ackNotifications(endpoint, [nid]);
        } catch (e) {
          console.warn('Ack failed', e);
        }
        container.innerHTML = '';
      });
    });
  }

  function startPolling(opts = {}) {
    if (pollingHandle) clearInterval(pollingHandle);
    pollAndRender(opts).catch(err => console.error(err));
    pollingHandle = setInterval(() => {
      pollAndRender(opts).catch(err => console.error(err));
    }, opts.interval || POLL_INTERVAL);
    return pollingHandle;
  }

  function stopPolling() {
    if (pollingHandle) {
      clearInterval(pollingHandle);
      pollingHandle = null;
    }
  }

  return {
    fetchPending,
    ackNotifications,
    startPolling,
    stopPolling
  };
})();