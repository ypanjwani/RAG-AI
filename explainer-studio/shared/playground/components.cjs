// Copied from the ai-playground experiment (Bloging-substack/ECommerce/ai-playground), adapted for Explainer Studio's Playground mode.
// Pre-built components. Layout, styling and events are fixed here; behaviour
// comes only from compiled slot functions passed in via `slots`.
// ctx = { store, apply(patch, where), run(slotFn, where, ...args), runWith(slotFn, where, state, ...args),
//         registerPlayer(p), reset() }
(function (root) {
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const PALETTE = ['blue', 'orange', 'green', 'red', 'purple', 'teal', 'gray', 'pink'];

  const el = (tag, cls, txt) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt !== undefined) e.textContent = txt;
    return e;
  };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (v) => {
    if (typeof v !== 'number') return String(v);
    if (v === 0) return '0';
    const a = Math.abs(v);
    if (a >= 1e4 || a < 1e-3) return v.toExponential(2);
    return String(Number(v.toPrecision(a >= 100 ? 4 : 3)));
  };

  // ---------------------------------------------------------------- controls
  function slider(box, spec, slots, ctx) {
    const wrap = el('label', 'pg-ctl pg-slider');
    const head = el('div', 'pg-ctl-head');
    const name = el('span', 'pg-ctl-label', spec.label || spec.key);
    const out = el('span', 'pg-ctl-value');
    head.append(name, out);
    const input = el('input');
    Object.assign(input, { type: 'range', min: spec.min, max: spec.max, step: spec.step || (spec.max - spec.min) / 100 });
    const range = el('div', 'pg-range');
    range.append(el('span', '', fmt(spec.min)), el('span', '', fmt(spec.max)));
    wrap.append(head, input, range);
    box.append(wrap);
    input.addEventListener('input', () => {
      const v = Number(input.value);
      const patch = slots.onChange ? ctx.run(slots.onChange, `${spec.id}.onChange`, v) : { [spec.key]: v };
      ctx.apply(patch, `${spec.id}.onChange`);
    });
    ctx.store.watch((st) => {
      const v = st[spec.key];
      if (typeof v === 'number') { input.value = v; out.textContent = fmt(v); }
    });
  }

  function select(box, spec, slots, ctx) {
    const wrap = el('label', 'pg-ctl pg-select');
    wrap.append(el('span', 'pg-ctl-label', spec.label || spec.key));
    const sel = el('select');
    const opts = spec.options.map((o) => (o !== null && typeof o === 'object' ? o : { value: o, label: String(o) }));
    opts.forEach((o, i) => { const op = el('option', '', o.label ?? String(o.value)); op.value = i; sel.append(op); });
    wrap.append(sel);
    box.append(wrap);
    sel.addEventListener('change', () => {
      const v = opts[Number(sel.value)].value;
      const patch = slots.onChange ? ctx.run(slots.onChange, `${spec.id}.onChange`, v) : { [spec.key]: v };
      ctx.apply(patch, `${spec.id}.onChange`);
    });
    ctx.store.watch((st) => {
      const cur = JSON.stringify(st[spec.key]); // option values may be arrays/objects
      const i = opts.findIndex((o) => JSON.stringify(o.value) === cur);
      if (i >= 0) sel.value = i;
    });
  }

  function toggle(box, spec, slots, ctx) {
    const wrap = el('label', 'pg-ctl pg-toggle');
    const input = el('input');
    input.type = 'checkbox';
    wrap.append(input, el('span', 'pg-switch'), el('span', 'pg-ctl-label', spec.label || spec.key));
    box.append(wrap);
    input.addEventListener('change', () => {
      const v = input.checked;
      const patch = slots.onChange ? ctx.run(slots.onChange, `${spec.id}.onChange`, v) : { [spec.key]: v };
      ctx.apply(patch, `${spec.id}.onChange`);
    });
    ctx.store.watch((st) => { input.checked = !!st[spec.key]; });
  }

  function button(box, spec, slots, ctx) {
    const b = el('button', 'pg-btn' + (spec.action === 'reset' ? ' pg-btn-ghost' : ''), spec.label || (spec.action === 'reset' ? 'Reset' : 'Go'));
    b.type = 'button';
    box.append(b);
    b.addEventListener('click', () => {
      if (spec.action === 'reset') return ctx.reset();
      ctx.apply(ctx.run(slots.onClick, `${spec.id}.onClick`), `${spec.id}.onClick`);
    });
  }

  function play(box, spec, slots, ctx) {
    const b = el('button', 'pg-btn pg-play');
    b.type = 'button';
    box.append(b);
    const label = spec.label || 'Play';
    let timer = null, step = 0;
    const show = () => { b.textContent = timer ? 'Pause' : label; b.classList.toggle('is-running', !!timer); };
    const stop = () => { clearInterval(timer); timer = null; show(); };
    const tick = () => {
      const patch = ctx.run(slots.onTick, `${spec.id}.onTick`, step);
      step++;
      if (patch === null || patch === undefined || step >= (spec.maxSteps || 500)) stop();
      if (patch) ctx.apply(patch, `${spec.id}.onTick`);
    };
    b.addEventListener('click', () => {
      if (timer) return stop();
      if (step >= (spec.maxSteps || 500)) step = 0;
      timer = setInterval(() => { try { tick(); } catch (e) { stop(); } }, spec.interval || 100);
      show();
    });
    ctx.registerPlayer({ stop, reset() { stop(); step = 0; } });
    show();
  }

  // ------------------------------------------------------------------ views
  function readout(box, spec, slots, ctx) {
    const tile = el('div', 'pg-readout');
    const v = el('div', 'pg-readout-value');
    tile.append(v, el('div', 'pg-readout-label', spec.label || ''));
    box.append(tile);
    ctx.store.watch((st) => {
      const r = ctx.runWith(slots.value, `${spec.id}.value`, st);
      if (r !== undefined) v.textContent = fmt(r);
    });
  }

  // Chart size, margins, axis rules and auto ranges are shared with the checker (structures.cjs chartFrame).
  const { W, H, STRUCT, AXISLESS, heatExtent, autoDomain, chartFrame } = root.structures;

  function niceTicks(lo, hi, n = 5) {
    const span = hi - lo;
    if (!(span > 0)) return [lo];
    const raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || 10 * mag;
    const out = [];
    for (let t = Math.ceil(lo / step) * step; t <= hi + step * 1e-9; t += step) out.push(Math.abs(t) < step * 1e-9 ? 0 : t);
    return out;
  }

  // Free-form diagram parts (shapes joined by links) are drawn in their own unclipped layer.
  const isDiagram = (m) => m.type === 'shape' || m.type === 'link';

  function colorAttr(c, prop) {
    if (typeof c === 'number') c = PALETTE[Math.abs(Math.round(c)) % PALETTE.length];
    if (typeof c === 'string' && PALETTE.includes(c)) return { cls: `c-${c}`, style: '' };
    if (typeof c === 'string' && /^#[0-9a-f]{3,8}$/i.test(c)) return { cls: '', style: `${prop}:${c};` };
    return { cls: 'c-blue', style: '' };
  }

  function chart(box, spec, slots, ctx) {
    const card = el('div', 'pg-chart');
    if (spec.title) card.append(el('div', 'pg-chart-title', spec.title));
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('class', 'pg-svg');
    card.append(svg);
    box.append(card);
    const clipId = 'clip-' + spec.id.replace(/[^\w-]/g, '');
    const M = { ...root.structures.MARGIN }; // left margin widens for category labels
    let dom = { x: spec.x || [0, 1], y: spec.y || [0, 1] };
    let lastMarks = [];
    let textAt = new Map();  // text mark -> place (structures.placeLabels), set in render()

    const sx = (v) => M.l + (v - dom.x[0]) / (dom.x[1] - dom.x[0]) * (W - M.l - M.r);
    const sy = (v) => H - M.b - (v - dom.y[0]) / (dom.y[1] - dom.y[0]) * (H - M.t - M.b);
    const n = (v) => Math.round(v * 10) / 10;
    const tip = (s) => ` data-tip="${esc(s)}"`;
    const xy = (x, y) => `${spec.xLabel || 'x'} ${fmt(x)} · ${spec.yLabel || 'y'} ${fmt(y)}`;
    // Clickable elements of structural marks: onClick(state, pos, id, index, part)
    const hit = (m, index, part) => ` data-hit="${esc(`${m.id || ''}|${index}|${part}`)}"`;
    const sctx = {
      n, esc, fmt, colorAttr, tip, hit, sx, sy,
      arrowId: `ah-${clipId}`,
      // plot area in px, or m.box = [x0, y0, x1, y1] as fractions of it (from top-left)
      box(m) {
        const L = M.l, T = M.t, Rr = W - M.r, B = H - M.b;
        const f = Array.isArray(m && m.box) && m.box.length === 4 ? m.box : [0, 0, 1, 1];
        return { x0: L + f[0] * (Rr - L), y0: T + f[1] * (B - T), x1: L + f[2] * (Rr - L), y1: T + f[3] * (B - T) };
      },
    };

    function renderMark(m) {
      // `colors: 'blue'` (a single string) is treated as `color: 'blue'`
      const col = colorAttr(m.color !== undefined ? m.color : (typeof m.colors === 'string' ? m.colors : undefined),m.type === 'text' ? 'fill' : (m.type === 'bars' || m.type === 'rect' || m.type === 'area' || m.type === 'points' || m.type === 'point') ? 'fill' : 'stroke');
      const op = m.opacity !== undefined ? ` opacity="${Number(m.opacity)}"` : '';
      const dash = m.dashed ? ' stroke-dasharray="5 4"' : '';
      const sw = m.width !== undefined && m.type !== 'bars' ? ` stroke-width="${Number(m.width)}"` : '';
      const st = col.style ? ` style="${col.style}"` : '';
      switch (m.type) {
        case 'graph': return root.structures.graph(m, sctx);
        case 'tree': return root.structures.tree(m, sctx);
        case 'grid': return root.structures.grid(m, sctx);
        case 'shape': return root.structures.shape(m, sctx);
        case 'link': return root.structures.link(m, diagramGeoms, sctx);
        case 'line':
          return `<polyline class="mk-line ${col.cls}"${st}${op}${dash}${sw} points="${m.points.map((p) => `${n(sx(p[0]))},${n(sy(p[1]))}`).join(' ')}"/>`;
        case 'area': {
          const y0 = sy(Math.min(Math.max(0, dom.y[0]), dom.y[1]));
          const pts = m.points.map((p) => `${n(sx(p[0]))},${n(sy(p[1]))}`);
          if (!pts.length) return '';
          return `<polygon class="mk-area ${col.cls}"${st}${op} points="${n(sx(m.points[0][0]))},${n(y0)} ${pts.join(' ')} ${n(sx(m.points[m.points.length - 1][0]))},${n(y0)}"/>`;
        }
        case 'points':
          return m.points.map((p, i) => {
            const c = Array.isArray(m.colors) ? colorAttr(m.colors[i], 'fill') : col;
            return `<circle class="mk-pt ${c.cls}${m.draggable ? ' is-drag' : ''}"${c.style ? ` style="${c.style}"` : ''}${op} cx="${n(sx(p[0]))}" cy="${n(sy(p[1]))}" r="${m.r || 4.5}"${m.draggable ? ` data-drag="${esc(m.id || '')}" data-index="${i}"` : ''}${tip(xy(p[0], p[1]))}/>`;
          }).join('');
        case 'point':
          return `<circle class="mk-pt mk-big ${col.cls}${m.draggable ? ' is-drag' : ''}"${st}${op} cx="${n(sx(m.x))}" cy="${n(sy(m.y))}" r="${m.r || 7}"${m.draggable ? ` data-drag="${esc(m.id || '')}" data-index="0"` : ''}${tip(xy(m.x, m.y))}/>`;
        case 'bars': {
          const lay = barLayout.get(m) || { w: 0.8, dx: 0 };
          const w = m.width || lay.w;
          const base = Math.min(Math.max(0, dom.y[0]), dom.y[1]);
          const [yLo, yHi] = [Math.min(...dom.y), Math.max(...dom.y)];
          // Many bars: label every k-th one so labels never run together (30 step labels read "0123456789101112…").
          let labelStep = 1;
          if (m.labels && m.items.length > 1) {
            const xs = m.items.map((p) => p[0]).sort((a, b) => a - b);
            let gap = Infinity;
            for (let i = 1; i < xs.length; i++) if (xs[i] > xs[i - 1]) gap = Math.min(gap, xs[i] - xs[i - 1]);
            const px = Number.isFinite(gap) ? Math.abs(sx(gap) - sx(0)) : (W - M.l - M.r) / m.items.length;
            const widest = Math.max(1, ...m.labels.slice(0, m.items.length).map((l) => String(l ?? '').length));
            labelStep = Math.max(1, Math.ceil((widest * 6.5 + 6) / Math.max(px, 1)));
          }
          return m.items.map((p, i) => {
            const cx = p[0] + lay.dx;
            const x1 = sx(cx - w / 2), x2 = sx(cx + w / 2), ya = sy(base), yb = sy(p[1]);
            const c = Array.isArray(m.colors) ? colorAttr(m.colors[i], 'fill') : col;
            const name = m.labels && m.labels[i] !== undefined ? m.labels[i] : fmt(p[0]);
            // labels sit under the group centre (unshifted), outside the clip
            if (m.labels && m.labels[i] !== undefined && i % labelStep === 0) overlay.push(`<text class="mk-barlabel" x="${n(sx(p[0]))}" y="${H - M.b + 18}" text-anchor="middle">${esc(m.labels[i])}</text>`);
            // bar past the fixed range: arrow + true value at the edge
            if (p[1] > yHi || p[1] < yLo) {
              const up = p[1] > yHi, ey = up ? M.t : H - M.b, mx = (x1 + x2) / 2;
              overlay.push(`<path class="mk-clip" d="M${n(mx - 5)},${n(ey + (up ? 7 : -7))} L${n(mx)},${n(ey + (up ? 1 : -1))} L${n(mx + 5)},${n(ey + (up ? 7 : -7))}"/>` +
                `<text class="mk-cliplabel" x="${n(mx)}" y="${n(ey + (up ? 19 : -11))}" text-anchor="middle">${esc(fmt(p[1]))}</text>`);
            }
            return `<rect class="mk-bar ${c.cls}"${c.style ? ` style="${c.style}"` : ''}${op} x="${n(Math.min(x1, x2))}" y="${n(Math.min(ya, yb))}" width="${n(Math.abs(x2 - x1))}" height="${n(Math.abs(yb - ya))}"${tip(`${m.label ? m.label + ' · ' : ''}${name}: ${fmt(p[1])}`)}/>`;
          }).join('');
        }
        case 'hline':
          return `<line class="mk-rule ${col.cls}"${st}${op}${dash}${sw} x1="${M.l}" x2="${W - M.r}" y1="${n(sy(m.y))}" y2="${n(sy(m.y))}"/>`;
        case 'vline':
          return `<line class="mk-rule ${col.cls}"${st}${op}${dash}${sw} y1="${M.t}" y2="${H - M.b}" x1="${n(sx(m.x))}" x2="${n(sx(m.x))}"/>`;
        case 'rect':
          return `<rect class="mk-rect ${col.cls}"${st}${m.opacity === undefined ? ' opacity="0.18"' : op} x="${n(Math.min(sx(m.x1), sx(m.x2)))}" y="${n(Math.min(sy(m.y1), sy(m.y2)))}" width="${n(Math.abs(sx(m.x2) - sx(m.x1)))}" height="${n(Math.abs(sy(m.y2) - sy(m.y1)))}"/>`;
        case 'arrow':
          return `<line class="mk-arrow ${col.cls}"${st}${op}${sw} marker-end="url(#ah-${clipId})" x1="${n(sx(m.x1))}" y1="${n(sy(m.y1))}" x2="${n(sx(m.x2))}" y2="${n(sy(m.y2))}"/>`;
        case 'text': {
          // kept inside the plot and off lines (structures.placeLabels, also what the checker measures)
          const t = textAt.get(m) || root.structures.placeText(m, { sx, sy, plot: sctx.box(null) });
          return `<text class="mk-text ${m.color === undefined ? '' : col.cls}"${st}${op} x="${n(t.x)}" y="${n(t.y)}" text-anchor="${t.anchor}" dy="-0.4em">${esc(m.text)}</text>`;
        }
        case 'heatmap': {
          // Color intensity = opacity of one palette color (works in both themes).
          // diverging: negatives use the second color, centred on 0.
          const v = m.values, rows = v.length, cols = v[0].length;
          const [x0, x1] = heatExtent(m, 'x'), [y0, y1] = heatExtent(m, 'y');
          const cw = (x1 - x0) / cols, ch = (y1 - y0) / rows;
          const dense = Math.min(Math.abs(sx(x0 + cw) - sx(x0)), Math.abs(sy(y0 + ch) - sy(y0))) < 16;
          const flat = v.flat();
          const lo = m.min !== undefined ? m.min : Math.min(...flat), hi = m.max !== undefined ? m.max : Math.max(...flat);
          const absMax = Math.max(Math.abs(lo), Math.abs(hi)) || 1;
          const pos = colorAttr(m.color === undefined ? 'blue' : m.color, 'fill');
          const neg = colorAttr(m.negColor === undefined ? 'red' : m.negColor, 'fill');
          let out = '';
          for (let r = 0; r < rows; r++) {
            for (let c = 0; c < cols; c++) {
              const val = v[r][c];
              const t = m.diverging ? Math.abs(val) / absMax : (hi > lo ? (val - lo) / (hi - lo) : 1);
              const cc = m.diverging && val < 0 ? neg : pos;
              const ax = sx(x0 + c * cw), bx = sx(x0 + (c + 1) * cw) + (dense ? 0.6 : 0), ay = sy(y1 - r * ch), by = sy(y1 - (r + 1) * ch) + (dense ? 0.6 : 0);
              // small cells: no borders (they would hide the colors); slight overlap avoids seams
              out += `<rect class="mk-cell${dense ? ' is-dense' : ''} ${cc.cls}"${cc.style ? ` style="${cc.style}"` : ''} fill-opacity="${n(0.06 + 0.94 * Math.min(1, Math.max(0, t)))}" x="${n(Math.min(ax, bx))}" y="${n(Math.min(ay, by))}" width="${n(Math.abs(bx - ax))}" height="${n(Math.abs(by - ay))}"${tip(`${m.yLabels ? m.yLabels[r] : `row ${r + 1}`} · ${m.xLabels ? m.xLabels[c] : `col ${c + 1}`}: ${fmt(val)}`)}/>`;
              if (m.showValues) {
                out += `<text class="mk-cellval${t > 0.55 ? ' is-strong' : ''}" x="${n((ax + bx) / 2)}" y="${n((ay + by) / 2)}" text-anchor="middle" dy="0.35em">${esc(fmt(val))}</text>`;
              }
            }
          }
          return out;
        }
        case 'pie': {
          const total = m.values.reduce((a, b) => a + b, 0);
          if (!(total > 0)) return '';
          const axesOff = !hasAxes;
          const cx = axesOff ? W / 2 : (M.l + W - M.r) / 2, cy = axesOff ? H / 2 : (M.t + H - M.b) / 2;
          const R = (axesOff ? H : Math.min(W - M.l - M.r, H - M.t - M.b)) / 2 - 8;
          const r0 = R * (m.donut || 0);
          const at = (a, rad) => [cx + rad * Math.cos(a), cy + rad * Math.sin(a)];
          let a = -Math.PI / 2, out = '';
          m.values.forEach((val, i) => {
            if (!(val > 0)) return;
            const frac = val / total, b = a + frac * 2 * Math.PI;
            const c = colorAttr(Array.isArray(m.colors) ? m.colors[i] : (m.color !== undefined && m.values.length === 1 ? m.color : i), 'fill');
            const cs = (c.style ? ` style="${c.style}"` : '') +
              tip(`${m.labels && m.labels[i] !== undefined ? m.labels[i] : `slice ${i + 1}`}: ${fmt(val)} (${Math.round(frac * 100)}%)`);
            if (frac > 0.9999) {
              out += `<circle class="mk-slice ${c.cls}"${cs}${op} cx="${n(cx)}" cy="${n(cy)}" r="${n(R)}"/>`;
              if (r0) out += `<circle class="mk-hole" cx="${n(cx)}" cy="${n(cy)}" r="${n(r0)}"/>`;
            } else {
              const big = frac > 0.5 ? 1 : 0;
              const [p1x, p1y] = at(a, R), [p2x, p2y] = at(b, R), [q1x, q1y] = at(b, r0), [q2x, q2y] = at(a, r0);
              const d = r0
                ? `M${n(p1x)},${n(p1y)} A${n(R)},${n(R)} 0 ${big} 1 ${n(p2x)},${n(p2y)} L${n(q1x)},${n(q1y)} A${n(r0)},${n(r0)} 0 ${big} 0 ${n(q2x)},${n(q2y)} Z`
                : `M${n(cx)},${n(cy)} L${n(p1x)},${n(p1y)} A${n(R)},${n(R)} 0 ${big} 1 ${n(p2x)},${n(p2y)} Z`;
              out += `<path class="mk-slice ${c.cls}"${cs}${op} d="${d}"/>`;
            }
            if (frac >= 0.04) {
              const [lx, ly] = at((a + b) / 2, r0 ? (R + r0) / 2 : R * 0.62);
              const lab = m.labels && m.labels[i] !== undefined ? `${esc(m.labels[i])} ` : '';
              out += `<text class="mk-slicelabel" x="${n(lx)}" y="${n(ly)}" text-anchor="middle" dy="0.35em">${lab}${Math.round(frac * 100)}%</text>`;
            }
            a = b;
          });
          return out;
        }
        default:
          return '';
      }
    }

    let hasAxes = true;
    let diagramGeoms = new Map();  // shape id -> where it is on screen, for links
    let overlay = [];
    let barLayout = new Map();

    // All bars marks in a chart are laid out together: width comes from the
    // closest x spacing across every series, and series sharing x positions
    // are placed side by side (grouped) instead of on top of each other.
    function layoutBars(marks) {
      barLayout = new Map();
      const bms = marks.filter((m) => m.type === 'bars');
      if (!bms.length) return;
      const xs = [...new Set(bms.flatMap((m) => m.items.map((p) => p[0])))].sort((a, b) => a - b);
      let gap = Infinity;
      for (let i = 1; i < xs.length; i++) gap = Math.min(gap, xs[i] - xs[i - 1]);
      if (!Number.isFinite(gap) || gap <= 0) gap = 1;
      const shared = bms.length > 1 && bms.some((m, j) => bms.some((o, k) => k !== j && m.items.some((p) => o.items.some((q) => q[0] === p[0]))));
      if (shared) {
        const w = gap * 0.8 / bms.length;
        bms.forEach((m, j) => barLayout.set(m, { w, dx: (j - (bms.length - 1) / 2) * w }));
      } else {
        bms.forEach((m) => barLayout.set(m, { w: gap * 0.8, dx: 0 }));
      }
    }
    function render(marks) {
      overlay = [];
      layoutBars(marks);
      const frame = chartFrame(spec, marks);  // the same frame the checker measures layout with
      dom = frame.dom;
      hasAxes = frame.hasAxes;
      Object.assign(M, frame.M);
      textAt = root.structures.placeLabels(marks, { sx, sy, plot: sctx.box(null) });
      const hasBarLabels = marks.some((m) => m.type === 'bars' && m.labels);
      const hm = marks.find((m) => m.type === 'heatmap' && (m.xLabels || m.yLabels));
      const xt = niceTicks(dom.x[0], dom.x[1]), yt = niceTicks(dom.y[0], dom.y[1]);
      let g = `<defs><clipPath id="${clipId}"><rect x="${M.l}" y="${M.t}" width="${W - M.l - M.r}" height="${H - M.t - M.b}"/></clipPath>` +
        `<marker id="ah-${clipId}" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" markerWidth="11" markerHeight="11" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="mk-head"/></marker></defs>`;
      if (hasAxes) {
        // Heatmap category labels replace numeric ticks on that axis.
        const cat = (axis) => {
          if (!hm || !hm[axis === 'x' ? 'xLabels' : 'yLabels']) return null;
          const labs = hm[axis === 'x' ? 'xLabels' : 'yLabels'], [a, b] = heatExtent(hm, axis), k = labs.length, step = (b - a) / k;
          // y labels follow row order: row 0 at the top
          return labs.map((l, i) => [axis === 'x' ? a + (i + 0.5) * step : b - (i + 0.5) * step, l]);
        };
        const yc = cat('y'), xc = cat('x');
        g += yc
          ? yc.map(([v, l]) => `<text class="ax-tick" x="${M.l - 6}" y="${n(sy(v))}" text-anchor="end" dy="0.32em">${esc(l)}</text>`).join('')
          : yt.map((t) => `<line class="ax-grid" x1="${M.l}" x2="${W - M.r}" y1="${n(sy(t))}" y2="${n(sy(t))}"/><text class="ax-tick" x="${M.l - 6}" y="${n(sy(t))}" text-anchor="end" dy="0.32em">${fmt(t)}</text>`).join('');
        if (xc) g += xc.map(([v, l]) => `<text class="ax-tick" x="${n(sx(v))}" y="${H - M.b + 18}" text-anchor="middle">${esc(l)}</text>`).join('');
        else if (!hasBarLabels) g += xt.map((t) => `<text class="ax-tick" x="${n(sx(t))}" y="${H - M.b + 18}" text-anchor="middle">${fmt(t)}</text>`).join('');
        g += `<line class="ax-line" x1="${M.l}" x2="${W - M.r}" y1="${H - M.b}" y2="${H - M.b}"/>`;
        if (spec.xLabel) g += `<text class="ax-label" x="${W - M.r}" y="${H - 2}" text-anchor="end">${esc(spec.xLabel)}</text>`;
        if (spec.yLabel) g += `<text class="ax-label" x="${M.l + 4}" y="${M.t + 10}">${esc(spec.yLabel)}</text>`;
      }
      const isStruct = (m) => STRUCT.has(m.type) && m.type !== 'grid';
      diagramGeoms = new Map(marks.filter((m) => m.type === 'shape' && m.id !== undefined).map((m) => [m.id, root.structures.shapeGeom(m, sctx)]));
      const body = marks.filter((m) => m.type !== 'text' && m.type !== 'pie' && !m.draggable && !isStruct(m) && !isDiagram(m)).map(renderMark).join('');
      // links first, then the shapes on top of their ends
      const diagram = marks.filter((m) => m.type === 'link').map(renderMark).join('') +
        marks.filter((m) => m.type === 'shape' && !m.draggable).map(renderMark).join('');
      // structural marks: unclipped (labels may extend); matrices and sequences lay out as groups
      let struct = marks.filter((m) => isStruct(m) && !m.draggable && (m.type === 'graph' || m.type === 'tree')).map(renderMark).join('');
      const mats = marks.filter((m) => m.type === 'matrix'), seqs = marks.filter((m) => m.type === 'sequence');
      if (mats.length) struct += root.structures.matrices(mats, sctx, sctx.box(mats[0]));
      if (seqs.length) struct += root.structures.sequences(seqs, sctx, sctx.box(seqs[0]));
      const top = marks.filter((m) => m.type === 'pie' || m.type === 'text' || m.draggable).map(renderMark).join('');
      g += `<g clip-path="url(#${clipId})">${body}</g>${struct}${diagram}${overlay.join('')}${top}`;
      svg.innerHTML = g;
    }

    ctx.store.watch((st) => {
      const marks = ctx.runWith(slots.draw, `${spec.id}.draw`, st);
      if (marks) { lastMarks = marks; render(marks); }
    });

    // ---- pointer: drag draggable marks, or click empty area
    const interactive = slots.onDrag || slots.onClick;
    if (interactive) svg.classList.add('is-interactive');
    let drag = null;
    const toData = (ev) => {
      const pt = svg.createSVGPoint();
      pt.x = ev.clientX; pt.y = ev.clientY;
      const p = pt.matrixTransform(svg.getScreenCTM().inverse());
      const x = dom.x[0] + (p.x - M.l) / (W - M.l - M.r) * (dom.x[1] - dom.x[0]);
      const y = dom.y[0] + (H - M.b - p.y) / (H - M.t - M.b) * (dom.y[1] - dom.y[0]);
      const c = (v, d) => Math.min(Math.max(v, Math.min(d[0], d[1])), Math.max(d[0], d[1]));
      const pos = { x: c(x, dom.x), y: c(y, dom.y) };
      // On a grid chart, also report which cell the pointer is over.
      const gm = lastMarks.find((m) => m.type === 'grid');
      return gm ? Object.assign(pos, root.structures.cellAt(gm, pos.x, pos.y)) : pos;
    };
    svg.addEventListener('pointerdown', (ev) => {
      const t = ev.target.closest && ev.target.closest('[data-drag]');
      if (t && slots.onDrag) {
        drag = { id: t.getAttribute('data-drag'), index: Number(t.getAttribute('data-index')) };
        svg.setPointerCapture(ev.pointerId);
        svg.classList.add('is-dragging');
        ev.preventDefault();
      } else if (slots.onClick) {
        // element of a structural mark -> (id, index, part); empty area -> pos only
        const h = ev.target.closest && ev.target.closest('[data-hit]');
        const args = [toData(ev)];
        if (h) { const [id, index, part] = h.getAttribute('data-hit').split('|'); args.push(id, Number(index), part); }
        ctx.apply(ctx.run(slots.onClick, `${spec.id}.onClick`, ...args), `${spec.id}.onClick`);
      }
    });
    svg.addEventListener('pointermove', (ev) => {
      if (!drag) return hover(ev);
      const pos = toData(ev);
      ctx.apply(ctx.run(slots.onDrag, `${spec.id}.onDrag`, pos, drag.id, drag.index), `${spec.id}.onDrag`);
      showTip(ev, xy(pos.x, pos.y));
    });
    const end = () => { drag = null; svg.classList.remove('is-dragging'); };
    svg.addEventListener('pointerup', end);
    svg.addEventListener('pointercancel', end);

    // ---- hover values: marks carry data-tip; lines snap to their nearest point
    const tipEl = el('div', 'pg-tip');
    tipEl.hidden = true;
    card.append(tipEl);
    function showTip(ev, text) {
      tipEl.textContent = text;
      tipEl.hidden = false;
      const r = card.getBoundingClientRect();
      const x = Math.min(ev.clientX - r.left + 12, r.width - tipEl.offsetWidth - 4);
      tipEl.style.left = `${Math.max(4, x)}px`;
      tipEl.style.top = `${Math.max(4, ev.clientY - r.top - 30)}px`;
    }
    function hideTip() {
      tipEl.hidden = true;
      const d = svg.querySelector('.hover-dot');
      if (d) d.remove();
    }
    function hover(ev) {
      const t = ev.target.closest && ev.target.closest('[data-tip]');
      const old = svg.querySelector('.hover-dot');
      if (old) old.remove();
      if (t) return showTip(ev, t.getAttribute('data-tip'));
      // nearest point on a line/area within 16 px (in SVG units)
      const pt = svg.createSVGPoint();
      pt.x = ev.clientX; pt.y = ev.clientY;
      const p = pt.matrixTransform(svg.getScreenCTM().inverse());
      let best = null, bd = 16 * 16;
      for (const m of lastMarks) {
        if (m.type !== 'line' && m.type !== 'area') continue;
        for (const q of m.points) {
          const dx = sx(q[0]) - p.x, dy = sy(q[1]) - p.y, d = dx * dx + dy * dy;
          if (d < bd) { bd = d; best = q; }
        }
      }
      if (!best) return hideTip();
      const dot = document.createElementNS(SVG_NS, 'circle');
      dot.setAttribute('class', 'hover-dot');
      dot.setAttribute('cx', n(sx(best[0])));
      dot.setAttribute('cy', n(sy(best[1])));
      dot.setAttribute('r', 4);
      svg.append(dot);
      showTip(ev, xy(best[0], best[1]));
    }
    svg.addEventListener('pointerleave', () => { if (!drag) hideTip(); });
  }

  const components = { slider, select, toggle, button, play, readout, chart };
  root.components = components;
  root.componentUtils = { niceTicks, autoDomain, fmt };
})(window);
