/**
 * Atlas WebUI report — viewmodel hydration.
 *
 * Fetches /reports/{name}/viewmodel and paints the three tabbed panels
 * (Compliance / Operational / Architecture). All animations use anime.js
 * v4 (already loaded in base.html). The tab switching itself is driven
 * by Alpine.js inside the template — this script just feeds it data and
 * orchestrates entrance/exit animations and the sliding tab indicator.
 *
 * Idempotency: the script tag re-runs on every hx-boost navigation TO
 * this page. We bail early if a previous run already hydrated the same
 * root element (matched by data-vm-session).
 */
(function () {
  'use strict';

  const root = document.getElementById('vm-root');
  if (!root) return;
  if (root.dataset.vmHydrated === root.dataset.vmSession) return;
  root.dataset.vmHydrated = root.dataset.vmSession;

  const url = root.dataset.vmUrl;
  const animeAvailable = typeof window.anime === 'object' && typeof window.anime.animate === 'function';
  const prefersReducedMotion = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ─────────────────────────────────────────────────────────────
  // Status display labels
  //
  // The viewmodel sends raw statuses (PASS / FAIL / SKIP / ERROR) but
  // the report surfaces "Compliant" / "Non-compliant" instead — both
  // in the rules-table pill and in the filter chips. This map keeps
  // the source of truth in one place.
  // ─────────────────────────────────────────────────────────────
  const STATUS_LABELS = {
    PASS:       'Compliant',
    COMPLIANT:  'Compliant',
    FAIL:       'Non-compliant',
    'NON-COMPLIANT': 'Non-compliant',
    SKIP:       'Skipped',
    SKIPPED:    'Skipped',
    ERROR:      'Error',
    WARN:       'Warning',
    INFO:       'Info',
  };

  function statusLabel(status) {
    if (!status) return '';
    const key = String(status).toUpperCase();
    return STATUS_LABELS[key] || status;
  }

  function statusClassKey(status) {
    if (!status) return 'INFO';
    return String(status).toUpperCase().replace(/[^A-Z]/g, '');
  }

  // ─────────────────────────────────────────────────────────────
  // Fetch
  // ─────────────────────────────────────────────────────────────
  fetch(url, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' — ' + r.statusText);
      return r.json();
    })
    .then(hydrate)
    .catch(function (err) {
      const alpine = root._x_dataStack && root._x_dataStack[0];
      if (alpine) alpine.error = err.message || String(err);
      console.error('[Atlas report] viewmodel fetch failed:', err);
    });

  let _vm = null;
  let _ruleDetail = null;

  function hydrate(vm) {
    _vm = vm;
    paintIdentity(vm.session || {});
    paintTabCounts(vm);
    paintCompliance(vm.compliance || {}, vm.session || {});
    paintOperational(vm.operational || {});
    paintArchitecture(vm.architecture || {});

    // Pre-flight reset for animated values: zero them out RIGHT BEFORE
    // we show the panel, so the user never sees the static final value
    // flash before the rollup/sweep takes over. setCounterTarget keeps
    // the final value in dataset.target as the source-of-truth and the
    // graceful fallback when motion is reduced.
    if (animeAvailable && !prefersReducedMotion) {
      const initial = root.querySelector('[data-vm-panel="compliance"]');
      if (initial) {
        initial.querySelectorAll('[data-vm-counter]').forEach(function (el) {
          if (el.dataset.target && el.dataset.target !== '0') el.textContent = '0';
        });
        const rate = initial.querySelector('[data-vm-id="pass_rate"]');
        if (rate) rate.textContent = '0';
      }
    }

    const alpine = root._x_dataStack && root._x_dataStack[0];
    if (alpine) alpine.loaded = true;

    // Build the rule-detail slide-over (hooked once data is in DOM).
    _ruleDetail = createRuleDetailController();

    // Sliding tab indicator + sliding filter pill — both hook to Alpine's
    // class toggling via MutationObserver, so they re-position on every
    // tab/filter change automatically.
    wireTabIndicator();
    wireFilterPill();
    // Click-time reset for counters in the about-to-be-visible panel —
    // prevents the brief flash of final values when switching tabs
    // (Alpine flips display:block on the panel before the entrance
    // animation gets a chance to reset to "0"). Listener is registered
    // after Alpine's @click, so by the time we run, `tab` has already
    // been reassigned and we know which panel is target.
    wireTabPreflightReset();

    requestAnimationFrame(function () {
      requestAnimationFrame(function () { runEntranceAnimations(vm); });
    });

    // Re-run entrance animations on tab switch. The 2px accent
    // underline lives in CSS — no JS-driven slider element to nudge.
    if (alpine && window.Alpine && typeof window.Alpine.effect === 'function') {
      let lastTab = alpine.tab;
      window.Alpine.effect(function () {
        const tab = alpine.tab;
        if (tab !== lastTab) {
          lastTab = tab;
          requestAnimationFrame(function () { animatePanelEntrance(tab, vm); });
        }
      });
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Identity row
  // ─────────────────────────────────────────────────────────────
  function paintIdentity(session) {
    setText('[data-vm-id="organization_name"]', session.organization_name || '—');
    setText('[data-vm-id="environment"]', session.environment || '—');
    setText('[data-vm-id="tier"]', (session.tier || 'extended').toUpperCase());
    setText('[data-vm-id="hostname"]', session.hostname || '—');
    setText('[data-vm-id="platform_version"]', session.platform_version || '—');
    setText('[data-vm-id="captured_at"]', session.captured_at || '—');

    const ruleset = session.ruleset || {};
    const rs = ruleset.id
      ? (ruleset.id + ' v' + (ruleset.version || '?') + (ruleset.profile ? ' (' + ruleset.profile + ')' : ''))
      : '—';
    setText('[data-vm-id="ruleset"]', rs);

    // Kicker — "AUDIT REPORT · STANDARD TIER" (uppercase via CSS).
    const tier = (session.tier || '').toLowerCase();
    setText('[data-vm-kicker]', tier ? 'Audit Report · ' + tier + ' tier' : 'Audit Report');

    const sub = root.querySelector('[data-vm="session-subtitle"]');
    if (sub) {
      const parts = [];
      if (session.environment) parts.push(session.environment);
      if (session.tier) parts.push((session.tier || '').toUpperCase());
      if (session.captured_at) parts.push('captured ' + session.captured_at);
      sub.innerHTML = '';
      sub.textContent = parts.join(' · ');
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Tab counts
  // ─────────────────────────────────────────────────────────────
  function paintTabCounts(vm) {
    const c = (vm.compliance && vm.compliance.summary && vm.compliance.summary.total_rules) || 0;
    const op = (vm.operational && (vm.operational.mongodb_pipelines || []).length) || 0;
    const oplogs = (vm.operational && (vm.operational.log_sections || []).length) || 0;
    const a = ((vm.architecture && (vm.architecture.extended_checks || []).length) || 0) +
              Object.keys((vm.architecture && vm.architecture.sections) || {})
                .filter(function (k) { return vm.architecture.sections[k] != null; }).length;

    setText('[data-vm-tab-count="compliance"]', String(c));
    setText('[data-vm-tab-count="operational"]', String(op + oplogs));
    setText('[data-vm-tab-count="architecture"]', String(a));
  }

  // ─────────────────────────────────────────────────────────────
  // Compliance panel
  // ─────────────────────────────────────────────────────────────
  function paintCompliance(comp, session) {
    const summary = comp.summary || {};

    setText('[data-vm-id="health_rating"]', summary.health_rating || '—');
    setText('[data-vm-id="evaluated"]', String(summary.evaluated || 0));
    setText('[data-vm-id="pass_rate"]', formatPct(summary.pass_rate));

    setCounterTarget('[data-vm-counter="compliant"]', summary.compliant || 0);
    setCounterTarget('[data-vm-counter="non_compliant"]', summary.non_compliant || 0);
    setCounterTarget('[data-vm-counter="skipped"]', summary.skipped || 0);
    setCounterTarget('[data-vm-counter="errors"]', summary.errors || 0);

    paintHeroLede(summary);
    paintPriorityActions(comp.priority_actions || []);
    paintCategoryBars(comp.by_category || []);
    paintSeveritySplit(comp.by_severity || []);
    paintRulesTable(comp.rules || []);
  }

  // Hero lede — narrates the run in human prose. Format adapts to the
  // skipped / errors counts so the reader gets natural English instead
  // of "0 skipped, 0 errors" filler when there's nothing to report.
  function paintHeroLede(summary) {
    const el = root.querySelector('[data-vm-hero-lede]');
    if (!el) return;
    const evaluated   = summary.evaluated   || 0;
    const total       = summary.total_rules || evaluated;
    const compliant   = summary.compliant   || 0;
    const nonComp     = summary.non_compliant || 0;
    const skipped     = summary.skipped     || 0;
    const errors      = summary.errors      || 0;

    let html = '<strong>' + evaluated + ' of ' + total + '</strong> rules evaluated, with ';
    html += '<strong class="text-ok">' + compliant + ' compliant</strong> and ';
    html += '<strong class="text-bad">' + nonComp + ' non-compliant</strong>.';
    if (skipped === 1) {
      html += ' One rule was deliberately skipped.';
    } else if (skipped > 1) {
      html += ' ' + skipped + ' rules were deliberately skipped.';
    }
    if (errors === 1) {
      html += ' One rule errored during evaluation.';
    } else if (errors > 1) {
      html += ' ' + errors + ' rules errored during evaluation.';
    }
    el.innerHTML = html;
  }

  function paintPriorityActions(actions) {
    const list = root.querySelector('[data-vm-priority]');
    if (!list) return;
    list.innerHTML = '';

    if (!actions.length) {
      list.innerHTML =
        '<li class="vm-priority-empty">' +
          '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
            '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>' +
          '</svg>' +
          '<div><strong>All clear.</strong><span>No failing rules in this report.</span></div>' +
        '</li>';
      return;
    }

    actions.forEach(function (a) {
      const sev = escapeAttr(a.severity || 'info');
      const li = document.createElement('li');
      // Severity class lives on the item itself so the CSS ::before
      // accent strip on the left edge gets the right color.
      li.className = 'vm-priority-item vm-sev-' + sev;
      li.innerHTML =
        '<div class="vm-priority-body">' +
          '<div class="vm-priority-title-row">' +
            '<div class="vm-priority-title">' + escapeHTML(a.name || '') + '</div>' +
            (a.severity
              ? '<span class="vm-sev-tag vm-sev-' + sev + '">' + escapeHTML(a.severity) + '</span>'
              : '') +
          '</div>' +
          '<div class="vm-priority-meta">' +
            '<span class="vm-priority-rule">' + escapeHTML(a.rule_number || '') + '</span>' +
            '<span class="vm-priority-cat">' + escapeHTML(a.category || '') + '</span>' +
          '</div>' +
          (a.recommendations
            ? '<p class="vm-priority-rec">' + escapeHTML(a.recommendations) + '</p>'
            : '') +
        '</div>';
      list.appendChild(li);
    });
  }

  function paintCategoryBars(categories) {
    const wrap = root.querySelector('[data-vm-bars="category"]');
    if (!wrap) return;
    wrap.innerHTML = '';

    if (!categories.length) {
      wrap.innerHTML = '<div class="vm-bars-empty">No category data.</div>';
      return;
    }

    const max = Math.max.apply(null, categories.map(function (c) { return c.total || 0; })) || 1;

    categories.forEach(function (c) {
      const passPct = c.total ? (c.compliant / c.total) * 100 : 0;
      const failPct = c.total ? (c.non_compliant / c.total) * 100 : 0;
      const skipPct = c.total ? (c.skipped / c.total) * 100 : 0;
      const errPct  = c.total ? (c.errors / c.total) * 100 : 0;
      const widthPct = (c.total / max) * 100;

      const row = document.createElement('div');
      row.className = 'vm-bar-row';
      row.innerHTML =
        '<div class="vm-bar-label">' +
          '<span class="vm-bar-name">' + escapeHTML(c.name || '') + '</span>' +
          '<span class="vm-bar-count">' +
            '<b>' + c.compliant + '</b>/' + c.total +
          '</span>' +
        '</div>' +
        '<div class="vm-bar-track" style="--bar-width:' + widthPct.toFixed(2) + '%;">' +
          '<i class="vm-bar-seg vm-bar-pass" style="--w:' + passPct.toFixed(2) + '%;"></i>' +
          '<i class="vm-bar-seg vm-bar-fail" style="--w:' + failPct.toFixed(2) + '%;"></i>' +
          '<i class="vm-bar-seg vm-bar-skip" style="--w:' + skipPct.toFixed(2) + '%;"></i>' +
          '<i class="vm-bar-seg vm-bar-err"  style="--w:' + errPct.toFixed(2)  + '%;"></i>' +
        '</div>';
      wrap.appendChild(row);
    });
  }

  function paintSeveritySplit(severities) {
    const wrap = root.querySelector('[data-vm-severity]');
    if (!wrap) return;
    wrap.innerHTML = '';

    const total = severities.reduce(function (a, s) { return a + (s.total || 0); }, 0);
    if (!total) {
      wrap.innerHTML = '<div class="vm-bars-empty">No severity data.</div>';
      return;
    }

    const stack = document.createElement('div');
    stack.className = 'vm-sev-stack';
    severities.forEach(function (s) {
      const pct = ((s.total || 0) / total) * 100;
      stack.innerHTML +=
        '<i class="vm-sev-seg vm-sev-' + escapeAttr(s.name || 'info') + '" ' +
           'style="--w:' + pct.toFixed(2) + '%;" ' +
           'title="' + escapeAttr(s.name) + ': ' + s.total + ' rules"></i>';
    });
    wrap.appendChild(stack);

    const legend = document.createElement('ul');
    legend.className = 'vm-sev-legend';
    severities.forEach(function (s) {
      const li = document.createElement('li');
      li.innerHTML =
        '<span class="vm-sev-swatch vm-sev-' + escapeAttr(s.name || 'info') + '"></span>' +
        '<span class="vm-sev-name">' + escapeHTML(s.name || '') + '</span>' +
        '<span class="vm-sev-counts">' +
          '<span class="text-ok">' + s.compliant + '</span> · ' +
          '<span class="text-bad">' + s.non_compliant + '</span> · ' +
          '<span class="text-text-3">' + s.skipped + '</span>' +
          (s.errors ? ' · <span class="text-warn">' + s.errors + '</span>' : '') +
          ' <span class="text-text-4">/ ' + s.total + '</span>' +
        '</span>';
      legend.appendChild(li);
    });
    wrap.appendChild(legend);
  }

  // ─────────────────────────────────────────────────────────────
  // Rules table — drops Expected/Actual columns, adds View button
  // ─────────────────────────────────────────────────────────────
  function paintRulesTable(rules) {
    const body = root.querySelector('[data-vm-rules-body]');
    const count = root.querySelector('[data-vm-rules-count]');
    if (!body) return;
    if (count) count.textContent = rules.length + ' rule' + (rules.length === 1 ? '' : 's');
    body.innerHTML = '';

    // Initial sort — rule_number A-Z so the table reads as an ordered
    // list out of the gate. localeCompare with numeric:true keeps
    // "PLF-9" before "PLF-10". We sort the array (not the DOM) so no
    // FLIP animation fires on the very first paint, then seed the
    // module-level sort-state vars + header indicator so the
    // click-to-sort handler picks up where we left off.
    const sorted = rules.slice().sort(function (a, b) {
      return String(a.rule_number || '').localeCompare(
        String(b.rule_number || ''), undefined, { numeric: true, sensitivity: 'base' });
    });
    _rulesSortKey = 'rule_number';
    _rulesSortDir = 'asc';
    const ruleHeader = root.querySelector('.vm-rules-table thead th[data-sort="rule_number"]');
    if (ruleHeader) {
      root.querySelectorAll('.vm-rules-table thead th[data-sort]').forEach(function (h) {
        h.classList.remove('is-sort-asc', 'is-sort-desc');
      });
      ruleHeader.classList.add('is-sort-asc');
    }

    sorted.forEach(function (r, idx) {
      const tr = document.createElement('tr');
      tr.className = 'vm-rule-row';
      const statusUp = (r.status || '').toUpperCase();
      tr.dataset.status = statusUp;
      tr.dataset.severity = (r.severity || '').toLowerCase();
      tr.dataset.idx = String(idx);
      tr.dataset.ruleNumber = (r.rule_number || '').toLowerCase();
      tr.dataset.haystack = (
        (r.rule_number || '') + ' ' + (r.name || '') + ' ' +
        (r.category || '') + ' ' + (r.path || '')
      ).toLowerCase();

      const display = statusLabel(statusUp);
      const statusKey = statusClassKey(statusUp);

      // View button is its own column at the right edge so all View
       // buttons line up vertically — like a normal data table.
      tr.innerHTML =
        '<td class="vm-col-rule mono text-text-3">' + escapeHTML(r.rule_number || '') + '</td>' +
        '<td class="vm-col-name">' + escapeHTML(r.name || '') + '</td>' +
        '<td class="vm-col-category text-text-3">' + escapeHTML(r.category || '') + '</td>' +
        '<td class="vm-col-status"><span class="vm-status vm-status-' + escapeAttr(statusKey) + '">' +
          escapeHTML(display) + '</span></td>' +
        '<td class="vm-col-view">' +
          '<button type="button" class="vm-view-btn" data-vm-rule-view aria-label="View rule details">' +
            '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
              '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>' +
            '</svg>' +
            '<span>View</span>' +
          '</button>' +
        '</td>';

      // Stash the full rule dict on the row for the slide-over panel.
      tr._vmRule = r;

      const viewBtn = tr.querySelector('[data-vm-rule-view]');
      if (viewBtn) {
        viewBtn.addEventListener('click', function (e) {
          e.stopPropagation();
          spawnRipple(viewBtn, e);
          if (_ruleDetail) _ruleDetail.openByRow(tr);
        });
      }

      body.appendChild(tr);
    });

    // Filter handler shared by Alpine search input + status chips.
    // Splits the query on whitespace so each word is matched independently
    // (all tokens must match, but order doesn't matter). A single token
    // that exactly matches a rule number shows only that one rule. After
    // flipping display:none, fade-up newly-visible rows so re-filtering
    // feels alive instead of silent.
    var _atlasFilterRaw = function (opts) {
      const raw = (opts && opts.q || '').trim().toLowerCase();
      const status = (opts && opts.status) || 'all';
      const tokens = raw.length ? raw.split(/\s+/).filter(Boolean) : [];
      const newlyVisible = [];
      let visible = 0;
      Array.prototype.forEach.call(body.children, function (row) {
        const matchStatus = status === 'all' || row.dataset.status === status;
        let matchQuery = true;
        if (tokens.length) {
          const ruleNum = row.dataset.ruleNumber || '';
          const haystack = row.dataset.haystack || '';
          // Single token that is an exact rule-number match → show only that rule.
          // Everything else: every token must appear somewhere in the haystack.
          if (tokens.length === 1 && ruleNum === tokens[0]) {
            matchQuery = true;
          } else {
            matchQuery = tokens.every(function (t) {
              return haystack.indexOf(t) !== -1;
            });
          }
        }
        const wasHidden = row.style.display === 'none';
        const visibleNow = matchStatus && matchQuery;
        row.style.display = visibleNow ? '' : 'none';
        if (visibleNow) visible++;
        if (visibleNow && wasHidden) newlyVisible.push(row);
      });
      if (count) {
        const total = rules.length;
        count.textContent = visible === total
          ? total + ' rule' + (total === 1 ? '' : 's')
          : visible + ' of ' + total + ' rule' + (total === 1 ? '' : 's');
      }
      if (animeAvailable && !prefersReducedMotion && newlyVisible.length) {
        window.anime.animate(newlyVisible, {
          opacity: [0, 1],
          translateY: [4, 0],
          duration: 260,
          delay: window.anime.stagger(14),
          ease: 'outQuad',
        });
      }
    };

    // Expose a debounce-wrapped version for text-input calls (high-frequency
    // @input events). Status chip clicks pass a changed status but the same q,
    // so we detect text-input by tracking the last q value — chip clicks run
    // immediately, keystrokes are batched into one call per 180 ms.
    var _filterTimer = null;
    var _filterLastQ = '';
    window.atlasReportRulesFilter = function (opts) {
      var q = (opts && opts.q) || '';
      if (q !== _filterLastQ) {
        _filterLastQ = q;
        clearTimeout(_filterTimer);
        _filterTimer = setTimeout(function () { _atlasFilterRaw(opts); }, 180);
      } else {
        clearTimeout(_filterTimer);
        _atlasFilterRaw(opts);
      }
    };

    // FLIP sort on the All Rules table — capture row positions, reorder
    // the DOM children in place (preserving each row's `_vmRule` stash and
    // the View button's click handler), then animate each row from its
    // old position back to natural via a single translateY tween.
    wireRulesSort(body);
  }

  // ─────────────────────────────────────────────────────────────
  // Rules table — FLIP sort on header click
  // ─────────────────────────────────────────────────────────────
  let _rulesSortKey = null;
  let _rulesSortDir = 'asc';

  function wireRulesSort(body) {
    const headers = root.querySelectorAll('.vm-rules-table thead th[data-sort]');
    headers.forEach(function (th) {
      // Idempotence: skip if we've already wired this header (paintRulesTable
      // is only called once per hydrate, but defensive against future calls).
      if (th._vmSortBound) return;
      th._vmSortBound = true;
      th.addEventListener('click', function () {
        const key = th.dataset.sort;
        if (_rulesSortKey === key) {
          _rulesSortDir = _rulesSortDir === 'asc' ? 'desc' : 'asc';
        } else {
          _rulesSortKey = key;
          _rulesSortDir = 'asc';
        }
        headers.forEach(function (h) { h.classList.remove('is-sort-asc', 'is-sort-desc'); });
        th.classList.add(_rulesSortDir === 'asc' ? 'is-sort-asc' : 'is-sort-desc');
        applyRulesSort(body, key, _rulesSortDir);
      });
    });
  }

  function applyRulesSort(body, key, dir) {
    const rows = Array.prototype.slice.call(body.children);
    if (!rows.length) return;

    // FLIP — capture each row's pre-sort top before we reorder the DOM.
    const oldTops = new Map();
    rows.forEach(function (r) { oldTops.set(r, r.getBoundingClientRect().top); });

    const valueOf = function (row) {
      const r = row._vmRule || {};
      const v = r[key];
      if (v != null) return String(v).toLowerCase();
      // Fallback for keys that live on dataset rather than _vmRule.
      const ds = row.dataset[key === 'rule_number' ? 'rule' : key];
      return (ds || '').toLowerCase();
    };
    rows.sort(function (a, b) {
      const av = valueOf(a);
      const bv = valueOf(b);
      const c = av.localeCompare(bv, undefined, { numeric: true, sensitivity: 'base' });
      return dir === 'asc' ? c : -c;
    });
    rows.forEach(function (r) { body.appendChild(r); });

    if (!animeAvailable || prefersReducedMotion) return;

    // INVERT — apply translateY equal to (oldTop - newTop) so each row
    // visually appears in its old position before we tween back to 0.
    const deltas = [];
    rows.forEach(function (r) {
      const oldTop = oldTops.get(r);
      const newTop = r.getBoundingClientRect().top;
      const dy = (oldTop != null) ? oldTop - newTop : 0;
      deltas.push(dy);
      if (dy) r.style.transform = 'translateY(' + dy + 'px)';
    });
    // PLAY — release each row back to its natural position.
    window.anime.animate(rows, {
      translateY: [function (_el, i) { return deltas[i]; }, 0],
      duration: 520,
      delay: window.anime.stagger(12),
      ease: 'outExpo',
      onComplete: function () {
        rows.forEach(function (r) { r.style.transform = ''; });
      },
    });
  }

  // ─────────────────────────────────────────────────────────────
  // Operational panel
  // ─────────────────────────────────────────────────────────────
  function paintOperational(op) {
    const summary = op.mongodb_summary || {};
    setCounterTarget('[data-vm-counter="pipeline_count"]', summary.pipeline_count || 0);
    setCounterTarget('[data-vm-counter="success_count"]', summary.success_count || 0);
    setCounterTarget('[data-vm-counter="error_count"]', summary.error_count || 0);

    paintMongoPipelines(op.mongodb_pipelines || []);
    paintLogSections(op.log_sections || []);
  }

  // MongoDB pipelines as horizontal sub-tabs.
  function paintMongoPipelines(pipelines) {
    const tabs = root.querySelector('[data-vm-mongo-tabs]');
    const pane = root.querySelector('[data-vm-mongo-pane]');
    const sub = root.querySelector('[data-vm-mongo-sub]');
    if (!tabs || !pane) return;

    if (sub) {
      sub.textContent = pipelines.length
        ? pipelines.length + ' pipeline' + (pipelines.length === 1 ? '' : 's')
        : 'No pipelines available';
    }

    tabs.innerHTML = '';
    pane.innerHTML = '';

    if (!pipelines.length) {
      pane.innerHTML = emptyState('No MongoDB pipelines were run for this session.');
      tabs.style.display = 'none';
      return;
    }
    tabs.style.display = '';

    pipelines.forEach(function (p, i) {
      const isError = p.status === 'error' || !!p.error;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'vm-subtab' + (isError ? ' is-error' : '');
      btn.dataset.idx = String(i);
      btn.innerHTML =
        '<span>' + escapeHTML(p.name || ('Pipeline ' + (i + 1))) + '</span>' +
        '<span class="vm-subtab-count">' + (p.row_count || 0) + '</span>';
      btn.addEventListener('click', function () { selectMongoPipeline(pipelines, i); });
      tabs.appendChild(btn);
    });

    selectMongoPipeline(pipelines, 0);
  }

  function selectMongoPipeline(pipelines, idx) {
    const tabs = root.querySelector('[data-vm-mongo-tabs]');
    const pane = root.querySelector('[data-vm-mongo-pane]');
    if (!tabs || !pane) return;

    Array.prototype.forEach.call(tabs.children, function (b) { b.classList.remove('is-active'); });
    const active = tabs.children[idx];
    if (active) active.classList.add('is-active');

    const p = pipelines[idx];
    if (!p) return;

    const isError = p.status === 'error' || !!p.error;
    const statusKey = isError ? 'ERROR' : 'COMPLIANT';
    const statusLbl = isError ? 'Errored' : 'Successful';

    let bodyHtml = '';
    bodyHtml +=
      '<div class="vm-mongo-summary">' +
        '<span class="vm-status vm-status-' + statusKey + '">' + statusLbl + '</span>' +
        '<span><strong>' + (p.row_count || 0) + '</strong> rows</span>' +
        '<span><strong>' + Math.round(p.duration_ms || 0) + '</strong> ms</span>' +
        (p.collection ? '<span class="mono">' + escapeHTML(p.collection) + '</span>' : '') +
      '</div>';
    if (p.description) bodyHtml += '<p class="vm-mongo-desc">' + escapeHTML(p.description) + '</p>';

    if (isError) {
      bodyHtml += '<div class="vm-error-inline"><strong>Error:</strong> ' + escapeHTML(p.error || 'unknown') + '</div>';
    } else {
      var linkDefs = null;
      if ((p.name || '').toLowerCase().indexOf('largest job') !== -1) {
        linkDefs = {};
        var platformBase = (_vm && _vm.session && _vm.session.platform_uri)
          ? _vm.session.platform_uri.replace(/\/+$/, '')
          : '';
        if (platformBase) {
          linkDefs['Job ID'] = function (val) {
            return platformBase + '/operations-manager/#/jobs/' + encodeURIComponent(val);
          };
        }
        linkDefs['Workflow Name'] = function (val) {
          return 'http://localhost:3000/automation-studio/#/edit?tab=0&workflow=' + encodeURIComponent(val);
        };
      }
      bodyHtml += renderSortableTable(p.columns || [], p.rows || [], linkDefs);
    }

    pane.innerHTML = bodyHtml;
    wireSortableTables(pane);

    if (animeAvailable && !prefersReducedMotion) {
      window.anime.animate(pane, {
        opacity: [0, 1],
        translateY: [8, 0],
        duration: 320,
        ease: 'outQuad',
      });
    }
  }

  // Log analysis — modern card list with stat strips, severity hint,
  // and specialized detail renderers. Status pill is now always shown
  // (color-coded), the eye lands on counts via the stat strip, and a
  // tiny "Copy JSON" affordance gives support a clean export.
  function paintLogSections(sections) {
    const wrap = root.querySelector('[data-vm-log-sections]');
    const sub = root.querySelector('[data-vm-logs-sub]');
    if (!wrap) return;

    if (sub) {
      sub.textContent = sections.length
        ? sections.length + ' section' + (sections.length === 1 ? '' : 's')
        : 'No log analysis available';
    }

    wrap.innerHTML = '';
    if (!sections.length) {
      wrap.innerHTML = emptyState('No log analysis was performed for this session.');
      return;
    }

    sections.forEach(function (s) {
      const statusUp = (s.status || 'INFO').toUpperCase();
      const statusKey = statusClassKey(statusUp);
      const statusBadge =
        '<span class="vm-status vm-status-' + escapeAttr(statusKey) + '">' +
          escapeHTML(statusLabel(statusUp)) +
        '</span>';

      // Severity bar — a thin colored stripe across the card's left
      // edge, tinted to the check outcome. CSS reads the class.
      const card = document.createElement('article');
      card.className = 'vm-log-card vm-log-card--' + escapeAttr(statusKey);
      card.innerHTML =
        '<header class="vm-log-card-head">' +
          '<div class="vm-log-card-head-main">' +
            (s.category ? '<span class="vm-log-card-eyebrow">' + escapeHTML(s.category) + '</span>' : '') +
            '<h4 class="vm-log-card-title">' + escapeHTML(s.name || '') + '</h4>' +
          '</div>' +
          '<div class="vm-log-card-head-right">' +
            statusBadge +
            (hasDetails(s.details) ? copyButtonFor(s.details) : '') +
          '</div>' +
        '</header>' +
        '<div class="vm-log-card-body">' +
          (s.message ? '<p class="vm-log-card-msg">' + escapeHTML(s.message) + '</p>' : '') +
          renderDetails(s.details) +
          (s.remediation ? '<p class="vm-log-card-rem"><strong>Remediation</strong> — ' + escapeHTML(s.remediation) + '</p>' : '') +
        '</div>';
      wrap.appendChild(card);
    });

    // Wire any sortable mini-tables that just rendered.
    wireMiniTables(wrap);

    // Stagger the entrance — same vocabulary as extended cards.
    if (animeAvailable && !prefersReducedMotion) {
      window.anime.animate(wrap.querySelectorAll('.vm-log-card'), {
        opacity: [0, 1],
        translateY: [8, 0],
        duration: 280,
        delay: window.anime.stagger(45),
        ease: 'outQuad',
      });
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Architecture panel
  // ─────────────────────────────────────────────────────────────
  function paintArchitecture(arch) {
    paintExtendedChecks(arch.extended_checks || []);
    paintArchSections(arch.sections || {}, arch.section_labels || {});
  }

  // Extended validation checks: horizontal tabs by category.
  function paintExtendedChecks(checks) {
    const root_ = root.querySelector('[data-vm-extended-root]');
    const tabs = root.querySelector('[data-vm-extended-tabs]');
    const body = root.querySelector('[data-vm-extended-checks]');
    const sub = root.querySelector('[data-vm-extended-sub]');
    if (!body) return;

    if (sub) {
      sub.textContent = checks.length
        ? checks.length + ' check' + (checks.length === 1 ? '' : 's')
        : 'No extended checks';
    }

    if (tabs) tabs.innerHTML = '';
    body.innerHTML = '';

    if (!checks.length) {
      if (tabs) tabs.style.display = 'none';
      body.innerHTML = emptyState('No extended validation checks ran for this session.');
      return;
    }
    if (tabs) tabs.style.display = '';

    // Group by category, preserving first-seen order.
    const order = [];
    const groups = {};
    checks.forEach(function (c) {
      const cat = c.category || 'General';
      if (!(cat in groups)) { groups[cat] = []; order.push(cat); }
      groups[cat].push(c);
    });

    const categoryHasIssues = function (list) {
      return list.some(function (c) {
        const s = (c.status || '').toUpperCase();
        return s === 'FAIL' || s === 'NON-COMPLIANT' || s === 'ERROR';
      });
    };

    if (tabs) {
      order.forEach(function (cat, i) {
        const list = groups[cat];
        const hasIssues = categoryHasIssues(list);
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'vm-subtab' + (hasIssues ? ' is-error' : '');
        btn.dataset.cat = cat;
        btn.innerHTML =
          '<span>' + escapeHTML(cat) + '</span>' +
          '<span class="vm-subtab-count">' + list.length + '</span>';
        btn.addEventListener('click', function () { selectExtendedCategory(order, groups, i); });
        tabs.appendChild(btn);
      });
    }

    selectExtendedCategory(order, groups, 0);
  }

  function selectExtendedCategory(order, groups, idx) {
    const tabs = root.querySelector('[data-vm-extended-tabs]');
    const body = root.querySelector('[data-vm-extended-checks]');
    if (!body) return;

    if (tabs) {
      Array.prototype.forEach.call(tabs.children, function (b) { b.classList.remove('is-active'); });
      const active = tabs.children[idx];
      if (active) active.classList.add('is-active');
    }

    const cat = order[idx];
    const list = groups[cat] || [];

    let html = '<div class="vm-extended-pane">';
    list.forEach(function (c) {
      const statusUp = (c.status || 'INFO').toUpperCase();
      const statusKey = statusClassKey(statusUp);
      const hasD = hasDetails(c.details);
      html +=
        '<article class="vm-extended-card vm-extended-card--' + escapeAttr(statusKey) + '">' +
          '<div class="vm-extended-card-status">' +
            '<span class="vm-status vm-status-' + escapeAttr(statusKey) + '">' + escapeHTML(statusLabel(statusUp)) + '</span>' +
          '</div>' +
          '<div>' +
            '<div class="vm-extended-card-titlerow">' +
              '<h4 class="vm-extended-card-title">' + escapeHTML(c.name || '') + '</h4>' +
              (hasD ? copyButtonFor(c.details) : '') +
            '</div>' +
            (c.message ? '<p class="vm-extended-card-msg">' + escapeHTML(c.message) + '</p>' : '') +
            (c.remediation ? '<p class="vm-extended-card-rem"><strong>Remediation</strong> — ' + escapeHTML(c.remediation) + '</p>' : '') +
            (hasD ? '<div class="vm-extended-card-details">' + renderDetails(c.details) + '</div>' : '') +
          '</div>' +
          '<div class="vm-extended-card-cat">' + escapeHTML(c.category || '') + '</div>' +
        '</article>';
    });
    html += '</div>';
    body.innerHTML = html;

    // Wire mini-table sorting for whatever the specialized renderers
    // produced inside the cards. Idempotent — wired tables carry a
    // marker attr so re-running on the same DOM is a no-op.
    wireMiniTables(body);

    if (animeAvailable && !prefersReducedMotion) {
      window.anime.animate(body.querySelectorAll('.vm-extended-card'), {
        opacity: [0, 1],
        translateY: [10, 0],
        duration: 320,
        delay: window.anime.stagger(40),
        ease: 'outQuad',
      });
    }
  }

  function hasDetails(d) {
    if (d == null) return false;
    if (typeof d === 'string') return d.length > 0;
    if (Array.isArray(d)) return d.length > 0;
    if (typeof d === 'object') return Object.keys(d).length > 0;
    return true;
  }

  // Architecture overview — only renders sections that were captured.
  function paintArchSections(sections, labels) {
    const wrap = root.querySelector('[data-vm-arch-sections]');
    if (!wrap) return;
    wrap.innerHTML = '';

    // Per spec: skip null sections entirely (don't render).
    const keys = Object.keys(sections).filter(function (k) { return sections[k] != null; });
    if (!keys.length) {
      wrap.innerHTML = emptyState('No architecture data captured for this session.');
      return;
    }

    keys.forEach(function (key) {
      const data = sections[key];
      const label = labels[key] || titleCase(key);
      const details = document.createElement('details');
      details.className = 'vm-collapse vm-arch-section';
      details.innerHTML =
        '<summary class="vm-collapse-summary">' +
          '<span class="vm-section-bullet"></span>' +
          '<span class="vm-collapse-title">' + escapeHTML(label) + '</span>' +
          '<span class="vm-collapse-meta text-text-3"></span>' +
          '<svg class="vm-collapse-chev" viewBox="0 0 24 24" width="14" height="14" fill="none" ' +
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
            '<polyline points="6 9 12 15 18 9"/></svg>' +
        '</summary>' +
        '<div class="vm-collapse-body">' +
          renderKeyValue(data) +
        '</div>';
      wrap.appendChild(details);
    });

    // Anime-driven height tween — overrides the native <details> toggle
    // so opening/closing slides instead of snapping.
    wireArchAccordion(wrap);
  }

  // ─────────────────────────────────────────────────────────────
  // Sortable tables (MongoDB pipelines)
  //
  // Click a header to sort by that column. Click again to reverse.
  // anime.js fades the rows out, mutates DOM order, fades back in.
  // ─────────────────────────────────────────────────────────────
  function renderSortableTable(columns, rows, linkDefs) {
    if (!rows || !rows.length) return '<div class="text-text-3 text-[12px] py-2">No rows.</div>';
    const cols = columns && columns.length ? columns : Object.keys(rows[0] || {});
    let out = '<div class="vm-table-wrap"><table class="vm-data-table" data-sortable><thead><tr>';
    cols.forEach(function (c) {
      out += '<th data-col="' + escapeAttr(c) + '">' +
        escapeHTML(c) + '<span class="vm-sort-arrow"></span></th>';
    });
    out += '</tr></thead><tbody>';
    rows.forEach(function (r) {
      out += '<tr>';
      cols.forEach(function (c) {
        const rawVal = formatLeaf(r[c]);
        const linkFn = linkDefs && linkDefs[c];
        if (linkFn && rawVal) {
          const href = linkFn(rawVal);
          out += '<td data-col="' + escapeAttr(c) + '" data-raw="' + escapeAttr(rawVal) + '">' +
            '<a href="' + escapeAttr(href) + '" target="_blank" rel="noopener noreferrer" class="vm-cell-link">' +
            escapeHTML(rawVal) +
            '<svg class="vm-cell-link-icon" viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
              '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>' +
              '<polyline points="15 3 21 3 21 9"/>' +
              '<line x1="10" y1="14" x2="21" y2="3"/>' +
            '</svg>' +
            '</a></td>';
        } else {
          out += '<td data-col="' + escapeAttr(c) + '" data-raw="' + escapeAttr(rawVal) + '">' +
            escapeHTML(rawVal) + '</td>';
        }
      });
      out += '</tr>';
    });
    out += '</tbody></table></div>';
    return out;
  }

  function wireSortableTables(scope) {
    Array.prototype.forEach.call((scope || root).querySelectorAll('table[data-sortable]'), function (table) {
      Array.prototype.forEach.call(table.querySelectorAll('thead th'), function (th) {
        th.addEventListener('click', function () { onSortClick(table, th); });
      });
    });
  }

  function onSortClick(table, th) {
    const col = th.dataset.col;
    const wasAsc = th.classList.contains('is-sort-asc');
    const direction = wasAsc ? 'desc' : 'asc';

    Array.prototype.forEach.call(table.querySelectorAll('thead th'), function (other) {
      other.classList.remove('is-sort-asc', 'is-sort-desc');
    });
    th.classList.add(direction === 'asc' ? 'is-sort-asc' : 'is-sort-desc');

    const tbody = table.querySelector('tbody');
    if (!tbody) return;
    const rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    const idx = Array.prototype.indexOf.call(table.querySelectorAll('thead th'), th);

    const cmp = function (a, b) {
      const av = (a.cells[idx] && a.cells[idx].dataset.raw) || '';
      const bv = (b.cells[idx] && b.cells[idx].dataset.raw) || '';
      const an = parseFloat(av);
      const bn = parseFloat(bv);
      let c;
      if (!isNaN(an) && !isNaN(bn) && av.match(/^-?\d/) && bv.match(/^-?\d/)) {
        c = an - bn;
      } else {
        c = av.localeCompare(bv, undefined, { numeric: true, sensitivity: 'base' });
      }
      return direction === 'asc' ? c : -c;
    };

    if (animeAvailable && !prefersReducedMotion) {
      window.anime.animate(rows, {
        opacity: [1, 0],
        translateY: [0, -4],
        duration: 160,
        ease: 'outQuad',
        onComplete: function () {
          rows.sort(cmp).forEach(function (row) { tbody.appendChild(row); });
          window.anime.animate(rows, {
            opacity: [0, 1],
            translateY: [4, 0],
            duration: 240,
            delay: window.anime.stagger(8),
            ease: 'outQuad',
          });
        },
      });
    } else {
      rows.sort(cmp).forEach(function (row) { tbody.appendChild(row); });
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Animations
  // ─────────────────────────────────────────────────────────────
  function runEntranceAnimations(vm) {
    if (!animeAvailable || prefersReducedMotion) {
      Array.prototype.forEach.call(root.querySelectorAll('[data-vm-counter]'), function (el) {
        el.textContent = el.dataset.target || '0';
      });
      const dial = root.querySelector('[data-vm-dial="pass-rate"]');
      if (dial && vm.compliance && vm.compliance.summary) {
        dial.style.strokeDashoffset = String(100 - clampPct(vm.compliance.summary.pass_rate));
      }
      return;
    }

    animatePanelEntrance('compliance', vm);
  }

  function animatePanelEntrance(tab, vm) {
    if (!animeAvailable || prefersReducedMotion) return;
    const panel = root.querySelector('[data-vm-panel="' + tab + '"]');
    if (!panel) return;

    // The panel itself fades in via Alpine x-transition (see CSS
     // .vm-panel-enter*). This block runs *after* that fade lands and
     // staggers the inner cards so the page feels alive when the user
     // switches tabs.
    const cards = panel.querySelectorAll('.card, .vm-stat, .vm-hero');
    if (cards.length) {
      window.anime.animate(cards, {
        opacity: [0, 1],
        translateY: [12, 0],
        duration: 480,
        delay: window.anime.stagger(50),
        ease: 'outQuad',
      });
    }

    // Counter animations are panel-scoped so each tab's stats animate
    // when the user lands there. Reset to "0" right before tweening so
    // there's no flash of the final value (paintCompliance sets it to
    // the final number so the static fallback works without anime).
    const counters = panel.querySelectorAll('[data-vm-counter]');
    counters.forEach(function (el) {
      const target = parseInt(el.dataset.target || '0', 10) || 0;
      if (target === 0) { el.textContent = '0'; return; }
      el.textContent = '0';
      const obj = { v: 0 };
      window.anime.animate(obj, {
        v: target,
        duration: 900,
        ease: 'outQuart',
        onUpdate: function () { el.textContent = String(Math.round(obj.v)); },
        onComplete: function () { el.textContent = String(target); },
      });
    });

    if (tab === 'compliance') {
      const dial = panel.querySelector('[data-vm-dial="pass-rate"]');
      const rateLabel = panel.querySelector('[data-vm-id="pass_rate"]');
      if (dial && vm && vm.compliance && vm.compliance.summary) {
        const passRate = clampPct(vm.compliance.summary.pass_rate);
        window.anime.animate(dial, {
          strokeDashoffset: [100, 100 - passRate],
          duration: 1100,
          ease: 'outCubic',
        });
        // Sync the big number under the ring with the sweep (mockup #3).
        // Reset to "0" right before tweening so there's no flash of the
        // final value (paintCompliance set it to formatPct so the static
        // fallback works without anime).
        if (rateLabel) {
          rateLabel.textContent = '0';
          const obj = { v: 0 };
          window.anime.animate(obj, {
            v: passRate,
            duration: 1100,
            ease: 'outCubic',
            onUpdate: function () { rateLabel.textContent = formatPct(obj.v); },
            onComplete: function () { rateLabel.textContent = formatPct(passRate); },
          });
        }
      }

      // Bar segments — reset to 0% then tween to their CSS-default --w.
      panel.querySelectorAll('.vm-bar-seg, .vm-sev-seg').forEach(function (seg) {
        const targetWidth = (seg.style.getPropertyValue('--w') || getComputedStyle(seg).getPropertyValue('--w') || '0%').trim();
        seg.style.width = '0%';
        // Force reflow so the transition kicks in even after innerHTML reset.
        // eslint-disable-next-line no-unused-expressions
        seg.offsetWidth;
        window.anime.animate(seg, {
          width: ['0%', targetWidth],
          duration: 850,
          delay: window.anime.stagger(40),
          ease: 'outCubic',
          onComplete: function () { seg.style.width = ''; },
        });
      });

      const priority = panel.querySelectorAll('.vm-priority-item, .vm-priority-empty');
      if (priority.length) {
        window.anime.animate(priority, {
          opacity: [0, 1],
          translateX: [-8, 0],
          duration: 380,
          delay: window.anime.stagger(45),
          ease: 'outQuad',
        });
      }

      const rows = panel.querySelectorAll('.vm-rule-row');
      const FIRST_BATCH = 24;
      const visibleRows = Array.prototype.slice.call(rows, 0, FIRST_BATCH);
      if (visibleRows.length) {
        window.anime.animate(visibleRows, {
          opacity: [0, 1],
          duration: 240,
          delay: window.anime.stagger(12),
          ease: 'outQuad',
        });
      }
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Sliding tab indicator — replaces the CSS-only border-bottom on
  // .vm-tab.is-active. JS positions the indicator under the active
  // tab and tweens between positions on tab change.
  // ─────────────────────────────────────────────────────────────
  function wireTabIndicator() {
    const tabs = root.querySelector('.vm-tabs');
    const indicator = tabs && tabs.querySelector('[data-vm-tab-indicator]');
    if (!tabs || !indicator) return;

    const moveIndicator = function (animate) {
      const active = tabs.querySelector('.vm-tab.is-active');
      if (!active) return;
      const left = active.offsetLeft;
      const width = active.offsetWidth;
      if (animate && animeAvailable && !prefersReducedMotion) {
        window.anime.animate(indicator, {
          translateX: left + 'px',
          width: width + 'px',
          duration: 460,
          ease: 'outElastic(1, 0.7)',
        });
      } else {
        indicator.style.transform = 'translateX(' + left + 'px)';
        indicator.style.width = width + 'px';
      }
    };

    // Initial position — no animate. Use rAF×2 so Alpine has finished
    // toggling the initial .is-active class before we measure.
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        moveIndicator(false);
        indicator.classList.add('is-ready');
      });
    });

    // Re-position whenever the .is-active class moves between tabs.
    new MutationObserver(function () {
      requestAnimationFrame(function () { moveIndicator(true); });
    }).observe(tabs, { attributes: true, attributeFilter: ['class'], subtree: true });

    // Re-position on viewport resize.
    window.addEventListener('resize', function () { moveIndicator(false); });
  }

  // ─────────────────────────────────────────────────────────────
  // Sliding filter pill — paints the active-chip background. JS
  // positions it behind the active chip and tweens on change.
  // ─────────────────────────────────────────────────────────────
  function wireFilterPill() {
    const group = root.querySelector('.vm-filter-chips');
    const pill = group && group.querySelector('[data-vm-filter-pill]');
    if (!group || !pill) return;

    const movePill = function (animate) {
      const active = group.querySelector('.vm-chip.is-active');
      if (!active) return;
      // Pill sits inside the .vm-filter-chips padding box (3px). offsetLeft
      // is already relative to that box so we subtract the 3px top/left
      // baseline embedded in the .vm-filter-pill stylesheet.
      const left = active.offsetLeft - 3;
      const width = active.offsetWidth;
      if (animate && animeAvailable && !prefersReducedMotion) {
        window.anime.animate(pill, {
          translateX: left + 'px',
          width: width + 'px',
          duration: 460,
          ease: 'outElastic(1, 0.7)',
        });
      } else {
        pill.style.transform = 'translateX(' + left + 'px)';
        pill.style.width = width + 'px';
      }
    };

    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        movePill(false);
        pill.classList.add('is-ready');
      });
    });

    new MutationObserver(function () {
      requestAnimationFrame(function () { movePill(true); });
    }).observe(group, { attributes: true, attributeFilter: ['class'], subtree: true });

    window.addEventListener('resize', function () { movePill(false); });
  }

  // ─────────────────────────────────────────────────────────────
  // Tab pre-flight reset — zeros out the counters / pass-rate of the
  // panel about to become visible so the animation tweens up cleanly
  // without flashing the final value first.
  // ─────────────────────────────────────────────────────────────
  function wireTabPreflightReset() {
    const tabsEl = root.querySelector('.vm-tabs');
    if (!tabsEl) return;
    tabsEl.addEventListener('click', function (e) {
      if (!animeAvailable || prefersReducedMotion) return;
      const btn = e.target.closest('.vm-tab[data-tab-id]');
      if (!btn) return;
      const targetTab = btn.dataset.tabId;
      const targetPanel = root.querySelector('[data-vm-panel="' + targetTab + '"]');
      if (!targetPanel) return;
      targetPanel.querySelectorAll('[data-vm-counter]').forEach(function (el) {
        if (el.dataset.target && el.dataset.target !== '0') el.textContent = '0';
      });
      if (targetTab === 'compliance') {
        const rate = targetPanel.querySelector('[data-vm-id="pass_rate"]');
        if (rate) rate.textContent = '0';
      }
    });
  }

  // ─────────────────────────────────────────────────────────────
  // View button ripple — radiates from the click point inside the
  // button before the slide-over opens. Pure visual flourish.
  // ─────────────────────────────────────────────────────────────
  function spawnRipple(btn, ev) {
    if (!animeAvailable || prefersReducedMotion) return;
    const rect = btn.getBoundingClientRect();
    const x = ev.clientX - rect.left;
    const y = ev.clientY - rect.top;
    const ripple = document.createElement('span');
    ripple.className = 'vm-ripple';
    ripple.style.left = x + 'px';
    ripple.style.top = y + 'px';
    const size = Math.max(rect.width, rect.height) * 2.4;
    ripple.style.width = size + 'px';
    ripple.style.height = size + 'px';
    btn.appendChild(ripple);
    window.anime.animate(ripple, {
      scale: [0, 1],
      opacity: [0.55, 0],
      duration: 560,
      ease: 'outQuad',
      onComplete: function () { ripple.remove(); },
    });
  }

  // ─────────────────────────────────────────────────────────────
  // Architecture accordion — anime-driven height tween. The
  // <details> element is kept for semantics + a11y; we hijack the
  // summary click to control opening with a height animation.
  // ─────────────────────────────────────────────────────────────
  function wireArchAccordion(scope) {
    (scope || root).querySelectorAll('details.vm-arch-section').forEach(function (det) {
      if (det._vmAccBound) return;
      det._vmAccBound = true;
      const summary = det.querySelector('summary');
      const body = det.querySelector('.vm-collapse-body');
      if (!summary || !body) return;

      summary.addEventListener('click', function (e) {
        if (!animeAvailable || prefersReducedMotion) return; // native handles it
        e.preventDefault();

        if (det.open) {
          // Collapse — fix current height, animate to 0, then close.
          const start = body.scrollHeight;
          body.style.overflow = 'hidden';
          body.style.height = start + 'px';
          // Force reflow so the height transition has a starting point.
          // eslint-disable-next-line no-unused-expressions
          body.offsetHeight;
          window.anime.animate(body, {
            height: 0,
            duration: 280,
            ease: 'inOutQuad',
            onComplete: function () {
              det.open = false;
              body.style.height = '';
              body.style.overflow = '';
            },
          });
        } else {
          // Expand — open native, measure, then animate from 0 to natural.
          det.open = true;
          body.style.overflow = 'hidden';
          body.style.height = '0px';
          // Force reflow so the from-state actually paints.
          // eslint-disable-next-line no-unused-expressions
          body.offsetHeight;
          const target = body.scrollHeight;
          window.anime.animate(body, {
            height: target,
            duration: 360,
            ease: 'outExpo',
            onComplete: function () {
              body.style.height = 'auto';
              body.style.overflow = '';
            },
          });
        }
      });
    });
  }

  // ─────────────────────────────────────────────────────────────
  // Renderers (specialized + generic key/value)
  //
  // The previous renderKeyValue was a single recursive function that
  // produced a <dl><dt>key</dt><dd>val</dd></dl> stack regardless of
  // shape — so {adapter,installed,latest} became six lines instead
  // of "adapter v1.2.0 → v1.3.0" on one. The new model:
  //
  //   renderDetails(d)               ── dispatcher; sniffs shape, routes
  //     ├── renderLogAnalysisDetails ── error_groups + heuristic_groups
  //     ├── renderWebserverLogDetails ── slow_endpoints + top_endpoints
  //     ├── renderAdapterOutdated   ── outdated:[{adapter,installed,latest}]
  //     └── renderKeyValue          ── generic fallback (flex rows, not <dl>)
  //
  // Shape detection is strict-positive (presence of named keys we own)
  // so any unknown shape gracefully falls back to renderKeyValue —
  // worst case the user gets a slightly less polished view, not broken.
  // ─────────────────────────────────────────────────────────────

  function renderDetails(details) {
    if (details == null) return '';
    if (typeof details === 'string') {
      return '<div class="vm-kv-leaf">' + escapeHTML(details) + '</div>';
    }
    if (Array.isArray(details)) {
      if (!details.length) return '';
      // Array of dicts that all look like {adapter,installed,latest} →
      // promote to a single mini-table rather than per-item cards.
      if (details.every(isAdapterRow)) {
        return renderAdapterTable(details);
      }
      return '<div class="vm-kv-list">' + details.map(function (item) {
        return '<div class="vm-kv-list-item">' + renderKeyValue(item) + '</div>';
      }).join('') + '</div>';
    }
    // Object dispatch — most specific match first.
    if (looksLikeLogAnalysis(details))      return renderLogAnalysisDetails(details);
    if (looksLikeWebserverLog(details))     return renderWebserverLogDetails(details);
    if (looksLikeAdapterOutdated(details))  return renderAdapterOutdatedDetails(details);
    return renderKeyValue(details);
  }

  // ── Shape detectors ─────────────────────────────────────────
  function isAdapterRow(d) {
    return d && typeof d === 'object' && !Array.isArray(d)
      && 'adapter' in d && 'installed' in d && 'latest' in d;
  }
  function looksLikeLogAnalysis(d) {
    return d && (Array.isArray(d.error_groups) || Array.isArray(d.heuristic_groups))
      && ('files_parsed' in d || 'total_lines' in d || 'total_matched' in d);
  }
  function looksLikeWebserverLog(d) {
    return d && (d.slow_endpoints || Array.isArray(d.top_endpoints))
      && ('total_requests' in d || 'avg_response_ms' in d);
  }
  function looksLikeAdapterOutdated(d) {
    return d && (Array.isArray(d.outdated) || Array.isArray(d.up_to_date) || Array.isArray(d.failed));
  }

  // ── Specialized renderers ───────────────────────────────────

  function renderLogAnalysisDetails(d) {
    const files = formatNumber(d.files_parsed || 0);
    const lines = formatNumber(d.total_lines || 0);
    const matched = formatNumber(d.total_matched || 0);
    const errorGroups = Array.isArray(d.error_groups) ? d.error_groups : [];
    const heuristicGroups = Array.isArray(d.heuristic_groups) ? d.heuristic_groups : [];

    let html = '<div class="vm-detail-block">';

    // Stat strip — the three KPIs that frame the whole analysis.
    html += '<div class="vm-stat-strip">' +
      statTile('Files parsed', files) +
      statTile('Total lines', lines) +
      statTile('Total matched', matched, +d.total_matched > 0 ? 'warn' : 'ok') +
    '</div>';

    // Top repeated messages — one row per group, with each message
    // shown as a stack of level pill + truncated message + count chip.
    html += sectionHead(
      'Top repeated messages',
      'Most frequent error/warning log lines by occurrence count',
    );
    if (!errorGroups.length) {
      html += '<div class="vm-mini-empty">✓ No error groups detected</div>';
    } else {
      html += '<div class="vm-mini-table-wrap"><table class="vm-mini-table" data-vm-sortable>' +
        '<thead><tr>' +
          '<th data-col="name">Group<span class="vm-sort-arrow"></span></th>' +
          '<th data-col="matched" class="num">Hits<span class="vm-sort-arrow"></span></th>' +
          '<th>Top messages</th>' +
        '</tr></thead><tbody>';
      errorGroups.forEach(function (g) {
        const topMsgs = Array.isArray(g.top_messages) ? g.top_messages : [];
        html += '<tr>' +
          '<td data-raw="' + escapeAttr(g.name || '') + '">' +
            '<strong class="vm-group-name">' + escapeHTML(g.name || '—') + '</strong>' +
          '</td>' +
          '<td class="num" data-raw="' + escapeAttr(String(g.matched || 0)) + '">' +
            '<span class="vm-count-chip">' + formatNumber(g.matched || 0) + '</span>' +
          '</td>' +
          '<td>' + renderLogMessageList(topMsgs) + '</td>' +
        '</tr>';
      });
      html += '</tbody></table></div>';
    }

    // Heuristic keyword scan — same structure but for keyword hits.
    html += sectionHead(
      'Heuristic keyword scan',
      'Log lines matching known problematic patterns (crashes, auth, connection)',
    );
    if (!heuristicGroups.length) {
      html += '<div class="vm-mini-empty">✓ No keyword matches detected</div>';
    } else {
      html += '<div class="vm-mini-table-wrap"><table class="vm-mini-table" data-vm-sortable>' +
        '<thead><tr>' +
          '<th data-col="name">Group<span class="vm-sort-arrow"></span></th>' +
          '<th data-col="total_hits" class="num">Hits<span class="vm-sort-arrow"></span></th>' +
          '<th>Keywords &amp; examples</th>' +
        '</tr></thead><tbody>';
      heuristicGroups.forEach(function (g) {
        const kws = Array.isArray(g.keywords) ? g.keywords : [];
        html += '<tr>' +
          '<td data-raw="' + escapeAttr(g.name || '') + '">' +
            '<strong class="vm-group-name">' + escapeHTML(g.name || '—') + '</strong>' +
          '</td>' +
          '<td class="num" data-raw="' + escapeAttr(String(g.total_hits || 0)) + '">' +
            '<span class="vm-count-chip vm-count-chip--warn">' + formatNumber(g.total_hits || 0) + '</span>' +
          '</td>' +
          '<td>' + renderKeywordList(kws) + '</td>' +
        '</tr>';
      });
      html += '</tbody></table></div>';
    }

    html += '</div>'; // /vm-detail-block
    return html;
  }

  function renderLogMessageList(msgs) {
    if (!msgs || !msgs.length) {
      return '<span class="vm-faint">—</span>';
    }
    return '<div class="vm-log-msgs">' + msgs.map(function (m) {
      const level = (m.level || '').toLowerCase();
      const levelTag = level
        ? '<span class="vm-level vm-level--' + escapeAttr(level) + '">' + escapeHTML(level) + '</span>'
        : '';
      // Strip a leading "[level]" prefix if present (the validator may
      // include it inline; the chip already shows the level).
      const msg = String(m.message || '').replace(/^\[(?:error|warn|warning|info|debug|trace)\]\s*/i, '');
      return '<div class="vm-log-msg">' +
        levelTag +
        '<span class="vm-log-msg-text">' + escapeHTML(msg) + '</span>' +
        '<span class="vm-count-chip">' + formatNumber(m.count || 0) + '×</span>' +
      '</div>';
    }).join('') + '</div>';
  }

  function renderKeywordList(kws) {
    if (!kws || !kws.length) {
      return '<span class="vm-faint">—</span>';
    }
    return '<div class="vm-kw-list">' + kws.map(function (k) {
      const examples = Array.isArray(k.examples) ? k.examples : [];
      const exHtml = examples.length
        ? '<div class="vm-kw-examples">' + examples.map(function (ex) {
            return '<div class="vm-kw-example">' + escapeHTML(String(ex).slice(0, 240)) + '</div>';
          }).join('') + '</div>'
        : '';
      return '<div class="vm-kw">' +
        '<span class="vm-kw-token">' + escapeHTML(k.keyword || '') + '</span>' +
        '<span class="vm-count-chip">' + formatNumber(k.count || 0) + '</span>' +
        exHtml +
      '</div>';
    }).join('') + '</div>';
  }

  function renderWebserverLogDetails(d) {
    const total = formatNumber(d.total_requests || 0);
    const avg = d.avg_response_ms != null ? formatNumber(d.avg_response_ms) + ' ms' : '—';
    const slow = formatNumber(d.slow_requests_count || 0);
    const errs = formatNumber(d.error_count || 0);
    const anon = formatNumber(d.anonymous_count || 0);

    let html = '<div class="vm-detail-block">';
    html += '<div class="vm-stat-strip">' +
      statTile('Total requests', total) +
      statTile('Avg response', avg) +
      statTile('Slow requests', slow, +d.slow_requests_count > 0 ? 'warn' : 'ok') +
      statTile('Errors', errs, +d.error_count > 0 ? 'bad' : 'ok') +
      statTile('Anonymous', anon) +
    '</div>';

    // Top endpoints by volume — concise table.
    const topEps = Array.isArray(d.top_endpoints) ? d.top_endpoints : [];
    if (topEps.length) {
      html += sectionHead('Top endpoints by volume', 'Most-called paths ranked by request count');
      html += '<div class="vm-mini-table-wrap"><table class="vm-mini-table" data-vm-sortable>' +
        '<thead><tr>' +
          '<th data-col="path">Path<span class="vm-sort-arrow"></span></th>' +
          '<th data-col="count" class="num">Count<span class="vm-sort-arrow"></span></th>' +
          '<th data-col="avg_ms" class="num">Avg ms<span class="vm-sort-arrow"></span></th>' +
          '<th>Methods</th>' +
          '<th data-col="error_count" class="num">Errors<span class="vm-sort-arrow"></span></th>' +
        '</tr></thead><tbody>';
      const maxCount = topEps.reduce(function (m, e) { return Math.max(m, e.count || 0); }, 0) || 1;
      topEps.forEach(function (e) {
        const methods = e.methods && typeof e.methods === 'object'
          ? Object.keys(e.methods).map(function (m) {
              return '<span class="vm-method vm-method--' + escapeAttr(m.toLowerCase()) + '">' + escapeHTML(m) + '</span>';
            }).join(' ')
          : '';
        const bar = Math.round(((e.count || 0) / maxCount) * 100);
        html += '<tr>' +
          '<td data-raw="' + escapeAttr(e.path || '') + '">' +
            '<code class="vm-path">' + escapeHTML(e.path || '—') + '</code>' +
          '</td>' +
          '<td class="num" data-raw="' + escapeAttr(String(e.count || 0)) + '">' +
            '<span class="vm-bar-row">' +
              '<span class="vm-bar"><span class="vm-bar-fill" style="width:' + bar + '%;"></span></span>' +
              '<span class="vm-bar-label">' + formatNumber(e.count || 0) + '</span>' +
            '</span>' +
          '</td>' +
          '<td class="num" data-raw="' + escapeAttr(String(e.avg_ms || 0)) + '">' + formatNumber(e.avg_ms || 0) + '</td>' +
          '<td>' + methods + '</td>' +
          '<td class="num" data-raw="' + escapeAttr(String(e.error_count || 0)) + '">' +
            (e.error_count > 0
              ? '<span class="vm-count-chip vm-count-chip--bad">' + formatNumber(e.error_count) + '</span>'
              : '<span class="vm-faint">0</span>') +
          '</td>' +
        '</tr>';
      });
      html += '</tbody></table></div>';
    }

    // Slow endpoints — keyed by path with worst_ms and examples.
    const slowEps = d.slow_endpoints && typeof d.slow_endpoints === 'object' ? d.slow_endpoints : null;
    if (slowEps && Object.keys(slowEps).length) {
      html += sectionHead('Slow endpoints', 'Paths whose worst response time exceeded the threshold');
      html += '<div class="vm-mini-table-wrap"><table class="vm-mini-table" data-vm-sortable>' +
        '<thead><tr>' +
          '<th data-col="path">Path<span class="vm-sort-arrow"></span></th>' +
          '<th data-col="worst_ms" class="num">Worst ms<span class="vm-sort-arrow"></span></th>' +
          '<th data-col="count" class="num">Slow hits<span class="vm-sort-arrow"></span></th>' +
          '<th>Recent examples</th>' +
        '</tr></thead><tbody>';
      const allWorst = Object.values(slowEps).map(function (v) { return v.worst_ms || 0; });
      const maxWorst = allWorst.reduce(function (m, v) { return Math.max(m, v); }, 0) || 1;
      Object.keys(slowEps).forEach(function (path) {
        const info = slowEps[path] || {};
        const examples = Array.isArray(info.examples) ? info.examples : [];
        const bar = Math.round(((info.worst_ms || 0) / maxWorst) * 100);
        const examplesHtml = examples.length
          ? '<div class="vm-slow-examples">' + examples.map(function (ex) {
              return '<div class="vm-slow-example">' +
                '<span class="vm-method vm-method--' + escapeAttr((ex.method || '').toLowerCase()) + '">' + escapeHTML(ex.method || '?') + '</span>' +
                '<code class="vm-path">' + escapeHTML(String(ex.url || '').slice(0, 160)) + '</code>' +
                '<span class="vm-count-chip vm-count-chip--warn">' + formatNumber(ex.total_time_ms || 0) + ' ms</span>' +
              '</div>';
            }).join('') + '</div>'
          : '<span class="vm-faint">—</span>';
        html += '<tr>' +
          '<td data-raw="' + escapeAttr(path) + '">' +
            '<code class="vm-path">' + escapeHTML(path) + '</code>' +
          '</td>' +
          '<td class="num" data-raw="' + escapeAttr(String(info.worst_ms || 0)) + '">' +
            '<span class="vm-bar-row">' +
              '<span class="vm-bar vm-bar--warn"><span class="vm-bar-fill" style="width:' + bar + '%;"></span></span>' +
              '<span class="vm-bar-label">' + formatNumber(info.worst_ms || 0) + '</span>' +
            '</span>' +
          '</td>' +
          '<td class="num" data-raw="' + escapeAttr(String(info.count || 0)) + '">' + formatNumber(info.count || 0) + '</td>' +
          '<td>' + examplesHtml + '</td>' +
        '</tr>';
      });
      html += '</tbody></table></div>';
    }

    // Status-code distribution — chips, no table needed.
    const statuses = d.status_distribution && typeof d.status_distribution === 'object'
      ? d.status_distribution
      : null;
    if (statuses && Object.keys(statuses).length) {
      html += sectionHead('Status code distribution', '');
      html += '<div class="vm-status-strip">';
      Object.keys(statuses).sort().forEach(function (code) {
        const cls = code[0] === '2' ? 'ok' : code[0] === '4' || code[0] === '5' ? 'bad' : 'warn';
        html += '<span class="vm-status-chip vm-status-chip--' + cls + '">' +
          '<span class="code">' + escapeHTML(code) + '</span>' +
          '<span class="count">' + formatNumber(statuses[code]) + '</span>' +
        '</span>';
      });
      html += '</div>';
    }

    html += '</div>';
    return html;
  }

  function renderAdapterTable(rows) {
    let html = '<div class="vm-detail-block">';
    html += '<div class="vm-mini-table-wrap"><table class="vm-mini-table" data-vm-sortable>' +
      '<thead><tr>' +
        '<th data-col="adapter">Adapter<span class="vm-sort-arrow"></span></th>' +
        '<th data-col="installed">Installed<span class="vm-sort-arrow"></span></th>' +
        '<th>Δ</th>' +
        '<th data-col="latest">Latest<span class="vm-sort-arrow"></span></th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      html += '<tr>' +
        '<td data-raw="' + escapeAttr(r.adapter || '') + '">' +
          '<strong class="vm-adapter-name">' + escapeHTML(r.adapter || '—') + '</strong>' +
        '</td>' +
        '<td data-raw="' + escapeAttr(r.installed || '') + '">' +
          formatVersionPill(r.installed, 'old') +
        '</td>' +
        '<td class="vm-arrow">→</td>' +
        '<td data-raw="' + escapeAttr(r.latest || '') + '">' +
          formatVersionPill(r.latest, 'new') +
        '</td>' +
      '</tr>';
    });
    html += '</tbody></table></div></div>';
    return html;
  }

  function renderAdapterOutdatedDetails(d) {
    const outdated = Array.isArray(d.outdated) ? d.outdated : [];
    const upToDate = Array.isArray(d.up_to_date) ? d.up_to_date : [];
    const failed = Array.isArray(d.failed) ? d.failed : [];

    let html = '<div class="vm-detail-block">';

    // Summary chip row — at-a-glance counts.
    html += '<div class="vm-summary-chips">';
    if (outdated.length) html += '<span class="vm-count-chip vm-count-chip--warn">' + outdated.length + ' outdated</span>';
    if (upToDate.length) html += '<span class="vm-count-chip vm-count-chip--ok">' + upToDate.length + ' up-to-date</span>';
    if (failed.length)   html += '<span class="vm-count-chip vm-count-chip--bad">' + failed.length + ' unchecked</span>';
    html += '</div>';

    if (outdated.length) {
      html += sectionHead('Outdated', 'Installed version is behind the published latest');
      html += renderAdapterTable(outdated).replace('<div class="vm-detail-block">', '').replace(/<\/div>$/, '');
    }

    if (failed.length) {
      html += sectionHead('Could not verify', 'Atlas could not look up the latest version for these');
      html += '<div class="vm-chip-list">' + failed.map(function (name) {
        return '<span class="vm-name-chip">' + escapeHTML(name) + '</span>';
      }).join('') + '</div>';
    }

    // Up-to-date list is intentionally a single collapsed chip line —
    // taking a row per adapter when they're all green is just noise.
    if (upToDate.length) {
      html += '<details class="vm-uptodate">' +
        '<summary>Show ' + upToDate.length + ' up-to-date adapter' + (upToDate.length === 1 ? '' : 's') + '</summary>' +
        '<div class="vm-chip-list">' + upToDate.map(function (name) {
          return '<span class="vm-name-chip vm-name-chip--ok">' + escapeHTML(name) + '</span>';
        }).join('') + '</div>' +
      '</details>';
    }

    html += '</div>';
    return html;
  }

  // ── Generic key/value (refactored) ──────────────────────────
  // Side-by-side rows: key on the left (mono, fixed-width, muted),
  // value on the right. Nested objects collapse under a chevron.
  // Single-line for primitives and short arrays, wraps gracefully
  // for long values.
  function renderKeyValue(data) {
    if (data == null) return '<span class="vm-faint">—</span>';
    if (typeof data !== 'object' || Array.isArray(data)) {
      return '<div class="vm-kv-leaf">' + renderInlineValue(data) + '</div>';
    }
    const keys = Object.keys(data);
    if (!keys.length) return '<span class="vm-faint">empty</span>';
    let out = '<div class="vm-rows">';
    keys.forEach(function (k) {
      const v = data[k];
      const label = humanize(k);
      // Decide layout based on value shape.
      if (v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length) {
        // Nested object — fold under a <details>
        out +=
          '<details class="vm-row vm-row--nested">' +
            '<summary class="vm-row-key">' +
              '<svg class="vm-row-chev" viewBox="0 0 24 24" width="11" height="11" fill="none" ' +
                'stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">' +
                '<polyline points="6 9 12 15 18 9"/></svg>' +
              '<span>' + escapeHTML(label) + '</span>' +
              '<span class="vm-row-meta">' + Object.keys(v).length + ' fields</span>' +
            '</summary>' +
            '<div class="vm-row-val vm-row-val--nested">' + renderKeyValue(v) + '</div>' +
          '</details>';
      } else if (Array.isArray(v)) {
        out += '<div class="vm-row">' +
          '<div class="vm-row-key">' + escapeHTML(label) + '</div>' +
          '<div class="vm-row-val">' + renderInlineValue(v) + '</div>' +
        '</div>';
      } else {
        // Scalar / leaf
        out += '<div class="vm-row">' +
          '<div class="vm-row-key">' + escapeHTML(label) + '</div>' +
          '<div class="vm-row-val">' + renderInlineValue(v, k) + '</div>' +
        '</div>';
      }
    });
    out += '</div>';
    return out;
  }

  // Render a single value inline — detects type-shape (version,
  // timestamp, path, boolean) and applies a formatted treatment.
  // The optional `keyHint` lets us tighten heuristics (e.g. a key
  // named "path" or "*_at" is a strong signal even if the value
  // itself is ambiguous).
  function renderInlineValue(v, keyHint) {
    if (v == null || v === '') return '<span class="vm-faint">—</span>';
    if (typeof v === 'boolean') return formatBooleanIcon(v);
    if (Array.isArray(v)) {
      if (!v.length) return '<span class="vm-faint">—</span>';
      // Array of primitives → chip strip
      if (v.every(function (item) { return typeof item !== 'object' || item === null; })) {
        return '<div class="vm-chip-list">' + v.map(function (item) {
          return '<span class="vm-name-chip">' + escapeHTML(formatLeaf(item)) + '</span>';
        }).join('') + '</div>';
      }
      // Array of objects → recurse per-item
      return '<div class="vm-kv-list">' + v.map(function (item) {
        return '<div class="vm-kv-list-item">' + renderKeyValue(item) + '</div>';
      }).join('') + '</div>';
    }
    if (typeof v === 'object') return renderKeyValue(v);
    // Scalar — pick a presentation per shape.
    const str = String(v);
    if (looksLikeVersion(str)) return formatVersionPill(str);
    if (looksLikeTimestamp(str) || /_(at|date|time)$/i.test(keyHint || '')) {
      return formatTimestampInline(str);
    }
    if (looksLikePath(str) || /_(path|file|dir)$/i.test(keyHint || '')) {
      return '<code class="vm-path" title="' + escapeAttr(str) + '">' + escapeHTML(str) + '</code>';
    }
    if (typeof v === 'number') {
      return '<span class="vm-num">' + formatNumber(v) + '</span>';
    }
    return '<span class="vm-leaf">' + escapeHTML(str) + '</span>';
  }

  // ── Helpers used by the renderers above ─────────────────────

  function statTile(label, value, tone) {
    return '<div class="vm-stat' + (tone ? ' vm-stat--' + tone : '') + '">' +
      '<div class="vm-stat-label">' + escapeHTML(label) + '</div>' +
      '<div class="vm-stat-value">' + escapeHTML(String(value)) + '</div>' +
    '</div>';
  }

  function sectionHead(title, sub) {
    return '<div class="vm-section-head">' +
      '<strong>' + escapeHTML(title) + '</strong>' +
      (sub ? '<span class="vm-section-sub">' + escapeHTML(sub) + '</span>' : '') +
    '</div>';
  }

  function formatVersionPill(v, kind) {
    if (v == null || v === '') return '<span class="vm-faint">—</span>';
    const cls = kind === 'old' ? ' vm-version-pill--old'
              : kind === 'new' ? ' vm-version-pill--new'
              : '';
    return '<span class="vm-version-pill' + cls + '">' + escapeHTML(String(v)) + '</span>';
  }

  function formatBooleanIcon(b) {
    if (b) {
      return '<span class="vm-bool vm-bool--true" title="true">' +
        '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" ' +
          'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>' +
        '<span>Yes</span></span>';
    }
    return '<span class="vm-bool vm-bool--false" title="false">' +
      '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" ' +
        'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
      '<span>No</span></span>';
  }

  function formatTimestampInline(str) {
    const d = new Date(str);
    if (isNaN(d.getTime())) {
      return '<code class="vm-ts">' + escapeHTML(str) + '</code>';
    }
    const now = Date.now();
    const diff = (now - d.getTime()) / 1000;
    let rel;
    if (diff < 0)                rel = 'in the future';
    else if (diff < 60)          rel = Math.round(diff) + 's ago';
    else if (diff < 3600)        rel = Math.round(diff / 60) + 'm ago';
    else if (diff < 86400)       rel = Math.round(diff / 3600) + 'h ago';
    else if (diff < 86400 * 30)  rel = Math.round(diff / 86400) + 'd ago';
    else                          rel = d.toISOString().slice(0, 10);
    return '<span class="vm-ts" title="' + escapeAttr(str) + '">' + escapeHTML(rel) + '</span>';
  }

  function looksLikeVersion(s) {
    return /^v?\d+\.\d+(\.\d+)?([.\-+][\w]+)?$/.test(s);
  }
  function looksLikeTimestamp(s) {
    return /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(s);
  }
  function looksLikePath(s) {
    return /^(\/[\w.\-]+){2,}/.test(s) || /^[A-Z]:\\/.test(s) || /^~\//.test(s);
  }

  // ── Mini-table sort + copy-as-JSON wiring ───────────────────
  // Mini-tables emit `data-vm-sortable` plus `data-col` on header
  // cells and `data-raw` on body cells. This wiring is shared by
  // every specialized renderer above.
  function wireMiniTables(root_) {
    const tables = (root_ || document).querySelectorAll('table.vm-mini-table[data-vm-sortable]');
    tables.forEach(function (table) {
      if (table.dataset.vmWired === '1') return;
      table.dataset.vmWired = '1';
      const heads = table.querySelectorAll('thead th[data-col]');
      heads.forEach(function (th) {
        th.style.cursor = 'pointer';
        th.addEventListener('click', function () { sortMiniTableBy(table, th); });
      });
    });
  }
  function sortMiniTableBy(table, th) {
    const col = th.getAttribute('data-col');
    const tbody = table.querySelector('tbody');
    if (!tbody) return;
    const rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    if (!rows.length) return;
    // Locate the column index from the header position.
    const allHeads = Array.prototype.slice.call(table.querySelectorAll('thead th'));
    const idx = allHeads.indexOf(th);
    const dir = th.classList.contains('is-sort-asc') ? 'desc' : 'asc';
    allHeads.forEach(function (h) { h.classList.remove('is-sort-asc', 'is-sort-desc'); });
    th.classList.add(dir === 'asc' ? 'is-sort-asc' : 'is-sort-desc');
    rows.sort(function (a, b) {
      const av = a.cells[idx] && (a.cells[idx].getAttribute('data-raw') || a.cells[idx].textContent || '');
      const bv = b.cells[idx] && (b.cells[idx].getAttribute('data-raw') || b.cells[idx].textContent || '');
      const an = Number(av), bn = Number(bv);
      const numeric = !isNaN(an) && !isNaN(bn);
      const cmp = numeric ? an - bn : String(av).localeCompare(String(bv));
      return dir === 'asc' ? cmp : -cmp;
    });
    // Reorder with a quick anime.js fade if available.
    if (animeAvailable && !prefersReducedMotion) {
      window.anime.animate(rows, {
        opacity: [1, 0],
        duration: 110,
        ease: 'outQuad',
        onComplete: function () {
          rows.forEach(function (r) { tbody.appendChild(r); });
          window.anime.animate(rows, { opacity: [0, 1], duration: 180, delay: window.anime.stagger(15), ease: 'outQuad' });
        },
      });
    } else {
      rows.forEach(function (r) { tbody.appendChild(r); });
    }
    void col; // not used directly; locating by idx is enough
  }

  // Copy button — emits the underlying details JSON to the
  // clipboard for support engineers. Bound late via delegation
  // so dynamically rendered cards pick it up automatically.
  function wireCopyButtons(scope) {
    (scope || document).addEventListener('click', function (e) {
      const btn = e.target.closest && e.target.closest('[data-vm-copy]');
      if (!btn) return;
      e.preventDefault();
      const payload = btn.getAttribute('data-vm-copy-payload') || '';
      if (!payload) return;
      try {
        navigator.clipboard.writeText(payload).then(function () {
          flashCopyOK(btn);
        }, function () {
          fallbackCopy(payload); flashCopyOK(btn);
        });
      } catch (err) {
        fallbackCopy(payload); flashCopyOK(btn);
      }
    }, true);
  }
  function fallbackCopy(text) {
    const ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', '');
    ta.style.position = 'fixed'; ta.style.left = '-9999px';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e) { /* swallow */ }
    document.body.removeChild(ta);
  }
  function flashCopyOK(btn) {
    btn.classList.add('is-copied');
    setTimeout(function () { btn.classList.remove('is-copied'); }, 1100);
  }
  // Bound exactly once when the viewmodel boots.
  wireCopyButtons(document);

  function copyButtonFor(details) {
    if (!details) return '';
    let payload;
    try { payload = JSON.stringify(details, null, 2); }
    catch (e) { return ''; }
    return '<button type="button" class="vm-copy-btn" data-vm-copy="1" ' +
      'data-vm-copy-payload="' + escapeAttr(payload) + '" ' +
      'title="Copy details as JSON">' +
      '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
        '<rect x="9" y="9" width="13" height="13" rx="2"/>' +
        '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>' +
      '</svg>' +
      '<span class="vm-copy-default">Copy JSON</span>' +
      '<span class="vm-copy-done">Copied</span>' +
    '</button>';
  }

  function emptyState(message) {
    return '<div class="vm-empty">' +
      '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" ' +
        'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" class="opacity-60">' +
        '<circle cx="12" cy="12" r="9"/><path d="M9 9h.01M15 9h.01M9 15h6"/></svg>' +
      '<span>' + escapeHTML(message) + '</span>' +
    '</div>';
  }

  // ─────────────────────────────────────────────────────────────
  // Rule-detail slide-over panel
  //
  // Walks visible rows so prev/next respects the user's filter +
  // search. esc closes; arrow keys nav; click-outside on the
  // backdrop closes; clicking the close button closes.
  // ─────────────────────────────────────────────────────────────
  const OP_LABELS = {
    eq: 'must equal', neq: 'must not equal',
    gt: 'must be greater than', gte: 'must be at least',
    lt: 'must be less than', lte: 'must be at most',
    in: 'must be one of', not_in: 'must not be one of',
    in_range: 'must be between',
    contains: 'must contain', not_contains: 'must not contain',
    contains_all: 'must contain all of', contains_any: 'must contain at least one of',
    subset_of: 'must be a subset of', none_in: 'must not contain any of',
    exists: 'must exist', empty: 'must be empty', not_empty: 'must not be empty',
    safe_chars: 'must contain only safe characters',
    odd: 'must be an odd number', even: 'must be an even number',
    min_odd: 'must be at least (odd)',
  };

  function createRuleDetailController() {
    // The modal panel lives OUTSIDE .vm-root (so its position: fixed is
    // unambiguously relative to the viewport). Look it up via
    // document.querySelector, not root.querySelector.
    const panel = document.querySelector('[data-vm-detail-panel]');
    if (!panel) return null;

    const elName    = panel.querySelector('[data-vm-detail-name]');
    const elId      = panel.querySelector('[data-vm-detail-id]');
    const elCat     = panel.querySelector('[data-vm-detail-cat]');
    const elPills   = panel.querySelector('[data-vm-detail-pills]');
    const elStripe  = panel.querySelector('[data-vm-detail-stripe]');
    const elRecCard = panel.querySelector('[data-vm-detail-rec-card]');
    const elRec     = panel.querySelector('[data-vm-detail-rec]');
    const elCmpCard = panel.querySelector('[data-vm-detail-cmp-card]');
    const elExp     = panel.querySelector('[data-vm-detail-expected]');
    const elAct     = panel.querySelector('[data-vm-detail-actual]');
    const elOp      = panel.querySelector('[data-vm-detail-operator]');
    const elFix     = panel.querySelector('[data-vm-detail-fix]');
    const elFixBody = panel.querySelector('[data-vm-detail-fix-body]');
    const elEmpty   = panel.querySelector('[data-vm-detail-empty]');
    const elPos   = panel.querySelector('[data-vm-detail-pos]');
    const btnPrev = panel.querySelector('[data-vm-detail-prev]');
    const btnNext = panel.querySelector('[data-vm-detail-next]');
    const btnClose = panel.querySelector('[data-vm-detail-close]');
    const elBackdrop = panel.querySelector('[data-vm-detail-backdrop]');
    const tableBody = root.querySelector('[data-vm-rules-body]');

    let currentRow = null;

    function visibleRows() {
      if (!tableBody) return [];
      return Array.prototype.filter.call(tableBody.children, function (tr) {
        return tr.style.display !== 'none';
      });
    }

    // Document scroll lock — pin the current scroll position by
     // setting overflow:hidden on <html>, while reserving the
     // scrollbar's width as right-padding so the layout doesn't
     // shift horizontally when the scrollbar disappears.
    function lockScroll() {
      const html = document.documentElement;
      const sw = window.innerWidth - html.clientWidth;
      if (sw > 0) html.style.paddingRight = sw + 'px';
      html.style.overflow = 'hidden';
    }
    function unlockScroll() {
      const html = document.documentElement;
      html.style.overflow = '';
      html.style.paddingRight = '';
    }

    function open() {
      panel.hidden = false;
      // Force reflow so the transition kicks in.
      // eslint-disable-next-line no-unused-expressions
      panel.offsetWidth;
      panel.classList.add('is-open');
      lockScroll();
      // Cascade the inner sections after the slide-in lands. Runs after
      // the .vm-detail-card transform transition (~380ms in CSS) so the
      // user sees: scrim fade → card slides in → contents cascade up.
      if (animeAvailable && !prefersReducedMotion) {
        const sections = panel.querySelectorAll(
          '.vm-detail-head, .vm-detail-body > .vm-detail-card-section, .vm-detail-body > .vm-detail-empty, .vm-detail-nav'
        );
        if (sections.length) {
          sections.forEach(function (s) {
            s.style.opacity = '0';
            s.style.transform = 'translateY(8px)';
          });
          window.setTimeout(function () {
            window.anime.animate(sections, {
              opacity: [0, 1],
              translateY: [8, 0],
              duration: 360,
              delay: window.anime.stagger(70),
              ease: 'outQuad',
              onComplete: function () {
                sections.forEach(function (s) {
                  s.style.opacity = '';
                  s.style.transform = '';
                });
              },
            });
          }, 220);
        }
      }
    }

    function close() {
      panel.classList.remove('is-open');
      unlockScroll();
      window.setTimeout(function () { panel.hidden = true; }, 380);
      currentRow = null;
    }

    function statusKey(s) {
      const upper = (s || '').toUpperCase();
      if (['COMPLIANT', 'PASS', 'OK', 'SUCCESS', 'TRUE'].indexOf(upper) >= 0) return 'pass';
      if (['NON-COMPLIANT', 'FAIL', 'FALSE', 'CRITICAL'].indexOf(upper) >= 0) return 'fail';
      if (['SKIP', 'SKIPPED', 'N/A', 'NA', 'NONE'].indexOf(upper) >= 0) return 'skip';
      if (upper === 'ERROR') return 'error';
      return '';
    }

    // Parse one block of how_to_fix markdown into an <ol> of <li>s.
    // Lines like "1. step" / "1 step" / "- step" / "* step" become
    // list items; backtick spans become <code>. Falls back to a plain
    // paragraph (with linebreaks preserved) if no list-like lines were
    // found, matching the standalone HTML report's behavior.
    function buildFixHTML(howToFix) {
      const text = String(howToFix || '');
      if (!text.trim()) return '';
      const lines = text.split('\n').filter(function (l) { return l.trim(); });
      const items = [];
      for (let i = 0; i < lines.length; i++) {
        const m = lines[i].match(/^\s*(?:\d+\.?|[-*])\s+(.+)$/);
        if (m) {
          let step = escapeHTML(m[1]);
          // Inline code spans — applied AFTER escaping, so the source
          // backticks survive escapeHTML and we only wrap the inside.
          step = step.replace(/`([^`]+)`/g, '<code>$1</code>');
          items.push('<li>' + step + '</li>');
        }
      }
      if (items.length) return '<ol class="vm-fix-steps">' + items.join('') + '</ol>';
      return '<p class="vm-fix-fallback">' + escapeHTML(text) + '</p>';
    }

    function renderFix(rule, sKey) {
      if (!elFix || !elFixBody) return;
      const fixes = (_vm && _vm.compliance && _vm.compliance.fixes) || {};
      const fix = sKey === 'fail' ? fixes[rule.rule_number] : null;
      if (!fix || !(fix.how_to_fix || '').trim()) {
        elFix.hidden = true;
        elFixBody.innerHTML = '';
        return;
      }
      elFixBody.innerHTML = buildFixHTML(fix.how_to_fix);
      elFix.hidden = false;
    }

    function fillFromRow(row) {
      currentRow = row;
      const r = row && row._vmRule;
      if (!r) return;

      const sUp = (r.status || '').toUpperCase();
      const sKey = statusKey(sUp);
      const sCls = statusClassKey(sUp);
      const sLbl = statusLabel(sUp);

      panel.dataset.status = sKey;

      // Status-tinted stripe at the top of the card. The CSS handles the
      // colour per modifier class; we just toggle them so the right tint
      // wins for the current rule.
      if (elStripe) {
        elStripe.className = 'vm-detail-stripe vm-stripe-' + (sKey || 'unknown');
      }

      // Header — rule ID + category breadcrumb above the rule name, then
      // a row of status + severity pills. Category lives in the breadcrumb
      // so the pill row reads as outcome-only.
      elId.textContent = r.rule_number || '';
      elName.textContent = r.name || '';
      if (elCat) {
        const catText = r.category || '';
        elCat.textContent = catText;
        // Hide the separator and category span entirely when there's no
        // category — avoids a dangling " · " in the breadcrumb.
        const sep = elCat.previousElementSibling;
        if (sep && sep.classList.contains('vm-detail-crumb-sep')) {
          sep.style.display = catText ? '' : 'none';
        }
        elCat.style.display = catText ? '' : 'none';
      }
      if (elPills) {
        let pillsHtml =
          '<span class="vm-status vm-status-' + escapeAttr(sCls) + '">' +
          escapeHTML(sLbl) + '</span>';
        if (r.severity) {
          pillsHtml +=
            '<span class="vm-sev-tag vm-sev-' + escapeAttr((r.severity || '').toLowerCase()) + '">' +
            escapeHTML(r.severity) + '</span>';
        }
        elPills.innerHTML = pillsHtml;
      }

      // Recommendation card — only shown when there's actual text.
      // For PASS rules with no recommendation, the empty-state card
      // below takes over the messaging instead.
      const recText = r.recommendations || r.message || '';
      if (elRecCard) {
        elRec.textContent = recText;
        elRecCard.hidden = !recText;
      }

      // Comparison card — Expected → Actual side-by-side with the rule
      // operator surfaced in the card header as a quiet sub-label.
      const exp = r.expected;
      const act = r.actual;
      const expEmpty = exp == null || exp === '';
      const actEmpty = act == null || act === '';
      if (elCmpCard) {
        if (expEmpty && actEmpty) {
          elCmpCard.hidden = true;
        } else {
          elCmpCard.hidden = false;
          elExp.textContent = expEmpty ? '—' : formatExpActVal(exp);
          elAct.textContent = actEmpty ? '—' : formatExpActVal(act);
        }
      }

      if (elOp) {
        if (r.operator) {
          const lbl = OP_LABELS[r.operator] || r.operator;
          elOp.innerHTML =
            'actual <span class="op-label">' + escapeHTML(lbl) + '</span> expected';
          elOp.style.display = '';
        } else {
          elOp.style.display = 'none';
          elOp.innerHTML = '';
        }
      }

      // How-to-fix steps from RULES_KNOWLEDGEBASE.md — only shown for
      // FAIL rules that have a knowledgebase entry. Same parse rules as
      // the standalone HTML report so the two surfaces render the same
      // bullet/numbered structure for the same source markdown.
      renderFix(r, sKey);

      // Empty state for compliant rules with no fix needed.
      elEmpty.hidden = sKey !== 'pass';

      updateNavPos();
      // Slide-over body scrolls back to the top for fresh content.
      const body = panel.querySelector('.vm-detail-body');
      if (body) body.scrollTop = 0;
    }

    function updateNavPos() {
      const rows = visibleRows();
      const idx = currentRow ? rows.indexOf(currentRow) : -1;
      if (elPos) elPos.textContent = idx >= 0 ? (idx + 1) + ' of ' + rows.length : '';
      if (btnPrev) btnPrev.disabled = idx <= 0;
      if (btnNext) btnNext.disabled = idx < 0 || idx >= rows.length - 1;
    }

    function navStep(delta) {
      const rows = visibleRows();
      if (!rows.length) return;
      const idx = currentRow ? rows.indexOf(currentRow) : -1;
      const next = Math.max(0, Math.min(rows.length - 1, idx + delta));
      if (next === idx) return;
      fillFromRow(rows[next]);
    }

    if (btnPrev) btnPrev.addEventListener('click', function () { navStep(-1); });
    if (btnNext) btnNext.addEventListener('click', function () { navStep(1); });
    if (btnClose) btnClose.addEventListener('click', close);
    if (elBackdrop) elBackdrop.addEventListener('click', close);

    document.addEventListener('keydown', function (e) {
      if (panel.hidden || !panel.classList.contains('is-open')) return;
      if (e.key === 'Escape') { close(); }
      else if (e.key === 'ArrowLeft') { navStep(-1); }
      else if (e.key === 'ArrowRight') { navStep(1); }
    });

    return {
      openByRow: function (row) { fillFromRow(row); open(); },
      close: close,
    };
  }

  // ─────────────────────────────────────────────────────────────
  // Helpers
  // ─────────────────────────────────────────────────────────────
  // setText / setCounterTarget update *every* element matching the
  // selector. The hero lede shares data-vm-counter names with the
  // stat tiles, so both surfaces need to update + animate together.
  function setText(selector, value) {
    const els = root.querySelectorAll(selector);
    els.forEach(function (el) { el.textContent = value; });
  }

  function setCounterTarget(selector, value) {
    const els = root.querySelectorAll(selector);
    els.forEach(function (el) {
      el.dataset.target = String(value);
      el.textContent = String(value);
    });
  }

  function clampPct(n) {
    const v = Number(n);
    if (!isFinite(v)) return 0;
    return Math.max(0, Math.min(100, v));
  }

  function formatPct(n) {
    const v = Number(n);
    if (!isFinite(v)) return '0';
    return Number.isInteger(v) ? String(v) : String(Math.round(v * 10) / 10);
  }

  function humanize(key) {
    return String(key)
      .replace(/_/g, ' ')
      .replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }

  function titleCase(s) { return humanize(s); }

  function formatLeaf(v) {
    if (v === null || v === undefined) return '—';
    if (typeof v === 'boolean') return v ? 'Yes' : 'No';
    return String(v);
  }

  // Locale-aware number formatter used by every specialized detail
  // renderer (stat tiles, count chips, table cells). Integers get
  // thousands separators; decimals keep one fractional digit so
  // sub-millisecond latencies don't render as "0".
  function formatNumber(v) {
    if (v == null || v === '') return '0';
    const n = typeof v === 'number' ? v : Number(v);
    if (!isFinite(n)) return String(v);
    if (Number.isInteger(n)) return n.toLocaleString();
    return (Math.round(n * 10) / 10).toLocaleString(undefined, {
      minimumFractionDigits: 0,
      maximumFractionDigits: 1,
    });
  }

  // Pretty-prints expected/actual rule values. Strings/numbers go through
  // unchanged; arrays/objects are JSON-formatted so the slide-over shows
  // structured values instead of "[object Object]".
  function formatExpActVal(v) {
    if (v === null || v === undefined) return '—';
    if (typeof v === 'object') {
      try { return JSON.stringify(v, null, 2); }
      catch (e) { return String(v); }
    }
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    return String(v);
  }

  function escapeHTML(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function escapeAttr(s) { return escapeHTML(s); }
})();
