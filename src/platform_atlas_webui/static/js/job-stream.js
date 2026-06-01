(function () {
  var streamEl = document.getElementById('job-stream');
  if (!streamEl) return;

  var jobId = streamEl.dataset.jobId;
  var startedAt = parseFloat(streamEl.dataset.startedAt) || 0;
  var statusEl = document.getElementById('job-status');
  var killBar = document.getElementById('kill-bar');
  var killBtn = document.getElementById('kill-btn');
  var evt = new EventSource('/jobs/' + jobId + '/stream');
  var isTerminal = false;
  var errorCount = 0;
  var killTimer = null;

  // Cap the rendered buffer at this many rows. The full event log is
  // persisted server-side; this is purely DOM hygiene to keep long
  // pipelines (100 rules × N nodes) from accumulating thousands of
  // children and pegging layout/paint.
  var BUFFER_CAP = 2000;

  // ── Cleanup on hx-boost navigation away ────────────────────────
  // Without this the EventSource keeps the connection open until the
  // tab is closed — every job page visited leaks one persistent SSE.
  // `once: true` so accumulated listeners don't pile up across nav.
  function cleanup() {
    if (evt) { try { evt.close(); } catch (_) {} evt = null; }
    if (killTimer) { clearTimeout(killTimer); killTimer = null; }
  }
  document.addEventListener('htmx:beforeSwap', cleanup, { once: true });
  // pagehide covers tab close, hard reload, and bfcache evictions —
  // releases the server-side stream FD faster than waiting for TCP RST.
  window.addEventListener('pagehide', cleanup, { once: true });

  // ── rAF-batched auto-scroll ────────────────────────────────────
  // Setting scrollTop after every appended line forces a synchronous
  // layout. With high-frequency events (preflight phase, large fleets)
  // that's hundreds of forced reflows per second. One scroll per paint
  // frame is indistinguishable to the user and dramatically cheaper.
  var pendingScroll = false;
  function scheduleScroll() {
    if (pendingScroll) return;
    pendingScroll = true;
    requestAnimationFrame(function () {
      streamEl.scrollTop = streamEl.scrollHeight;
      pendingScroll = false;
    });
  }

  function trimBuffer() {
    while (streamEl.children.length > BUFFER_CAP) {
      streamEl.removeChild(streamEl.firstChild);
    }
  }

  // ── Output mode toggle (human vs raw) ──────────────────────────
  // Default to human-friendly; raw additionally shows kind='debug'
  // events forwarded from the platform_atlas Python logger. State is
  // persisted in localStorage so the choice survives page reloads.
  var MODE_KEY = 'atlas-job-stream-mode';
  var savedMode = localStorage.getItem(MODE_KEY) || 'human';
  function applyMode(mode) {
    streamEl.classList.remove('mode-human', 'mode-raw');
    streamEl.classList.add('mode-' + mode);
    var hint = document.getElementById('stream-mode-hint');
    if (hint) {
      hint.textContent = mode === 'raw'
        ? 'curated narrative + raw debug logs'
        : 'curated narrative only';
    }
  }
  applyMode(savedMode);

  var modeWrap = document.getElementById('stream-mode');
  if (modeWrap) {
    modeWrap.dataset.mode = savedMode;
    var pills = modeWrap.querySelectorAll('.stream-mode-pill');
    pills.forEach(function (pill) {
      var isCurrent = pill.dataset.mode === savedMode;
      pill.classList.toggle('is-active', isCurrent);
      pill.setAttribute('aria-selected', isCurrent ? 'true' : 'false');
      pill.addEventListener('click', function () {
        var mode = pill.dataset.mode;
        applyMode(mode);
        modeWrap.dataset.mode = mode;
        localStorage.setItem(MODE_KEY, mode);
        pills.forEach(function (p) {
          var active = p.dataset.mode === mode;
          p.classList.toggle('is-active', active);
          p.setAttribute('aria-selected', active ? 'true' : 'false');
        });
      });
    });
  }

  // ── Kill button timer ──────────────────────────────────────────
  if (killBar && startedAt) {
    var elapsed = Date.now() / 1000 - startedAt;
    var delay = Math.max(0, 300 - elapsed) * 1000;
    killTimer = setTimeout(function () {
      killTimer = null;
      // isConnected guard handles the race where the timer fires after
      // the user has already navigated and the killBar node is detached.
      if (!isTerminal && killBar.isConnected) {
        killBar.removeAttribute('hidden');
        killBar.style.display = 'flex';
      }
    }, delay);
  }

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
  }

  // Bind directly to the button instead of exposing a window.forceStopJob
  // global. Avoids name collisions and lets the closure capture jobId
  // without re-reading dataset on each click.
  if (killBtn) {
    killBtn.addEventListener('click', function () {
      killBtn.disabled = true;
      killBtn.textContent = 'Stopping…';
      fetch('/jobs/' + jobId + '/cancel', {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrfToken() },
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.ok) {
            killBtn.disabled = false;
            killBtn.textContent = 'Force stop';
          }
        })
        .catch(function () {
          killBtn.disabled = false;
          killBtn.textContent = 'Force stop';
        });
    });
  }

  // ── SSE stream ─────────────────────────────────────────────────
  function formatTs(epoch) {
    return new Date(epoch * 1000).toLocaleTimeString([], {
      hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  }

  function appendLine(payload) {
    var line = document.createElement('div');
    line.className = 'job-line job-line--' + (payload.kind || 'info');
    line.textContent = '[' + formatTs(payload.timestamp) + '] ' + payload.message;
    streamEl.appendChild(line);
    trimBuffer();
    scheduleScroll();
  }

  var STATUS_LABELS = { pass: 'PASS', fail: 'FAIL', skip: 'SKIP', warn: 'WARN' };

  function appendCheck(payload) {
    var data = payload.data || {};
    var status = (data.status || 'skip').toLowerCase();
    var row = document.createElement('div');
    row.className = 'check-row check-row--' + status;

    var dot = document.createElement('span');
    dot.className = 'check-dot check-dot--' + status;

    var name = document.createElement('span');
    name.className = 'check-name';
    name.textContent = data.name || payload.message;

    var badge = document.createElement('span');
    badge.className = 'check-badge check-badge--' + status;
    badge.textContent = STATUS_LABELS[status] || status.toUpperCase();

    var msg = document.createElement('span');
    msg.className = 'check-msg';
    msg.textContent = data.message || '';

    row.appendChild(dot);
    row.appendChild(name);
    row.appendChild(badge);
    if (data.message) { row.appendChild(msg); }

    if (data.details) {
      var det = document.createElement('span');
      det.className = 'check-detail';
      det.title = data.details;
      det.textContent = data.details;
      row.appendChild(det);
    }

    streamEl.appendChild(row);
    trimBuffer();
    scheduleScroll();
  }

  // ── Preflight target cards (live status on /preflight) ───────
  // When the page contains [data-target-name] cards, route SSE events to
  // them: marching-ants while checking, green/red/amber on completion.
  // Independent of the inline checks panel below, which is only present
  // on the dedicated job page.
  var pfCards = document.querySelectorAll('[data-target-name]');
  var hasCards = pfCards.length > 0;
  var pfSummary = document.getElementById('pf-summary');
  var pfCounts = { pass: 0, fail: 0, warn: 0, skip: 0, total: 0 };

  // Build name/module indexes once instead of linear-scanning per event.
  var pfByName = {};
  var pfByModule = {};
  pfCards.forEach(function (c) {
    var n = c.dataset.targetName;
    if (n) pfByName[n] = c;
    var mods = (c.dataset.targetModules || '').split(',');
    mods.forEach(function (m) {
      m = m.trim();
      // First-wins: matches the previous linear-scan semantics.
      if (m && !pfByModule[m]) pfByModule[m] = c;
    });
  });

  function pfCardByName(name) { return name ? (pfByName[name] || null) : null; }
  function pfCardByModule(mod) { return mod ? (pfByModule[mod] || null) : null; }

  function pfMarkChecking(card) {
    if (!card) return;
    if (card.dataset.pfState === 'checking') return;
    card.dataset.pfState = 'checking';
    var badge = card.querySelector('.pf-status-badge');
    if (badge) { badge.textContent = 'CHECKING'; badge.hidden = false; badge.style.color=''; badge.style.borderColor=''; badge.style.background=''; }
  }
  // Worst-status-wins so a single failure on a target doesn't get
  // overwritten by a later passing check on the same card.
  var STATUS_WEIGHT = { pass: 1, skip: 2, warn: 3, fail: 4 };
  function pfMarkResult(card, status) {
    if (!card || !status) return;
    var s = String(status).toLowerCase();
    if (!STATUS_WEIGHT[s]) return;
    var prev = card.dataset.pfState;
    if (STATUS_WEIGHT[prev] && STATUS_WEIGHT[prev] >= STATUS_WEIGHT[s]) return;
    card.dataset.pfState = s;
    var badge = card.querySelector('.pf-status-badge');
    if (badge) { badge.textContent = s.toUpperCase(); badge.hidden = false; }
  }
  // Hoisted regexes — compiled once at module load instead of per-event.
  var RE_PROBE_SSH    = /Probing\s+SSH\s+→\s+(\S+)/i;
  var RE_CHECK_SVCS   = /Checking\s+services\s+on\s+(\S+?):/i;
  var RE_CONN_MONGO   = /Connecting\s+to\s+MongoDB/i;
  var RE_CONN_REDIS   = /Connecting\s+to\s+Redis/i;
  var RE_CONN_PLATFORM= /Connecting\s+to\s+Platform\s+OAuth/i;
  var RE_CONN_GW4     = /Connecting\s+to\s+Gateway4/i;
  var RE_TRAIL        = /[…\.\s]+$/;
  var RE_ARROW        = /[→]|->|—/;
  var RE_HOST_PARENS  = /\s*\([^)]*\)\s*$/;

  function pfStripTrailing(s) { return String(s || '').replace(RE_TRAIL, ''); }
  function pfCardFromInfo(message) {
    if (!message) return null;
    var m;
    m = message.match(RE_PROBE_SSH);     if (m) return pfCardByName(pfStripTrailing(m[1]));
    m = message.match(RE_CHECK_SVCS);    if (m) return pfCardByName(pfStripTrailing(m[1]));
    if (RE_CONN_MONGO.test(message))     return pfCardByModule('mongo');
    if (RE_CONN_REDIS.test(message))     return pfCardByModule('redis');
    if (RE_CONN_PLATFORM.test(message))  return pfCardByModule('platform');
    if (RE_CONN_GW4.test(message))       return pfCardByModule('gateway4_api');
    return null;
  }
  function pfCardFromCheck(data) {
    if (!data || !data.name) return null;
    var n = String(data.name);
    var match = n.match(RE_ARROW);
    if (match) {
      var idx = n.lastIndexOf(match[0]);
      var rhs = n.substring(idx + match[0].length).trim();
      rhs = rhs.replace(RE_HOST_PARENS, '').trim();
      if (rhs) {
        var card = pfCardByName(rhs);
        if (card) return card;
      }
    }
    if (/mongo/i.test(n))    return pfCardByModule('mongo');
    if (/redis/i.test(n))    return pfCardByModule('redis');
    if (/platform/i.test(n)) return pfCardByModule('platform');
    if (/gateway/i.test(n))  return pfCardByModule('gateway4_api');
    return null;
  }
  function pfFinalize() {
    pfCards.forEach(function (c) {
      if (c.dataset.pfState === 'checking' || c.dataset.pfState === 'pending') {
        pfMarkResult(c, 'skip');
      }
    });
    if (pfSummary) {
      pfCards.forEach(function (c) {
        var s = c.dataset.pfState;
        if (STATUS_WEIGHT[s]) { pfCounts[s] = (pfCounts[s]||0) + 1; pfCounts.total++; }
      });
      var pass = 0, fail = 0;
      pfCards.forEach(function (c) {
        if (c.dataset.pfState === 'pass') pass++;
        else if (c.dataset.pfState === 'fail') fail++;
      });
      pfSummary.textContent = pass + ' pass · ' + fail + ' fail';
      // Use the existing theme tokens so the colors track [data-mode]
      // and [data-theme] instead of being hardcoded oklch literals.
      pfSummary.style.background = fail ? 'var(--bad-soft)' : 'var(--ok-soft)';
      pfSummary.style.color = fail ? 'var(--bad)' : 'var(--ok)';
    }
  }

  // ── Preflight checks panel (preflight jobs only) ─────────────
  var pfCard = document.querySelector('[data-pf-logs]');
  var pfList = document.getElementById('pf-list');
  var pfCurrent = document.getElementById('pf-current');
  var pfCurrentText = document.getElementById('pf-current-text');
  var pfToggle = document.getElementById('pf-toggle-logs');
  var isPreflight = !!pfList;

  if (pfToggle && pfCard) {
    pfToggle.addEventListener('click', function () {
      var hidden = pfCard.getAttribute('data-pf-logs') === 'hidden';
      if (hidden) {
        pfCard.removeAttribute('data-pf-logs');
        pfToggle.textContent = 'Hide full output logs ↑';
        pfToggle.setAttribute('aria-expanded', 'true');
      } else {
        pfCard.setAttribute('data-pf-logs', 'hidden');
        pfToggle.textContent = 'Show full output logs ↓';
        pfToggle.setAttribute('aria-expanded', 'false');
      }
    });
  }

  function pfAddPhase(message) {
    if (!pfList) return;
    var div = document.createElement('div');
    div.className = 'pf-phase';
    div.textContent = message;
    pfList.appendChild(div);
  }
  function pfAddCheck(payload) {
    if (!pfList) return;
    var data = payload.data || {};
    var status = (data.status || 'skip').toLowerCase();
    var row = document.createElement('div');
    row.className = 'pf-row ' + status;
    var dot = document.createElement('span'); dot.className = 'pf-dot';
    var name = document.createElement('span'); name.className = 'pf-name';
    name.textContent = data.name || payload.message || '';
    var badge = document.createElement('span'); badge.className = 'pf-badge';
    badge.textContent = status.toUpperCase();
    row.appendChild(dot); row.appendChild(name); row.appendChild(badge);
    if (data.message && data.message !== data.name) {
      var msg = document.createElement('span'); msg.className = 'pf-msg';
      msg.textContent = data.message;
      row.appendChild(msg);
    }
    pfList.appendChild(row);
    pfHideCurrent();
  }
  function pfShowCurrent(text) {
    if (!pfCurrent) return;
    pfCurrentText.textContent = text;
    pfCurrent.hidden = false;
  }
  function pfHideCurrent() {
    if (!pfCurrent) return;
    pfCurrent.hidden = true;
  }
  var PF_ACTIVITY = [
    /^Probing\s+SSH\s+→\s+(.+?)\s*[…\.]*$/i,
    /^Checking\s+services\s+on\s+(.+?):/i,
    /^Connecting\s+to\s+(.+?)\s*[…\.]*$/i,
  ];
  function pfMaybeShowFromInfo(message) {
    if (!message || !pfCurrent) return;
    for (var i = 0; i < PF_ACTIVITY.length; i++) {
      var m = String(message).match(PF_ACTIVITY[i]);
      if (m) { pfShowCurrent(m[1]); return; }
    }
  }

  // ── Pipeline v2 (pp2) ─────────────────────────────────────────
  // Support-bundle-style phase stepper + contextual activity panels.
  // Three phases: Capture → Validate → Report.  Each phase gets a
  // stage-appropriate activity card (module list, agg pipelines,
  // validate categories, report checklist).  Terminal state renders
  // a hero success/error screen instead of the old inline mini-report.
  var isPipeline = streamEl.dataset.jobKind === 'pipeline';

  var pp2Phases = isPipeline ? [null,
    document.getElementById('pp2-ph-1'),
    document.getElementById('pp2-ph-2'),
    document.getElementById('pp2-ph-3'),
  ] : null;
  var pp2Conns = isPipeline ? [null,
    document.getElementById('pp2-conn-1'),
    document.getElementById('pp2-conn-2'),
  ] : null;
  var pp2RunningView = isPipeline ? document.getElementById('pp2-running-view') : null;
  var pp2SuccessView = isPipeline ? document.getElementById('pp2-success-view') : null;
  var pp2ErrorView   = isPipeline ? document.getElementById('pp2-error-view')   : null;
  var pp2StageNameEl = isPipeline ? document.getElementById('pp2-stage-name')   : null;
  var pp2ElapsedEl   = isPipeline ? document.getElementById('pp2-elapsed')      : null;
  var pp2StatusEl    = isPipeline ? document.getElementById('pp2-status')       : null;
  var pp2ActivityEl  = isPipeline ? document.getElementById('pp2-activity')     : null;
  var pp2LogLabelEl  = isPipeline ? document.getElementById('pp2-log-label')    : null;

  // Phase state (1-indexed; index 0 unused)
  var pp2State = [null, 'pending', 'pending', 'pending'];
  var pp2CurrentStage = 0;

  // Per-stage sub-state
  var pp2Tier      = '';     // 'standard' | 'extended'
  var pp2CollDone  = false;  // received "Collection pass complete"
  var pp2Modules   = [];     // module names from "  ✓ name" post-collection
  var pp2AggTotal  = 0;
  var pp2Aggs      = {};     // idx → {name,collection,status,meta}
  var pp2ValCats   = [];     // [{name,pass,fail,skip}]
  var pp2Reports   = { compliance: false, operational: false, architecture: false };
  var pp2ElapsedTimer = null;
  var pp2LastMsg    = '';   // last meaningful activity line during the silent collect wait
  var pp2TierDetail = '';   // e.g. "107 rules" extracted from the tier announcement

  // Elapsed clock
  if (isPipeline && pp2ElapsedEl && startedAt) {
    pp2ElapsedTimer = setInterval(function () {
      var s = Math.floor(Date.now() / 1000 - startedAt);
      var m = Math.floor(s / 60);
      pp2ElapsedEl.textContent = m + ':' + (s % 60 < 10 ? '0' : '') + (s % 60);
    }, 1000);
    // Register cleanup alongside the main EventSource cleanup
    document.addEventListener('htmx:beforeSwap', function () {
      if (pp2ElapsedTimer) { clearInterval(pp2ElapsedTimer); pp2ElapsedTimer = null; }
    }, { once: true });
    window.addEventListener('pagehide', function () {
      if (pp2ElapsedTimer) { clearInterval(pp2ElapsedTimer); pp2ElapsedTimer = null; }
    }, { once: true });
  }

  var PP2_CHECK = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
  var PP2_X     = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

  function pp2Esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function pp2SetPhase(n, status) {
    if (!pp2Phases || !pp2Phases[n]) return;
    var node = pp2Phases[n];
    var dot = node.querySelector('.pp2-dot');
    if (!dot) return;
    dot.dataset.status = status;
    node.classList.toggle('is-active', status === 'active');
    node.classList.toggle('is-done',   status === 'done');
    node.classList.toggle('is-error',  status === 'error');
    if (status === 'done')  dot.innerHTML = PP2_CHECK;
    if (status === 'error') dot.innerHTML = PP2_X;
  }

  function pp2ConnDone(n) {
    if (pp2Conns && pp2Conns[n]) pp2Conns[n].classList.add('done');
  }

  var PP2_LABELS = ['', 'Stage 1 of 3 — Capture', 'Stage 2 of 3 — Validate', 'Stage 3 of 3 — Report'];

  function pp2AdvanceTo(n) {
    if (!isPipeline || n < 1 || n > 3 || pp2CurrentStage >= n) return;
    for (var i = pp2CurrentStage; i < n; i++) {
      if (pp2State[i] && pp2State[i] !== 'error') {
        pp2State[i] = 'done'; pp2SetPhase(i, 'done'); pp2ConnDone(i);
      }
    }
    pp2State[n] = 'active';
    pp2CurrentStage = n;
    pp2SetPhase(n, 'active');
    if (pp2StageNameEl) pp2StageNameEl.textContent = PP2_LABELS[n] || '';
    pp2ResetActivity(n);
  }

  function pp2StagePass(n) {
    pp2State[n] = 'done'; pp2SetPhase(n, 'done'); pp2ConnDone(n);
  }

  function pp2StageFail(n) {
    if (n < 1 || n > 3) return;
    pp2State[n] = 'error'; pp2SetPhase(n, 'error');
  }

  function pp2AllDone() {
    for (var i = 1; i <= 3; i++) {
      if (pp2State[i] !== 'error') { pp2State[i] = 'done'; pp2SetPhase(i, 'done'); }
    }
    pp2ConnDone(1); pp2ConnDone(2);
    if (pp2StageNameEl) pp2StageNameEl.textContent = 'Pipeline complete';
  }

  // ── Activity panel ─────────────────────────────────────────
  function pp2ResetActivity(stage) {
    if (!pp2ActivityEl) return;
    if (stage === 1) {
      pp2CollDone = false; pp2Modules = []; pp2Aggs = {}; pp2AggTotal = 0;
      pp2LastMsg = ''; pp2TierDetail = '';
      pp2RenderCapture();
    } else if (stage === 2) {
      pp2ValCats = []; pp2RenderValidate();
    } else if (stage === 3) {
      pp2RenderReport();
    }
  }

  var ICON_CHECK_SM = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" style="color:var(--ok);flex-shrink:0"><polyline points="20 6 9 17 4 12"/></svg>';
  var ICON_CIRCLE_SM = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:var(--text-4);flex-shrink:0"><circle cx="12" cy="12" r="9"/></svg>';

  function pp2RenderCapture() {
    if (!pp2ActivityEl) return;
    var tierLabel = pp2Tier === 'standard' ? 'Standard' : pp2Tier === 'extended' ? 'Extended' : '';
    var tierCls   = pp2Tier === 'standard' ? 'pp2-tier-std' : 'pp2-tier-ext';
    var tierHtml  = tierLabel
      ? '<span class="pp2-tier-badge ' + tierCls + '">'
        + tierLabel + (pp2TierDetail ? ' · ' + pp2TierDetail : '') + '</span>'
      : '';
    var header = tierHtml ? '<div class="pp2-act-header">' + tierHtml + '</div>' : '';

    var body;
    if (Object.keys(pp2Aggs).length > 0) {
      body = pp2AggHtml();
    } else if (pp2CollDone && pp2Modules.length) {
      var grid = '<div class="pp2-mod-grid">';
      pp2Modules.forEach(function (name) {
        grid += '<div class="pp2-mod-item is-done">' + ICON_CHECK_SM + '<span>' + pp2Esc(name) + '</span></div>';
      });
      grid += '</div>';
      body = grid;
    } else {
      var mainMsg = pp2CollDone ? 'Analyzing collected data…' : 'Collecting data from targets…';
      body = '<div class="pp2-activity-msg"><span class="pp2-pulse-dot"></span>' + mainMsg + '</div>'
        + (pp2LastMsg ? '<div class="pp2-act-sub">' + pp2Esc(pp2LastMsg) + '</div>' : '');
    }

    pp2ActivityEl.innerHTML = header + '<div class="pp2-act-body">' + body + '</div>';
    pp2ActivityEl.removeAttribute('hidden');
  }

  function pp2AggHtml() {
    var keys = Object.keys(pp2Aggs).sort(function (a, b) { return +a - +b; });
    if (!keys.length) return '<div class="pp2-agg-empty">Starting pipelines…</div>';
    var html = '<div class="pp2-agg-list">';
    keys.forEach(function (k) {
      var ag = pp2Aggs[k];
      var cls = 'pp2-agg-row' + (ag.status === 'running' ? ' is-running' : ag.status === 'done' ? ' is-done' : ag.status === 'error' ? ' is-error' : '');
      var icon = ag.status === 'running' ? '●' : ag.status === 'done' ? '✓' : ag.status === 'error' ? '✗' : '○';
      html += '<div class="' + cls + '">'
        + '<span class="pp2-agg-icon">' + icon + '</span>'
        + '<span class="pp2-agg-name">' + pp2Esc(ag.name) + ' <span style="opacity:.5">→</span> ' + pp2Esc(ag.collection) + '</span>'
        + (ag.meta ? '<span class="pp2-agg-meta">' + pp2Esc(ag.meta) + '</span>' : '')
        + '<span class="pp2-agg-idx">' + k + '/' + pp2AggTotal + '</span>'
        + '</div>';
    });
    return html + '</div>';
  }

  function pp2RenderValidate() {
    if (!pp2ActivityEl) return;
    var html;
    if (!pp2ValCats.length) {
      html = '<div class="pp2-activity-msg"><span class="pp2-pulse-dot"></span>Evaluating rules…</div>';
    } else {
      var totPass = 0, totFail = 0, totSkip = 0;
      pp2ValCats.forEach(function(c) { totPass += c.pass; totFail += c.fail; totSkip += c.skip; });
      var summary = '<div class="pp2-val-summary">'
        + '<span style="color:var(--ok)">✓ ' + totPass + ' pass</span>'
        + (totFail ? ' <span style="color:var(--bad)">· ✗ ' + totFail + ' fail</span>' : '')
        + (totSkip ? ' <span style="color:var(--text-4)">· ' + totSkip + ' skip</span>' : '')
        + '</div>';
      html = summary + '<div class="pp2-cat-grid">';
      pp2ValCats.forEach(function (cat) {
        var hasFail = cat.fail > 0;
        html += '<div class="pp2-cat-row ' + (hasFail ? 'has-fail' : 'all-pass') + '">'
          + '<span class="pp2-cat-name">' + pp2Esc(cat.name) + '</span>'
          + '<span class="pp2-cat-counts"><b style="color:var(--ok)">' + cat.pass + '</b>'
          + (hasFail ? ' <span style="color:var(--bad)">· ' + cat.fail + '</span>' : '')
          + (cat.skip ? ' <span style="color:var(--text-4)">· ' + cat.skip + '</span>' : '')
          + '</span></div>';
      });
      html += '</div>';
    }
    pp2ActivityEl.innerHTML = html;
    pp2ActivityEl.removeAttribute('hidden');
  }

  function pp2RenderReport() {
    if (!pp2ActivityEl) return;
    var items = [
      { key: 'compliance',   label: 'Compliance report' },
      { key: 'operational',  label: 'Operational report', ext: true },
      { key: 'architecture', label: 'Architecture report' },
    ];
    var html = '<div class="pp2-report-list">';
    items.forEach(function (item) {
      if (item.ext && pp2Tier === 'standard') return;
      var done = pp2Reports[item.key];
      html += '<div class="pp2-report-item' + (done ? ' is-done' : '') + '">'
        + (done ? ICON_CHECK_SM : ICON_CIRCLE_SM)
        + '<span>' + pp2Esc(item.label) + '</span></div>';
    });
    pp2ActivityEl.innerHTML = html + '</div>';
    pp2ActivityEl.removeAttribute('hidden');
  }

  // ── Main pp2 event dispatcher ───────────────────────────────
  // Robust patterns — accept common Unicode variants (✓/✔, ✗/✘/×, →/->, ·/•, —/-)
  var PP2_CAT_RE  = /^\s+\S+\s+([\w][\w\-]*):\s+(\d+)\s+pass\s+[·•]\s+(\d+)\s+fail\s+[·•]\s+(\d+)\s+skip/;
  var PP2_AGG_RUN = /\[(\d+)\/(\d+)\]\s+Running\s+(\S+)\s+(?:→|->)\s+(\S+)/;
  var PP2_AGG_OK  = /\[(\d+)\/(\d+)\]\s+[✓✔]\s+(\S+)\s+[—\-]+\s+([\d,]+)\s+rows?\s+[·•]\s+([\d.]+)/;
  var PP2_AGG_ERR = /\[(\d+)\/(\d+)\]\s+[✗✘×]\s+(\S+)/;
  var PP2_SKIP    = [/^Stage\s+\d\s+of\s+3/i, /^Full pipeline\s+·/i, /^Three stages:/i, /^Pipeline\s+(complete|halted)/i];

  function pp2Handle(payload) {
    if (!isPipeline) return;
    var msg = payload.message || '';
    var kind = payload.kind || 'info';

    // Stage transitions (from run_full_pipeline_job)
    var sm = msg.match(/^Stage\s+(\d)\s+of\s+3/i);
    if (sm) { pp2AdvanceTo(+sm[1]); }

    var cm = msg.match(/^Stage\s+(\d)\s+complete\s+in/i);
    if (cm) { pp2StagePass(+cm[1]); }

    if (/^Pipeline complete$/i.test(msg.trim())) pp2AllDone();
    if (/^Pipeline halted at Capture/i.test(msg))  pp2StageFail(1);
    if (/^Pipeline halted at Validate/i.test(msg)) pp2StageFail(2);
    if (/^Report stage failed/i.test(msg))          pp2StageFail(3);

    // Live status message — skip boilerplate phase headers
    if (pp2StatusEl && kind !== 'debug') {
      var skip = PP2_SKIP.some(function (re) { return re.test(msg); });
      if (!skip && msg.trim()) pp2StatusEl.textContent = msg.replace(/\s+/g, ' ').trim();
    }

    // Stage-specific activity
    if (pp2CurrentStage === 1) {
      // Tier detection — also extract rule count for the badge
      if (!pp2Tier) {
        var tmM = msg.match(/Tier:\s+(Standard|Extended)/i);
        if (tmM) {
          pp2Tier = tmM[1].toLowerCase();
          var rmM = msg.match(/(\d+)\s+rules?/i);
          pp2TierDetail = rmM ? rmM[1] + ' rules' : '';
          pp2RenderCapture();
        }
      }
      // Collection complete — clear the sub-label and re-render
      if (/collection pass complete/i.test(msg) && !pp2CollDone) {
        pp2CollDone = true; pp2LastMsg = ''; pp2RenderCapture();
      }
      // During the silent collection wait, show the last meaningful message as a sub-label
      if (!pp2CollDone && kind !== 'debug') {
        var skipLast = PP2_SKIP.some(function(re) { return re.test(msg); })
          || /^Tier:/i.test(msg) || !msg.trim();
        if (!skipLast) {
          pp2LastMsg = msg.replace(/\s+/g, ' ').trim();
          if (!Object.keys(pp2Aggs).length) pp2RenderCapture();
        }
      }
      // Module names from "  ✓ / ✔ module" info events post-collection
      if (pp2CollDone && kind === 'info' && !Object.keys(pp2Aggs).length) {
        var mm = msg.match(/^\s+[✓✔]\s+(.+)$/);
        if (mm) { pp2Modules.push(mm[1].trim()); pp2RenderCapture(); }
      }
      // Aggregation phase start
      if (kind === 'phase' && /mongodb aggregation pipelines/i.test(msg)) {
        var scm = msg.match(/pipelines\s*\(([^)]+)\)/i);
        pp2AggTotal = 0;
        var aggTitle = '<div class="pp2-activity-title">MongoDB aggregation pipelines'
          + (scm ? ' — ' + pp2Esc(scm[1]) : '') + '</div>';
        if (pp2ActivityEl) {
          pp2ActivityEl.innerHTML = aggTitle + pp2AggHtml();
          pp2ActivityEl.removeAttribute('hidden');
        }
      }
      // Aggregation: pipeline starting
      var arM = msg.match(PP2_AGG_RUN);
      if (arM && kind === 'info') {
        pp2AggTotal = +arM[2];
        pp2Aggs[arM[1]] = { name: arM[3], collection: arM[4].replace(/[…\.]+$/, ''), status: 'running', meta: '' };
        if (pp2ActivityEl) pp2ActivityEl.innerHTML = '<div class="pp2-activity-title">MongoDB aggregation pipelines</div>' + pp2AggHtml();
        if (pp2ActivityEl) pp2ActivityEl.removeAttribute('hidden');
      }
      // Aggregation: pipeline done (info with ✓)
      var aoM = msg.match(PP2_AGG_OK);
      if (aoM && kind === 'info') {
        var ai = aoM[1];
        pp2Aggs[ai] = pp2Aggs[ai] || { name: aoM[3], collection: '?', status: 'done', meta: '' };
        pp2Aggs[ai].status = 'done';
        pp2Aggs[ai].meta = aoM[4] + ' rows · ' + Math.round(+aoM[5]) + ' ms';
        if (pp2ActivityEl) pp2ActivityEl.innerHTML = '<div class="pp2-activity-title">MongoDB aggregation pipelines</div>' + pp2AggHtml();
      }
      // Aggregation: pipeline failed (warning with ✗)
      var aeM = msg.match(PP2_AGG_ERR);
      if (aeM && kind === 'warning') {
        var ei = aeM[1];
        pp2Aggs[ei] = pp2Aggs[ei] || { name: aeM[3], collection: '?', status: 'error', meta: '' };
        pp2Aggs[ei].status = 'error';
        if (pp2ActivityEl) pp2ActivityEl.innerHTML = '<div class="pp2-activity-title">MongoDB aggregation pipelines</div>' + pp2AggHtml();
      }
    }

    if (pp2CurrentStage === 2) {
      var catM = msg.match(PP2_CAT_RE);
      if (catM && kind === 'info') {
        pp2ValCats.push({ name: catM[1], pass: +catM[2], fail: +catM[3], skip: +catM[4] });
        pp2RenderValidate();
      }
    }

    if (pp2CurrentStage === 3 && kind === 'success') {
      if (/compliance report/i.test(msg) && /ready/i.test(msg))  { pp2Reports.compliance  = true; pp2RenderReport(); }
      if (/operational report/i.test(msg) && /ready/i.test(msg)) { pp2Reports.operational = true; pp2RenderReport(); }
      if (/architecture report/i.test(msg) && /ready/i.test(msg)){ pp2Reports.architecture= true; pp2RenderReport(); }
    }
  }

  // ── Success / error terminal screens ───────────────────────
  function pp2ShowSuccess() {
    pp2AllDone();
    if (pp2ElapsedTimer) { clearInterval(pp2ElapsedTimer); pp2ElapsedTimer = null; }
    var sessName = streamEl.dataset.sessionName || '';
    if (!sessName) { pp2RevealSuccess({}); return; }
    fetch('/sessions/' + encodeURIComponent(sessName) + '/summary.json', { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function (d) { pp2RevealSuccess(d); })
      .catch(function ()  { pp2RevealSuccess({}); });
  }

  function pp2RevealSuccess(data) {
    if (!pp2RunningView || !pp2SuccessView) return;
    var pct  = data.compliance_pct || 0;
    var fail = data.fail_count  || 0;
    var pass = data.pass_count  || 0;
    var skip = data.skip_count  || 0;

    var titleEl = document.getElementById('pp2-hero-title');
    var subEl   = document.getElementById('pp2-hero-sub');
    var pctEl   = document.getElementById('pp2-score-pct');
    var passEl  = document.getElementById('pp2-pass-count');
    var failEl  = document.getElementById('pp2-fail-count');
    var skipEl  = document.getElementById('pp2-skip-count');

    if (titleEl) titleEl.textContent = fail === 0 ? 'All rules passed' : 'Audit complete — ' + fail + ' finding' + (fail !== 1 ? 's' : '');
    if (subEl) {
      var parts = [];
      if (data.environment) parts.push(data.environment);
      if (data.ruleset_id)  parts.push(data.ruleset_id);
      subEl.textContent = parts.join(' · ');
    }
    if (passEl) passEl.textContent = pass;
    if (failEl) failEl.textContent = fail;
    if (skipEl) skipEl.textContent = skip;
    if (pctEl)  pctEl.style.color  = fail > 0 ? (pct >= 80 ? 'var(--warn)' : 'var(--bad)') : 'var(--ok)';

    // Report links
    var sessName  = streamEl.dataset.sessionName || (data.name || '');
    var returnUrl = streamEl.dataset.returnUrl   || ('/sessions/' + encodeURIComponent(sessName));
    var compBtn  = document.getElementById('pp2-link-compliance');
    var sessBtn  = document.getElementById('pp2-link-session');
    var altRow   = document.getElementById('pp2-alt-row');
    if (compBtn && data.report_file_url) { compBtn.href = data.report_file_url; compBtn.removeAttribute('hidden'); }
    if (sessBtn) { sessBtn.href = returnUrl; sessBtn.removeAttribute('hidden'); }
    if (altRow)  { altRow.removeAttribute('hidden'); }

    pp2RunningView.setAttribute('hidden', '');
    pp2SuccessView.removeAttribute('hidden');

    // Animate percentage count-up
    if (pct > 0 && pctEl) {
      var t0 = performance.now(), DUR = 1200;
      (function frame(now) {
        var prog = Math.min(1, (now - t0) / DUR);
        var e = 1 - Math.pow(1 - prog, 4); // easeOutQuart
        pctEl.textContent = Math.round(pct * e) + '%';
        if (prog < 1) requestAnimationFrame(frame);
      }(performance.now()));
    } else if (pctEl) {
      pctEl.textContent = Math.round(pct) + '%';
    }
  }

  function pp2ShowError(errMsg) {
    if (pp2ElapsedTimer) { clearInterval(pp2ElapsedTimer); pp2ElapsedTimer = null; }
    if (!pp2RunningView || !pp2ErrorView) return;
    var errEl = document.getElementById('pp2-error-msg');
    if (errEl) errEl.textContent = errMsg || 'An error occurred. Check the output log for details.';
    pp2RunningView.setAttribute('hidden', '');
    pp2ErrorView.removeAttribute('hidden');
  }

  function pp2UpdateLogLabel() {
    if (!pp2LogLabelEl || !streamEl) return;
    pp2LogLabelEl.textContent = 'Live output (' + streamEl.children.length + ' lines)';
  }

  // ── Single dispatch for narrative events ───────────────────────
  // Was previously six listeners each calling JSON.parse on the same
  // payload — same JSON parsed 6× per event. One handler, one parse.
  function handleNarrative(e) {
    errorCount = 0;
    var payload;
    try { payload = JSON.parse(e.data); } catch (_) { return; }
    appendLine(payload);
    if (isPipeline) { pp2Handle(payload); pp2UpdateLogLabel(); }
    if (isPreflight) {
      if (payload.kind === 'phase') pfAddPhase(payload.message);
      else if (payload.kind === 'info') pfMaybeShowFromInfo(payload.message);
    }
    if (hasCards && payload.kind === 'info') {
      var c = pfCardFromInfo(payload.message);
      if (c) pfMarkChecking(c);
    }
  }
  ['info', 'success', 'warning', 'error', 'phase', 'debug'].forEach(function (k) {
    evt.addEventListener(k, handleNarrative);
  });

  evt.addEventListener('check', function (e) {
    errorCount = 0;
    var payload;
    try { payload = JSON.parse(e.data); } catch (_) { return; }
    appendCheck(payload);
    if (isPipeline) pp2UpdateLogLabel();
    if (isPreflight) pfAddCheck(payload);
    if (hasCards) {
      var c = pfCardFromCheck(payload.data);
      if (c) pfMarkResult(c, (payload.data || {}).status);
    }
  });

  evt.addEventListener('status', function (e) {
    // Close the stream FIRST so any late event delivered between flag-set
    // and close (the previous race) cannot append after the terminal line.
    if (evt) { try { evt.close(); } catch (_) {} evt = null; }
    isTerminal = true;
    if (killBar) { killBar.hidden = true; }
    var payload;
    try { payload = JSON.parse(e.data); } catch (_) { return; }
    if (statusEl) {
      statusEl.className = 'job-status job-status--' + (payload.data.status || 'failed');
      statusEl.textContent = payload.data.status || 'failed';
    }
    appendLine({
      kind: payload.data.status === 'succeeded' ? 'success' : 'error',
      message: 'Job ' + (payload.data.status || 'finished') + (payload.data.error ? ' — ' + payload.data.error : ''),
      timestamp: payload.timestamp,
    });
    if (isPreflight) pfHideCurrent();
    if (hasCards) pfFinalize();
    if (isPipeline) {
      var pp2ok = payload.data.status === 'succeeded';
      if (pp2ok) {
        pp2ShowSuccess();
      } else {
        if (pp2CurrentStage > 0 && pp2State[pp2CurrentStage] !== 'done' && pp2State[pp2CurrentStage] !== 'error') {
          pp2StageFail(pp2CurrentStage);
        }
        pp2ShowError(payload.data.error || '');
      }
    }
  });

  evt.onerror = function () {
    errorCount++;
    // EventSource auto-reconnects on transient blips; logging on every
    // retry produces a flood of "connection closed" lines for what is
    // really one momentary network hiccup. Emit one warning at the
    // third consecutive failure and stay quiet otherwise — successful
    // events reset the counter.
    if (errorCount === 3) {
      appendLine({
        kind: 'warning',
        message: 'Stream connection lost — retrying…',
        timestamp: Date.now() / 1000,
      });
    }
  };
})();
