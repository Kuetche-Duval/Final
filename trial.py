questions.html
{% extends 'base.html' %}

{% block title %}
  Ask a Question
{% endblock %}

{% block styles %}
<link rel="manifest" href="{{ url_for('static', filename='manifest.json') }}">
<style>
  :root{
    --bg: #f6f8fb;
    --card-bg: #ffffff;
    --text: #0b1220;
    --muted: #6b7280;
    --accent-green: #25d366;
    --card-radius: 12px;
  }
  [data-theme="dark"]{
    --bg: #08101a;
    --card-bg: #071019;
    --text: #e6eef8;
    --muted: #98a0b3;
  }
  body { background: var(--bg); color: var(--text); font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial; }
  .card { background: var(--card-bg); border-radius: var(--card-radius); box-shadow: 0 6px 20px rgba(3,3,3,0.06); padding: 14px; }
  .page-header { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:12px; }
  .controls { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .question-list { list-style:none; padding:0; margin:0; display:grid; gap:10px; }
  .question-item { display:flex; gap:12px; align-items:flex-start; justify-content:space-between; padding:12px 14px; border-radius:12px; background: rgba(0,0,0,0.02); cursor: pointer; }
  .category-badge { background: rgba(37,211,102,0.12); color: var(--accent-green); padding:4px 8px; border-radius:10px; font-weight:700; font-size:.86rem; }
  .author { color: var(--muted); font-weight:700; font-size:.88rem; max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .message-text { color: var(--text); font-size: .97rem; white-space:pre-wrap; word-break:break-word; }
  .op-attempt { margin-top:8px; padding:8px; background:rgba(0,0,0,0.02); border-radius:8px; color:var(--muted); font-size:.92rem; }
  .op-attempt small { color:var(--muted); display:block; margin-bottom:6px; font-weight:700; }
  .btn-sub { font-size: .85rem; padding:6px 8px; border-radius:8px; }
  .answer-panel { display:none; margin-top:8px; }
  .tiny-link { font-size:.85rem; color:var(--accent-green); text-decoration:underline; cursor:pointer; background:none;border:0;padding:0; }
  .subscribe-btn { background:#fff3cd; border:1px solid #ffeeba; color:#856404; }
  .subscribe-btn.subscribed { background:#d4edda; border-color:#c3e6cb; color:#155724; }
  .status-inline { margin-left:8px; font-size:0.85rem; color:var(--muted); }
  @media (max-width:576px) { .question-item{flex-direction:column} .author { max-width:140px; } }
</style>
{% endblock %}

{% block content %}
<div class="container py-4">
  <div class="card">
    <div class="d-flex justify-content-between align-items-center mb-3">
      <div><strong>💬 Ask & Answer Questions</strong></div>
      <div class="d-flex gap-2 align-items-center">
        <button id="theme-toggle" class="btn btn-sm btn-outline-secondary">Toggle theme</button>
        <div class="small text-muted">Newton's Nerds</div>
      </div>
    </div>

    <!-- New Question Form -->
    <form id="new-question-form" method="POST" action="{{ url_for('questions_page') }}" class="mb-4" novalidate>
      <div class="row g-2">
        <div class="col-md-5">
          <label for="category" class="form-label small">Category</label>
          <select class="form-select" id="category" name="category" required>
            <option value="">-- Select Category --</option>
            {% for c in categories %}
              <option value="{{ c }}">{{ c }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="col-md-7 d-flex align-items-end">
          <button type="submit" class="btn btn-success w-100">➕ Submit Question (attempt required)</button>
        </div>
      </div>

      <div class="mb-3 mt-2">
        <label for="question" class="form-label small">Your Question</label>
        <textarea class="form-control" id="question" name="question" rows="3" required></textarea>
      </div>

      <div class="mb-3">
        <label for="attempt" class="form-label small">Your Attempt (required)</label>
        <textarea class="form-control" id="attempt" name="attempt" rows="3" required></textarea>
        <div class="form-text small text-muted">You must provide your attempt before submitting the question.</div>
      </div>

      <!-- optional name (kept but hidden by default) -->
      <div style="display:none">
        <input type="text" name="name" placeholder="Optional name">
      </div>
    </form>

    <hr>

    <!-- Filter + sort -->
    <div class="mb-3 d-flex flex-wrap gap-2 align-items-center">
      <div class="d-flex align-items-center gap-2">
        <label class="small mb-0">📂 Filter:</label>
        <form id="filter-form" method="GET" action="{{ url_for('questions_page') }}">
          <select class="form-select form-select-sm" id="categoryFilter" name="categoryFilter" onchange="this.form.submit()" style="max-width:220px;">
            <option value="">All</option>
            {% for c in categories %}
              <option value="{{ c }}" {% if selected_category == c %}selected{% endif %}>{{ c }}</option>
            {% endfor %}
          </select>
        </form>
      </div>

      <div class="ms-auto d-flex gap-2 align-items-center">
        <input id="searchBox" class="form-control form-control-sm" placeholder="Search questions..." style="min-width:200px;">
        <select id="sortSelect" class="form-select form-select-sm" style="width:170px">
          <option value="none">Sort: Default</option>
          <option value="new">Sort: Newest first</option>
          <option value="old">Sort: Oldest first</option>
        </select>
      </div>
    </div>

    <!-- Questions list -->
    <ul class="question-list" id="questionsList" aria-live="polite">
      {% for q in questions %}
        {% set cat = q[0] if q|length > 0 else "Other" %}
        {% set author = q[1] if q|length > 1 and q[1] else "Anonymous" %}
        {% set text = q[2] if q|length > 2 else "" %}
        {% set attempt = q[3] if q|length > 3 else "" %}
        {% set timestr = q[4] if q|length > 4 else "" %}
        <li tabindex="0" class="question-item" role="button"
            data-author="{{ author|e }}"
            data-category="{{ cat|e }}"
            data-question="{{ text|e }}"
            data-attempt="{{ attempt|e }}"
            data-time="{{ timestr|e }}">
          <div style="min-width:0; max-width:75%">
            <div class="d-flex gap-2 align-items-center mb-1">
              <div class="category-badge">{{ cat }}</div>
              <div class="author">{{ author }}</div>
              <div class="small text-muted ms-auto">{{ timestr }}</div>
            </div>

            <div class="message-text">{{ text }}</div>

            <!-- OP's attempt (visible to answerers) -->
            <div class="op-attempt" aria-hidden="false">
              <small>OP's attempt</small>
              <div class="op-attempt-short">{{ attempt[:180] }}{% if attempt|length > 180 %}…{% endif %}</div>
              {% if attempt|length > 180 %}
                <button class="tiny-link show-full-attempt">Show full attempt</button>
              {% endif %}
              <div class="op-attempt-full" style="display:none; margin-top:8px;">{{ attempt }}</div>
            </div>

            <!-- Answer / send panel -->
            <div class="answer-panel mt-2" aria-hidden="true">
              <div class="mb-2">
                <label class="form-label small">Your answer (you'll send it to the community)</label>
                <textarea class="form-control answer-text" rows="3" placeholder="Write your short answer here..."></textarea>
              </div>

              <div class="d-flex gap-2">
                {% if community_links.get(cat) %}
                  <a class="btn btn-outline-success btn-sub open-group" href="{{ community_links.get(cat) }}" target="_blank" rel="noopener">Open {{ cat }} Community</a>
                {% else %}
                  <button class="btn btn-outline-secondary btn-sub" disabled>Community not set</button>
                {% endif %}

                <button class="btn btn-outline-warning btn-sub subscribe-btn" data-subject="{{ cat }}">🔔 Subscribe</button>

                <button class="btn btn-primary btn-sub copy-answer">Copy answer</button>
                <button class="btn btn-info btn-sub share-whatsapp">Share to WhatsApp</button>
              </div>

              <div class="form-text small text-muted mt-2">Tip: open the community link, paste your answer and send it in the group. The app cannot securely post into WhatsApp groups for you.</div>
            </div>
          </div>

          <div style="min-width:80px; text-align:right">
            <div class="small text-muted">Asked by {{ author }}</div>
          </div>
        </li>
      {% endfor %}
    </ul>
  </div>
</div>

<!-- Community JSON & vapid key -->
<script type="application/json" id="communityLinksData">{{ community_links|tojson }}</script>
<script type="application/json" id="vapidKeyData">{{ vapid_public_key|tojson }}</script>

<script>
document.addEventListener('DOMContentLoaded', () => {
  const communityLinksEl = document.getElementById('communityLinksData');
  const vapidEl = document.getElementById('vapidKeyData');
  let communityLinks = {};
  let vapidPublicKey = "";
  try { communityLinks = communityLinksEl ? JSON.parse(communityLinksEl.textContent || '{}') : {}; } catch(e){ communityLinks = {}; console.error(e); }
  try { vapidPublicKey = vapidEl ? JSON.parse(vapidEl.textContent || '""') : ""; } catch(e){ vapidPublicKey = ""; console.error(e); }

  /* --- Interaction handlers (existing features) --- */
  document.querySelectorAll('.question-item').forEach(item => {
    item.addEventListener('click', (e) => {
      if (['BUTTON','A','TEXTAREA','INPUT','SELECT','LABEL'].includes(e.target.tagName)) return;
      const panel = item.querySelector('.answer-panel');
      if (!panel) return;
      const isVisible = panel.style.display === 'block';
      panel.style.display = isVisible ? 'none' : 'block';
      panel.setAttribute('aria-hidden', String(isVisible ? 'true' : 'false'));
    });
    item.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); item.click(); }
    });
  });

  document.querySelectorAll('.show-full-attempt').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      const item = btn.closest('.question-item');
      const full = item.querySelector('.op-attempt-full');
      const short = item.querySelector('.op-attempt-short');
      if (!full || !short) return;
      if (full.style.display === 'none' || !full.style.display) {
        full.style.display = 'block'; short.style.display = 'none'; btn.textContent = 'Hide full attempt';
      } else {
        full.style.display = 'none'; short.style.display = ''; btn.textContent = 'Show full attempt';
      }
    });
  });

  document.querySelectorAll('.copy-answer').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const item = btn.closest('.question-item');
      const answerEl = item.querySelector('.answer-text');
      const answer = (answerEl && (answerEl.value ?? answerEl.textContent ?? '')) || '';
      navigator.clipboard.writeText(answer).then(() => {
        const old = btn.textContent;
        btn.textContent = 'Copied ✓';
        setTimeout(() => btn.textContent = old, 1500);
      }).catch(() => alert('Copy failed — please select and copy manually.'));
    });
  });

  document.querySelectorAll('.share-whatsapp').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      const item = btn.closest('.question-item');
      if(!item) return;
      const answer = (item.querySelector('.answer-text')?.value || '').trim();
      const subject = item.dataset?.category || '';
      const group = communityLinks[subject] || '';
      let text = answer || '';
      if (group) text += (text ? "\n\n" : "") + "Group: " + group;
      const url = 'https://api.whatsapp.com/send?text=' + encodeURIComponent(text || '(no text)');
      window.open(url, '_blank');
    });
  });

  /* --- Theme --- */
  function applyTheme(theme) {
    if (theme) document.documentElement.setAttribute('data-theme', theme);
    else document.documentElement.removeAttribute('data-theme');
    localStorage.setItem('site-theme', theme || '');
  }
  const themeToggle = document.getElementById('theme-toggle');
  if (themeToggle) {
    themeToggle.addEventListener('click', () => {
      const cur = localStorage.getItem('site-theme') || (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
      applyTheme(cur === 'dark' ? 'light' : 'dark');
    });
  }
  const saved = localStorage.getItem('site-theme'); if (saved) applyTheme(saved);

  /* --- Search & Sort --- */
  const searchBox = document.getElementById('searchBox');
  const sortSelect = document.getElementById('sortSelect');
  const list = document.getElementById('questionsList');
  function filterList() {
    if (!searchBox || !list) return;
    const q = (searchBox.value || '').toLowerCase();
    Array.from(list.children).forEach(li => {
      const txt = (li.dataset.question || '').toLowerCase() + ' ' + (li.dataset.author || '').toLowerCase() + ' ' + (li.dataset.category || '').toLowerCase() + ' ' + (li.dataset.attempt || '').toLowerCase();
      li.style.display = txt.includes(q) ? '' : 'none';
    });
  }
  if (searchBox) searchBox.addEventListener('input', filterList);
  function sortListBy(option) {
    if (!list) return;
    const nodes = Array.from(list.children).filter(n => n.style.display !== 'none');
    if (option === 'new') {
      nodes.sort((a,b) => (Date.parse(b.dataset.time || 0) - Date.parse(a.dataset.time || 0)));
    } else if (option === 'old') {
      nodes.sort((a,b) => (Date.parse(a.dataset.time || 0) - Date.parse(b.dataset.time || 0)));
    } else return;
    nodes.forEach(n => list.appendChild(n));
  }
  if (sortSelect) {
    sortSelect.addEventListener('change', () => sortListBy(sortSelect.value));
    if (sortSelect.value && sortSelect.value !== 'none') sortListBy(sortSelect.value);
  }

  /* --- Form validation --- */
  const newQForm = document.getElementById('new-question-form');
  if (newQForm) {
    newQForm.addEventListener('submit', (ev) => {
      const q = document.getElementById('question');
      const a = document.getElementById('attempt');
      if (!q.value.trim() || !a.value.trim()) {
        ev.preventDefault();
        alert('Please fill both question and your attempt before submitting.');
      }
    });
  }

  /* --- Service worker registration & subscription logic (robust) --- */
  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  // SW registration + subscription management
  let registrationForPush = null;
  async function initServiceWorkerAndSubscription() {
    if (!('serviceWorker' in navigator)) {
      console.warn("Service workers not supported.");
      return;
    }
    try {
      registrationForPush = await navigator.serviceWorker.register('/service-worker.js');
      await navigator.serviceWorker.ready;
      console.log("[subscribe] service worker ready:", registrationForPush);
      const existing = await registrationForPush.pushManager.getSubscription();
      console.log("[subscribe] existing subscription:", existing);
      if (existing) {
        // mark all subscribe buttons as subscribed (coarse: server stores per-subject)
        document.querySelectorAll('.subscribe-btn').forEach(b => {
          b.textContent = 'Subscribed ✓';
          b.classList.add('subscribed');
          b.disabled = true;
        });
      }
    } catch (err) {
      console.error("[subscribe] SW register failed:", err);
    }
  }

  async function subscribeForSubject(subject, btn) {
    if (!registrationForPush) {
      alert("Service worker not ready — reload the page and try again.");
      return;
    }
    // request permission
    const permission = await Notification.requestPermission();
    console.log("[subscribe] notification permission:", permission);
    if (permission !== 'granted') {
      alert('Please allow notifications to subscribe.');
      return;
    }

    try {
      let sub = await registrationForPush.pushManager.getSubscription();
      if (!sub) {
        if (!vapidPublicKey) {
          alert('Push not configured on the server (no VAPID public key).');
          console.error("Missing VAPID public key value:", vapidPublicKey);
          return;
        }
        sub = await registrationForPush.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapidPublicKey)
        });
      }
      console.log("[subscribe] obtained subscription:", sub);
      const payload = (typeof sub.toJSON === 'function') ? sub.toJSON() : sub;
      const res = await fetch('{{ url_for("save_subscription_route") }}', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subscription: payload, subject: subject, client_id: 'web' })
      });
      let jsonRes;
      try { jsonRes = await res.json(); } catch(e){ jsonRes = null; }
      if (!res.ok) {
        console.error("[subscribe] server returned error", res.status, jsonRes);
        alert('Failed to save subscription on server: ' + (jsonRes && jsonRes.error ? jsonRes.error : res.statusText));
        return;
      }
      if (jsonRes && jsonRes.ok) {
        if (btn) { btn.textContent = 'Subscribed ✓'; btn.classList.add('subscribed'); btn.disabled = true; }
        alert('Subscribed successfully ✓');
      } else {
        console.error("[subscribe] unexpected server response", jsonRes);
        alert('Subscription saved? server response: ' + JSON.stringify(jsonRes));
      }
    } catch (err) {
      console.error("pushManager.subscribe failed:", err);
      alert("Subscription failed: " + (err && err.message ? err.message : String(err)));
    }
  }

  // wire subscribe buttons
  document.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('.subscribe-btn');
    if (!btn) return;
    e.preventDefault();
    const subj = btn.dataset.subject || 'Other';
    subscribeForSubject(subj, btn);
  });

  // initialize SW & subscription detection
  initServiceWorkerAndSubscription();
});
</script>
{% endblock %}

