// Copied from the ai-playground experiment (Bloging-substack/ECommerce/ai-playground), adapted for Explainer Studio's Playground mode.
// Turns slot snippet strings into functions that can only see `state`, their
// own arguments and the helper library. Real isolation comes from running the
// playground in a sandboxed iframe; this layer gives clear early errors.
(function (root) {
  const helpers = typeof module !== 'undefined' && module.exports ? require('./helpers.cjs') : root.helpers;

  // Arguments each slot receives (after `state`).
  const SLOT_ARGS = {
    init: ['state'],
    onChange: ['state', 'value'],
    onClick: ['state', 'pos', 'id', 'index', 'part'],
    onTick: ['state', 'step'],
    onDrag: ['state', 'pos', 'id', 'index'],
    draw: ['state'],
    value: ['state'],
  };

  const BANNED = ['window', 'document', 'globalThis', 'self', 'top', 'parent', 'frames', 'opener',
    'fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource', 'eval', 'Function', 'import', 'require',
    'process', 'module', 'exports', 'localStorage', 'sessionStorage', 'indexedDB', 'navigator',
    'location', 'postMessage', 'setTimeout', 'setInterval', 'constructor', '__proto__', 'prototype',
    'Worker', 'importScripts', 'alert', 'confirm', 'prompt', 'open', 'Reflect', 'Proxy', 'async', 'await'];
  // Names shadowed as undefined parameters (reserved words can't be parameters).
  const SHADOW = BANNED.filter((n) => !['eval', 'import', 'async', 'await', 'constructor', '__proto__', 'prototype'].includes(n));
  const BANNED_RE = new RegExp('\\b(' + BANNED.join('|') + ')\\b');
  const HELPER_NAMES = Object.keys(helpers);
  const HELPER_VALUES = HELPER_NAMES.map((k) => helpers[k]);

  // Compile a snippet body for a slot. Throws an Error with a readable message.
  function compile(code, slot, where) {
    if (typeof code !== 'string' || !code.trim()) throw new Error(`${where}: ${slot} must be a non-empty string`);
    const bad = code.replace(/(["'`])(?:\\.|(?!\1).)*\1/g, '""').match(BANNED_RE);
    if (bad) throw new Error(`${where}: ${slot} uses forbidden name "${bad[1]}"`);
    if (!/\breturn\b/.test(code)) throw new Error(`${where}: ${slot} must use "return"`);
    let fn;
    try {
      // The block lets a snippet declare `const dist = ...` etc. without
      // clashing with the helper of the same name (helpers are parameters).
      fn = new Function(...HELPER_NAMES, ...SHADOW, ...SLOT_ARGS[slot], '"use strict";\n{\n' + code + '\n}');
    } catch (e) {
      throw new Error(`${where}: ${slot} has a syntax error: ${e.message}`);
    }
    const pad = SHADOW.map(() => undefined);
    return (...args) => fn(...HELPER_VALUES, ...pad, ...args);
  }

  // Read-only view of state that records which keys were read.
  function view(data, seen) {
    return new Proxy(data, {
      get(t, k) { if (typeof k === 'string') seen && seen.add(k); return t[k]; },
      set(t, k) { throw new Error(`state is read-only (tried to set "${String(k)}"); return a patch instead`); },
      deleteProperty(t, k) { throw new Error(`state is read-only (tried to delete "${String(k)}")`); },
    });
  }

  // Freeze values entering state so in-place edits (arr.push) fail loudly.
  function deepFreeze(v) {
    if (v && typeof v === 'object' && !Object.isFrozen(v)) {
      Object.freeze(v);
      for (const k of Object.keys(v)) deepFreeze(v[k]);
    }
    return v;
  }

  // Returns a path to the first non-finite number / unsupported value, or null.
  function findBadValue(v, path = '') {
    if (typeof v === 'number') return Number.isFinite(v) ? null : `${path || 'value'} is ${v}`;
    if (v === null || typeof v === 'string' || typeof v === 'boolean') return null;
    if (v === undefined) return `${path || 'value'} is undefined`;
    if (Array.isArray(v)) {
      for (let i = 0; i < v.length; i++) { const r = findBadValue(v[i], `${path}[${i}]`); if (r) return r; }
      return null;
    }
    if (typeof v === 'object') {
      for (const k of Object.keys(v)) { const r = findBadValue(v[k], path ? `${path}.${k}` : k); if (r) return r; }
      return null;
    }
    return `${path || 'value'} has unsupported type ${typeof v}`;
  }

  // Validate the array returned by a chart's draw(). Returns error string or null.
  const MARK_FIELDS = {
    line: ['points'], area: ['points'], points: ['points'], point: ['x', 'y'], bars: ['items'],
    hline: ['y'], vline: ['x'], rect: ['x1', 'y1', 'x2', 'y2'], arrow: ['x1', 'y1', 'x2', 'y2'], text: ['x', 'y', 'text'],
    heatmap: ['values'], pie: ['values'],
    graph: ['nodes', 'edges'], tree: ['root'], matrix: ['values'], sequence: ['tokens'], grid: ['cells'],
    shape: [], link: ['from', 'to'],
  };
  const isInt = (v, lo, hi) => Number.isInteger(v) && v >= lo && v < hi;
  const short = (v, max) => (typeof v === 'string' || typeof v === 'number') && String(v).length <= max;
  const rect2d = (v) => Array.isArray(v) && v.length > 0 && v.every((r) => Array.isArray(r) && r.length === v[0].length && r.length > 0);

  // Structure checks for graph / tree / matrix / sequence / grid. Returns error or null.
  function checkStructure(m, at) {
    switch (m.type) {
      case 'graph': {
        if (!Array.isArray(m.nodes) || !m.nodes.length || m.nodes.length > 60) return `${at} (graph) needs 1-60 nodes`;
        if (!Array.isArray(m.edges) || m.edges.length > 400) return `${at} (graph) edges must be an array of at most 400 [from, to, weight?]`;
        const N = m.nodes.length;
        for (const nd of m.nodes) {
          if (!nd || typeof nd !== 'object') return `${at} (graph) every node must be an object like {label, layer}`;
          if (nd.label !== undefined && !short(nd.label, 12)) return `${at} (graph) node labels must be at most 12 characters`;
          if (m.layout === 'manual' && !(Number.isFinite(nd.x) && Number.isFinite(nd.y))) return `${at} (graph) layout 'manual' needs x and y on every node`;
        }
        for (const e of m.edges) {
          if (!Array.isArray(e) || !isInt(e[0], 0, N) || !isInt(e[1], 0, N)) return `${at} (graph) edge ${JSON.stringify(e)} must be [fromIndex, toIndex, weight?] with indices 0-${N - 1}`;
        }
        if (m.layout !== undefined && !['layers', 'circle', 'manual'].includes(m.layout)) return `${at} (graph) layout must be 'layers', 'circle' or 'manual'`;
        if (m.draggable && m.layout !== 'manual') return `${at} (graph) only layout 'manual' graphs can be draggable`;
        return null;
      }
      case 'tree': {
        let count = 0, depth = 0;
        const walk = (nd, d) => {
          if (!nd || typeof nd !== 'object') return 'every tree node must be an object like {label, children}';
          if (++count > 127) return 'has too many nodes (max 127)';
          depth = Math.max(depth, d);
          if (d > 8) return 'is too deep (max depth 8)';
          if (nd.label !== undefined && !short(nd.label, 16)) return 'node labels must be at most 16 characters';
          if (nd.children !== undefined && !Array.isArray(nd.children)) return 'children must be an array';
          for (const ch of nd.children || []) { const e = walk(ch, d + 1); if (e) return e; }
          return null;
        };
        const e = walk(m.root, 0);
        return e ? `${at} (tree) ${e}` : null;
      }
      case 'matrix':
        if (!rect2d(m.values)) return `${at} (matrix) values must be a non-empty 2D array with equal-length rows`;
        if (m.values.length > 12 || m.values[0].length > 12) return `${at} (matrix) is too big (max 12x12; use heatmap for larger)`;
        if (!m.values.every((r) => r.every((v) => typeof v === 'number'))) return `${at} (matrix) values must be numbers`;
        return null;
      case 'sequence':
        if (!Array.isArray(m.tokens) || !m.tokens.length || m.tokens.length > 40) return `${at} (sequence) needs 1-40 tokens`;
        if (!m.tokens.every((t) => short(t, 12))) return `${at} (sequence) tokens must be strings/numbers of at most 12 characters`;
        if (m.links !== undefined && !(Array.isArray(m.links) && m.links.every((l) => Array.isArray(l) && isInt(l[0], 0, m.tokens.length) && Number.isInteger(l[1])))) {
          return `${at} (sequence) links must be [[i, j, weight], ...] from token i to token j of the next sequence`;
        }
        return null;
      case 'grid': {
        if (!rect2d(m.cells)) return `${at} (grid) cells must be a non-empty 2D array with equal-length rows`;
        const R = m.cells.length, C = m.cells[0].length;
        if (R > 30 || C > 30) return `${at} (grid) is too big (max 30x30)`;
        if (!m.cells.every((r) => r.every((v) => v === null || Number.isInteger(v)))) return `${at} (grid) cells must be integer categories (0 empty, 1 wall, 2 goal, 3 hazard, ...) or null`;
        for (const k of ['labels', 'arrows']) {
          if (m[k] !== undefined && !(rect2d(m[k]) && m[k].length === R && m[k][0].length === C)) return `${at} (grid) ${k} must be a 2D array the same shape as cells`;
        }
        if (m.arrows && !m.arrows.every((r) => r.every((a) => a === null || ['up', 'down', 'left', 'right'].includes(a)))) return `${at} (grid) arrows must be 'up', 'down', 'left', 'right' or null`;
        if (m.agents !== undefined && !(Array.isArray(m.agents) && m.agents.every((a) => a && isInt(a.r, 0, R) && isInt(a.c, 0, C)))) return `${at} (grid) agents must be [{r, c}] inside the grid`;
        return null;
      }
      case 'shape': {
        const kind = m.shape;
        if (!['circle', 'box', 'diamond'].includes(kind)) return `${at} (shape) must be a circle, box or diamond`;
        const fin = (k) => Number.isFinite(m[k]);
        if (!(fin('x') && fin('y')) && !(fin('x1') && fin('y1') && fin('x2') && fin('y2'))) {
          return `${at} (${kind}) needs a position: node(id, x, y) or ${kind}(x1, y1, x2, y2) with numbers`;
        }
        if (m.id !== undefined && !(typeof m.id === 'string' && /^[\w-]{1,32}$/.test(m.id))) return `${at} (${kind}) id must be a short word like "h1"`;
        if (m.label !== undefined && !short(m.label, 24)) return `${at} (${kind}) label must be at most 24 characters`;
        for (const k of ['r', 'w', 'h']) {
          if (m[k] !== undefined && !(Number.isFinite(m[k]) && m[k] >= 4 && m[k] <= 200)) return `${at} (${kind}) ${k} is a size in pixels between 4 and 200`;
        }
        return null;
      }
      case 'link':
        if (typeof m.from !== 'string' || typeof m.to !== 'string') return `${at} (link) from and to must be the ids of shapes in the same draw`;
        if (m.label !== undefined && !short(m.label, 16)) return `${at} (link) label must be at most 16 characters`;
        if (m.curve !== undefined && !(Number.isFinite(m.curve) && Math.abs(m.curve) <= 1)) return `${at} (link) curve must be a number between -1 and 1`;
        return null;
    }
    return null;
  }
  const isRange = (r) => Array.isArray(r) && r.length === 2 && r.every(Number.isFinite) && r[0] < r[1];
  function checkMarks(marks) {
    if (!Array.isArray(marks)) return 'draw must return an array of marks';
    if (marks.length > 400) return 'draw returned too many marks (max 400)';
    for (let i = 0; i < marks.length; i++) {
      const m = marks[i];
      const at = `mark ${i}`;
      if (!m || !MARK_FIELDS[m.type]) {
        // name the likely fix for common slips (a bare root / node list / grid)
        const hint = !m || typeof m !== 'object' ? ''
          : m.children !== undefined || m.label !== undefined ? '; it looks like a tree node: return tree(root) or treeNodes(fittedTree)'
          : m.nodes !== undefined ? '; it looks like graph data: return graph({ nodes, edges })'
          : Array.isArray(m) ? '; draw must return an array of marks, not nested arrays' : '';
        return `${at} has unknown type "${m && m.type}" (use the mark helpers${hint})`;
      }
      for (const f of MARK_FIELDS[m.type]) if (m[f] === undefined) return `${at} (${m.type}) is missing ${f}`;
      const list = m.points || m.items;
      if (list) {
        if (!Array.isArray(list)) return `${at} (${m.type}) points must be an array of [x, y]`;
        if (list.length > 5000) return `${at} has too many points`;
        for (let j = 0; j < list.length; j++) {
          const p = list[j];
          if (!Array.isArray(p) || p.length < 2) return `${at} (${m.type}) item ${j} must be [x, y]`;
        }
      }
      if (m.type === 'heatmap') {
        const v = m.values;
        if (!Array.isArray(v) || !v.length || !v.every((r) => Array.isArray(r) && r.length === v[0].length && r.length > 0)) {
          return `${at} (heatmap) values must be a non-empty 2D array with equal-length rows`;
        }
        if (v.length * v[0].length > 2500) return `${at} (heatmap) has too many cells (max 2500)`;
        if (!v.every((r) => r.every((x) => typeof x === 'number'))) return `${at} (heatmap) values must be numbers`;
        if ((m.x !== undefined && !isRange(m.x)) || (m.y !== undefined && !isRange(m.y))) return `${at} (heatmap) x/y must be [min, max]`;
      }
      if (m.type === 'pie') {
        if (!Array.isArray(m.values) || !m.values.length || m.values.length > 24) return `${at} (pie) values must be an array of 1-24 numbers`;
        if (!m.values.every((x) => typeof x === 'number' && x >= 0)) return `${at} (pie) values must be numbers >= 0`;
        if (m.donut !== undefined && !(m.donut >= 0 && m.donut < 0.95)) return `${at} (pie) donut must be between 0 and 0.9`;
      }
      const se = checkStructure(m, at);
      if (se) return se;
      if (m.draggable && !['point', 'points', 'graph', 'grid', 'shape'].includes(m.type)) return `${at}: only point, points, shapes (node, box, circle, diamond), graph (manual layout) and grid agents can be draggable`;
      const { text, color, label, labels, xLabels, yLabels, id, from, to, ...nums } = m;
      const bad = findBadValue(nums, m.type);
      if (bad) return `${at}: ${bad}`;
    }
    // links join shapes by id: every id must exist in this draw, once
    const ids = new Set();
    for (let i = 0; i < marks.length; i++) {
      const m = marks[i];
      if (m.type !== 'shape' || m.id === undefined) continue;
      if (ids.has(m.id)) return `mark ${i} (${m.shape}) repeats the id "${m.id}"; shape ids must be unique`;
      ids.add(m.id);
    }
    for (let i = 0; i < marks.length; i++) {
      const m = marks[i];
      if (m.type !== 'link') continue;
      for (const end of [m.from, m.to]) {
        if (!ids.has(end)) return `mark ${i} (link) joins "${end}" but no shape in this draw has that id; give the node/box/circle/diamond that id`;
      }
    }
    return null;
  }

  const sandbox = { SLOT_ARGS, HELPER_NAMES, compile, view, deepFreeze, findBadValue, checkMarks };
  if (typeof module !== 'undefined' && module.exports) module.exports = sandbox;
  else root.sandbox = sandbox;
})(typeof window !== 'undefined' ? window : globalThis);
