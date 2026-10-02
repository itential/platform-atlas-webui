/**
 * atlas-motion.js — the Platform Atlas WebUI motion system.
 *
 * Loaded once from <head> (defer), after vendor/gsap/*. Like atlas.js and
 * palette.js it survives hx-boost body swaps, so everything here is either
 * a document-level delegated listener or is re-run from `mountPage()` after
 * each swap.
 *
 * Responsibilities
 *   • Page choreography — one orchestrated entrance per navigation (title
 *     lines rise out of a mask, the header rule draws, blocks cascade in),
 *     and a short exit while the next page is fetched.
 *   • Scroll reveals — blocks and table rows below the fold reveal once as
 *     they enter (GSAP ScrollTrigger).
 *   • Shell — gliding active/hover indicator in the sidebar, rail (collapsed
 *     sidebar) toggle with tooltips, mobile drawer, topbar scroll state,
 *     navigation progress bar, ⌘K trigger.
 *   • Micro-interactions — press ripple on buttons, magnetic primary CTAs,
 *     pointer spotlight on opt-in surfaces, number count-ups, theme-toggle
 *     circular reveal.
 *   • Masthead — lazily imports atlas-facets.js when a [data-atlas-facets]
 *     element exists and disposes it before the body is swapped.
 *
 * Principles
 *   • Content is never hidden without a guarantee it comes back: the only
 *     pre-hide is `html.motion-init`, removed here the moment from-states are
 *     staged and by an inline <head> timer as a fail-safe.
 *   • Every tween clears the inline properties it wrote, so no transform is
 *     left on an ancestor of a position:fixed modal.
 *   • prefers-reduced-motion disables choreography entirely; the page is
 *     simply shown. Direct feedback (progress bar, nav highlight) remains.
 *   • GSAP missing ⇒ the page still works; motion degrades to nothing.
 */