content.html
{% extends 'base.html' %}

{% block title %}
TimeTable • Quotes • Forms
{% endblock %}

{% block styles %}{% endblock %}

{% block content %}
<div class="container py-4">

  <!-- 1) TODAY + DRIVE IMAGE CARD -->
  <section class="section">
    <div class="row g-4">
      <div class="col-lg-7" id="today_timetable">
        <div class="card shadow">
          <div class="card-header bg-info text-white fw-bold">
            🕒 Today’s Timetable (Example)
          </div>
          <div class="card-body">
            <div class="table-responsive">
              <table class="table table-hover align-middle">
                <thead>
                  <tr>
                    {% for title in sheet_title %}
                    <th>{{ title }}</th>
                    {% endfor %}
                  </tr>
                </thead>
                <tbody>
                  {% for val in today_date %}
                  {% set row = sheet_content[val[0] - 2] %}
                  <tr>
                    <td>{{ val[1] }}</td>
                    <td>{{ row[1] }}</td>
                    <td>{{ row[2] }}</td>
                    <td><a href="https://meet.google.com/your-link" target="_blank">{{ row[3] }}</a></td>
                    <td>{{ row[4] }}</td>
                    <td>{{ row[5] }}</td>
                  </tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
            <a class="btn btn-outline-info" href="#" role="button">Add to Calendar (.ics)</a>
          </div>
        </div>
      </div>

      <div class="col-lg-5" id="daily_motivation">
        <div class="card shadow h-100">
          <div class="card-header bg-success text-white fw-bold">💡 Daily Motivation</div>
          <div class="card-body text-center">
            <div style="position: relative; width: 100%; padding-top: 56.25%; overflow: hidden;">
              <img class="hero-img"
                   src="{{ url_for('drive_image', file_id='1tTyhT4O_LoCaU8OL0DcCjuVm3o1z9gzY') }}"
                   alt="Motivation Image"
                   loading="lazy"
                   style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; object-fit: contain;" />
            </div>
            <h5 class="card-title mt-3">“Stay Consistent”</h5>
            <p class="card-text small text-muted">
              Success = small efforts repeated daily.
            </p>
            <a class="btn btn-outline-success" href="https://drive.google.com/drive/folders/1A7Hu5mNYkaUS1wVtCtoVMEd2u7QxZy3h?usp=drive_link">See all quotes</a>
          </div>
        </div>
      </div>

      <!-- 2) IMAGE GALLERY -->
      <section class="section">
        <h4 class="mb-3">🖼️ Motivation / Prayers / Verses (Gallery)</h4>
        <div class="row g-3">
          {% for item in dr_values %}
          <div class="col-6 col-md-4 col-lg-3">
            <div class="card shadow-sm h-100">
              <div style="position: relative; width: 100%; padding-top: 100%; overflow: hidden;">
                <img class="thumb"
                     src="{{ url_for('drive_image', file_id=item[1][1]) }}"
                     alt="{{ item[0] }}"
                     loading="lazy"
                     style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; object-fit: cover;" />
              </div>
              <div class="card-body">
                <div class="fw-bold small text-truncate">{{ item[0] }}</div>
                <div class="text-muted small text-truncate">{{ item[1][0] }}</div>
              </div>
            </div>
          </div>
          {% endfor %}
        </div>
      </section>
    </div>
  </section>

  <!-- 3) FULL TIMETABLE (WEEK) -->
  <section class="section">
    <h4 class="mb-3">📅 Full Timetable (Week)</h4>
    <div class="table-responsive">
      <table class="table table-striped table-hover align-middle">
        <thead class="table-primary">
          <tr>
            {% for title in sheet_title %}
            <th>{{ title }}</th>
            {% endfor %}
          </tr>
        </thead>
        <tbody>
          {% for value in sheet_content %}
          <tr>
            {% for val in value %}
            <td>{{ val }}</td>
            {% endfor %}
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </section>

  <!-- 4) LINKS: DRIVE + GOOGLE FORMS -->
  <section class="section">
    <div class="row g-4" id="notes_and_ressources">
      <div class="col-md-6">
        <div class="card shadow">
          <div class="card-header bg-warning fw-bold">📂 Notes & Resources (Drive)</div>
          <div class="card-body">
            <p class="mb-2">All PDFs and notes are in the shared Drive:</p>
            <a class="btn btn-warning" target="_blank" href="https://drive.google.com/drive/folders/16iL3bG1KHn3l-RDtrgof2kkA3DAILgbo">
              Open Shared Drive
            </a>
            <p class="mt-3 small text-muted">Tip: keep folders by subject → Math / Physics / Chemistry…</p>
          </div>
        </div>
      </div>

      <div class="col-md-6" id="google_forms_quizzes">
        <div class="card shadow">
            <div class="card-header bg-secondary text-white fw-bold">📝 Google Forms (Quizzes)</div>
            <div class="card-body">
                {% if form_list %}
                    <ul class="list-group">
                        {% for f in form_list %}
                            {% set fname = f.get('name') or f.get('title') or 'Untitled Form' %}
                            {% set link = f.get('webViewLink') or ('https://docs.google.com/forms/d/' ~ f.get('id') ~ '/viewform') %}
    
                            {# ensure the link is always the answerer format #}
                            {% if '/viewform' not in link %}
                                {% set link = 'https://docs.google.com/forms/d/' ~ f.get('id') ~ '/viewform' %}
                            {% endif %}
    
                            <li class="list-group-item d-flex justify-content-between align-items-center">
                                <div>
                                    <div class="fw-bold">{{ fname }}</div>
                                    {% if f.get('description') %}
                                        <div class="small text-muted">{{ f.get('description') }}</div>
                                    {% endif %}
                                </div>
                                <a class="btn btn-outline-secondary" 
                                   target="_blank" 
                                   rel="noopener noreferrer" 
                                   href="{{ link }}">
                                   Open
                                </a>
                            </li>
                        {% endfor %}
                    </ul>
                {% else %}
                    <div class="small text-muted">
                        No forms found. Ensure <code>FORMS_FOLDER_ID</code> is set and the service account has Viewer access to the folder.
                    </div>
                {% endif %}
            </div>
        </div>
    </div>
    
    
    
      
    </div>
  </section>

</div>
{% endblock %}

health.html
{% extends "base.html" %}
{% block title %}Health Advice — Newton's Nerds{% endblock %}

{% block styles %}
<style>
  .plant-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap:14px; }
  .plant-card { padding:14px; border-radius:10px; background:var(--card-bg); box-shadow:0 6px 18px rgba(0,0,0,0.04); }
  .virtues { font-size:0.85rem; color:var(--muted); margin-bottom:8px; }
  .search-row { display:flex; gap:8px; align-items:center; margin-bottom:12px; }
  @media (max-width:600px){ .search-row{ flex-direction:column; align-items:stretch } }
