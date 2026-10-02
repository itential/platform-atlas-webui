// ⌘K command palette — vanilla JS, no dependencies.
//
// Architecture choices:
//   * Lazy index — first ⌘K press fetches /api/search-index, then we cache
//     in module scope for the page lifetime. Subsequent opens are instant.
//   * Vanilla, not Alpine — palette state is largely imperative (global
//     keyboard, focus trap, scrollIntoView, modal lifecycle). Alpine adds
//     no value here and would mean wiring `x-data` through ~150 lines of
//     logic that reads cleaner as a single IIFE.
//   * Loaded from <head> with defer — same as atlas.js. The IIFE runs once
//     per cold load; the document-level keydown listener survives hx-boost
//     body swaps so the shortcut keeps working across page navigation.
//   * Modal is appended to body on first open. Body is replaced on every
//     hx-boost nav, so we check document.body.contains(modal) and re-build
//     when stale. Cached `items` lives in the module closure (not the DOM)
//     and persists across swaps.

(function () {
  'use strict';

  var INDEX_URL = '/api/search-index';

  var modal = null;
  var input = null;
  var list = null;

  var items = null;        // cached search index
  var itemsPromise = null; // dedupes concurrent loads if user mashes ⌘K
  var filtered = [];
  var selectedIdx = 0;

  function open() {
    if (!modal || !document.body.contains(modal)) build();
    modal.setAttribute('data-open', 'true');
    input.value = '';
    // Focus after the data-open attr swap so the focus event fires on a
    // visible element — some assistive tech ignores invisible focus.
    requestAnimationFrame(function () { input.focus(); });
    ensureIndex().then(function () { render(''); });
  }

  function close() {
    if (modal) modal.setAttribute('data-open', 'false');
  }

  function ensureIndex() {
    if (items) return Promise.resolve(items);
    if (itemsPromise) return itemsPromise;
    itemsPromise = fetch(INDEX_URL, { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : { items: [] }; })
      .then(function (data) { items = (data && data.items) || []; return items; })
      .catch(function () { items = []; return items; });
    return itemsPromise;
  }

  // Scoring: prefix match on label beats substring beats sublabel. Pages
  // get a small boost so navigating to a top-level page via its name wins
  // over a session with a similar prefix.
  function score(item, q) {
    if (!q) return 1;
    var label = (item.label || '').toLowerCase();
    var sub = (item.sublabel || '').toLowerCase();
    var ql = q.toLowerCase();
    if (label.startsWith(ql)) return 100 + (item.kind === 'page' ? 5 : 0);
    var i = label.indexOf(ql);
    if (i >= 0) return 50 - Math.min(i, 30);
    var j = sub.indexOf(ql);
    if (j >= 0) return 20 - Math.min(j, 15);
    // Word-prefix on tokens (helps "rs" match "p6-master-ruleset", etc.)
    var tokens = label.split(/[\s\-_./]+/);
    for (var k = 0; k < tokens.length; k++) {
      if (tokens[k].startsWith(ql)) return 10;
    }
    return -1;
  }

  function render(q) {
    if (!items) {
      list.innerHTML = '<div class="palette-empty">Loading…</div>';
      return;
    }
    var scored = [];
    for (var i = 0; i < items.length; i++) {
      var s = score(items[i], q);
      if (s > 0) scored.push({ it: items[i], s: s });
    }
    scored.sort(function (a, b) { return b.s - a.s; });
    if (scored.length > 25) scored.length = 25;
    filtered = scored.map(function (x) { return x.it; });
    selectedIdx = 0;
    if (!filtered.length) {
      list.innerHTML = '<div class="palette-empty">No matches.</div>';
      return;
    }
    var html = '';
    for (var k = 0; k < filtered.length; k++) {
      var it = filtered[k];
      html += '<div class="palette-item" role="option" data-idx="' + k + '"' +
              ' aria-selected="' + (k === 0 ? 'true' : 'false') + '">' +
              '<span class="palette-name">' + escapeHtml(it.label) + '</span>' +
              (it.sublabel ? '<span class="palette-sub">' + escapeHtml(it.sublabel) + '</span>' : '') +
              '<span class="palette-kind">' + escapeHtml(it.kind || '') + '</span>' +
              '</div>';
    }
    list.innerHTML = html;
    var nodes = list.querySelectorAll('.palette-item');
    for (var n = 0; n < nodes.length; n++) {
      (function (idx, el) {
        el.addEventListener('mousemove', function () { select(idx); });
        el.addEventListener('click', activate);
      })(n, nodes[n]);
    }
  }

  function select(i) {
    if (!filtered.length) return;
    selectedIdx = Math.max(0, Math.min(filtered.length - 1, i));
    var nodes = list.querySelectorAll('.palette-item');
    for (var k = 0; k < nodes.length; k++) {
      nodes[k].setAttribute('aria-selected', k === selectedIdx ? 'true' : 'false');
    }
    var el = list.querySelector('.palette-item[data-idx="' + selectedIdx + '"]');
    if (el) el.scrollIntoView({ block: 'nearest' });
  }

  function activate() {
    var item = filtered[selectedIdx];
    if (!item || !item.href) return;
    close();
    // Plain navigation — hx-boost intercepts and swaps the body so this
    // feels like an internal route change with View Transitions.
    window.location.assign(item.href);
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }

  function build() {
    modal = document.createElement('div');
    modal.className = 'palette';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-label', 'Command palette');
    modal.setAttribute('data-open', 'false');
    modal.innerHTML =
      '<div class="palette-box">' +
        '<input type="text" class="palette-input" autocomplete="off" spellcheck="false"' +
        ' placeholder="Jump to a session, environment, ruleset, or page…">' +
        '<div class="palette-list" role="listbox"></div>' +
        '<div class="palette-footer">' +
          '<span><kbd>↑↓</kbd> navigate</span>' +
          '<span><kbd>Enter</kbd> open</span>' +
          '<span><kbd>Esc</kbd> close</span>' +
        '</div>' +
      '</div>';
    document.body.appendChild(modal);
    input = modal.querySelector('.palette-input');
    list = modal.querySelector('.palette-list');

    // Click outside closes; inside the box doesn't.
    modal.addEventListener('click', function (e) { if (e.target === modal) close(); });
    input.addEventListener('input', function () { render(input.value); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); select(selectedIdx + 1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); select(selectedIdx - 1); }
      else if (e.key === 'Enter') { e.preventDefault(); activate(); }
      else if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
  }

  // Global shortcut. Capture so an input field on a content page can't
  // swallow the keystroke. preventDefault stops the browser's default
  // (Chrome: focus address bar on macOS).
  //
  // The data-palette-enabled attribute on <html> is rendered from
  // `prefs.palette_enabled` (see dependencies.py::template_context).
  // We check it on every keystroke instead of at module load so the
  // toggle takes effect after a full page reload — hx-boost only
  // swaps body.innerHTML and doesn't touch <html> attributes, so a
  // load-time check would miss the new value until the user navigates
  // away and back. The config page surfaces this with an "applied on
  // next page load" hint to set expectations.
  function paletteEnabled() {
    return document.documentElement.getAttribute('data-palette-enabled') !== 'false';
  }
  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
      if (!paletteEnabled()) return;
      e.preventDefault();
      open();
    }
  }, true);

  // Public handle so UI affordances (the topbar search button in base.html)
  // can open the palette without synthesising a keyboard event.
  window.atlasPalette = {
    open: function () { if (paletteEnabled()) open(); },
    close: close,
  };
})();
