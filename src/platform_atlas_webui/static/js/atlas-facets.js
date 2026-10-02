/**
 * atlas-facets.js — the dashboard masthead backdrop ("Crystal facets").
 *
 * A low-poly crystalline surface, flat-shaded by a light that slowly orbits
 * the scene, so the facets catch brass and sky in turn. Purely atmospheric:
 * no data, and deliberately not interactive — the pointer never alters the
 * animation, so the masthead stays calm while people work around it.
 *
 * Hand-written ES module, no dependencies, no build step. atlas-motion.js
 * imports it lazily when a [data-atlas-facets] element is on the page, so no
 * other page pays for it. About 140 triangles per frame on Canvas 2D; it runs
 * comfortably on software-rendered VMs.
 *
 * Contract with the template (dashboard.html):
 *   <header class="dash-mast" data-facets-host>
 *     <div class="dash-facets" data-atlas-facets aria-hidden="true">
 *       <div class="facets-probe"><i data-c="a"></i><i data-c="b"></i></div>
 *     </div>
 *     …
 *
 * Highlight colours come from CSS custom properties (--facet-a / --facet-b
 * on .dash-mast, see atlas-studio.css) via the probe elements, so every
 * theme recolours the facets. They are re-read when <html> data-theme or
 * data-mode changes. The navy body of the crystal is fixed, like the mast.
 *
 * Variants — chosen by data-facets-variant on the [data-atlas-facets] host:
 *
 *   (none)      The dashboard masthead, as described above. Unchanged.
 *
 *   watermark   A faint, slow crystal behind a page header (the shared
 *               partials/_page_header.html macro). Larger facets, low opacity
 *               and capped highlights; its body tones come from two extra
 *               probes, [data-c="lo"] / [data-c="hi"] (--facet-lo/--facet-hi,
 *               set from the theme's own ground in atlas-studio.css), so it
 *               reads as texture in either mode. It never erases behind text:
 *               the soft edge is a CSS mask on the host.
 */

const TAU = Math.PI * 2;
const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
const lerp = (a, b, t) => a + (b - a) * t;
const ease = (x) => 1 - Math.pow(1 - clamp(x), 3);
const smooth = (a, b, x) => { const t = clamp((x - a) / (b - a)); return t * t * (3 - 2 * t); };

/** Seeded PRNG (mulberry32): the mesh is identical on every visit. */
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Smooth 3-D value noise; drives the slow drift of each vertex. */
function hash(n) { const x = Math.sin(n * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); }
function noise3(x, y, z) {
  const xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z);
  const s = (t) => t * t * (3 - 2 * t);
  const u = s(x - xi), w = s(y - yi), q = s(z - zi);
  const v = (a, b, c) => hash(a * 57 + b * 113 + c * 271);
  const x00 = lerp(v(xi, yi, zi), v(xi + 1, yi, zi), u), x10 = lerp(v(xi, yi + 1, zi), v(xi + 1, yi + 1, zi), u);
  const x01 = lerp(v(xi, yi, zi + 1), v(xi + 1, yi, zi + 1), u), x11 = lerp(v(xi, yi + 1, zi + 1), v(xi + 1, yi + 1, zi + 1), u);
  return lerp(lerp(x00, x10, w), lerp(x01, x11, w), q);
}

/**
 * Resolve any CSS colour (hex, oklch, color-mix…) to [r, g, b] by painting
 * it onto a 1×1 canvas: the one reliable way to normalise every syntax.
 */
const probeCanvas = document.createElement('canvas');
probeCanvas.width = probeCanvas.height = 1;
const probeCtx = probeCanvas.getContext('2d', { willReadFrequently: true });
function cssRgb(el, fallback) {
  const value = el ? getComputedStyle(el).color : '';
  if (!value || !probeCtx) return fallback;
  probeCtx.clearRect(0, 0, 1, 1);
  probeCtx.fillStyle = '#000';
  probeCtx.fillStyle = value;
  probeCtx.fillRect(0, 0, 1, 1);
  const d = probeCtx.getImageData(0, 0, 1, 1).data;
  return [d[0], d[1], d[2]];
}