</style>
{% endblock %}

{% block content %}
<div class="container py-4">
  <div class="card p-3">
    <div class="d-flex justify-content-between align-items-start mb-3">
      <div>
        <h3 class="mb-1">Health Advice — Plants & Uses</h3>
        <div class="text-muted small">Informational — not medical advice.</div>
      </div>
      <div class="text-end">
        <a class="btn btn-outline-secondary btn-sm" href="{{ url_for('home_page') }}">Back home</a>
      </div>
    </div>

    <div class="search-row">
      <input id="searchInput" class="form-control" placeholder="Search plant name or virtue...">
      <select id="filterVirtue" class="form-select" style="max-width:260px;">
        <option value="">Filter by virtue (optional)</option>
      </select>
    </div>

    <div class="plant-grid" id="plantList">
      {% for p in plants %}
        <div class="plant-card" data-name="{{ p.name|lower }}" data-virtues="{{ p.virtues|join(' ')|lower }}">
          <h5 class="mb-1">{{ p.name }}</h5>
          <div class="virtues">{{ p.virtues|join(', ') }}</div>
          <p style="margin-top:8px;">{{ p.uses }}</p>
          <div class="d-flex gap-2 mt-2">
            <a class="btn btn-sm btn-outline-primary" href="{{ url_for('health_detail', slug=p.slug) }}">Read more</a>
            <button class="btn btn-sm btn-outline-success copy-info" data-info="{{ p.name }} — {{ p.uses }}">Copy Info</button>
          </div>
        </div>
      {% endfor %}
    </div>
  </div>
