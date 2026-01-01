/* notifications.js — part 1/4
   Helpers, cookie funcs, and base64->Uint8 conversion.
   Paste parts 1..4 in order into one file. */
(function(window){
  'use strict';

  // -- config --
  const POLL_INTERVAL_MS = 30000; // 30s while visible
  const NN_SUB_COOKIE = 'nn_subscription';
  const NN_USER_COOKIE = 'nn_user';
  const NN_CATEGORY_COOKIE = 'nn_last_category';

  // -- cookie helpers --
  function getCookie(name){
    const v = ("; " + document.cookie).split("; " + name + "=");
    if(v.length === 2) return decodeURIComponent(v.pop().split(";").shift());
    return null;
  }
  function setCookie(name, value, days = 365){
    const maxAge = days * 24 * 60 * 60;
    document.cookie = `${name}=${encodeURIComponent(value)};path=/;max-age=${maxAge}`;
  }

  // base64url -> Uint8Array (for VAPID key)
  function urlBase64ToUint8Array(base64String) {
    if (!base64String) return new Uint8Array();
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  // small logger wrapper
  function log(...args){ try{ console.log('[NN-notify]', ...args); }catch(e){} }

  // expose minimal API container
  const NNNotify = {
    _reg: null,
    _pollTimer: null,
    _isVisible: document.visibilityState === 'visible',
    _isSubscribed: false,
    pollInterval: POLL_INTERVAL_MS
  };
/* notifications.js — part 2/4
   Registration and subscribe/unsubscribe logic */
  // Register service worker and keep registration reference
  async function registerServiceWorker(){
    if(!('serviceWorker' in navigator)){
      log('Service Worker not supported');
      return null;
    }
    try{
      const reg = await navigator.serviceWorker.register('/service-worker.js');
      log('Service worker registered at', reg.scope);
      NNNotify._reg = reg;
      return reg;
    } catch(err){
      log('Service worker registration failed', err);
      return null;
    }
  }

  // Request permission, fetch VAPID key and subscribe
  async function subscribePush(options = {}){
    try{
      if(!('Notification' in window) || !('serviceWorker' in navigator) || !('PushManager' in window)){
        throw new Error('Push not supported in this browser');
      }

      // ensure SW registered
      let reg = NNNotify._reg || await registerServiceWorker();
      if(!reg) throw new Error('service-worker-registration-failed');

      const perm = await Notification.requestPermission();
      if(perm !== 'granted') throw new Error('Notification permission denied');

      // get vapid
      const vapidResp = await fetch('/api/vapid');
      const vapidJson = await vapidResp.json().catch(()=>({ok:false}));
      if(!vapidJson || !vapidJson.ok || !vapidJson.publicKey) throw new Error('vapid-not-configured');

      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidJson.publicKey)
      });

      // send to server
      const userName = getCookie(NN_USER_COOKIE) || options.client_id || '';
      const subject = getCookie(NN_CATEGORY_COOKIE) || options.subject || 'Other';
      const res = await fetch('/save-subscription', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ subscription: sub, client_id: userName, subject })
      });
      const json = await res.json().catch(()=>({ok:false}));
      if(!json || !json.ok) log('save-subscription response', json);

      if(sub && sub.endpoint) setCookie(NN_SUB_COOKIE, sub.endpoint, 365);
      NNNotify._isSubscribed = true;
      log('Subscribed:', sub.endpoint);
      return {ok:true, subscription: sub, server: json};
    }catch(err){
      log('subscribePush error', err);
      return {ok:false, error: (err && err.message) ? err.message : String(err)};
    }
  }

  // Unsubscribe (local + server-side deletion is handled server when push fails; here we remove cookie and attempt to unsubscribe)
  async function unsubscribePush(){
    try{
      if(!NNNotify._reg) await registerServiceWorker();
      const reg = NNNotify._reg;
      if(!reg) return {ok:false, error:'no-sw'};
      const sub = await reg.pushManager.getSubscription();
      if(sub){
        await sub.unsubscribe().catch(()=>{});
        log('Unsubscribed from push manager');
      }
      setCookie(NN_SUB_COOKIE, '', -1);
      NNNotify._isSubscribed = false;
      return {ok:true};
    }catch(err){
      log('unsubscribePush error', err);
      return {ok:false, error: (err&&err.message)?err.message:String(err)};
    }
  }