/** Jittered grid → triangle list. Density follows the frame's size. */
function buildMesh(w, h, R) {
  const cols = Math.round(clamp(w / R.cellW, R.minCols, R.maxCols));
  const rows = Math.round(clamp(h / R.cellH, R.minRows, R.maxRows));
  const r = rng(24);
  const pts = [];
  for (let j = 0; j <= rows; j++) {
    for (let i = 0; i <= cols; i++) {
      // Edge vertices stay on the frame so the surface never shows a gap.
      const jx = i > 0 && i < cols ? (r() - 0.5) * 0.72 : 0;
      const jy = j > 0 && j < rows ? (r() - 0.5) * 0.72 : 0;
      pts.push({ u: (i + jx) / cols, v: (j + jy) / rows, ph: r() * TAU });
    }
  }
  const tris = [];
  for (let j = 0; j < rows; j++) {
    for (let i = 0; i < cols; i++) {
      const a = j * (cols + 1) + i, b = a + 1, c = a + cols + 1, d = c + 1;
      if ((i + j) % 2) tris.push([a, b, d], [a, d, c]);
      else tris.push([a, b, c], [b, d, c]);
    }
  }
  return { pts, tris, cell: w / cols };
}

/**
 * Mount the facets into `root`. Returns { destroy } — callers must destroy
 * before the element leaves the DOM (hx-boost body swaps).
 */
/* Per-variant tuning. The masthead values are the originals, verbatim. */
const RECIPES = {
  masthead:  { cellW: 92, cellH: 70, minCols: 8, maxCols: 18, minRows: 5, maxRows: 9, alpha: .92, clear: true },
  watermark: { cellW: 34, cellH: 34, minCols: 6, maxCols: 40, minRows: 2, maxRows: 8, alpha: .7,  clear: false,
               lift: .8, tintDark: .2, tintLight: .14 },
  // Ambient — a calm, coarse crystal for a full-page background (setup only).
  // Larger cells and lower alpha than the watermark so it reads as a soft
  // faceted ground rather than a busy texture. Non-toned (toned:false) so it
  // keeps the whats-new hero's fixed navy body + brass/sky glints regardless of
  // theme; the host fades it right→left with a CSS mask.
  ambient:   { cellW: 118, cellH: 118, minCols: 3, maxCols: 13, minRows: 3, maxRows: 10, alpha: .9, clear: false,
               toned: false },
};