</div>

<script>
const cards = Array.from(document.querySelectorAll('.plant-card'));
const searchInput = document.getElementById('searchInput');
const filterVirtue = document.getElementById('filterVirtue');

// populate virtues list
const virtuesSet = new Set();
cards.forEach(card => {
  const v = (card.dataset.virtues || '').split(/\s+/).filter(Boolean);
  v.forEach(x => virtuesSet.add(x));
});
Array.from(virtuesSet).sort().forEach(v => {
  const opt = document.createElement('option'); opt.value = v; opt.textContent = v;
  filterVirtue.appendChild(opt);
});

function filterPlants(){
  const q = (searchInput.value||'').toLowerCase().trim();
  const fv = (filterVirtue.value||'').toLowerCase().trim();
  cards.forEach(card => {
    const name = card.dataset.name || '';
    const vs = card.dataset.virtues || '';
    const match = (name.includes(q) || vs.includes(q) || q === '');
    const virtok = (!fv) || vs.includes(fv);
    card.style.display = (match && virtok) ? '' : 'none';
  });
}

searchInput.addEventListener('input', filterPlants);
filterVirtue.addEventListener('change', filterPlants);

document.querySelectorAll('.copy-info').forEach(b => {
  b.addEventListener('click', () => {
    const info = b.dataset.info || '';
    navigator.clipboard.writeText(info).then(()=> {
      const old = b.textContent;
      b.textContent = 'Copied ✓';
      setTimeout(()=> b.textContent = old, 1000);
    });
  });
});
</script>
{% endblock %}

