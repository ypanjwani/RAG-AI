// Copied from the ai-playground experiment (Bloging-substack/ECommerce/ai-playground), adapted for Explainer Studio's Playground mode.
// Helper library available to slot snippets (and nothing else).
// Pure functions only: no DOM, no network. Shared by browser and Node validator.
(function (root) {
  // ---- seeded randomness (deterministic so reset/validation are repeatable)
  let s = 1;
  function seed(n) { s = (Math.floor(n) >>> 0) || 1; }
  function rand() { // mulberry32
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }
  function randn(mu = 0, sigma = 1) {
    const u = 1 - rand(), v = rand();
    return mu + sigma * Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  }
  const randInt = (n) => Math.floor(rand() * n);
  function shuffle(a) {
    const b = a.slice();
    for (let i = b.length - 1; i > 0; i--) { const j = randInt(i + 1); [b[i], b[j]] = [b[j], b[i]]; }
    return b;
  }

  // ---- arrays
  const range = (a, b) => { if (b === undefined) { b = a; a = 0; } const r = []; for (let i = a; i < b; i++) r.push(i); return r; };
  const linspace = (a, b, n = 50) => n < 2 ? [a] : range(n).map((i) => a + (b - a) * i / (n - 1));
  const sum = (a) => a.reduce((t, x) => t + x, 0);
  const mean = (a) => a.length ? sum(a) / a.length : 0;
  const variance = (a) => { const m = mean(a); return mean(a.map((x) => (x - m) ** 2)); };
  const std = (a) => Math.sqrt(variance(a));
  const cov = (a, b) => { const ma = mean(a), mb = mean(b); return mean(a.map((x, i) => (x - ma) * (b[i] - mb))); };
  const corr = (a, b) => { const d = std(a) * std(b); return d ? cov(a, b) / d : 0; };
  const minOf = (a) => a.reduce((m, x) => Math.min(m, x), Infinity);
  const maxOf = (a) => a.reduce((m, x) => Math.max(m, x), -Infinity);
  const argmax = (a) => a.reduce((bi, x, i) => x > a[bi] ? i : bi, 0);
  const argmin = (a) => a.reduce((bi, x, i) => x < a[bi] ? i : bi, 0);
  const cumsum = (a) => { let t = 0; return a.map((x) => (t += x)); };
  const zip = (a, b) => a.map((x, i) => [x, b[i]]);
  const counts = (labels) => labels.reduce((c, l) => (c[l] = (c[l] || 0) + 1, c), {});
  const proportions = (labels, k) => { // class proportions for classes 0..k-1
    const n = labels.length || 1;
    if (k === undefined) k = labels.length ? maxOf(labels) + 1 : 0;
    return range(k).map((c) => labels.filter((l) => l === c).length / n);
  };

  // ---- numbers
  const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
  const lerp = (a, b, t) => a + (b - a) * t;
  const round = (x, d = 2) => { const p = 10 ** d; return Math.round(x * p) / p; };

  // ---- vectors / matrices
  const dot = (a, b) => a.reduce((t, x, i) => t + x * b[i], 0);
  const add = (a, b) => a.map((x, i) => x + b[i]);
  const sub = (a, b) => a.map((x, i) => x - b[i]);
  const scale = (a, k) => a.map((x) => x * k);
  const norm = (a) => Math.sqrt(dot(a, a));
  const dist = (a, b) => norm(sub(a, b));
  const transpose = (m) => m[0].map((_, j) => m.map((r) => r[j]));
  const matmul = (A, B) => A.map((r) => transpose(B).map((c) => dot(r, c)));
  const matvec = (A, v) => A.map((r) => dot(r, v));

  // ---- ML
  const sigmoid = (x) => 1 / (1 + Math.exp(-x));
  const relu = (x) => Math.max(0, x);
  const tanh = Math.tanh;
  function softmax(v, temp = 1) {
    const z = v.map((x) => x / temp), m = maxOf(z), e = z.map((x) => Math.exp(x - m)), t = sum(e);
    return e.map((x) => x / t);
  }
  const entropy = (p) => -sum(p.filter((x) => x > 0).map((x) => x * Math.log2(x)));
  const gini = (p) => 1 - sum(p.map((x) => x * x));
  const mse = (a, b) => mean(a.map((x, i) => (x - b[i]) ** 2));
  const gaussian = (x, mu = 0, sigma = 1) => Math.exp(-0.5 * ((x - mu) / sigma) ** 2) / (sigma * Math.sqrt(2 * Math.PI));
  function linreg(xs, ys) {
    const mx = mean(xs), my = mean(ys);
    const v = sum(xs.map((x) => (x - mx) ** 2)) || 1e-12;
    const m = sum(xs.map((x, i) => (x - mx) * (ys[i] - my))) / v;
    return { m, b: my - m * mx };
  }
  // Solve A x = b (Gaussian elimination with partial pivoting).
  function solve(A, b) {
    const d = b.length;
    const M = A.map((r, i) => [...r, b[i]]);
    for (let c = 0; c < d; c++) {
      let p = c;
      for (let r = c + 1; r < d; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
      [M[c], M[p]] = [M[p], M[c]];
      for (let r = 0; r < d; r++) {
        if (r === c || !M[c][c]) continue;
        const f = M[r][c] / M[c][c];
        for (let k = c; k <= d; k++) M[r][k] -= f * M[c][k];
      }
    }
    return range(d).map((i) => M[i][d] / (M[i][i] || 1e-12));
  }
  // Least-squares polynomial fit (ridge = L2 penalty); returns [c0, c1, ...].
  function polyfit(xs, ys, degree, ridge = 1e-9) {
    const d = degree + 1;
    const X = xs.map((x) => range(d).map((j) => x ** j));
    const Xt = transpose(X);
    const A = matmul(Xt, X).map((r, i) => r.map((v, j) => v + (i === j ? ridge : 0)));
    return solve(A, matvec(Xt, ys));
  }
  const polyval = (coefs, x) => coefs.reduce((t, c, j) => t + c * x ** j, 0);

  // ---- tiny MLP (plain JSON so it can live in state)
  const ACT = {
    tanh: [Math.tanh, (y) => 1 - y * y],
    relu: [(x) => Math.max(0, x), (y) => (y > 0 ? 1 : 0)],
    sigmoid: [(x) => 1 / (1 + Math.exp(-x)), (y) => y * (1 - y)],
    linear: [(x) => x, () => 1],
  };
  function mlpInit(sizes, scale = 1) {
    const W = [], b = [];
    for (let l = 0; l < sizes.length - 1; l++) {
      const s = scale * Math.sqrt(2 / (sizes[l] + sizes[l + 1]));
      W.push(range(sizes[l + 1]).map(() => range(sizes[l]).map(() => randn(0, s))));
      b.push(range(sizes[l + 1]).map(() => 0));
    }
    return { sizes: sizes.slice(), W, b };
  }
  // acts[0] is the input, acts[L] the output. Output activation defaults to sigmoid.
  function mlpForward(net, x, act = 'tanh', outAct = 'sigmoid') {
    const acts = [x.slice()];
    let a = x;
    for (let l = 0; l < net.W.length; l++) {
      const f = ACT[l === net.W.length - 1 ? outAct : act][0];
      a = net.W[l].map((row, j) => f(dot(row, a) + net.b[l][j]));
      acts.push(a);
    }
    return { acts, out: a };
  }
  // One full-batch gradient step. Loss is cross-entropy for a sigmoid output, else MSE.
  function mlpStep(net, X, Y, lr = 0.1, act = 'tanh', outAct = 'sigmoid') {
    const L = net.W.length;
    const gW = net.W.map((m) => m.map((r) => r.map(() => 0))), gb = net.b.map((v) => v.map(() => 0));
    let loss = 0;
    X.forEach((x, i) => {
      const { acts } = mlpForward(net, x, act, outAct);
      const y = Array.isArray(Y[i]) ? Y[i] : [Y[i]], out = acts[L];
      let delta;
      if (outAct === 'sigmoid') {
        delta = out.map((o, k) => o - y[k]);
        loss -= sum(out.map((o, k) => y[k] * Math.log(o + 1e-12) + (1 - y[k]) * Math.log(1 - o + 1e-12)));
      } else {
        delta = out.map((o, k) => (o - y[k]) * ACT[outAct][1](o));
        loss += sum(out.map((o, k) => (o - y[k]) ** 2)) / 2;
      }
      for (let l = L - 1; l >= 0; l--) {
        const prev = acts[l];
        delta.forEach((d, j) => { gb[l][j] += d; prev.forEach((a, k) => { gW[l][j][k] += d * a; }); });
        if (l > 0) delta = prev.map((a, k) => sum(delta.map((d, j) => d * net.W[l][j][k])) * ACT[act][1](a));
      }
    });
    const n = X.length || 1;
    return {
      net: {
        sizes: net.sizes,
        W: net.W.map((m, l) => m.map((r, j) => r.map((w, k) => w - lr * gW[l][j][k] / n))),
        b: net.b.map((v, l) => v.map((bb, j) => bb - lr * gb[l][j] / n)),
      },
      loss: loss / n,
    };
  }

  // ---- decision trees (classification: integer labels; criterion 'mse' = regression)
  function fitTree(X, y, o = {}) {
    const { maxDepth = 3, minLeaf = 1, criterion = 'gini' } = o;
    const reg = criterion === 'mse';
    const k = reg ? 0 : (y.length ? maxOf(y) + 1 : 1);
    const imp = (ys) => (reg ? variance(ys) : (criterion === 'entropy' ? entropy : gini)(proportions(ys, k)));
    function build(idx, depth) {
      const ys = idx.map((i) => y[i]);
      const node = { n: idx.length, impurity: round(imp(ys), 4) };
      if (reg) node.value = round(mean(ys), 4);
      else { node.counts = range(k).map((c) => ys.filter((v) => v === c).length); node.value = argmax(node.counts); }
      if (depth >= maxDepth || idx.length < 2 * minLeaf || node.impurity <= 1e-9) return node;
      let best = null;
      for (let f = 0; f < X[idx[0]].length; f++) {
        const vals = [...new Set(idx.map((i) => X[i][f]))].sort((a, b) => a - b);
        for (let t = 1; t < vals.length; t++) {
          const thr = (vals[t - 1] + vals[t]) / 2;
          const Lx = idx.filter((i) => X[i][f] < thr), Rx = idx.filter((i) => X[i][f] >= thr);
          if (Lx.length < minLeaf || Rx.length < minLeaf) continue;
          const score = (Lx.length * imp(Lx.map((i) => y[i])) + Rx.length * imp(Rx.map((i) => y[i]))) / idx.length;
          if (!best || score < best.score - 1e-12) best = { f, thr, Lx, Rx, score };
        }
      }
      if (!best || best.score >= imp(ys) - 1e-12) return node;
      return { ...node, feature: best.f, threshold: round(best.thr, 3), left: build(best.Lx, depth + 1), right: build(best.Rx, depth + 1) };
    }
    return build(range(X.length), 0);
  }
  function predictTree(t, x) {
    while (t.left) t = x[t.feature] < t.threshold ? t.left : t.right;
    return t.value;
  }
  // Fitted tree -> tree mark (like mlpGraph), ready to return from draw.
  // Leaves are colored by class. tree(treeNodes(t)) also works.
  function treeNodes(t, names = [], o = {}) {
    const name = (f) => String(names[f] !== undefined ? names[f] : `x${f}`).slice(0, 6);
    const conv = (nd, edge) => {
      const out = nd.left
        ? { label: `${name(nd.feature)} < ${nd.threshold}`, value: nd.n, children: [conv(nd.left, 'yes'), conv(nd.right, 'no')] }
        : { label: nd.counts ? `class ${nd.value}` : String(nd.value), value: nd.n, children: [] };
      if (!nd.left && nd.counts) out.color = nd.value;
      if (edge) out.edge = edge;
      return out;
    };
    return { type: 'tree', root: conv(t), ...o };
  }
  // Rect marks for the decision regions of a tree on 2 features, box = [x0, y0, x1, y1].
  function treeRegions(t, box, o = {}) {
    const out = [];
    (function go(nd, [x0, y0, x1, y1]) {
      if (!nd.left) {
        out.push({ type: 'rect', x1: x0, y1: y0, x2: x1, y2: y1, opacity: 0.16, ...(nd.counts ? { color: nd.value } : { color: 'gray' }), ...o });
        return;
      }
      const s = nd.threshold;
      if (nd.feature === 0) { go(nd.left, [x0, y0, Math.min(x1, Math.max(x0, s)), y1]); go(nd.right, [Math.max(x0, Math.min(x1, s)), y0, x1, y1]); }
      else { go(nd.left, [x0, y0, x1, Math.min(y1, Math.max(y0, s))]); go(nd.right, [x0, Math.max(y0, Math.min(y1, s)), x1, y1]); }
    })(t, box);
    return out;
  }

  // ---- convolution (valid cross-correlation, as in CNNs)
  function convolve2d(img, kernel, stride = 1) {
    const kh = kernel.length, kw = kernel[0].length;
    const oh = Math.floor((img.length - kh) / stride) + 1, ow = Math.floor((img[0].length - kw) / stride) + 1;
    return range(Math.max(0, oh)).map((r) => range(Math.max(0, ow)).map((c) =>
      sum(kernel.flatMap((row, i) => row.map((k, j) => k * img[r * stride + i][c * stride + j])))));
  }

  // ---- chart marks (what a chart's draw() returns)
  const line = (pts, o = {}) => ({ type: 'line', points: pts, ...o });
  const area = (pts, o = {}) => ({ type: 'area', points: pts, ...o });
  const points = (pts, o = {}) => ({ type: 'points', points: pts, ...o });
  const point = (x, y, o = {}) => ({ type: 'point', x, y, ...o });
  const bars = (items, o = {}) => ({ type: 'bars', items, ...o });
  const hline = (y, o = {}) => ({ type: 'hline', y, ...o });
  const vline = (x, o = {}) => ({ type: 'vline', x, ...o });
  const rect = (x1, y1, x2, y2, o = {}) => ({ type: 'rect', x1, y1, x2, y2, ...o });
  const arrow = (x1, y1, x2, y2, o = {}) => ({ type: 'arrow', x1, y1, x2, y2, ...o });
  const text = (x, y, str, o = {}) => ({ type: 'text', x, y, text: String(str), ...o });
  // values[row][col]; row 0 is drawn at the top (matrix convention).
  const heatmap = (values, o = {}) => ({ type: 'heatmap', values, ...o });
  const pie = (values, o = {}) => ({ type: 'pie', values, ...o });
  // Bars of category counts (seaborn-style countplot).
  function countplot(labels, o = {}) {
    const { order, ...rest } = o;
    const cats = order || [...new Set(labels)].sort((a, b) => (typeof a === 'number' && typeof b === 'number' ? a - b : String(a).localeCompare(String(b))));
    const c = counts(labels);
    return bars(cats.map((k, i) => [i, c[k] || 0]), { labels: cats.map(String), ...(rest.color === undefined && rest.colors === undefined ? { colors: cats.map((_, i) => i) } : {}), ...rest });
  }
  // Structural marks: the renderer does the layout.
  const graph = (data, o = {}) => ({ type: 'graph', nodes: data.nodes, edges: data.edges || [], ...o });
  // Accepts a root node or an existing tree mark (e.g. from treeNodes).
  const tree = (rootNode, o = {}) => (rootNode && rootNode.type === 'tree' ? { ...rootNode, ...o } : { type: 'tree', root: rootNode, ...o });
  const matrix = (values, o = {}) => ({ type: 'matrix', values, ...o });
  const sequence = (tokens, o = {}) => ({ type: 'sequence', tokens, ...o });
  const grid = (cells, o = {}) => ({ type: 'grid', cells, ...o });
  // Free-form diagram parts. box / circle / diamond fit the rectangle (x1, y1)-(x2, y2) in data units; node sits at a
  // point with a size in px (r, or w and h) so its label always fits. link joins two shapes by id (see structures.cjs).
  const box = (x1, y1, x2, y2, o = {}) => ({ type: 'shape', shape: 'box', x1, y1, x2, y2, ...o });
  const circle = (x1, y1, x2, y2, o = {}) => ({ type: 'shape', shape: 'circle', x1, y1, x2, y2, ...o });
  const diamond = (x1, y1, x2, y2, o = {}) => ({ type: 'shape', shape: 'diamond', x1, y1, x2, y2, ...o });
  const node = (id, x, y, o = {}) => ({ type: 'shape', shape: 'circle', id, x, y, ...o });
  const link = (from, to, o = {}) => ({ type: 'link', from, to, ...o });
  // Network -> graph mark: one column per layer, edge weight = W, node value = activation.
  function mlpGraph(net, acts, o = {}) {
    const nodes = [], start = [];
    net.sizes.forEach((s, l) => {
      start.push(nodes.length);
      range(s).forEach((i) => {
        const label = l === 0 ? `x${i + 1}` : l === net.sizes.length - 1 ? (s === 1 ? 'y' : `y${i + 1}`) : '';
        const nd = { label, layer: l };
        if (acts && acts[l]) nd.value = round(acts[l][i], 3);
        nodes.push(nd);
      });
    });
    const edges = [];
    net.W.forEach((m, l) => m.forEach((row, j) => row.forEach((w, k) => edges.push([start[l] + k, start[l + 1] + j, round(w, 3)]))));
    return graph({ nodes, edges }, o);
  }

  // Gaussian kernel density curve; bandwidth defaults to Silverman's rule.
  function kde(samples, o = {}) {
    const { bandwidth, range: r, n = 120, fill, ...rest } = o;
    if (!samples.length) return { type: fill ? 'area' : 'line', points: [], ...rest };
    const bw = bandwidth || (1.06 * std(samples) * samples.length ** -0.2) || 1;
    const [lo, hi] = r || [minOf(samples) - 3 * bw, maxOf(samples) + 3 * bw];
    const pts = linspace(lo, hi, n).map((x) => [x, mean(samples.map((s) => gaussian(x, s, bw)))]);
    return { type: fill ? 'area' : 'line', points: pts, ...rest };
  }

  const helpers = {
    seed, rand, randn, randInt, shuffle,
    range, linspace, sum, mean, variance, std, cov, corr, minOf, maxOf, argmax, argmin, cumsum, zip, counts, proportions,
    clamp, lerp, round,
    dot, add, sub, scale, norm, dist, transpose, matmul, matvec, solve,
    sigmoid, relu, tanh, softmax, entropy, gini, mse, gaussian, linreg, polyfit, polyval,
    mlpInit, mlpForward, mlpStep, mlpGraph, fitTree, predictTree, treeNodes, treeRegions, convolve2d,
    graph, tree, matrix, sequence, grid, box, circle, diamond, node, link,
    line, area, points, point, bars, hline, vline, rect, arrow, text, heatmap, pie, countplot, kde,
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = helpers;
  else root.helpers = helpers;
})(typeof window !== 'undefined' ? window : globalThis);
