// Run dock + degraded-mode banner.
//
//   * Run dock: a small card, bottom-right on every page, showing the running
//     (or just-finished) job with its phase and elapsed time, so a run no
//     longer owns the page it was started from. Explicit states for a lost
//     connection and for a server restart (jobs live in server memory, so a
//     restart stops the run — the dock says so instead of silently emptying).
//   * Health banner: a slim strip when the credential store is unreachable.
//
// Like palette.js this loads once from <head>; hx-boost replaces <body> on every
// navigation, so the DOM nodes are re-attached after each swap while the state
// lives in this closure. All dynamic text goes through textContent.
(function () {
  'use strict';

  var JOB_URL = '/api/jobs/active';
  var STATUS_URL = '/api/system/status';
  var RECHECK_URL = '/api/system/recheck';
  var POLL_MS = 2500;
  var RETRY_MS = 6000;
  var HEALTH_MS = 30000;
  var FAILS_BEFORE_OFFLINE = 2;

  var KEY_BOOT = 'atlas.boot';
  var KEY_JOB = 'atlas.dock.job';
  var KEY_SEEN = 'atlas.dock.seen';

  var dock = null;
  var banner = null;
  var state = { job: null, offline: false, restarted: false, auth: false, fails: 0 };
  var tick = null;
  var lastHealthAt = 0;
  var pollTimer = null;

  function store(op, key, value) {
    try {
      if (op === 'get') return sessionStorage.getItem(key);
      if (op === 'set') sessionStorage.setItem(key, value);
      if (op === 'del') sessionStorage.removeItem(key);
    } catch (e) { /* storage may be blocked; the dock still works without it */ }
    return null;
  }

  function csrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.content : '';
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function fmt(sec) {
    sec = Math.max(0, Math.floor(sec));
    var m = Math.floor(sec / 60), s = sec % 60;
    return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }

  var ICON_OK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
  var ICON_BAD = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  var ICON_WARN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8v5M12 17h.01"/><circle cx="12" cy="12" r="9.5"/></svg>';

  // ── Run dock ──────────────────────────────────────────────────────────

  function onOwnJobPage(job) {
    if (!job) return false;
    var p = location.pathname;
    return p === '/jobs/' + job.id || location.search.indexOf('job=' + job.id) !== -1;
  }

  function ensureDock() {
    if (dock && document.body.contains(dock)) return dock;
    dock = el('div', 'atlas-dock');
    dock.id = 'atlas-run-dock';
    dock.setAttribute('role', 'region');
    dock.setAttribute('aria-label', 'Audit run status');
    dock.hidden = true;
    document.body.appendChild(dock);
    return dock;
  }

  function button(label, cls, onClick, href) {
    var b = href ? el('a', 'atlas-dock__btn ' + (cls || ''), label) : el('button', 'atlas-dock__btn ' + (cls || ''), label);
    if (href) { b.href = href; } else { b.type = 'button'; }
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }

  function cancelJob(btn, id) {
    btn.disabled = true;
    btn.textContent = 'Cancelling…';
    fetch('/jobs/' + encodeURIComponent(id) + '/cancel', { method: 'POST', headers: { 'X-CSRF-Token': csrf() } })
      .catch(function () { btn.disabled = false; btn.textContent = 'Cancel run'; });
  }

  function render() {
    var d = ensureDock();
    var job = state.job;
    var notice = null;

    if (state.restarted) {
      notice = { tone: 'warn', title: 'Server restarted',
        sub: 'A run that was in progress was stopped. Start it again from the session page.', icon: ICON_WARN };
    } else if (state.auth) {
      notice = { tone: 'warn', title: 'Session expired',
        sub: 'Open a fresh login link with: platform-atlas-webui login-url', icon: ICON_WARN };
    }

    var hasContent = notice || (job && !onOwnJobPage(job)) || (state.offline && job);
    if (!hasContent) { d.hidden = true; d.className = 'atlas-dock'; return; }

    d.hidden = false;
    d.textContent = '';
    var row = el('div', 'atlas-dock__row');
    var icon = el('span', 'atlas-dock__icon');
    var text = el('div', 'atlas-dock__text');
    var title = el('div', 'atlas-dock__title');
    var sub = el('div', 'atlas-dock__sub');
    text.appendChild(title); text.appendChild(sub);

    var cls = 'atlas-dock';
    var actions = el('div', 'atlas-dock__actions');

    if (notice) {
      cls += ' is-' + notice.tone;
      icon.innerHTML = notice.icon;
      title.textContent = notice.title;
      sub.textContent = notice.sub;
      sub.style.whiteSpace = 'normal';
      actions.appendChild(button('Dismiss', '', function () {
        state.restarted = false; state.auth = false; render();
      }));
    } else if (job.active) {
      cls += ' is-running';
      icon.appendChild(el('span', 'atlas-dock__spin'));
      title.textContent = state.offline ? 'Reconnecting…' : (job.name || 'Audit running');
      sub.textContent = state.offline
        ? 'The run continues on the server. This updates when the connection returns.'
        : (job.phase || job.message || 'Working…');
      if (state.offline) sub.style.whiteSpace = 'normal';
      row.appendChild(icon); row.appendChild(text);
      var t = el('span', 'atlas-dock__time', fmt(job.elapsed_s));
      t.dataset.start = String(Date.now() - job.elapsed_s * 1000);
      row.appendChild(t);
      actions.appendChild(button('Open', 'atlas-dock__btn--primary', null, job.url));
      var c = button('Cancel run', '', null);
      c.addEventListener('click', function () { cancelJob(c, job.id); });
      actions.appendChild(c);
    } else {
      var ok = job.status === 'succeeded';
      cls += ok ? ' is-ok' : ' is-bad';
      icon.innerHTML = ok ? ICON_OK : ICON_BAD;
      title.textContent = (ok ? 'Finished: ' : 'Failed: ') + (job.name || 'run');
      sub.textContent = ok ? 'Completed in ' + fmt(job.elapsed_s) : (job.error || 'See the log for details.');
      actions.appendChild(button(ok ? 'View' : 'View log', 'atlas-dock__btn--primary', null, ok ? job.url : job.log_url));
      actions.appendChild(button('Dismiss', '', function () { store('set', KEY_SEEN, job.id); state.job = null; render(); }));
    }

    if (notice || !job.active) { row.appendChild(icon); row.appendChild(text); }
    d.className = cls;
    d.appendChild(row);
    if (job && job.active && !notice) d.appendChild(el('div', 'atlas-dock__bar')).appendChild(el('i'));
    d.appendChild(actions);

    if (!tick) {
      tick = setInterval(function () {
        var t2 = document.querySelector('#atlas-run-dock .atlas-dock__time');
        if (t2 && t2.dataset.start) t2.textContent = fmt((Date.now() - Number(t2.dataset.start)) / 1000);
      }, 1000);
    }
  }

  function applyJob(payload) {
    state.fails = 0;
    state.offline = false;
    state.auth = false;
    store('set', 'atlas.dock.ok', '1');

    var prevBoot = store('get', KEY_BOOT);
    var wasRunning = store('get', KEY_JOB);
    if (prevBoot && payload.boot_id && prevBoot !== payload.boot_id && wasRunning) {
      state.restarted = true;
    }
    if (payload.boot_id) store('set', KEY_BOOT, payload.boot_id);

    var job = payload.job;
    if (job && !job.active && store('get', KEY_SEEN) === job.id) job = null;   // already dismissed
    state.job = job;
    if (job && job.active) store('set', KEY_JOB, job.id); else store('del', KEY_JOB);
    render();
  }

  function poll() {
    clearTimeout(pollTimer);
    if (document.hidden) { pollTimer = setTimeout(poll, POLL_MS); return; }
    fetch(JOB_URL, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
      .then(function (r) {
        if (r.status === 401) { state.auth = !!store('get', 'atlas.dock.ok'); render(); throw new Error('auth'); }
        var ct = r.headers.get('content-type') || '';
        if (!r.ok || ct.indexOf('json') === -1) throw new Error('bad');
        return r.json();
      })
      .then(function (p) { applyJob(p); pollTimer = setTimeout(poll, POLL_MS); })
      .catch(function (e) {
        // Only talk about connection trouble once we have heard from the server
        // in this browser session (so /setup and login pages stay quiet).
        if (e && e.message !== 'auth' && store('get', 'atlas.dock.ok')) {
          state.fails += 1;
          if (state.fails >= FAILS_BEFORE_OFFLINE) { state.offline = true; render(); }
        }
        pollTimer = setTimeout(poll, RETRY_MS);
      });
  }

  // ── Health banner ─────────────────────────────────────────────────────

  function ensureBanner() {
    if (banner && document.body.contains(banner)) return banner;
    var host = document.querySelector('.app-main-inner');
    if (!host) return null;
    banner = el('div', 'atlas-health');
    banner.id = 'atlas-health-banner';
    banner.setAttribute('role', 'status');
    banner.hidden = true;
    host.insertBefore(banner, host.firstChild);
    return banner;
  }

  var lastHealth = null;

  function renderHealth() {
    var b = ensureBanner();
    if (!b) return;
    var c = lastHealth;
    if (!c || c.ok || c.supported === false || location.pathname === '/system') { b.hidden = true; return; }
    b.hidden = false;
    b.textContent = '';
    var msg = el('div', 'atlas-health__msg');
    var strong = el('strong', null, 'Credential store unreachable. ');
    msg.appendChild(strong);
    msg.appendChild(document.createTextNode(
      'You can still browse sessions and reports. Runs and checks that need credentials will fail until it is back.'
      + (c.hint ? ' ' + c.hint : '')));
    b.appendChild(msg);
    var actions = el('div', 'atlas-health__actions');
    var retry = button('Retry', '', function () {
      retry.disabled = true; retry.textContent = 'Checking…';
      fetch(RECHECK_URL, { method: 'POST', headers: { 'X-CSRF-Token': csrf() } })
        .then(function (r) { return r.json(); })
        .then(function (p) { lastHealth = p.credentials; renderHealth(); })
        .catch(function () { retry.disabled = false; retry.textContent = 'Retry'; });
    });
    actions.appendChild(retry);
    actions.appendChild(button('Details', '', null, '/system'));
    b.appendChild(actions);
  }

  function pollHealth() {
    lastHealthAt = Date.now();
    fetch(STATUS_URL, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
      .then(function (r) {
        var ct = r.headers.get('content-type') || '';
        if (!r.ok || ct.indexOf('json') === -1) throw new Error('bad');
        return r.json();
      })
      .then(function (p) { lastHealth = p.credentials; renderHealth(); })
      .catch(function () { /* the job poll already reports connection trouble */ });
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────

  function afterSwap() {
    render();
    renderHealth();
    if (Date.now() - lastHealthAt > 15000) pollHealth();
  }

  document.addEventListener('htmx:afterSettle', afterSwap);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { poll(); } });
  setInterval(function () { if (!document.hidden) pollHealth(); }, HEALTH_MS);

  function boot() { poll(); pollHealth(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
}());