/* notifications.js — part 3/4
   Poll pending notifications for this endpoint and show them,
   then ack to server so they are marked delivered. */

  // show notification using Notification API (if supported) or fallback alert
  function showLocalNotification(item){
    try{
      if(!item) return;
      const title = item.title || 'Notification';
      const body = item.body || (item.data && JSON.stringify(item.data)) || '';
      const url = (item.data && item.data.url) || '/';
      // attempt to show via Service Worker if possible
      if(NNNotify._reg && NNNotify._reg.showNotification){
        NNNotify._reg.showNotification(title, { body, data: { url }, tag: `nn-${item.id}`});
      } else if('Notification' in window && Notification.permission === 'granted'){
        const n = new Notification(title, { body });
        n.onclick = (ev) => { ev.preventDefault(); try{ window.focus(); window.location.href = url; }catch(e){} };
      } else {
        // fallback
        console.log('Notification:', title, body);
      }
    }catch(err){
      console.error('showLocalNotification error', err);
    }
  }

  // fetch pending notifications for current endpoint (cookie)
  async function pollPending(){
    try{
      const endpoint = getCookie(NN_SUB_COOKIE) || '';
      if(!endpoint) return;

      const resp = await fetch('/notifications/pending?endpoint=' + encodeURIComponent(endpoint));
      if(!resp.ok) return;
      const j = await resp.json().catch(()=>null);
      if(!j || !j.ok) return;
      const items = Array.isArray(j.items) ? j.items : [];
      if(!items.length) return;

      // show and collect ids to ack
      const ids = [];
      for(const it of items){
        showLocalNotification(it);
        if(it && it.id) ids.push(it.id);
      }

      // ack them
      if(ids.length){
        await fetch('/notifications/ack', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ endpoint, ids })
        }).catch(()=>{});
      }
    }catch(err){
      console.error('pollPending error', err);
    }
  }

  // start/stop polling depending on visibility
  function startPolling(){
    if(NNNotify._pollTimer) return;
    NNNotify._pollTimer = setInterval(()=> {
      if(document.visibilityState === 'visible') pollPending();
    }, NNNotify.pollInterval);
    // do an immediate check
    if(document.visibilityState === 'visible') pollPending();
  }
  function stopPolling(){
    if(NNNotify._pollTimer){ clearInterval(NNNotify._pollTimer); NNNotify._pollTimer = null; }
  }

  document.addEventListener('visibilitychange', () => {
    if(document.visibilityState === 'visible'){ startPolling(); }
    else { stopPolling(); }
  });
/* notifications.js — part 4/4
   Initialization, button hookup, and auto-recover of existing subscription. */

  // check existing subscription
  async function ensureSubscriptionState(){
    try{
      if(!('serviceWorker' in navigator)) return;
      NNNotify._reg = NNNotify._reg || await registerServiceWorker();
      if(!NNNotify._reg) return;
      const existing = await NNNotify._reg.pushManager.getSubscription();
      NNNotify._isSubscribed = !!existing;
      if(existing && existing.endpoint){
        setCookie(NN_SUB_COOKIE, existing.endpoint, 365);
      }
    }catch(err){
      console.warn('ensureSubscriptionState', err);
    }
  }

  // Hookup subscribe button (id default 'subscribe-btn')
  function attachSubscribeButton(btnId = 'subscribe-btn'){
    const btn = document.getElementById(btnId);
    if(!btn) return;
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      btn.disabled = true;
      btn.classList.add('btn-loading');
      try{
        await ensureSubscriptionState();
        if(!NNNotify._isSubscribed){
          const res = await subscribePush();
          if(res && res.ok) btn.textContent = 'Subscribed ✓';
          else btn.textContent = 'Subscribe (failed)';
        } else {
          // already subscribed — offer unsubscribe
          const unsub = await unsubscribePush();
          if(unsub && unsub.ok) btn.textContent = 'Subscribe';
        }
      }catch(err){
        console.error('subscribe button:', err);
        alert('Subscription failed: ' + (err && err.message ? err.message : String(err)));
      } finally {
        btn.disabled = false;
        btn.classList.remove('btn-loading');
      }
    });
  }

  // auto-init: register sw, recover subscription, start polling if permitted
  async function init(opts = {}){
    try{
      await ensureSubscriptionState();
      attachSubscribeButton(opts.buttonId || 'subscribe-btn');
      // only start polling if cookie exists and permission granted
      const ep = getCookie(NN_SUB_COOKIE);
      if(ep && Notification.permission === 'granted'){
        startPolling();
      } else {
        // try to get subscription from server cookie or from service worker
        if(NNNotify._isSubscribed && Notification.permission === 'granted'){ startPolling(); }
      }
      // if user has a name cookie, attempt a silent subscribe (optional)
      if(getCookie(NN_USER_COOKIE) && !NNNotify._isSubscribed){
        try{ await subscribePush(); }catch(e){ log('silent subscribe failed', e); }
      }
    }catch(err){
      console.error('init notifications failed', err);
    }
  }

  // expose to window
  window.NNNotify = Object.assign(window.NNNotify || {}, {
    init,
    subscribePush,
    unsubscribePush,
    pollPending,
    ensureSubscriptionState
  });

  // auto init on DOM ready
  document.addEventListener('DOMContentLoaded', () => { init(); });

})(window);