health_detail.html
{% extends "base.html" %}
{% block title %}{{ plant.name }} — Health Info{% endblock %}

{% block styles %}
<style>
  .virtue-badge { display:inline-block; margin-right:6px; margin-bottom:6px; padding:6px 8px; border-radius:999px; background:rgba(0,0,0,0.04); font-weight:600; font-size:0.85rem; }
</style>
{% endblock %}

{% block content %}
<div class="container py-4">
  <div class="card p-3">
    <div class="d-flex justify-content-between align-items-start">
      <div>
        <h3 class="mb-1">{{ plant.name }}</h3>
        <div class="small text-muted">Virtues:
          {% for v in plant.virtues %}
            <span class="virtue-badge">{{ v }}</span>
          {% endfor %}
        </div>
      </div>
      <div class="text-end">
        <a class="btn btn-outline-secondary btn-sm" href="{{ url_for('health_index') }}">Back to list</a>
      </div>
    </div>

    <hr>

    <h5>Traditional uses</h5>
    <p>{{ plant.uses }}</p>

    <h5>Notes / Precautions</h5>
    <p>{{ plant.notes or 'None specified.' }}</p>

    {% if plant.source %}
      <div class="mt-3">
        <strong>Source:</strong>
        <a href="{{ plant.source }}" target="_blank" rel="noopener noreferrer">{{ plant.source }}</a>
      </div>
    {% endif %}

    <div class="mt-3">
      <a href="{{ url_for('health_index') }}" class="btn btn-outline-secondary">Back to list</a>
    </div>
  </div>
</div>
{% endblock %}
Pls, all forms of errors or functionalities not functioning