(function () {
  'use strict';

  var root = document.documentElement;
  var gsap = window.gsap;
  var ScrollTrigger = window.ScrollTrigger;
  var SplitText = window.SplitText;
  if (gsap) {
    var plugins = [ScrollTrigger, SplitText].filter(Boolean);
    if (plugins.length) gsap.registerPlugin.apply(gsap, plugins);
    gsap.defaults({ overwrite: 'auto' });
    // Many selectors are optional per page (meta pills, legends…); an empty
    // match is expected, not an error.
    gsap.config({ nullTargetWarn: false });
  }

  var reduceMq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  function reduced() { return !!(reduceMq && reduceMq.matches); }
  function animated() { return !!gsap && !reduced(); }

  // Cache-buster for lazily imported modules — reuse this script's own ?v=.
  var scriptEl = document.currentScript;
  var assetQuery = (scriptEl && scriptEl.src.indexOf('?') !== -1) ? scriptEl.src.slice(scriptEl.src.indexOf('?')) : '';

  var CLEAR = 'transform,opacity,visibility,translate,scale,rotate';

  function unlock() { root.classList.remove('motion-init'); }

  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }

  function isRendered(el) {
    if (!(el instanceof HTMLElement)) return false;
    if (/^(SCRIPT|STYLE|TEMPLATE|LINK|META)$/.test(el.tagName)) return false;
    var cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.position === 'fixed') return false;
    return el.offsetWidth > 0 || el.offsetHeight > 0;
  }

  // ── Progress bar ────────────────────────────────────────────────────
  // One node for the life of the page; re-attached to <body> after every
  // swap so an in-flight tween survives the innerHTML replacement.
  var progress = (function () {
    var bar = document.createElement('div');
    bar.className = 'app-progress';
    bar.setAttribute('aria-hidden', 'true');
    var fill = document.createElement('i');
    bar.appendChild(fill);
    var pending = 0;
    var showTimer = null;
    var trickle = null;

    function attach() { if (document.body && bar.parentNode !== document.body) document.body.appendChild(bar); }
    function start() {
      attach();
      pending++;
      if (pending > 1) return;
      clearTimeout(showTimer);
      // Only surface the bar for requests that are actually slow enough to notice.
      showTimer = setTimeout(function () {
        if (!gsap) { fill.style.opacity = '1'; fill.style.transform = 'scaleX(.6)'; return; }
        if (trickle) trickle.kill();
        gsap.set(fill, { opacity: 1, scaleX: 0.04 });
        trickle = gsap.to(fill, { scaleX: 0.86, duration: 6, ease: 'power3.out' });
      }, 120);
    }
    function done() {
      pending = Math.max(0, pending - 1);
      if (pending) return;
      clearTimeout(showTimer);
      if (!gsap) { fill.style.opacity = '0'; fill.style.transform = 'scaleX(0)'; return; }
      if (trickle) trickle.kill();
      if (Number(gsap.getProperty(fill, 'opacity')) === 0) return;
      gsap.timeline()
        .to(fill, { scaleX: 1, duration: 0.22, ease: 'power2.out' })
        .to(fill, { opacity: 0, duration: 0.3, ease: 'power1.out' })
        .set(fill, { scaleX: 0 });
    }
    return { start: start, done: done, attach: attach };
  })();

  // ── Sidebar: glide, hover, rail, tooltips, drawer ───────────────────
  var navMemory = { y: null, h: null, scroll: 0 };

  function initNav() {
    var nav = $('.app-nav');
    if (!nav) return;
    var glide = $('.nav-glide', nav);
    var hover = $('.nav-hover', nav);
    var active = $('.nav-link.is-active', nav);
    nav.scrollTop = navMemory.scroll || 0;
    if (!glide || !hover) return;

    function geom(link) { return { y: link.offsetTop, h: link.offsetHeight }; }

    function placeGlide(animate) {
      if (!active || !active.offsetParent) { glide.style.opacity = '0'; return; }
      var g = geom(active);
      nav.classList.add('has-glide');
      glide.style.removeProperty('opacity');
      if (!gsap) { glide.style.transform = 'translateY(' + g.y + 'px)'; glide.style.height = g.h + 'px'; return; }
      if (animate && navMemory.y !== null && animated()) {
        gsap.fromTo(glide, { y: navMemory.y, height: navMemory.h }, { y: g.y, height: g.h, duration: 0.6, ease: 'expo.out' });
      } else {
        gsap.set(glide, { y: g.y, height: g.h });
      }
    }
    placeGlide(true);
    nav._atlasPlaceGlide = placeGlide;

    // Hover layer glides between rows while the pointer is in the nav.
    var hoverShown = false;
    nav.addEventListener('pointerover', function (ev) {
      var link = ev.target.closest('.nav-link');
      if (!link || !nav.contains(link)) return;
      var g = geom(link);
      if (!gsap) return;
      if (!hoverShown) {
        gsap.set(hover, { y: g.y, height: g.h });
        gsap.to(hover, { opacity: 1, duration: 0.18 });
        hoverShown = true;
      } else {
        gsap.to(hover, { y: g.y, height: g.h, duration: animated() ? 0.32 : 0, ease: 'power3.out' });
      }
      showTip(link);
    });
    nav.addEventListener('pointerleave', function () {
      hoverShown = false;
      if (gsap) gsap.to(hover, { opacity: 0, duration: 0.2 });
      hideTip();
    });
    nav.addEventListener('scroll', hideTip, { passive: true });
  }

  function rememberNav() {
    var nav = $('.app-nav');
    var glide = nav && $('.nav-glide', nav);
    if (!nav) return;
    navMemory.scroll = nav.scrollTop;
    if (glide && gsap && nav.classList.contains('has-glide')) {
      navMemory.y = Number(gsap.getProperty(glide, 'y')) || 0;
      navMemory.h = glide.offsetHeight;
    }
  }

  // Rail tooltips (collapsed sidebar only).
  var tip = null;
  function isRail() {
    var side = $('.app-side');
    return !!side && side.getBoundingClientRect().width < 120;
  }
  function showTip(link) {
    if (!isRail()) { hideTip(); return; }
    var label = $('.nav-link-label', link);
    if (!label) return;
    if (!tip) { tip = document.createElement('div'); tip.className = 'nav-tip'; tip.setAttribute('role', 'tooltip'); }
    if (tip.parentNode !== document.body) document.body.appendChild(tip);
    tip.textContent = label.textContent.trim();
    var r = link.getBoundingClientRect();
    tip.style.left = (r.right + 12) + 'px';
    tip.style.top = (r.top + r.height / 2 - 15) + 'px';
    requestAnimationFrame(function () { tip.classList.add('is-shown'); });
  }
  function hideTip() { if (tip) tip.classList.remove('is-shown'); }

  function setRail(on) {
    root.setAttribute('data-rail', on ? 'true' : 'false');
    try { localStorage.setItem('atlas-rail', on ? 'true' : 'false'); } catch (_) { /* private mode */ }
    hideTip();
    // Re-place the glide once the width transition settles (labels hide,
    // so row offsets change).
    var nav = $('.app-nav');
    setTimeout(function () { if (nav && nav._atlasPlaceGlide) { navMemory.y = null; nav._atlasPlaceGlide(false); } }, 20);
    setTimeout(function () { if (ScrollTrigger) ScrollTrigger.refresh(); }, 460);
  }

  document.addEventListener('click', function (ev) {
    var railBtn = ev.target.closest('[data-atlas-rail-toggle]');
    if (railBtn) { setRail(root.getAttribute('data-rail') !== 'true'); return; }

    var drawerBtn = ev.target.closest('[data-atlas-drawer-toggle]');
    if (drawerBtn) {
      var open = root.getAttribute('data-drawer') === 'open';
      root.setAttribute('data-drawer', open ? 'closed' : 'open');
      drawerBtn.setAttribute('aria-expanded', open ? 'false' : 'true');
      return;
    }
    if (ev.target.closest('.app-scrim') || (ev.target.closest('.app-nav .nav-link') && root.getAttribute('data-drawer') === 'open')) {
      root.setAttribute('data-drawer', 'closed');
    }

    var paletteBtn = ev.target.closest('[data-atlas-palette-open]');
    if (paletteBtn && window.atlasPalette && typeof window.atlasPalette.open === 'function') {
      ev.preventDefault();
      window.atlasPalette.open();
    }
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && root.getAttribute('data-drawer') === 'open') root.setAttribute('data-drawer', 'closed');
    // "[" toggles the rail, matching common editor muscle memory.
    if (ev.key === '[' && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
      var t = ev.target;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      setRail(root.getAttribute('data-rail') !== 'true');
    }
  });

  // ── Topbar scroll state ─────────────────────────────────────────────
  var scrollTicking = false;
  window.addEventListener('scroll', function () {
    if (scrollTicking) return;
    scrollTicking = true;
    requestAnimationFrame(function () {
      scrollTicking = false;
      var top = $('.app-top');
      if (top) top.classList.toggle('is-scrolled', window.scrollY > 4);
    });
  }, { passive: true });

  // ── Micro-interactions ──────────────────────────────────────────────

  // Press ripple on buttons.
  document.addEventListener('pointerdown', function (ev) {
    if (!animated() || ev.button !== 0) return;
    var btn = ev.target.closest('.btn');
    if (!btn || btn.disabled) return;
    var r = btn.getBoundingClientRect();
    var dot = document.createElement('span');
    dot.className = 'st-ripple';
    dot.setAttribute('aria-hidden', 'true');
    dot.style.left = (ev.clientX - r.left) + 'px';
    dot.style.top = (ev.clientY - r.top) + 'px';
    btn.appendChild(dot);
    gsap.fromTo(dot, { scale: 0, opacity: 0.22 }, {
      scale: Math.max(r.width, r.height) / 5, opacity: 0, duration: 0.65, ease: 'power2.out',
      onComplete: function () { dot.remove(); },
    });
  }, { passive: true });

  // Pointer spotlight — writes the pointer position into --mx/--my.
  var spotRaf = 0;
  var spotEv = null;
  document.addEventListener('pointermove', function (ev) {
    spotEv = ev;
    if (spotRaf) return;
    spotRaf = requestAnimationFrame(function () {
      spotRaf = 0;
      var el = spotEv.target.closest && spotEv.target.closest('[data-spotlight]');
      if (!el) return;
      var r = el.getBoundingClientRect();
      el.style.setProperty('--mx', (spotEv.clientX - r.left) + 'px');
      el.style.setProperty('--my', (spotEv.clientY - r.top) + 'px');
    });
  }, { passive: true });

  // Magnetic CTAs — [data-magnetic] leans toward the pointer (max ~6px).
  function initMagnetic(scope) {
    if (!animated() || !window.matchMedia('(pointer: fine)').matches) return;
    $$('[data-magnetic]', scope).forEach(function (el) {
      if (el._atlasMagnetic) return;
      el._atlasMagnetic = true;
      var xTo = gsap.quickTo(el, 'x', { duration: 0.5, ease: 'power3.out' });
      var yTo = gsap.quickTo(el, 'y', { duration: 0.5, ease: 'power3.out' });
      el.addEventListener('pointermove', function (ev) {
        var r = el.getBoundingClientRect();
        xTo(((ev.clientX - r.left) / r.width - 0.5) * 12);
        yTo(((ev.clientY - r.top) / r.height - 0.5) * 8);
      });
      el.addEventListener('pointerleave', function () {
        gsap.to(el, { x: 0, y: 0, duration: 0.7, ease: 'elastic.out(1, 0.45)', clearProps: 'transform' });
      });
    });
  }

  // Number count-up. Only animates plain numerals ("122", "1,204", "94%",
  // "3.5") and always restores the exact original text afterwards.
  var COUNT_SEL = '[data-countup], .ledger-stat-num, .stat-strip-num, .tile-value .num, .kpi-value, .kpi-num';
  function countUp(el, delay) {
    if (!animated() || el._atlasCounted) return;
    var original = el.textContent;
    var m = /^\s*([\d,]*\.?\d+)\s*(%?)\s*$/.exec(original);
    if (!m) return;
    el._atlasCounted = true;
    var target = parseFloat(m[1].replace(/,/g, ''));
    if (!isFinite(target) || target === 0) return;
    var decimals = (m[1].split('.')[1] || '').length;
    var useCommas = m[1].indexOf(',') !== -1 || target >= 10000;
    var state = { v: 0 };
    // Lock the width so the numeral doesn't jitter the layout as it grows.
    el.style.minWidth = el.offsetWidth + 'px';
    gsap.to(state, {
      v: target, duration: Math.min(1.6, 0.7 + Math.log10(target + 1) * 0.35), delay: delay || 0, ease: 'power3.out',
      onUpdate: function () {
        var n = decimals ? state.v.toFixed(decimals) : String(Math.round(state.v));
        if (useCommas) n = Number(n).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
        el.textContent = n + m[2];
      },
      onComplete: function () { el.textContent = original; el.style.removeProperty('min-width'); },
    });
  }

  // Theme toggle — circular reveal from the button using the View
  // Transitions API. atlas.js still owns the actual mode change; we wrap
  // its click in a transition by re-dispatching it inside the callback.
  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest('[data-atlas-theme-toggle]');
    if (!btn || btn.dataset.vtPass === '1') return;
    if (!document.startViewTransition || reduced()) return;
    ev.stopImmediatePropagation();
    ev.preventDefault();
    var r = btn.getBoundingClientRect();
    var x = r.left + r.width / 2;
    var y = r.top + r.height / 2;
    var radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
    root.classList.add('st-vt-theme');
    var vt = document.startViewTransition(function () {
      btn.dataset.vtPass = '1';
      btn.click();
      delete btn.dataset.vtPass;
      // Snap colours to the new mode so the "new" snapshot is final.
      root.classList.remove('theme-changing');
    });
    vt.ready.then(function () {
      root.animate(
        { clipPath: ['circle(0px at ' + x + 'px ' + y + 'px)', 'circle(' + radius + 'px at ' + x + 'px ' + y + 'px)'] },
        { duration: 620, easing: 'cubic-bezier(.65,0,.35,1)', pseudoElement: '::view-transition-new(root)' }
      );
    }).catch(function () { /* transition skipped */ });
    vt.finished.finally(function () { root.classList.remove('st-vt-theme'); });
  }, true);

  // ── Page choreography ───────────────────────────────────────────────

  // A "block" is a top-level visual unit of the page. If the page wraps
  // everything in one plain container (e.g. an Alpine x-data div), look
  // through it; if a block is a layout grid of cards, stagger the cards.
  function looksLikeSurface(el) {
    if (el.matches('.card, .dossier-card, .stat-strip, .form, .flash, .tip, section, article, aside, table, .dash-mast')) return true;
    var cs = getComputedStyle(el);
    return (cs.backgroundColor && cs.backgroundColor !== 'rgba(0, 0, 0, 0)' && cs.backgroundColor !== 'transparent') ||
      parseFloat(cs.borderTopWidth) > 0 || cs.boxShadow !== 'none';
  }
  function collectBlocks(scope) {
    var kids = Array.prototype.filter.call(scope.children, function (el) {
      return isRendered(el) && !el.matches('.page-header, .dash-mast');
    });
    var guard = 0;
    while (kids.length === 1 && !looksLikeSurface(kids[0]) && kids[0].children.length && guard++ < 3) {
      kids = Array.prototype.filter.call(kids[0].children, function (el) { return isRendered(el) && !el.matches('.dash-mast'); });
    }
    var out = [];
    kids.forEach(function (el) {
      if (el.matches('[data-cascade], .metrics') || el.querySelector('[data-cascade], .metrics')) { return; }
      var cs = getComputedStyle(el);
      var isLayout = (cs.display === 'grid' || cs.display === 'flex') && !looksLikeSurface(el);
      var children = isLayout ? Array.prototype.filter.call(el.children, isRendered) : [];
      if (children.length >= 2 && children.length <= 12) out.push.apply(out, children);
      else out.push(el);
    });
    return out;
  }

  function splitTitle(h1) {
    if (!SplitText || !h1 || !h1.textContent.trim()) return null;
    try {
      return SplitText.create(h1, { type: 'lines', mask: 'lines', linesClass: 'st-line', maskClass: 'st-line-mask' });
    } catch (_) { return null; }
  }

  function revealRows(scope, delay) {
    var rows = $$('tbody > tr', scope).filter(isRendered).slice(0, 14);
    if (!rows.length) return;
    gsap.fromTo(rows, { opacity: 0, y: 8 }, {
      opacity: 1, y: 0, duration: 0.5, ease: 'power3.out', stagger: 0.035, delay: delay || 0, clearProps: CLEAR,
    });
  }

  var pageTriggers = [];
  function killPageTriggers() {
    pageTriggers.forEach(function (t) { try { t.kill(); } catch (_) {} });
    pageTriggers = [];
  }

  function enterDashboard(mast, tl) {
    var title = $('.dash-mast-title', mast);
    var split = splitTitle(title);
    tl.fromTo(mast, { opacity: 0, scale: 0.985 }, { opacity: 1, scale: 1, duration: 0.9, ease: 'expo.out', clearProps: CLEAR }, 0);
    tl.fromTo($$('.dash-mast-eyebrow', mast), { opacity: 0, y: 10 }, { opacity: 1, y: 0, duration: 0.6, clearProps: CLEAR }, 0.15);
    if (split) {
      tl.from(split.lines, { yPercent: 110, duration: 1.0, ease: 'expo.out', stagger: 0.09, onComplete: function () { split.revert(); } }, 0.2);
    }
    tl.fromTo($$('.dash-mast-sub, .dash-mast-actions > *', mast), { opacity: 0, y: 14 }, { opacity: 1, y: 0, duration: 0.7, stagger: 0.07, clearProps: CLEAR }, 0.42);
    var stats = $$('.ledger-stat', mast).filter(isRendered);
    tl.fromTo(stats, { opacity: 0, y: 18 }, { opacity: 1, y: 0, duration: 0.7, stagger: 0.07, clearProps: CLEAR }, 0.55);
    stats.forEach(function (s, i) { var n = $('.ledger-stat-num', s); if (n) countUp(n, 0.6 + i * 0.07); });
  }

  // Standalone pages (setup wizard, error page) have no app shell; a
  // [data-motion-page] container gets a simple staggered entrance instead.
  function enterStandalone() {
    var page = $('[data-motion-page]');
    if (!page || !animated()) return;
    var kids = Array.prototype.filter.call(page.children, isRendered);
    gsap.fromTo(kids, { opacity: 0, y: 24 }, { opacity: 1, y: 0, duration: 0.9, ease: 'expo.out', stagger: 0.09, clearProps: CLEAR });
  }

  // Multi-step flows toggle `hidden` on [data-motion-step] sections; animate
  // the newly shown step's direct children so the change reads as progress.
  function watchSteps() {
    var steps = $$('[data-motion-step]');
    if (!steps.length || typeof MutationObserver === 'undefined') return;
    var mo = new MutationObserver(function (records) {
      records.forEach(function (r) {
        var el = r.target;
        if (r.attributeName !== 'hidden' || el.hidden || !animated()) return;
        var kids = Array.prototype.filter.call(el.children, isRendered);
        gsap.fromTo(kids, { opacity: 0, x: 18 }, { opacity: 1, x: 0, duration: 0.6, ease: 'expo.out', stagger: 0.05, clearProps: CLEAR });
      });
    });
    steps.forEach(function (el) { mo.observe(el, { attributes: true, attributeFilter: ['hidden'] }); });
  }

  function enterPage() {
    var main = $('.app-main-inner');
    if (!main) { enterStandalone(); unlock(); return; }
    if (!animated()) { unlock(); return; }

    var tl = gsap.timeline({ defaults: { ease: 'expo.out' } });
    var header = $('.page-header', main);
    var mast = $('.dash-mast', main);

    if (header) {
      var h1 = $('h1', header);
      var split = splitTitle(h1);
      if (split) {
        tl.from(split.lines, { yPercent: 110, duration: 0.95, stagger: 0.08, onComplete: function () { split.revert(); } }, 0);
      } else if (h1) {
        tl.fromTo(h1, { opacity: 0, y: 16 }, { opacity: 1, y: 0, duration: 0.8, clearProps: CLEAR }, 0);
      }
      tl.fromTo($$('.page-header-meta, .page-header p, .page-header-actions > *', header), { opacity: 0, y: 10 },
        { opacity: 1, y: 0, duration: 0.7, stagger: 0.05, clearProps: CLEAR }, 0.12);
      // The survey rule draws in under the title.
      tl.fromTo(header, { '--st-rule': 0 }, { '--st-rule': 1, duration: 1.1, ease: 'expo.inOut' }, 0.05);
    }
    if (mast) enterDashboard(mast, tl);

    var blocks = collectBlocks(main);
    var fold = window.innerHeight * 0.94;
    var above = [];
    var below = [];
    blocks.forEach(function (b) { (b.getBoundingClientRect().top < fold ? above : below).push(b); });

    var start = mast ? 0.55 : (header ? 0.18 : 0);
    if (above.length) {
      tl.fromTo(above, { opacity: 0, y: 22 }, {
        opacity: 1, y: 0, duration: 0.85, stagger: 0.07, clearProps: CLEAR,
        onStart: function () { above.forEach(function (b, i) { revealRows(b, 0.12 + i * 0.07); }); },
      }, start);
    }

    // Stage below-the-fold blocks and reveal them as they scroll in.
    if (below.length && ScrollTrigger) {
      gsap.set(below, { opacity: 0, y: 28 });
      var batch = ScrollTrigger.batch(below, {
        start: 'top 90%',
        once: true,
        onEnter: function (els) {
          gsap.to(els, { opacity: 1, y: 0, duration: 0.9, ease: 'expo.out', stagger: 0.08, clearProps: CLEAR });
          els.forEach(function (b, i) { revealRows(b, 0.1 + i * 0.08); $$(COUNT_SEL, b).forEach(function (n) { countUp(n, 0.1); }); });
        },
      });
      pageTriggers.push.apply(pageTriggers, batch);
    }

    // Count-ups for numerals in the first viewport (outside the mast).
    $$(COUNT_SEL, main).forEach(function (n) {
      if (mast && mast.contains(n)) return;
      if (n.getBoundingClientRect().top < fold) countUp(n, start + 0.15);
    });

    // Activity calendar fills in like ink on a survey sheet.
    var cells = $$('.calendar-cells .cell', main);
    if (cells.length && cells[0].getBoundingClientRect().top < fold * 1.4) {
      tl.fromTo(cells, { opacity: 0, scale: 0.3 }, {
        opacity: 1, scale: 1, duration: 0.5, ease: 'back.out(2)', stagger: { each: 0.0016, from: 'start' }, clearProps: CLEAR,
      }, start + 0.25);
    }

    unlock();
    initMagnetic(main);
  }

  // Exit — soften the outgoing page while the next one is fetched.
  function exitPage() {
    var main = $('.app-main-inner');
    if (!main || !animated()) return;
    gsap.to(main, { opacity: 0.4, y: -6, duration: 0.22, ease: 'power2.in' });
  }
  function cancelExit() {
    var main = $('.app-main-inner');
    if (!main || !gsap) return;
    gsap.to(main, { opacity: 1, y: 0, duration: 0.2, clearProps: CLEAR });
  }

  // ── Masthead facets lifecycle ───────────────────────────────────
  // atlas-facets.js is an ES module imported on demand, so only the
  // dashboard loads it. Instances are destroyed before every body swap.
  var facets = [];
  var facetsModule = null;
  function mountFacets() {
    var hosts = $$('[data-atlas-facets]');
    if (!hosts.length) return;
    var load = facetsModule
      ? Promise.resolve(facetsModule)
      : import('/static/js/atlas-facets.js' + assetQuery).then(function (m) { facetsModule = m; return m; });
    load.then(function (m) {
      hosts.forEach(function (el) {
        if (el._atlasFacets || !el.isConnected) return;
        try { el._atlasFacets = m.mount(el, { reducedMotion: reduced() }); facets.push(el._atlasFacets); }
        catch (err) { /* decorative: the navy mast stands on its own */ }
      });
    }).catch(function () { /* decorative: ignore load failures */ });
  }
  function destroyFacets() {
    facets.forEach(function (f) { try { f.destroy(); } catch (_) {} });
    facets = [];
  }

  // ── Lifecycle ───────────────────────────────────────────────────────
  function mountPage() {
    progress.attach();
    initNav();
    enterPage();
    watchSteps();
    mountFacets();
    var top = $('.app-top');
    if (top) top.classList.toggle('is-scrolled', window.scrollY > 4);
    if (ScrollTrigger) requestAnimationFrame(function () { ScrollTrigger.refresh(); });
  }

  function isBodySwap(evt) {
    var t = evt && evt.detail && (evt.detail.target || evt.detail.elt);
    return t === document.body;
  }

  document.addEventListener('htmx:beforeRequest', function (evt) {
    progress.start();
    if (isBodySwap(evt)) exitPage();
  });
  document.addEventListener('htmx:afterRequest', function (evt) {
    progress.done();
    var d = evt.detail || {};
    if (isBodySwap(evt) && (d.failed || (d.xhr && d.xhr.status >= 400 && d.xhr.status !== 422 && d.xhr.status !== 400 && d.xhr.status !== 409))) cancelExit();
  });
  document.addEventListener('htmx:sendError', function () { cancelExit(); });
  document.addEventListener('htmx:beforeSwap', function (evt) {
    if (!isBodySwap(evt) || !evt.detail.shouldSwap) return;
    rememberNav();
    killPageTriggers();
    destroyFacets();
    hideTip();
    if (animated()) root.classList.add('motion-init');
  });
  document.addEventListener('htmx:afterSettle', function (evt) {
    if (!isBodySwap(evt)) return;
    mountPage();
  });
  // A body swap that errors out after beforeSwap must never leave the page hidden.
  document.addEventListener('htmx:swapError', unlock);

  // Pause heavy work in background tabs.
  document.addEventListener('visibilitychange', function () {
    if (!gsap) return;
    if (document.hidden) gsap.ticker.sleep(); else gsap.ticker.wake();
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountPage);
  else mountPage();

  window.atlasMotion = { countUp: countUp, refresh: function () { if (ScrollTrigger) ScrollTrigger.refresh(); } };
})();
