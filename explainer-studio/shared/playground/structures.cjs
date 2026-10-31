// Copied from the ai-playground experiment (Bloging-substack/ECommerce/ai-playground), adapted for Explainer Studio's Playground mode.
// Layout + SVG for structural marks: graph, tree, matrix, sequence, grid.
// Snippets pass structure only; every position is computed here.
// ctx = { n, esc, fmt, colorAttr, tip(text), hit(mark, index, part), sx, sy, box(mark), arrowId }
(function (root) {
  const PALETTE = ['blue', 'orange', 'green', 'red', 'purple', 'teal', 'gray', 'pink'];
  // grid categories: 0 empty, 1 wall, 2 goal, 3 hazard, then the palette
  const GRID_COLORS = [null, 'gray', 'green', 'red', 'blue', 'orange', 'purple', 'teal', 'pink'];
  const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
  const cut = (s, max) => { s = String(s); return s.length > max ? s.slice(0, Math.max(1, max - 1)) + '…' : s; };
  const attrs = (c) => `${c.cls}"${c.style ? ` style="${c.style}"` : ''}`;
  const maxAbs = (vals) => vals.reduce((m, v) => (Number.isFinite(v) ? Math.max(m, Math.abs(v)) : m), 0) || 1;

  // ------------------------------------------------------------ chart frame
  // A chart's size and plot area in px (its 440x290 viewBox). components.cjs draws with chartFrame and validator.cjs
  // checks layout with it, so a layout problem the checker reports is one the reader would see.
  const W = 440, H = 290, MARGIN = { l: 46, r: 14, t: 16, b: 32 };
  // Structural marks are laid out here; charts made only of these (plus pie/text) have no axes.
  const STRUCT = new Set(['graph', 'tree', 'matrix', 'sequence', 'grid']);
  const AXISLESS = new Set(['graph', 'tree', 'matrix', 'sequence', 'grid', 'pie', 'text', 'shape', 'link']);
  // Data-space extent of a heatmap: explicit x/y, else one unit per cell.
  const heatExtent = (m, axis) => axis === 'x' ? (m.x || [0, m.values[0].length]) : (m.y || [0, m.values.length]);

  function autoDomain(marks, axis) {
    let lo = Infinity, hi = -Infinity;
    const see = (v) => { if (Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } };
    for (const m of marks) {
      const i = axis === 'x' ? 0 : 1;
      if (m.type === 'heatmap') { heatExtent(m, axis).forEach(see); continue; }
      if (m.type === 'grid') { gridExtent(m)[axis].forEach(see); continue; }
      if (m.type === 'graph' && m.layout === 'manual') { m.nodes.forEach((nd) => see(nd[axis])); continue; }
      if (STRUCT.has(m.type) || m.type === 'pie') continue;
      (m.points || []).forEach((p) => see(p[i]));
      if (m.type === 'bars') {
        if (axis === 'y') { see(0); m.items.forEach((p) => see(p[1])); } else m.items.forEach((p) => { see(p[0] - 0.5); see(p[0] + 0.5); });
      }
      if (axis === 'x') [m.x, m.x1, m.x2].forEach(see); else [m.y, m.y1, m.y2].forEach(see);
    }
    if (lo === Infinity) return [0, 1];
    if (lo === hi) { lo -= 1; hi += 1; }
    const tight = marks.length && marks.every((m) => m.type === 'heatmap' || m.type === 'grid' || m.type === 'text' || m.type === 'bars');
    const pad = tight ? 0 : (hi - lo) * 0.08;
    return [lo - pad, hi + pad];
  }

  // { dom, M, hasAxes, sx, sy, plot } for a chart spec drawing these marks (the rules components.cjs renders with).
  function chartFrame(spec, marks) {
    const dom = { x: spec.x || autoDomain(marks, 'x'), y: spec.y || autoDomain(marks, 'y') };
    // No axes for charts made only of pie/structural/text marks, or with a grid; `axes: true/false` overrides.
    const auto = !(marks.some((m) => m.type !== 'text') &&
      (marks.every((m) => AXISLESS.has(m.type)) || marks.some((m) => m.type === 'grid')));
    const hasAxes = spec.axes === undefined ? auto : !!spec.axes;
    const hm = marks.find((m) => m.type === 'heatmap' && (m.xLabels || m.yLabels));
    const M = hasAxes ? { ...MARGIN } : { l: 12, r: 12, t: 12, b: 12 };
    if (hasAxes && hm && hm.yLabels) M.l = Math.min(140, Math.max(MARGIN.l, 14 + 7.5 * Math.max(...hm.yLabels.map((l) => String(l).length))));
    const sx = (v) => M.l + (v - dom.x[0]) / (dom.x[1] - dom.x[0]) * (W - M.l - M.r);
    const sy = (v) => H - M.b - (v - dom.y[0]) / (dom.y[1] - dom.y[0]) * (H - M.t - M.b);
    return { dom, M, hasAxes, sx, sy, plot: { x0: M.l, y0: M.t, x1: W - M.r, y1: H - M.b } };
  }

  // ------------------------------------------------------------------ graph
  function graph(m, ctx) {
    const { n, esc, fmt, colorAttr, tip, hit } = ctx;
    const b = ctx.box(m), N = m.nodes.length;
    const layered = m.layout === 'layers' || (!m.layout && m.nodes.some((nd) => nd.layer !== undefined));
    const pos = new Array(N);
    let span = 1;
    if (m.layout === 'manual') {
      m.nodes.forEach((nd, i) => { pos[i] = [ctx.sx(nd.x), ctx.sy(nd.y)]; });
      span = Math.ceil(Math.sqrt(N));
    } else if (layered) {
      const layers = [...new Set(m.nodes.map((nd) => nd.layer || 0))].sort((a, c) => a - c);
      const cols = layers.map((l) => m.nodes.map((nd, i) => ((nd.layer || 0) === l ? i : -1)).filter((i) => i >= 0));
      cols.forEach((ids, li) => ids.forEach((i, k) => {
        pos[i] = [b.x0 + (li + 0.5) * (b.x1 - b.x0) / cols.length, b.y0 + (k + 0.5) * (b.y1 - b.y0) / ids.length];
      }));
      span = Math.max(cols.length, ...cols.map((c) => c.length));
    } else {
      // leave room outside the ring for self-loops
      const loops = m.edges.some((e) => e[0] === e[1]);
      const r0 = m.r || clamp(Math.min(b.x1 - b.x0, b.y1 - b.y0) / (Math.max(3, N / 2.2) * 2.8), 7, 18);
      const cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2, R = Math.min(b.x1 - b.x0, b.y1 - b.y0) / 2 - 22 - (loops ? r0 * 2.6 : 0);
      m.nodes.forEach((nd, i) => { const a = -Math.PI / 2 + 2 * Math.PI * i / N; pos[i] = [cx + R * Math.cos(a), cy + R * Math.sin(a)]; });
      span = Math.max(3, N / 2.2);
    }
    const r = m.r || clamp(Math.min(b.x1 - b.x0, b.y1 - b.y0) / (span * 2.8), 7, 18);

    const wMax = maxAbs(m.edges.map((e) => e[2]));
    const cx0 = pos.reduce((t, p) => t + p[0], 0) / N, cy0 = pos.reduce((t, p) => t + p[1], 0) / N;
    const pairs = new Set(m.edges.map((e) => `${e[0]}>${e[1]}`));
    const unit = (x, y) => { const L = Math.hypot(x, y) || 1; return [x / L, y / L]; };
    let out = '';
    m.edges.forEach((e, j) => {
      const [a, c, w] = e;
      const [x1, y1] = pos[a], [x2, y2] = pos[c];
      const has = Number.isFinite(w), t = has ? Math.abs(w) / wMax : 0.5;
      const col = colorAttr(has ? (w >= 0 ? (m.posColor || 'blue') : (m.negColor || 'red')) : 'gray', 'stroke');
      const name = `${m.nodes[a].label || `#${a}`} → ${m.nodes[c].label || `#${c}`}`;
      let d, lx, ly;
      if (a === c) {
        // self-loop, bulging away from the graph's centre
        const [ux, uy] = N > 1 && Math.hypot(x1 - cx0, y1 - cy0) > 1 ? unit(x1 - cx0, y1 - cy0) : [0, -1];
        const [px, py] = [-uy, ux];
        const sx0 = x1 + (ux * 0.7 + px * 0.7) * r, sy0 = y1 + (uy * 0.7 + py * 0.7) * r;
        const ex = x1 + (ux * 0.7 - px * 0.7) * r, ey = y1 + (uy * 0.7 - py * 0.7) * r;
        const k = r * 2.6;
        d = `M${n(sx0)},${n(sy0)} C${n(sx0 + ux * k + px * r)},${n(sy0 + uy * k + py * r)} ${n(ex + ux * k - px * r)},${n(ey + uy * k - py * r)} ${n(ex)},${n(ey)}`;
        lx = x1 + ux * (r + k * 0.8); ly = y1 + uy * (r + k * 0.8);
      } else {
        // edges in both directions curve apart instead of overlapping
        const bend = m.directed && pairs.has(`${c}>${a}`) ? 0.2 : 0;
        const [ux, uy] = unit(x2 - x1, y2 - y1), L = Math.hypot(x2 - x1, y2 - y1);
        const qx = (x1 + x2) / 2 - uy * L * bend, qy = (y1 + y2) / 2 + ux * L * bend;
        const [sx1, sy1] = unit(qx - x1, qy - y1), [sx2, sy2] = unit(x2 - qx, y2 - qy);
        const end = r + (m.directed ? 3 : 0);
        d = `M${n(x1 + sx1 * r)},${n(y1 + sy1 * r)} Q${n(qx)},${n(qy)} ${n(x2 - sx2 * end)},${n(y2 - sy2 * end)}`;
        lx = 0.25 * x1 + 0.5 * qx + 0.25 * x2; ly = 0.25 * y1 + 0.5 * qy + 0.25 * y2;
      }
      out += `<path class="st-edge ${attrs(col)} d="${d}" stroke-width="${n(has ? 0.8 + 4 * t : 1.5)}" stroke-opacity="${n(has ? 0.22 + 0.78 * t : 0.6)}"${m.directed ? ` marker-end="url(#${ctx.arrowId})"` : ''}/>`;
      out += `<path class="st-hit" d="${d}"${tip(has ? `${name}: ${fmt(w)}` : name)}${hit(m, j, 'edge')}/>`;
      if (m.edgeLabels && has) out += `<text class="st-small" x="${n(lx)}" y="${n(ly)}" text-anchor="middle" dy="0.35em">${esc(fmt(w))}</text>`;
    });

    const vMax = maxAbs(m.nodes.map((nd) => nd.value));
    m.nodes.forEach((nd, i) => {
      const [x, y] = pos[i];
      let c, fop = 1, plain = false;
      if (nd.color !== undefined) { c = colorAttr(nd.color, 'fill'); fop = 0.85; }
      else if (Number.isFinite(nd.value)) { c = colorAttr(nd.value >= 0 ? (m.posColor || 'blue') : (m.negColor || 'red'), 'fill'); fop = 0.12 + 0.88 * Math.abs(nd.value) / vMax; }
      else { c = { cls: '', style: '' }; plain = true; }
      const label = nd.label === undefined ? '' : String(nd.label);
      const drag = m.layout === 'manual' && m.draggable ? ` data-drag="${esc(m.id || '')}" data-index="${i}"` : '';
      out += `<circle class="st-node${plain ? ' is-plain' : ''} ${attrs(c)} cx="${n(x)}" cy="${n(y)}" r="${n(r)}" fill-opacity="${n(fop)}"${drag}` +
        `${tip(`${label || `node ${i}`}${Number.isFinite(nd.value) ? `: ${fmt(nd.value)}` : ''}`)}${hit(m, i, 'node')}/>`;
      if (label) {
        out += label.length <= 3
          ? `<text class="st-label" x="${n(x)}" y="${n(y)}" text-anchor="middle" dy="0.35em">${esc(label)}</text>`
          : `<text class="st-label" x="${n(x)}" y="${n(y + r + 13)}" text-anchor="middle">${esc(cut(label, 12))}</text>`;
      }
      if (m.showValues && Number.isFinite(nd.value)) {
        out += `<text class="st-small" x="${n(x)}" y="${n(y - r - 5)}" text-anchor="middle">${esc(fmt(nd.value))}</text>`;
      }
    });
    return out;
  }

  // ------------------------------------------------------------------- tree
  function tree(m, ctx) {
    const { n, esc, fmt, colorAttr, tip, hit } = ctx;
    const b = ctx.box(m);
    const nodes = [];
    (function walk(nd, d, p) {
      const i = nodes.length;
      nodes.push({ nd, d, p, kids: [] });
      if (p >= 0) nodes[p].kids.push(i);
      (nd.children || []).forEach((ch) => walk(ch, d + 1, i));
    })(m.root, 0, -1);
    const depth = Math.max(...nodes.map((x) => x.d));
    let leaf = 0;
    const xs = new Array(nodes.length);
    (function place(i) {
      const k = nodes[i].kids;
      if (!k.length) { xs[i] = leaf++; return; }
      k.forEach(place);
      xs[i] = (xs[k[0]] + xs[k[k.length - 1]]) / 2;
    })(0);
    const slot = (b.x1 - b.x0) / leaf, row = (b.y1 - b.y0) / (depth + 1);
    const X = (i) => b.x0 + (xs[i] + 0.5) * slot, Y = (i) => b.y0 + (nodes[i].d + 0.5) * row;
    const bh = clamp(row * 0.5, 16, 26), maxW = clamp(slot - 6, 24, 120);
    const hl = new Set(m.highlight || []);
    let out = '';
    nodes.forEach((x, i) => {
      if (x.p < 0) return;
      const x1 = X(x.p), y1 = Y(x.p) + bh / 2, x2 = X(i), y2 = Y(i) - bh / 2;
      const on = hl.has(i) || x.nd.active;
      out += `<line class="st-tedge${on ? ' is-on' : ''}" x1="${n(x1)}" y1="${n(y1)}" x2="${n(x2)}" y2="${n(y2)}"/>`;
      if (x.nd.edge !== undefined) out += `<text class="st-small" x="${n((x1 + x2) / 2)}" y="${n((y1 + y2) / 2)}" text-anchor="middle" dy="0.35em">${esc(cut(x.nd.edge, 8))}</text>`;
    });
    nodes.forEach((x, i) => {
      const label = x.nd.label === undefined ? '' : String(x.nd.label);
      const w = Math.min(maxW, Math.max(24, label.length * 6.6 + 14));
      const c = x.nd.color !== undefined ? colorAttr(x.nd.color, 'fill') : { cls: '', style: '' };
      const on = hl.has(i) || x.nd.active;
      out += `<rect class="st-box${x.nd.color !== undefined ? ' is-colored' : ''}${on ? ' is-on' : ''} ${attrs(c)} x="${n(X(i) - w / 2)}" y="${n(Y(i) - bh / 2)}" width="${n(w)}" height="${n(bh)}" rx="6"` +
        `${tip(`${label || `node ${i}`}${x.nd.value !== undefined ? ` (${fmt(x.nd.value)})` : ''}`)}${hit(m, i, 'node')}/>`;
      if (label) out += `<text class="st-label" x="${n(X(i))}" y="${n(Y(i))}" text-anchor="middle" dy="0.35em">${esc(cut(label, Math.floor((w - 8) / 6.6)))}</text>`;
    });
    return out;
  }

  // ------------------------------------------------- matrices (left to right)
  function matrices(ms, ctx, b) {
    const { n, esc, fmt, colorAttr, tip, hit } = ctx;
    const OP = 26, PAD = 10, ROWLAB = 44;
    const cols = ms.reduce((t, m) => t + m.values[0].length, 0);
    const rows = Math.max(...ms.map((m) => m.values.length));
    const colLab = ms.some((m) => m.colLabels);
    const overhead = ms.reduce((t, m) => t + 2 * PAD + (m.op ? OP : 0) + (m.rowLabels ? ROWLAB : 0), 0) + (ms.length - 1) * 6;
    const cs = clamp(Math.min((b.x1 - b.x0 - overhead) / cols, (b.y1 - b.y0 - 26 - (colLab ? 16 : 0)) / rows), 10, 44);
    let x = b.x0 + (b.x1 - b.x0 - (cols * cs + overhead)) / 2;
    const cy = (b.y0 + b.y1) / 2 + 8 + (colLab ? 8 : 0);
    const fs = clamp(cs * 0.36, 9, 14);
    let out = '';
    ms.forEach((m) => {
      const R = m.values.length, C = m.values[0].length, h = R * cs;
      if (m.op) { out += `<text class="st-op" x="${n(x + OP / 2)}" y="${n(cy)}" text-anchor="middle" dy="0.35em">${esc(cut(m.op, 2))}</text>`; x += OP; }
      if (m.rowLabels) x += ROWLAB;
      const left = x + PAD, top = cy - h / 2;
      const name = m.label === undefined ? 'M' : String(m.label);
      if (m.label !== undefined) out += `<text class="st-mlabel" x="${n(left + C * cs / 2)}" y="${n(top - 8 - (m.colLabels ? 15 : 0))}" text-anchor="middle">${esc(cut(m.label, 10))}</text>`;
      (m.colLabels || []).slice(0, C).forEach((l, c) => { out += `<text class="st-small" x="${n(left + (c + 0.5) * cs)}" y="${n(top - 5)}" text-anchor="middle">${esc(cut(l, Math.max(2, Math.floor(cs / 6.5))))}</text>`; });
      (m.rowLabels || []).slice(0, R).forEach((l, r) => { out += `<text class="st-small" x="${n(left - 7)}" y="${n(top + (r + 0.5) * cs)}" text-anchor="end" dy="0.35em">${esc(cut(l, 6))}</text>`; });
      const vMax = maxAbs(m.values.flat());
      const hr = new Set(m.highlightRows || []), hc = new Set(m.highlightCols || []);
      const hcell = new Set((m.highlightCells || []).map((p) => `${p[0]},${p[1]}`));
      m.values.forEach((rowVals, r) => rowVals.forEach((v, c) => {
        const on = hr.has(r) || hc.has(c) || hcell.has(`${r},${c}`);
        const heat = m.heat ? colorAttr(v >= 0 ? (m.posColor || 'blue') : (m.negColor || 'red'), 'fill') : null;
        out += `<rect class="st-cell${on ? ' is-on' : ''}${heat ? ` ${heat.cls}` : ''}"${heat && heat.style ? ` style="${heat.style}"` : ''}${heat ? ` fill-opacity="${n(0.1 + 0.75 * Math.abs(v) / vMax)}"` : ''} x="${n(left + c * cs)}" y="${n(top + r * cs)}" width="${n(cs)}" height="${n(cs)}"` +
          `${tip(`${name}[${r + 1},${c + 1}] = ${fmt(v)}`)}${hit(m, r * C + c, 'cell')}/>`;
        out += `<text class="st-num${on ? ' is-on' : ''}" x="${n(left + (c + 0.5) * cs)}" y="${n(top + (r + 0.5) * cs)}" text-anchor="middle" dy="0.35em" font-size="${n(fs)}">${esc(cut(fmt(v), Math.max(3, Math.floor(cs / (fs * 0.55)))))}</text>`;
      }));
      const right = left + C * cs;
      out += `<path class="st-bracket" d="M${n(left - 2)},${n(top - 3)} h-5 v${n(h + 6)} h5 M${n(right + 2)},${n(top - 3)} h5 v${n(h + 6)} h-5"/>`;
      x = right + PAD + 6;
    });
    return out;
  }

  // ------------------------------------------------ sequences (stacked rows)
  function sequences(ms, ctx, b) {
    const { n, esc, fmt, colorAttr, tip, hit } = ctx;
    const rows = ms.length, band = (b.y1 - b.y0) / rows;
    const labW = ms.some((m) => m.label !== undefined) ? 58 : 0;
    const GAP = 6;
    const geo = ms.map((m, i) => {
      const N = m.tokens.length;
      const tw = Math.min(84, (b.x1 - b.x0 - labW - GAP * (N - 1)) / N);
      const th = clamp(band * 0.4, 18, 34);
      const x0 = b.x0 + labW + (b.x1 - b.x0 - labW - (N * tw + (N - 1) * GAP)) / 2;
      const y = b.y0 + band * (i + 0.5) - th / 2 - (m.values ? 7 : 0);
      return { tw, th, y, cx: m.tokens.map((_, k) => x0 + k * (tw + GAP) + tw / 2) };
    });
    let out = '';
    // links first so boxes sit on top
    ms.forEach((m, i) => {
      if (!m.links || !ms[i + 1]) return;
      const g = geo[i], h = geo[i + 1], next = ms[i + 1].tokens;
      const wMax = maxAbs(m.links.map((l) => l[2]));
      const col = colorAttr(m.linkColor || 'purple', 'stroke');
      m.links.forEach(([a, c, w], j) => {
        if (c < 0 || c >= next.length) return;
        const t = Number.isFinite(w) ? Math.abs(w) / wMax : 0.5;
        const y1 = g.y + g.th + (m.values ? 14 : 0), y2 = h.y;
        const xy = `x1="${n(g.cx[a])}" y1="${n(y1)}" x2="${n(h.cx[c])}" y2="${n(y2)}"`;
        out += `<line class="st-link ${attrs(col)} ${xy} stroke-width="${n(0.6 + 5 * t)}" stroke-opacity="${n(0.12 + 0.88 * t)}"/>`;
        out += `<line class="st-hit" ${xy}${tip(`${m.tokens[a]} → ${next[c]}${Number.isFinite(w) ? `: ${fmt(w)}` : ''}`)}${hit(m, j, 'link')}/>`;
      });
    });
    ms.forEach((m, i) => {
      const g = geo[i];
      const hl = new Set(m.highlight || []);
      const win = Array.isArray(m.window) ? m.window : null;
      const maxChars = Math.max(2, Math.floor((g.tw - 6) / 7));
      if (m.label !== undefined) out += `<text class="st-mlabel" x="${n(b.x0 + labW - 8)}" y="${n(g.y + g.th / 2)}" text-anchor="end" dy="0.35em">${esc(cut(m.label, 8))}</text>`;
      m.tokens.forEach((tok, k) => {
        const on = hl.has(k) || (win && k >= win[0] && k <= win[1]);
        const c = Array.isArray(m.colors) && m.colors[k] !== undefined && m.colors[k] !== null ? colorAttr(m.colors[k], 'fill') : null;
        const x = g.cx[k] - g.tw / 2;
        if (m.arrows && k < m.tokens.length - 1) {
          out += `<line class="st-tedge" x1="${n(x + g.tw)}" y1="${n(g.y + g.th / 2)}" x2="${n(x + g.tw + GAP - 1)}" y2="${n(g.y + g.th / 2)}" marker-end="url(#${ctx.arrowId})"/>`;
        }
        const val = m.values && m.values[k] !== undefined ? m.values[k] : undefined;
        out += `<rect class="st-tok${on ? ' is-on' : ''}${c ? ` is-colored ${c.cls}` : ''}"${c && c.style ? ` style="${c.style}"` : ''} x="${n(x)}" y="${n(g.y)}" width="${n(g.tw)}" height="${n(g.th)}" rx="6"` +
          `${tip(`${tok}${val !== undefined ? `: ${fmt(val)}` : ''}`)}${hit(m, k, 'item')}/>`;
        out += `<text class="st-label" x="${n(g.cx[k])}" y="${n(g.y + g.th / 2)}" text-anchor="middle" dy="0.35em">${esc(cut(tok, maxChars))}</text>`;
        if (val !== undefined) out += `<text class="st-small" x="${n(g.cx[k])}" y="${n(g.y + g.th + 12)}" text-anchor="middle">${esc(cut(fmt(val), maxChars + 1))}</text>`;
      });
    });
    return out;
  }

  // ------------------------------------------------- grid (in data units)
  const gridExtent = (m) => ({ x: m.x || [0, m.cells[0].length], y: m.y || [0, m.cells.length] });
  const ARROW = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] };

  function grid(m, ctx) {
    const { n, esc, colorAttr, tip, hit } = ctx;
    const R = m.cells.length, C = m.cells[0].length, ext = gridExtent(m);
    const cw = (ext.x[1] - ext.x[0]) / C, ch = (ext.y[1] - ext.y[0]) / R;
    const px = (c) => ctx.sx(ext.x[0] + c * cw), py = (r) => ctx.sy(ext.y[1] - r * ch);
    const cellPx = Math.min(Math.abs(px(1) - px(0)), Math.abs(py(1) - py(0)));
    const fs = clamp(cellPx * 0.28, 8, 13);
    let out = '';
    m.cells.forEach((row, r) => row.forEach((v, c) => {
      const x = Math.min(px(c), px(c + 1)), y = Math.min(py(r), py(r + 1)), w = Math.abs(px(c + 1) - px(c)), h = Math.abs(py(r + 1) - py(r));
      const name = v === null || v === 0 ? null : (Array.isArray(m.colors) && m.colors[v] !== undefined ? m.colors[v] : (GRID_COLORS[v] || PALETTE[v % PALETTE.length]));
      const col = name ? colorAttr(name, 'fill') : null;
      const label = m.labels ? m.labels[r][c] : null;
      out += `<rect class="st-gcell${col ? ` is-colored ${col.cls}` : ''}"${col && col.style ? ` style="${col.style}"` : ''}${col ? ` fill-opacity="${m.opacity !== undefined ? Number(m.opacity) : v === 1 ? 0.8 : 0.5}"` : ''} x="${n(x)}" y="${n(y)}" width="${n(w)}" height="${n(h)}"` +
        `${tip(`(${r}, ${c})${label !== null && label !== undefined && label !== '' ? `: ${label}` : ''}`)}${hit(m, r * C + c, 'cell')}/>`;
      const a = m.arrows ? m.arrows[r][c] : null;
      const hasLabel = label !== null && label !== undefined && label !== '';
      const cx = x + w / 2, cy = y + h / 2;
      if (a) {
        const [dx, dy] = ARROW[a], len = cellPx * 0.26, ay = hasLabel ? y + h * 0.32 : cy;
        out += `<line class="st-garrow" x1="${n(cx - dx * len)}" y1="${n(ay - dy * len)}" x2="${n(cx + dx * len)}" y2="${n(ay + dy * len)}" marker-end="url(#${ctx.arrowId})"/>`;
      }
      if (hasLabel) out += `<text class="st-label" x="${n(cx)}" y="${n(a ? y + h * 0.72 : cy)}" text-anchor="middle" dy="0.35em" font-size="${n(fs)}">${esc(cut(label, Math.max(2, Math.floor(w / (fs * 0.6)))))}</text>`;
    }));
    (m.highlight || []).forEach((p) => {
      if (!Array.isArray(p) || p[0] < 0 || p[0] >= R || p[1] < 0 || p[1] >= C) return;
      out += `<rect class="st-ghl" x="${n(Math.min(px(p[1]), px(p[1] + 1)))}" y="${n(Math.min(py(p[0]), py(p[0] + 1)))}" width="${n(Math.abs(px(1) - px(0)))}" height="${n(Math.abs(py(1) - py(0)))}"/>`;
    });
    (m.agents || []).forEach((ag, i) => {
      const col = colorAttr(ag.color !== undefined ? ag.color : 'purple', 'fill');
      const drag = m.draggable ? ` data-drag="${esc(m.id || '')}" data-index="${i}"` : '';
      // with a label in the cell, the agent sits in the top-left corner so the label stays readable
      const lab = m.labels && m.labels[ag.r][ag.c] !== null && m.labels[ag.r][ag.c] !== undefined && m.labels[ag.r][ag.c] !== '';
      const x0 = Math.min(px(ag.c), px(ag.c + 1)), y0 = Math.min(py(ag.r), py(ag.r + 1));
      const [ax, ay, ar] = lab ? [x0 + cellPx * 0.22, y0 + cellPx * 0.22, cellPx * 0.16] : [x0 + cellPx / 2, y0 + cellPx / 2, cellPx * 0.3];
      out += `<circle class="st-agent ${attrs(col)} cx="${n(ax)}" cy="${n(ay)}" r="${n(ar)}"${drag}${tip(`agent (${ag.r}, ${ag.c})`)}/>`;
    });
    return out;
  }

  // Row/column of a data-space position on a grid mark (for pos.row / pos.col).
  function cellAt(m, x, y) {
    const R = m.cells.length, C = m.cells[0].length, ext = gridExtent(m);
    const col = Math.floor((x - ext.x[0]) / ((ext.x[1] - ext.x[0]) / C));
    const row = Math.floor((ext.y[1] - y) / ((ext.y[1] - ext.y[0]) / R));
    return { row: clamp(row, 0, R - 1), col: clamp(col, 0, C - 1) };
  }

  // ------------------------------------------------------------ shapes and links
  // Free-form diagram parts: a shape (box / circle / diamond, or a node placed by a point) and a link that joins two
  // shapes by id. Shapes sit at data-unit positions; a node's size is in pixels so a label always fits.
  const SHAPE_SIZE = { circle: [16, 16], box: [32, 16], diamond: [32, 22] };  // default half-width, half-height (px)

  // Where a shape is on screen: centre and half-sizes in px.
  function shapeGeom(m, ctx) {
    if (m.x1 !== undefined) {
      const a = ctx.sx(m.x1), b = ctx.sx(m.x2), c = ctx.sy(m.y1), d = ctx.sy(m.y2);
      return { shape: m.shape, cx: (a + b) / 2, cy: (c + d) / 2, hw: Math.max(3, Math.abs(b - a) / 2), hh: Math.max(3, Math.abs(d - c) / 2) };
    }
    const [dw, dh] = SHAPE_SIZE[m.shape];
    const hw = m.r !== undefined ? m.r : m.w !== undefined ? m.w / 2 : dw;
    const hh = m.r !== undefined ? m.r : m.h !== undefined ? m.h / 2 : dh;
    return { shape: m.shape, cx: ctx.sx(m.x), cy: ctx.sy(m.y), hw, hh };
  }

  // The point where the line from a shape's centre toward (tx, ty) leaves the shape.
  function edgePoint(g, tx, ty) {
    const dx = tx - g.cx, dy = ty - g.cy;
    if (!dx && !dy) return [g.cx, g.cy];
    const t = g.shape === 'box' ? Math.min(dx ? g.hw / Math.abs(dx) : Infinity, dy ? g.hh / Math.abs(dy) : Infinity)
      : g.shape === 'diamond' ? 1 / (Math.abs(dx) / g.hw + Math.abs(dy) / g.hh)
      : 1 / Math.hypot(dx / g.hw, dy / g.hh);
    return [g.cx + dx * t, g.cy + dy * t];
  }

  function shape(m, ctx) {
    const { n, esc, colorAttr, tip, hit } = ctx;
    const g = shapeGeom(m, ctx);
    const plain = m.color === undefined;
    const c = plain ? { cls: '', style: '' } : colorAttr(m.color, 'fill');
    const fop = m.opacity !== undefined ? Number(m.opacity) : plain ? 1 : 0.28;
    const drag = m.draggable ? ` data-drag="${esc(m.id || '')}" data-index="0"` : '';
    const label = m.label === undefined ? '' : String(m.label);
    const extra = `${m.dashed ? ' stroke-dasharray="5 4"' : ''}${m.width !== undefined ? ` stroke-width="${Number(m.width)}"` : ''}`;
    const common = `class="st-node${plain ? ' is-plain' : ''} ${attrs(c)} fill-opacity="${n(fop)}"${extra}${drag}${tip(label || m.id || m.shape)}${hit(m, 0, 'node')}`;
    let out;
    if (g.shape === 'box') out = `<rect ${common} x="${n(g.cx - g.hw)}" y="${n(g.cy - g.hh)}" width="${n(g.hw * 2)}" height="${n(g.hh * 2)}" rx="${n(Math.min(6, g.hh))}"/>`;
    else if (g.shape === 'diamond') out = `<polygon ${common} points="${n(g.cx)},${n(g.cy - g.hh)} ${n(g.cx + g.hw)},${n(g.cy)} ${n(g.cx)},${n(g.cy + g.hh)} ${n(g.cx - g.hw)},${n(g.cy)}"/>`;
    else out = `<ellipse ${common} cx="${n(g.cx)}" cy="${n(g.cy)}" rx="${n(g.hw)}" ry="${n(g.hh)}"/>`;
    if (label) out += `<text class="st-label" x="${n(g.cx)}" y="${n(g.cy)}" text-anchor="middle" dy="0.35em">${esc(cut(label, labelRoom(g)))}</text>`;
    return out;
  }

  // How many label characters fit inside a shape (longer labels are cut with …); a diamond has less room.
  function labelRoom(g) {
    return Math.max(3, Math.floor(g.hw * (g.shape === 'diamond' ? 1.2 : 1.8) / 6.5));
  }

  // A line (arrow by default) from one shape to another, starting and ending on their outlines. curve bends it
  // (-1..1); a link from a shape to itself is a loop above the shape.
  function link(m, geoms, ctx) {
    const lg = linkGeom(m, geoms, ctx.n);
    if (!lg) return '';
    const { n, esc, colorAttr } = ctx;
    const col = colorAttr(m.color === undefined ? 'gray' : m.color, 'stroke');
    const directed = m.directed !== false;
    const { d, lx, ly } = lg;
    const extra = `${m.dashed ? ' stroke-dasharray="5 4"' : ''}${m.opacity !== undefined ? ` stroke-opacity="${Number(m.opacity)}"` : ''}`;
    let out = `<path class="st-link ${attrs(col)} d="${d}" stroke-width="${m.width !== undefined ? Number(m.width) : 1.8}"${extra}${directed ? ` marker-end="url(#${ctx.arrowId})"` : ''}/>`;
    if (m.label !== undefined && m.label !== '') out += `<text class="st-small" x="${n(lx)}" y="${n(ly)}" text-anchor="middle" dy="-0.4em">${esc(String(m.label))}</text>`;
    return out;
  }

  // A link's path and label point (px), or null when an end isn't a known shape. Shared by link() and the checker.
  function linkGeom(m, geoms, n = (v) => v) {
    const a = geoms.get(m.from), b = geoms.get(m.to);
    if (!a || !b) return null;
    let d, lx, ly, path = null;
    if (a === b) {
      const top = a.cy - a.hh * 3;
      const [x1, y1] = edgePoint(a, a.cx - a.hw * 0.7, top), [x2, y2] = edgePoint(a, a.cx + a.hw * 0.7, top);
      const k = a.hh * 2.4;
      d = `M${n(x1)},${n(y1)} C${n(x1 - a.hw * 0.3)},${n(y1 - k)} ${n(x2 + a.hw * 0.3)},${n(y2 - k)} ${n(x2)},${n(y2)}`;
      lx = a.cx; ly = a.cy - a.hh - k * 0.85;
    } else {
      const L = Math.hypot(b.cx - a.cx, b.cy - a.cy) || 1;
      const bend = Number.isFinite(m.curve) ? m.curve : 0;
      const qx = (a.cx + b.cx) / 2 - (b.cy - a.cy) / L * L * bend * 0.5, qy = (a.cy + b.cy) / 2 + (b.cx - a.cx) / L * L * bend * 0.5;
      const [x1, y1] = edgePoint(a, qx, qy), [x2, y2] = edgePoint(b, qx, qy);
      d = `M${n(x1)},${n(y1)} Q${n(qx)},${n(qy)} ${n(x2)},${n(y2)}`;
      lx = 0.25 * x1 + 0.5 * qx + 0.25 * x2; ly = 0.25 * y1 + 0.5 * qy + 0.25 * y2;
      path = { x1, y1, qx, qy, x2, y2 };  // the quadratic curve, so the checker can trace where the link runs
    }
    return { d, lx, ly, self: a === b, path };
  }

  // Where a text mark is drawn: { x, y, anchor } in px (y before the 0.4em lift). A label that would cross the plot's
  // edge is moved the least it takes to sit inside (seen Oct 31: curve names centred near the left or top edge, and
  // labels following a moving point). Width is estimated at ~8 px per character (14px/600); the checker uses the same.
  // Added in Explainer Studio (not in the ai-playground experiment).
  const TEXT_CHAR_W = 8;
  function placeText(m, f) {
    const s = String(m.text == null ? '' : m.text), w = s.length * TEXT_CHAR_W, { plot } = f;
    let x = f.sx(m.x), y = f.sy(m.y);
    let anchor = m.align === 'left' ? 'start' : m.align === 'right' ? 'end' : 'middle';
    if (!Number.isFinite(x) || !Number.isFinite(y)) return { x, y, anchor, w };
    const x0 = anchor === 'start' ? x : anchor === 'end' ? x - w : x - w / 2;
    if (w <= plot.x1 - plot.x0 - 4) {
      if (x0 < plot.x0) { anchor = 'start'; x = plot.x0 + 2; }
      else if (x0 + w > plot.x1) { anchor = 'end'; x = plot.x1 - 2; }
    }
    // drawn raised 0.4em (5.6 px): the glyphs run from about y - 16.6 to y - 1.6
    if (y - 16.6 < plot.y0) y = plot.y0 + 17.6;
    else if (y - 1.6 > plot.y1) y = plot.y1 + 0.6;
    return { x, y, anchor, w };
  }

  // The glyph box of a placed text mark (placeText's x/y/anchor/w), in px.
  function textBox(t) {
    const x0 = t.anchor === 'start' ? t.x : t.anchor === 'end' ? t.x - t.w : t.x - t.w / 2;
    return { x0, x1: x0 + t.w, y0: t.y - 16.6, y1: t.y - 1.6 };
  }

  // Does segment (ax, ay)-(bx, by) cross box b (shrunk by 1 px)? Liang-Barsky clipping.
  function segHitsBox(ax, ay, bx, by, b) {
    const x0 = b.x0 + 1, x1 = b.x1 - 1, y0 = b.y0 + 1, y1 = b.y1 - 1, dx = bx - ax, dy = by - ay;
    let t0 = 0, t1 = 1;
    for (const [p, q] of [[-dx, ax - x0], [dx, x1 - ax], [-dy, ay - y0], [dy, y1 - ay]]) {
      if (p === 0) { if (q < 0) return false; continue; }
      const r = q / p;
      if (p < 0) { if (r > t1) return false; if (r > t0) t0 = r; } else { if (r < t0) return false; if (r < t1) t1 = r; }
    }
    return true;
  }

  // Lines, horizontal rules and arrows a label must not sit on, as px segments.
  function lineSegments(marks, f) {
    const segs = [], { plot } = f;
    for (const m of marks) {
      if ((m.type === 'line' || m.type === 'area') && Array.isArray(m.points)) {
        let prev = null;
        for (const p of m.points) {
          const q = Array.isArray(p) ? [f.sx(p[0]), f.sy(p[1])] : null;
          if (q && Number.isFinite(q[0]) && Number.isFinite(q[1])) { if (prev) segs.push([prev[0], prev[1], q[0], q[1]]); prev = q; } else prev = null;
        }
      } else if (m.type === 'hline' && Number.isFinite(f.sy(m.y))) segs.push([plot.x0, f.sy(m.y), plot.x1, f.sy(m.y)]);
      else if (m.type === 'arrow') {
        const s = [f.sx(m.x1), f.sy(m.y1), f.sx(m.x2), f.sy(m.y2)];
        if (s.every(Number.isFinite)) segs.push(s);
      }
      // vline is left out: it spans the whole height, so no up/down nudge clears it (and it is usually a moving marker)
    }
    return segs;
  }

  const LABEL_NUDGE = [-6, 6, -12, 12, -18, 18, -24, 24, -30, 30, -36, 36, -42, 42, -48, 48];  // px; up first
  const MAX_NUDGED_LABELS = 12;  // more text marks are data labels, not curve names: left where they are
  // Every text mark's place: placeText, then (seen Oct 31: curve names like 'unscaled' or 'mode -2' drawn across a
  // line) nudged up or down the least it takes to clear every line, rule and earlier label, staying inside the plot.
  // Map mark -> placement; `onLine` is set when no nudge worked. Added in Explainer Studio, like placeText.
  function placeLabels(marks, f) {
    const out = new Map(), texts = marks.filter((m) => m.type === 'text' && String(m.text == null ? '' : m.text).trim());
    const segs = texts.length <= MAX_NUDGED_LABELS ? lineSegments(marks, f) : [];
    const taken = [], { plot } = f;
    const overlaps = (a, b) => Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0) > 1 && Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0) > 1;
    const clear = (b) => !segs.some((s) => segHitsBox(s[0], s[1], s[2], s[3], b)) && !taken.some((o) => overlaps(o, b));
    for (const m of texts) {
      let t = placeText(m, f);
      if (segs.length && Number.isFinite(t.x) && Number.isFinite(t.y) && !clear(textBox(t))) {
        const free = LABEL_NUDGE.map((dy) => ({ ...t, y: t.y + dy }))
          .find((c) => c.y - 16.6 >= plot.y0 && c.y - 1.6 <= plot.y1 && clear(textBox(c)));
        t = free || { ...t, onLine: segs.some((s) => segHitsBox(s[0], s[1], s[2], s[3], textBox(t))) };
      }
      taken.push(textBox(t));
      out.set(m, t);
    }
    return out;
  }

  const structures = {
    graph, tree, matrices, sequences, grid, gridExtent, cellAt, shape, shapeGeom, edgePoint, labelRoom, cut, link, linkGeom,
    W, H, MARGIN, STRUCT, AXISLESS, heatExtent, autoDomain, chartFrame, placeText, placeLabels, textBox, TEXT_CHAR_W,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = structures;
  else root.structures = structures;
})(typeof window !== 'undefined' ? window : globalThis);