export function mount(root, options = {}) {
  const reduced = options.reducedMotion ?? window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const variant = RECIPES[root.dataset.facetsVariant] ? root.dataset.facetsVariant : 'masthead';
  const R = RECIPES[variant];
  const toned = R.toned !== undefined ? R.toned : (variant !== 'masthead');
  const canvas = document.createElement('canvas');
  canvas.className = 'facets-canvas';
  canvas.setAttribute('aria-hidden', 'true');
  const ctx = canvas.getContext('2d');
  if (!ctx) return { destroy() {} };
  root.appendChild(canvas);
  root.classList.add('facets--live');

  let colA = [224, 168, 103];
  let colB = [77, 180, 234];
  let colLo = [10, 16, 30];
  let colHi = [30, 43, 71];
  let lightGround = false;
  function readColours() {
    colA = cssRgb(root.querySelector('.facets-probe [data-c="a"]'), colA);
    colB = cssRgb(root.querySelector('.facets-probe [data-c="b"]'), colB);
    if (toned) {
      colLo = cssRgb(root.querySelector('.facets-probe [data-c="lo"]'), colLo);
      colHi = cssRgb(root.querySelector('.facets-probe [data-c="hi"]'), colHi);
      // A pale ground (light mode) gets softer highlights and dark hairlines.
      lightGround = (0.2126 * colLo[0] + 0.7152 * colLo[1] + 0.0722 * colLo[2]) / 255 > 0.5;
    }
  }
  readColours();

  let w = 1, h = 1, mesh = null, meshKey = '';
  // Where the headline column ends (px, relative to the canvas). Measured
  // from the DOM so the crystal always stays clear of the text, whatever
  // the copy length or viewport.
  let textRight = 0, textBottom = 0;
  function layout() {
    const rect = root.getBoundingClientRect();
    w = Math.max(1, rect.width);
    h = Math.max(1, rect.height);
    const inner = R.clear ? (root.closest('[data-facets-host]') || root.parentElement).querySelector('.dash-mast-inner') : null;
    if (inner) {
      const r = inner.getBoundingClientRect();
      textRight = r.right - rect.left;
      textBottom = r.bottom - rect.top;
    } else {
      textRight = w * 0.5;
      textBottom = h * 0.6;
    }
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const key = Math.round(w / R.cellW) + 'x' + Math.round(h / R.cellH);
    if (key !== meshKey) { mesh = buildMesh(w, h, R); meshKey = key; }
  }
  layout();

  // ── Light: a slow orbit, independent of any input.
  const light = [0, 0, 1];

  // ── Render ─────────────────────────────────────────────────────────────
  let time = 0;
  let introStart = null;

  function draw(dt) {
    time += dt;
    const t = time;
    if (introStart === null) introStart = t;
    const intro = reduced ? 1 : clamp((t - introStart) / 1.8);

    // Light direction: the orbit, normalised.
    light[0] = Math.cos(t * 0.3);
    light[1] = Math.sin(t * 0.3) * 0.6;
    light[2] = 0.9;
    const ll = Math.hypot(light[0], light[1], light[2]);
    light[0] /= ll; light[1] /= ll; light[2] /= ll;

    // Vertex positions: the surface settles out of flat during the intro.
    // Relief scales with facet size so the tilt (and therefore how often a
    // face catches the light) stays constant at any masthead width.
    const lift = ease(intro / 0.9) * (mesh.cell / 70);
    const P = mesh.pts.map((p) => [
      p.u * w * 1.02 - w * 0.01,
      p.v * h * 1.04 - h * 0.02,
      (Math.sin(t * 0.35 + p.ph) * 40 + noise3(p.u * 3, p.v * 3, t * 0.06) * 90) * lift,
    ]);

    ctx.clearRect(0, 0, w, h);
    for (const [a, b, c] of mesh.tris) {
      const A = P[a], B = P[b], C = P[c];
      const mx = (A[0] + B[0] + C[0]) / 3 / w;
      // Facets assemble from right to left.
      const appear = reduced ? 1 : ease((intro * 1.6 - (1 - mx) * 0.6) / 0.6);
      if (appear <= 0) continue;

      const ux = B[0] - A[0], uy = B[1] - A[1], uz = B[2] - A[2];
      const vx = C[0] - A[0], vy = C[1] - A[1], vz = C[2] - A[2];
      let nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
      const nl = Math.hypot(nx, ny, nz) || 1;
      nx /= nl; ny /= nl; nz /= nl;
      if (nz < 0) { nx = -nx; ny = -ny; nz = -nz; }

      const d = clamp(nx * light[0] + ny * light[1] + nz * light[2]);
      // Sharp glint plus a soft sheen, so some facets always read as lit
      // wherever the light is in its orbit.
      const spec = Math.pow(d, 16);
      const hi = [lerp(colA[0], colB[0], smooth(0.45, 0.82, mx)), lerp(colA[1], colB[1], smooth(0.45, 0.82, mx)), lerp(colA[2], colB[2], smooth(0.45, 0.82, mx))];
      let r, g, bl, edge;
      if (!toned) {
        // Masthead: navy body (fixed, like the mast) lifted toward the theme highlight.
        const sheen = Math.pow(d, 5) * 0.14;
        const base = lerp(18, 40, d);
        const tint = Math.min(0.62, spec * 0.55 + sheen);
        r = Math.round(lerp(base, hi[0], tint));
        g = Math.round(lerp(base + 10, hi[1], tint));
        bl = Math.round(lerp(base + 30, hi[2], tint));
        edge = `rgba(255,255,255,${(0.035 + spec * 0.22) * appear})`;
      } else {
        // Toned variants: the body runs from the ground's shadow tone to its
        // lit tone, so the crystal reads as texture in the page, with soft
        // brass/sky highlights capped low.
        const cap = lightGround ? R.tintLight : R.tintDark;
        const tint = Math.min(cap, spec * 0.55 + Math.pow(d, 5) * 0.16);
        const k = d * R.lift;
        r = Math.round(lerp(lerp(colLo[0], colHi[0], k), hi[0], tint));
        g = Math.round(lerp(lerp(colLo[1], colHi[1], k), hi[1], tint));
        bl = Math.round(lerp(lerp(colLo[2], colHi[2], k), hi[2], tint));
        edge = lightGround
          ? `rgba(14,22,39,${(0.035 + spec * 0.03) * appear})`
          : `rgba(233,237,245,${(0.03 + spec * 0.1) * appear})`;
      }

      ctx.beginPath();
      ctx.moveTo(A[0], A[1]); ctx.lineTo(B[0], B[1]); ctx.lineTo(C[0], C[1]); ctx.closePath();
      ctx.fillStyle = `rgba(${r},${g},${bl},${R.alpha * appear})`;
      ctx.fill();
      ctx.strokeStyle = edge;
      ctx.lineWidth = 0.8;
      ctx.stroke();
    }

    // Keep the headline side clean: erase toward the text. Wide frames fade
    // from the left; narrow (stacked) frames fade from the top. (Masthead
    // only — the watermark's soft edge is a CSS mask on its host.)
    if (!R.clear) return;
    ctx.save();
    ctx.globalCompositeOperation = 'destination-out';
    // Fully clear behind the text column, then ramp in over ~180px.
    const wide = w > 700;
    const edge = wide ? textRight : textBottom;
    const reach = Math.max(1, edge + 180);
    const g = wide ? ctx.createLinearGradient(0, 0, reach, 0) : ctx.createLinearGradient(0, 0, 0, reach);
    g.addColorStop(0, 'rgba(0,0,0,1)');
    g.addColorStop(clamp((edge - 40) / reach), 'rgba(0,0,0,.97)');
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, wide ? reach : w, wide ? h : reach);
    ctx.restore();
  }

  // ── Loop control: animate only while visible; single frames otherwise.
  let raf = 0;
  let running = false;
  let visible = true;
  let last = performance.now();
  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    draw(dt);
    if (running) raf = requestAnimationFrame(frame);
  }
  function start() {
    if (running || reduced || !visible || document.hidden) return;
    running = true;
    last = performance.now();
    raf = requestAnimationFrame(frame);
  }
  function stop() { running = false; cancelAnimationFrame(raf); }
  let pending = 0;
  function requestRender() {
    cancelAnimationFrame(pending);
    pending = requestAnimationFrame(() => { draw(reduced ? 0 : 0.016); });
  }

  const ro = new ResizeObserver(() => { layout(); if (!running) requestRender(); });
  ro.observe(root);
  const io = new IntersectionObserver((entries) => {
    visible = entries[0] ? entries[0].isIntersecting : true;
    if (visible) start(); else stop();
  });
  io.observe(root);
  function onVisibility() { if (document.hidden) stop(); else start(); }
  document.addEventListener('visibilitychange', onVisibility);
  const mo = new MutationObserver(() => setTimeout(() => { readColours(); if (!running) requestRender(); }, 60));
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-mode'] });

  if (reduced) { time = 6; requestRender(); } else start();

  return {
    destroy() {
      stop();
      cancelAnimationFrame(pending);
      ro.disconnect();
      io.disconnect();
      mo.disconnect();
      document.removeEventListener('visibilitychange', onVisibility);
      canvas.remove();
      root.classList.remove('facets--live');
    },
  };
}
