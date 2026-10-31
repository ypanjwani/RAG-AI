// Copied from the ai-playground experiment (Bloging-substack/ECommerce/ai-playground), adapted for Explainer Studio's Playground mode.
// Checks a spec before it is shown: shape, snippets compile, a dry run with no
// errors/NaN, every control changes state, and every change reaches a view.
// Returns { ok, errors: [string], facts? }. Errors are phrased for the model's retry. facts (only when ok) is what
// Explainer Studio's reviewer (Jev) reads: the controls, the labels a reader sees and which views each control changes.
(function (root) {
  const isNode = typeof module !== 'undefined' && module.exports;
  const sandbox = isNode ? require('./sandbox.cjs') : root.sandbox;
  const helpers = isNode ? require('./helpers.cjs') : root.helpers;
  const structures = isNode ? require('./structures.cjs') : root.structures;
  const countTree = (nd) => 1 + (nd.children || []).reduce((t, ch) => t + countTree(ch), 0);

  const TYPES = ['slider', 'select', 'toggle', 'button', 'play', 'chart', 'readout'];
  const SLOTS = ['onChange', 'onClick', 'onTick', 'onDrag', 'draw', 'value'];
  const MAX_SNIPPET = 1500;
  const INIT_SEED = 42;
  const TICKS = 60;

  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const isRange = (r) => Array.isArray(r) && r.length === 2 && isNum(r[0]) && isNum(r[1]) && r[0] < r[1];
  const clone = (v) => JSON.parse(JSON.stringify(v));

  function validate(spec) {
    const errors = [];
    const err = (m) => { if (errors.length < 12 && !errors.includes(m)) errors.push(m); };
    let facts = null;
    let layout = [];  // soft layout problems (layoutProblems): reported for one repair, never a failure
    const done = () => (errors.length === 0 ? { ok: true, errors, facts, layout } : { ok: false, errors });
    const t0 = Date.now();

    // ------------------------------------------------------------ shape
    if (!spec || typeof spec !== 'object' || Array.isArray(spec)) { err('spec must be a JSON object'); return done(); }
    if (typeof spec.title !== 'string' || !spec.title.trim() || spec.title.length > 80) err('title must be a short non-empty string');
    if (!spec.state || typeof spec.state !== 'object' || Array.isArray(spec.state)) { err('state must be an object'); return done(); }
    const badState = sandbox.findBadValue(spec.state);
    if (badState) err(`state: ${badState}`);
    if (Object.keys(spec.state).length > 40) err('state has too many keys (max 40)');
    if (!Array.isArray(spec.components) || spec.components.length < 2 || spec.components.length > 16) {
      err('components must be an array of 2 to 16 items'); return done();
    }
    if (spec.init !== undefined && typeof spec.init !== 'string') err('init must be a snippet string');

    const ids = new Set();
    const fns = {};
    for (const c of spec.components) {
      if (!c || typeof c !== 'object') { err('every component must be an object'); continue; }
      const id = c.id;
      if (typeof id !== 'string' || !/^[\w-]{1,32}$/.test(id)) { err(`component id ${JSON.stringify(id)} must be a short word`); continue; }
      if (ids.has(id)) err(`duplicate component id "${id}"`);
      ids.add(id);
      if (!TYPES.includes(c.type)) { err(`${id}: unknown type "${c.type}" (allowed: ${TYPES.join(', ')})`); continue; }
      fns[id] = {};
      for (const s of SLOTS) {
        if (c[s] === undefined) continue;
        if (typeof c[s] !== 'string') { err(`${id}.${s} must be a string of JS code`); continue; }
        if (c[s].length > MAX_SNIPPET) err(`${id}.${s} is too long (max ${MAX_SNIPPET} chars)`);
        try { fns[id][s] = sandbox.compile(c[s], s, id); } catch (e) { err(e.message); }
      }
    }
    let initFn = null;
    if (typeof spec.init === 'string') {
      if (spec.init.length > MAX_SNIPPET * 2) err('init is too long');
      try { initFn = sandbox.compile(spec.init, 'init', 'init'); } catch (e) { err(e.message); }
    }
    if (errors.length) return done();

    // ------------------------------------------------------- initial state
    let S0 = clone(spec.state);
    if (initFn) {
      try {
        helpers.seed(INIT_SEED);
        const p = initFn(sandbox.view(sandbox.deepFreeze(clone(S0))));
        if (!p || typeof p !== 'object' || Array.isArray(p)) err('init must return an object patch');
        else {
          const bad = sandbox.findBadValue(p);
          if (bad) err(`init: ${bad}`); else S0 = Object.assign(S0, clone(p));
        }
      } catch (e) { err(`init threw: ${e.message}`); }
    }
    if (errors.length) return done();
    sandbox.deepFreeze(S0);
    const KEYS = new Set(Object.keys(S0));

    // ------------------------------------------------ per-type field checks
    const comps = spec.components;
    for (const c of comps) {
      const id = c.id, f = fns[id];
      const needKey = () => { if (!KEYS.has(c.key)) err(`${id}: key "${c.key}" is not in state`); };
      switch (c.type) {
        case 'slider':
          needKey();
          if (!isNum(c.min) || !isNum(c.max) || c.min >= c.max) err(`${id}: slider needs numeric min < max`);
          if (c.step !== undefined && !(isNum(c.step) && c.step > 0)) err(`${id}: step must be a positive number`);
          if (KEYS.has(c.key) && !isNum(S0[c.key])) err(`${id}: state.${c.key} must be a number for a slider`);
          else if (KEYS.has(c.key) && (S0[c.key] < c.min || S0[c.key] > c.max)) err(`${id}: initial state.${c.key} is outside [min, max]`);
          break;
        case 'select': {
          needKey();
          if (!Array.isArray(c.options) || c.options.length < 2 || c.options.length > 12) { err(`${id}: select needs 2-12 options`); break; }
          const vals = c.options.map((o) => (o !== null && typeof o === 'object' ? o.value : o));
          // option values may be arrays/objects: compare by value
          if (KEYS.has(c.key) && !vals.some((v) => JSON.stringify(v) === JSON.stringify(S0[c.key]))) err(`${id}: initial state.${c.key} must equal one of the option values`);
          break;
        }
        case 'toggle':
          needKey();
          if (KEYS.has(c.key) && typeof S0[c.key] !== 'boolean') err(`${id}: state.${c.key} must be a boolean for a toggle`);
          break;
        case 'button':
          if (c.action !== 'reset' && !f.onClick) err(`${id}: button needs onClick (or "action": "reset")`);
          break;
        case 'play':
          if (!f.onTick) err(`${id}: play needs onTick`);
          if (c.interval !== undefined && !(isNum(c.interval) && c.interval >= 30 && c.interval <= 2000)) err(`${id}: interval must be 30-2000 ms`);
          break;
        case 'chart':
          if (!f.draw) err(`${id}: chart needs draw`);
          if (c.x !== undefined && !isRange(c.x)) err(`${id}: x must be [min, max] with min < max`);
          if (c.y !== undefined && !isRange(c.y)) err(`${id}: y must be [min, max] with min < max`);
          break;
        case 'readout':
          if (!f.value) err(`${id}: readout needs value`);
          break;
      }
    }
    const views = comps.filter((c) => c.type === 'chart' || c.type === 'readout');
    const controls = comps.filter((c) => (['slider', 'select', 'toggle', 'play'].includes(c.type)) ||
      (c.type === 'button' && c.action !== 'reset') || (c.type === 'chart' && (fns[c.id].onDrag || fns[c.id].onClick)));
    if (!views.length) err('add at least one chart or readout');
    if (!controls.length) err('add at least one control the user can change (slider, select, toggle, button, play, or a draggable/clickable chart)');
    if (errors.length) return done();

    // ------------------------------------------------------------ dry run
    const viewKeys = new Set();
    const lastMarks = {};
    // Charts that drew something in at least one state of the dry run. A line or area needs 2 points,
    // points/bars 1, text some characters; any other mark counts. A chart may start empty and fill in later.
    const drawn = new Set();
    // For the layout check: each chart's marks on the initial state (what the reader sees first), and the widest x
    // extent its data reaches in any state (a range sized for a slider's maximum is not empty space).
    const firstMarks = {};
    const xReach = {};
    const yReach = {};  // the same for y (Explainer Studio, Oct 31: the empty-y-range check)
    // Each chart's marks in the states the dry run reaches, for the layout check: at most STATE_KEEP, thinned evenly
    // (when full, every other one is dropped and only every 2nd, then 4th... new state is kept). The first stays first.
    const stateMarks = {}, stateSeen = {}, stateStride = {};
    const STATE_KEEP = 48;
    const keepState = (id, r) => {
      const n = stateSeen[id] = (stateSeen[id] || 0) + 1, stride = stateStride[id] || 1;
      if ((n - 1) % stride) return;
      const list = stateMarks[id] || (stateMarks[id] = []);
      list.push(r);
      if (list.length >= STATE_KEEP) { stateMarks[id] = list.filter((_, i) => i % 2 === 0); stateStride[id] = stride * 2; }
    };
    const visible = (m) => (m.type === 'line' || m.type === 'area') ? m.points.length >= 2
      : (m.type === 'points' || m.type === 'bars') ? (m.points || m.items).length > 0
        : m.type === 'text' ? String(m.text).trim() !== '' : true;

    // Run every view on S. Returns map id -> output JSON, or null on error.
    function runViews(S, label) {
      const out = {};
      for (const v of views) {
        const seen = new Set();
        let r;
        try { r = v.type === 'chart' ? fns[v.id].draw(sandbox.view(S, seen)) : fns[v.id].value(sandbox.view(S, seen)); }
        catch (e) { err(`${v.id}.${v.type === 'chart' ? 'draw' : 'value'} threw ${label}: ${e.message}`); return null; }
        if (v.type === 'chart') {
          const bad = sandbox.checkMarks(r);
          if (bad) { err(`${v.id}.draw ${label}: ${bad}`); return null; }
          lastMarks[v.id] = r;
          if (!(v.id in firstMarks)) firstMarks[v.id] = r;
          keepState(v.id, r);
          const ext = dataXExtent(r);
          if (ext) xReach[v.id] = xReach[v.id] ? [Math.min(xReach[v.id][0], ext[0]), Math.max(xReach[v.id][1], ext[1])] : ext;
          const ey = dataYExtent(r);
          if (ey) yReach[v.id] = yReach[v.id] ? [Math.min(yReach[v.id][0], ey[0]), Math.max(yReach[v.id][1], ey[1])] : ey;
          if (r.some(visible)) drawn.add(v.id);
        } else if (!(isNum(r) || typeof r === 'string')) {
          err(`${v.id}.value ${label} must return a finite number or short string, got ${r}`); return null;
        }
        seen.forEach((k) => viewKeys.add(k));
        out[v.id] = JSON.stringify(r);
      }
      return out;
    }

    // Apply a patch to S. Returns { S, changed:Set } or null on error.
    function applyPatch(S, patch, where) {
      if (patch === null || patch === undefined) return { S, changed: new Set() };
      if (typeof patch !== 'object' || Array.isArray(patch)) { err(`${where} must return an object patch like {key: value}`); return null; }
      for (const k of Object.keys(patch)) if (!KEYS.has(k)) { err(`${where} writes "${k}" which is not a state key (keys: ${[...KEYS].join(', ')})`); return null; }
      const bad = sandbox.findBadValue(patch);
      if (bad) { err(`${where} produced ${bad}`); return null; }
      const next = Object.assign({}, S), changed = new Set();
      for (const k of Object.keys(patch)) {
        if (JSON.stringify(S[k]) !== JSON.stringify(patch[k])) changed.add(k);
        next[k] = sandbox.deepFreeze(clone(patch[k]));
      }
      return { S: Object.freeze(next), changed };
    }

    const base = runViews(S0, 'on the initial state');
    if (!base) return done();

    // Charts with fixed ranges must show something at the start, and
    // draggable handles must be reachable (inside the range).
    for (const c of views) {
      // Bars cut off by a fixed y range misrepresent their value.
      if (c.type === 'chart' && c.y) {
        const tol = (c.y[1] - c.y[0]) * 0.02;
        for (const m of lastMarks[c.id] || []) {
          if (m.type !== 'bars') continue;
          const out = m.items.map((p) => p[1]).filter((v) => v > c.y[1] + tol || v < c.y[0] - tol);
          if (out.length) {
            const worst = out.reduce((a, v) => (Math.abs(v) > Math.abs(a) ? v : a));
            err(`${c.id}: a bar has value ${+worst.toPrecision(3)}, outside the chart's y range ${JSON.stringify(c.y)}, so it is cut off; widen y to fit the values (or omit y to auto-scale)`);
            break;
          }
        }
      }
      if (c.type !== 'chart' || !c.x || !c.y) continue;
      const inX = (v) => v >= c.x[0] && v <= c.x[1], inY = (v) => v >= c.y[0] && v <= c.y[1];
      let coords = 0, inside = 0;
      for (const m of lastMarks[c.id] || []) {
        const pts = m.type === 'point' ? [[m.x, m.y]] : (m.type === 'points' || m.type === 'line' || m.type === 'area') ? m.points
          : m.type === 'shape' ? (m.x1 !== undefined ? [[m.x1, m.y1], [m.x2, m.y2]] : [[m.x, m.y]]) : [];
        pts.forEach((p, i) => {
          coords++;
          if (inX(p[0]) && inY(p[1])) inside++;
          else if (m.draggable) err(`${c.id}: draggable mark "${m.id || ''}"${m.type === 'points' ? ` point ${i}` : ''} starts at (${+p[0].toPrecision(3)}, ${+p[1].toPrecision(3)}), outside the chart range x ${JSON.stringify(c.x)} y ${JSON.stringify(c.y)}; move it inside or widen x/y`);
        });
      }
      if (coords && !inside) err(`${c.id}: nothing is visible on the initial state; every point lies outside x ${JSON.stringify(c.x)} y ${JSON.stringify(c.y)}`);
    }
    if (errors.length) return done();

    const stats = {};
    comps.forEach((c) => { stats[c.id] = { reads: new Set(), writes: new Set(), viewChanged: false, views: new Set() }; });
    let anyViewChanged = false;

    function call(c, slot, S, args, where) {
      const seen = new Set();
      try {
        const p = fns[c.id][slot](sandbox.view(S, seen), ...args);
        seen.forEach((k) => stats[c.id].reads.add(k));
        return { patch: p };
      } catch (e) {
        const frozen = /not extensible|read.only|frozen/i.test(e.message);
        err(`${where} threw: ${e.message}${frozen ? ' (state is immutable: copy with [...arr] / {...obj} and return a patch)' : ''}`);
        return null;
      }
    }

    function recordEffect(c, before, after, changed, label) {
      changed.forEach((k) => stats[c.id].writes.add(k));
      if (!changed.size) return true;
      const out = runViews(after, label);
      if (!out) return false;
      for (const id of Object.keys(out)) if (out[id] !== before[id]) stats[c.id].views.add(id);
      if (JSON.stringify(out) !== JSON.stringify(before)) { stats[c.id].viewChanged = true; anyViewChanged = true; }
      return true;
    }

    function domainOf(c, axis) {
      if (c[axis]) return c[axis];
      let lo = Infinity, hi = -Infinity;
      for (const m of lastMarks[c.id] || []) {
        const i = axis === 'x' ? 0 : 1;
        if (m.type === 'grid') { const e = structures.gridExtent(m)[axis]; lo = Math.min(lo, e[0]); hi = Math.max(hi, e[1]); continue; }
        if (m.type === 'graph' && m.layout === 'manual') { m.nodes.forEach((nd) => { lo = Math.min(lo, nd[axis]); hi = Math.max(hi, nd[axis]); }); continue; }
        (m.points || m.items || []).forEach((p) => { if (isNum(p[i])) { lo = Math.min(lo, p[i]); hi = Math.max(hi, p[i]); } });
        const v = m[axis];
        if (isNum(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      }
      return lo < hi ? [lo, hi] : [lo === Infinity ? 0 : lo - 1, lo === Infinity ? 1 : hi + 1];
    }

    // One probe run of control c from state S. Returns final state (for chaining).
    function probe(c, S, label) {
      const viewsBefore = runViews(S, label);
      if (!viewsBefore) return null;
      const valueProbe = (v, where) => {
        let patch = { [c.key]: v };
        if (fns[c.id].onChange) { const r = call(c, 'onChange', S, [v], where); if (!r) return; patch = r.patch; }
        const a = applyPatch(S, patch, where);
        if (a) recordEffect(c, viewsBefore, a.S, a.changed, `after ${where}`);
      };
      switch (c.type) {
        case 'slider': {
          const step = c.step || (c.max - c.min) / 100;
          const snap = (v) => Math.min(c.max, Math.max(c.min, c.min + Math.round((v - c.min) / step) * step));
          [c.min, c.max, snap((c.min + c.max) / 2), snap(c.min + (c.max - c.min) * 0.3)]
            .forEach((v) => valueProbe(v, `${c.id}.onChange(${+v.toPrecision(4)}) ${label}`));
          return S;
        }
        case 'select':
          c.options.map((o) => (o !== null && typeof o === 'object' ? o.value : o))
            .forEach((v) => valueProbe(v, `${c.id}.onChange(${JSON.stringify(v)}) ${label}`));
          return S;
        case 'toggle':
          [true, false].forEach((v) => valueProbe(v, `${c.id}.onChange(${v}) ${label}`));
          return S;
        case 'button': {
          let cur = S;
          for (let i = 0; i < 2; i++) {
            const where = `${c.id}.onClick (click ${i + 1}) ${label}`;
            const r = call(c, 'onClick', cur, [undefined], where);
            if (!r) return null;
            const a = applyPatch(cur, r.patch, where);
            if (!a) return null;
            const vb = i === 0 ? viewsBefore : runViews(cur, label);
            if (!vb || !recordEffect(c, vb, a.S, a.changed, `after ${where}`)) return null;
            cur = a.S;
          }
          return cur;
        }
        case 'play': {
          let cur = S;
          const startViews = viewsBefore;
          const allChanged = new Set();
          for (let step = 0; step < TICKS; step++) {
            const where = `${c.id}.onTick(step ${step}) ${label}`;
            const r = call(c, 'onTick', cur, [step], where);
            if (!r) return null;
            if (r.patch === null || r.patch === undefined) break;
            const a = applyPatch(cur, r.patch, where);
            if (!a) return null;
            a.changed.forEach((k) => allChanged.add(k));
            cur = a.S;
            if (step % 10 === 9 && !runViews(cur, `after ${where}`)) return null;
          }
          if (!recordEffect(c, startViews, cur, allChanged, `after playing ${c.id} ${label}`)) return null;
          return cur;
        }
        case 'chart': {
          const xs = domainOf(c, 'x'), ys = domainOf(c, 'y');
          const marks = lastMarks[c.id] || [];
          const gm = marks.find((m) => m.type === 'grid');
          // positions on a grid chart also carry row/col, as in the browser
          const at = (fx, fy) => {
            const p = { x: xs[0] + (xs[1] - xs[0]) * fx, y: ys[0] + (ys[1] - ys[0]) * fy };
            return gm ? Object.assign(p, structures.cellAt(gm, p.x, p.y)) : p;
          };
          const drags = [];
          for (const m of marks) {
            if (!m.draggable) continue;
            if (m.type === 'point' || m.type === 'shape') drags.push([m.id || '', 0]);
            else if (m.type === 'points') m.points.slice(0, 2).forEach((_, i) => drags.push([m.id || '', i]));
            else if (m.type === 'graph') m.nodes.slice(0, 2).forEach((_, i) => drags.push([m.id || '', i]));
            else if (m.type === 'grid') (m.agents || []).slice(0, 2).forEach((_, i) => drags.push([m.id || '', i]));
          }
          if (fns[c.id].onDrag) {
            if (!drags.length) err(`${c.id}: has onDrag but draw returns no draggable marks (set draggable: true and an id)`);
            for (const [id, index] of drags.slice(0, 3)) {
              for (const pos of [at(0.25, 0.3), at(0.75, 0.7)]) {
                const where = `${c.id}.onDrag(id "${id}", index ${index}, pos {x:${+pos.x.toPrecision(3)}, y:${+pos.y.toPrecision(3)}}) ${label}`;
                const r = call(c, 'onDrag', S, [pos, id, index], where);
                if (!r) continue;
                const a = applyPatch(S, r.patch, where);
                if (a) recordEffect(c, viewsBefore, a.S, a.changed, `after ${where}`);
              }
            }
          } else if (drags.length) {
            err(`${c.id}: draw returns draggable marks but the chart has no onDrag`);
          }
          if (fns[c.id].onClick) {
            // empty-area clicks, then the first and last element of each structural mark
            const probes = [[at(0.3, 0.4)], [at(0.7, 0.6)]];
            for (const m of marks) {
              const parts = m.type === 'graph' ? [['node', m.nodes.length], ['edge', m.edges.length]]
                : m.type === 'tree' ? [['node', countTree(m.root)]]
                : m.type === 'matrix' ? [['cell', m.values.length * m.values[0].length]]
                : m.type === 'sequence' ? [['item', m.tokens.length]]
                : m.type === 'shape' ? [['node', 1]]
                : m.type === 'grid' ? [['cell', m.cells.length * m.cells[0].length]] : [];
              for (const [part, count] of parts) {
                for (const index of [...new Set([0, count - 1])].filter((i) => i >= 0)) {
                  let pos = at(0.5, 0.5);
                  if (m.type === 'grid') { const C = m.cells[0].length; pos = Object.assign(at(0.5, 0.5), { row: Math.floor(index / C), col: index % C }); }
                  probes.push([pos, m.id || '', index, part]);
                }
              }
            }
            for (const args of probes) {
              const [pos, id, index, part] = args;
              const where = `${c.id}.onClick(pos {x:${+pos.x.toPrecision(3)}, y:${+pos.y.toPrecision(3)}}${part ? `, id "${id}", index ${index}, part "${part}"` : ''}) ${label}`;
              const r = call(c, 'onClick', S, args, where);
              if (!r) continue;
              const a = applyPatch(S, r.patch, where);
              if (a) recordEffect(c, viewsBefore, a.S, a.changed, `after ${where}`);
            }
          }
          return S;
        }
      }
      return S;
    }

    // Probe from the initial state, then again from a state reached by
    // playing/clicking, which catches errors that only show up later.
    const later = [];
    for (const c of controls) {
      const end = probe(c, S0, 'from the initial state');
      if (end && end !== S0 && (c.type === 'play' || c.type === 'button')) later.push(end);
      if (Date.now() - t0 > 2000) { err('snippets are too slow (dry run took over 2 s)'); return done(); }
    }
    for (const S of later.slice(0, 2)) {
      for (const c of controls) probe(c, S, 'after playing/clicking');
      if (Date.now() - t0 > 2000) { err('snippets are too slow (dry run took over 2 s)'); return done(); }
    }
    if (errors.length) return done();

    // ----------------------------------------- every chart must draw something
    for (const c of views) {
      if (c.type === 'chart' && !drawn.has(c.id)) {
        err(`${c.id}: the chart stays empty (draw returns no visible marks at the start or after any control), so the reader sees a blank box; make draw return the data, curve or picture it is meant to show`);
      }
    }
    if (errors.length) return done();

    // ----------------------------------------- every control must matter
    const effective = new Set(viewKeys);
    for (let grew = true; grew;) {
      grew = false;
      for (const c of controls) {
        const s = stats[c.id];
        if ([...s.writes].some((k) => effective.has(k))) {
          s.reads.forEach((k) => { if (!effective.has(k)) { effective.add(k); grew = true; } });
          if (c.key && !effective.has(c.key)) { effective.add(c.key); grew = true; }
        }
      }
    }
    for (const c of controls) {
      const s = stats[c.id];
      const kind = c.type === 'chart' ? `${c.id} (drag/click)` : c.id;
      if (!s.writes.size) err(`${kind} never changes state; its slot must return a patch that changes a value`);
      else if (![...s.writes].some((k) => effective.has(k))) {
        err(`${kind} changes [${[...s.writes].join(', ')}] but no chart or readout depends on that (directly or through another control)`);
      }
    }
    if (!anyViewChanged) err('no chart or readout ever changes when the controls are used');
    if (!errors.length) {
      facts = factsOf(comps, S0, lastMarks, stats);
      layout = layoutProblems(views, stateMarks, { x: xReach, y: yReach });
    }
    return done();
  }

  // ------------------------------------------------------------ layout (soft)
  // Problems the reader would see on screen although every snippet works: shapes that overlap or stick out, arrows
  // with no room, cut labels, link labels on shapes, a series drawn mostly outside the y range, a mostly empty x range.
  // Measured with the renderer's own geometry (structures.chartFrame / shapeGeom / linkGeom), so they match the page.
  const SERIES = new Set(['line', 'points', 'area', 'bars']);
  const NO_SPACE_CHECK = new Set(['heatmap', 'graph', 'tree', 'matrix', 'sequence', 'grid', 'pie', 'shape', 'link']);
  const r1 = (v) => Math.round(v * 10) / 10;
  const r2 = (v) => +Number(v).toPrecision(3);

  // Is point (px, py) inside shape geometry g (from structures.shapeGeom), scaled by k?
  function insideShape(g, px, py, k = 1) {
    const dx = Math.abs(px - g.cx) / (g.hw * k), dy = Math.abs(py - g.cy) / (g.hh * k);
    return g.shape === 'box' ? dx < 1 && dy < 1 : g.shape === 'diamond' ? dx + dy < 1 : dx * dx + dy * dy < 1;
  }

  function dataXExtent(marks) {
    let lo = Infinity, hi = -Infinity;
    for (const m of marks || []) {
      if (!SERIES.has(m.type)) continue;
      for (const p of m.points || m.items || []) if (Array.isArray(p) && isNum(p[0])) { lo = Math.min(lo, p[0]); hi = Math.max(hi, p[0]); }
    }
    return lo <= hi ? [lo, hi] : null;
  }

  // The y values a chart draws at: series (bars and areas from 0), points, rules, rectangles and arrows.
  function dataYExtent(marks) {
    let lo = Infinity, hi = -Infinity;
    const at = (v) => { if (isNum(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } };
    for (const m of marks || []) {
      if (SERIES.has(m.type)) {
        for (const p of m.points || m.items || []) if (Array.isArray(p)) at(p[1]);
        if (m.type === 'bars' || m.type === 'area') at(0);
      } else if (m.type === 'point' || m.type === 'hline') at(m.y);
      else if (m.type === 'rect' || m.type === 'arrow') { at(m.y1); at(m.y2); }
    }
    return lo <= hi ? [lo, hi] : null;
  }

  // Added in Explainer Studio (Oct 31): every kept state is measured, not only the first. A label following a moving
  // point, or a line that reaches the top of the range only after a slider moves, showed up in 14 more of 167 saved
  // playgrounds. A problem first seen in a later state is reported once, with LATER appended.
  const LATER = ' (after the controls move, not at the start)';
  // the same problem in another state: equal once numbers outside 'quoted names' are taken out
  const problemKey = (m) => m.split("'").map((p, i) => (i % 2 ? p : p.replace(/-?\d[\d.]*(?:e[-+]?\d+)?/g, '#'))).join("'");

  function layoutProblems(views, stateMarks, reach) {
    const out = [];
    for (const c of views) {
      if (c.type !== 'chart') continue;
      const states = stateMarks[c.id] && stateMarks[c.id].length ? stateMarks[c.id] : [[]];
      const keys = new Set(), mine = [];
      states.forEach((marks, i) => {
        for (const m of chartLayout(c, marks, i === 0 ? { x: reach.x[c.id], y: reach.y[c.id] } : null, i > 0)) {
          const k = problemKey(m);
          if (mine.length >= 4 || (i > 0 && keys.has(k))) continue;  // at most 4 messages per chart
          keys.add(k);
          mine.push(i === 0 ? m : m + LATER);
        }
      });
      for (const m of mine) if (out.length < 10 && !out.includes(m)) out.push(m);
    }
    return out;
  }

  // One chart's layout problems in one state (marks). reached: the data's {x, y} extent over all states, for the
  // empty-range checks (passed once, with the first state). later: not the first state; a line pushed off the range
  // there is a slider at its extreme (a fitted line at the steepest slope), which widening y would only squash.
  function chartLayout(c, marks, reached, later) {
    const out = [];
    const add = (m) => { if (!out.includes(m)) out.push(m); };
    {
      const f = structures.chartFrame(c, marks);
      const { plot } = f;
      const ux = (plot.x1 - plot.x0) / (f.dom.x[1] - f.dom.x[0]), uy = (plot.y1 - plot.y0) / (f.dom.y[1] - f.dom.y[0]);
      const room = () => out.length < 4;  // at most 4 messages per chart

      // ---- diagrams: shapes and links
      const shapes = marks.filter((m) => m.type === 'shape').map((m, i) => ({
        m, g: structures.shapeGeom(m, f), name: m.id !== undefined ? `'${m.id}'` : m.label ? `'${m.label}'` : `shape ${i + 1}`,
      }));
      const geoms = new Map(shapes.filter((s) => s.m.id !== undefined).map((s) => [s.m.id, s.g]));
      for (const { m, g, name } of shapes) {
        if (!room()) break;
        const over = { left: plot.x0 - (g.cx - g.hw), right: g.cx + g.hw - plot.x1, top: plot.y0 - (g.cy - g.hh), bottom: g.cy + g.hh - plot.y1 };
        const [side, px] = Object.entries(over).reduce((a, b) => (b[1] > a[1] ? b : a));
        if (px > 4) {
          const horiz = side === 'left' || side === 'right';
          const shift = r2(px / (horiz ? ux : uy) + 0.03 * (horiz ? f.dom.x[1] - f.dom.x[0] : f.dom.y[1] - f.dom.y[0]));
          const dir = { left: 'right (larger x)', right: 'left (smaller x)', top: 'down (smaller y)', bottom: 'up (larger y)' }[side];
          add(`${c.id}: shape ${name} sticks out ${Math.round(px)} px past the ${side} edge of the chart, so part of it is cut off; move it about ${shift} units ${dir}, or make it smaller (1 ${horiz ? 'x' : 'y'} unit = ${r1(horiz ? ux : uy)} px here)`);
        }
        const label = m.label === undefined ? '' : String(m.label);
        const fit = structures.labelRoom(g);
        if (label.length > fit && room()) {
          const needW = Math.ceil(label.length * 6.5 / (g.shape === 'diamond' ? 1.2 : 1.8)) * 2;
          add(`${c.id}: the label '${label}' shows as '${structures.cut(label, fit)}' because shape ${name} is too narrow; make it wider (${m.x1 !== undefined ? `a box at least ${r2(needW / ux)} x units wide` : `w: ${needW}`}) or shorten the label`);
        }
      }
      // distance from a shape's centre to its outline, toward another shape's centre
      const reach = (a, b) => { const [x, y] = structures.edgePoint(a, b.cx, b.cy); return Math.hypot(x - a.cx, y - a.cy); };
      const overlapping = new Set();
      for (let i = 0; i < shapes.length && room(); i++) {
        for (let j = i + 1; j < shapes.length && room(); j++) {
          const a = shapes[i].g, b = shapes[j].g;
          const dx = b.cx - a.cx, dy = b.cy - a.cy, dist = Math.hypot(dx, dy);
          const need = dist < 1 ? a.hw + b.hw : reach(a, b) + reach(b, a);
          if (need - dist <= 4) continue;
          overlapping.add(`${i}|${j}`);
          const horiz = Math.abs(dx) >= Math.abs(dy);
          const unit = horiz ? ux : uy;
          add(`${c.id}: shapes ${shapes[i].name} and ${shapes[j].name} overlap by ${Math.round(need - dist)} px; on this chart 1 ${horiz ? 'x' : 'y'} unit = ${r1(unit)} px, so put their centres at least ${r2((need + 24) / unit)} ${horiz ? 'x' : 'y'} units apart (now ${r2(dist / unit)}), or make them smaller (w/h/r)`);
        }
      }
      const idx = new Map(shapes.map((s, i) => [s.g, i]));
      for (const m of marks) {
        if (m.type !== 'link' || !room()) continue;
        const a = geoms.get(m.from), b = geoms.get(m.to);
        if (!a || !b) continue;
        if (a !== b) {
          const key = [idx.get(a), idx.get(b)].sort((p, q) => p - q).join('|');
          const gap = Math.hypot(b.cx - a.cx, b.cy - a.cy) - reach(a, b) - reach(b, a);
          if (gap < 14 && !overlapping.has(key)) add(`${c.id}: the link from '${m.from}' to '${m.to}' has only ${Math.max(0, Math.round(gap))} px between the shapes, so its arrow can't be seen; move them further apart`);
        }
        const lg = structures.linkGeom(m, geoms);
        // a link that runs through a third shape (sampled along its curve; shapes taken at 90% size)
        if (lg && lg.path) {
          const { x1, y1, qx, qy, x2, y2 } = lg.path;
          const through = shapes.find(({ g }) => g !== a && g !== b && [0.15, 0.3, 0.5, 0.7, 0.85].some((t) => {
            const u = 1 - t, px = u * u * x1 + 2 * u * t * qx + t * t * x2, py = u * u * y1 + 2 * u * t * qy + t * t * y2;
            return insideShape(g, px, py, 0.9);
          }));
          if (through && room()) add(`${c.id}: the link from '${m.from}' to '${m.to}' runs through shape ${through.name}, so it looks connected to it; bend the link with curve (e.g. 0.4) or move ${through.name} out of its way`);
        }
        const text = m.label === undefined ? '' : String(m.label);
        if (!text || !lg) continue;
        // the label is drawn centred at (lx, ly) and raised 0.4em: about 12 px tall, 6.5 px per character
        const w = text.length * 6.5, box = { x0: lg.lx - w / 2, x1: lg.lx + w / 2, y0: lg.ly - 14, y1: lg.ly - 2 };
        const hit = shapes.find(({ g }) => Math.min(box.x1, g.cx + g.hw) - Math.max(box.x0, g.cx - g.hw) > 2 &&
          Math.min(box.y1, g.cy + g.hh) - Math.max(box.y0, g.cy - g.hh) > 2);
        if (hit) add(`${c.id}: the label '${text}' on the link from '${m.from}' to '${m.to}' sits on shape ${hit.name}, so they cover each other; leave more room between the shapes, bend the link with curve, or shorten the label`);
      }

      // ---- plotted charts
      if (c.y) {
        const tol = (c.y[1] - c.y[0]) * 0.02;
        for (const m of marks) {
          if (!room() || !(m.type === 'line' || m.type === 'points' || m.type === 'area')) continue;
          const ys = (m.points || []).map((p) => p[1]).filter(isNum);
          const outside = ys.filter((v) => v > c.y[1] + tol || v < c.y[0] - tol);
          if (!later && ys.length && outside.length * 2 > ys.length) {
            add(`${c.id}: ${m.label ? `the ${m.type} '${m.label}'` : `a ${m.type} mark`} lies outside the y range ${JSON.stringify(c.y)} for ${outside.length} of ${ys.length} points (values ${r2(Math.min(...ys))} to ${r2(Math.max(...ys))}), so the reader sees it cut off at the edge; widen y to fit it, or give it its own chart`);
            continue;
          }
          // values capped at the top of the range (e.g. Math.min(ymax, v)) draw a flat line along the edge that hides the real values
          const pinned = ys.filter((v) => Math.abs(v - c.y[1]) <= tol / 4);
          if (ys.length >= 3 && pinned.length * 2 > ys.length) {
            add(`${c.id}: ${m.label ? `the ${m.type} '${m.label}'` : `a ${m.type} mark`} sits on the top edge of the chart (y = ${r2(c.y[1])}) for ${pinned.length} of ${ys.length} points, so its values look capped and cut off; don't clamp the values: widen y to fit them, or give it its own chart`);
          }
        }
      }
      // ---- text marks past the plot (drawn on top, unclipped, raised 0.4em). Measured where the renderer draws them:
      // structures.placeLabels moves a label that would cross an edge inside and nudges one off a line, so only a label
      // wider than the plot, or one with no free spot near it, is left.
      const placed = structures.placeLabels(marks, f);
      for (const m of marks) {
        if (m.type !== 'text' || !room()) continue;
        const s = String(m.text || ''), t = placed.get(m) || structures.placeText(m, f), w = t.w, x = t.x, y = t.y - 5.6;
        const x0 = t.anchor === 'start' ? x : t.anchor === 'end' ? x - w : x - w / 2;
        const past = Math.max(plot.x0 - x0, x0 + w - plot.x1, plot.y0 - (y - 11), y + 4 - plot.y1);
        if (s.trim() && past > 4) add(`${c.id}: the text '${clip(s, 40)}' runs ${Math.round(past)} px past the edge of the chart, so it is cut off or covers the axes; move it inside, shorten it, or set align to 'left'/'right'`);
        // first state only: a curve sweeping past a fixed label while a slider moves is brief, and a repair can't help
        else if (t.onLine && !later) add(`${c.id}: the text '${clip(s, 40)}' is drawn across a line, so they cover each other; put it in an empty part of the chart, beside the line it names`);
      }
      // ---- heatmaps bigger than a fixed range (drawn clipped to the plot, so the outer cells are cut)
      for (const m of marks) {
        if (m.type !== 'heatmap' || !room()) continue;
        for (const axis of ['x', 'y']) {
          const fixed = c[axis];
          if (!fixed) continue;
          const [e0, e1] = structures.heatExtent(m, axis), tol = (fixed[1] - fixed[0]) * 0.02;
          if (Math.min(e0, e1) < fixed[0] - tol || Math.max(e0, e1) > fixed[1] + tol) {
            add(`${c.id}: the heatmap covers ${axis} ${JSON.stringify([r2(e0), r2(e1)])} but the chart's ${axis} range is ${JSON.stringify(fixed)}, so its outer cells are cut off; set the chart's ${axis} to ${JSON.stringify([r2(Math.min(e0, e1)), r2(Math.max(e0, e1))])} or give the heatmap ${axis}: [...] inside the range`);
            break;
          }
        }
      }
      // ---- the y-axis name is drawn inside the plot, at its top-left corner: on a heatmap it sits on a cell. (A curve
      // touching it is left to the screenshot check: it is common and minor, and each flag costs a repair call.)
      if (c.yLabel && f.hasAxes && room()) {
        const lb = { x0: f.M.l + 2, x1: f.M.l + 6 + String(c.yLabel).length * 7.5, y0: f.M.t - 2, y1: f.M.t + 14 };
        const onCells = marks.some((m) => {
          if (m.type !== 'heatmap') return false;
          const [hx0, hx1] = structures.heatExtent(m, 'x').map(f.sx), [hy0, hy1] = structures.heatExtent(m, 'y').map(f.sy);
          return Math.min(lb.x1, Math.max(hx0, hx1)) > Math.max(lb.x0, Math.min(hx0, hx1)) && Math.min(lb.y1, Math.max(hy0, hy1)) > Math.max(lb.y0, Math.min(hy0, hy1));
        });
        if (onCells) add(`${c.id}: the y-axis name '${c.yLabel}' is drawn over the heatmap's top-left cells; use yLabels to name the rows and leave yLabel out`);
      }
      if (c.x && reached && reached.x && room() && !marks.some((m) => NO_SPACE_CHECK.has(m.type))) {
        const span = c.x[1] - c.x[0];
        const lo = Math.max(c.x[0], reached.x[0]), hi = Math.min(c.x[1], reached.x[1]);
        const cover = hi > lo ? (hi - lo) / span : 0;
        if (hi > lo && cover < 0.4) {
          add(`${c.id}: the data never fills more than ${Math.round(cover * 100)}% of the x range ${JSON.stringify(c.x)} in any state, so most of the chart stays empty; narrow x to about [${r2(lo - 0.05 * (hi - lo))}, ${r2(hi + 0.05 * (hi - lo))}]`);
        }
      }
      // the same for y (seen Oct 31: sample densities peaking at 0.27 on a fixed [0, 1]); a little more room on top
      if (c.y && reached && reached.y && room() && !marks.some((m) => NO_SPACE_CHECK.has(m.type))) {
        const span = c.y[1] - c.y[0];
        const lo = Math.max(c.y[0], reached.y[0]), hi = Math.min(c.y[1], reached.y[1]);
        const cover = hi > lo ? (hi - lo) / span : 0;
        if (hi > lo && cover < 0.4) {
          add(`${c.id}: the data never fills more than ${Math.round(cover * 100)}% of the y range ${JSON.stringify(c.y)} in any state, so most of the chart stays empty; narrow y to about [${r2(lo - 0.05 * (hi - lo))}, ${r2(hi + 0.08 * (hi - lo))}]`);
        }
      }
    }
    return out;
  }

  // What a reviewer reads about a playground that passed: like Explainer Studio's check-widget.mjs facts.
  const clip = (t, n) => { const x = String(t == null ? '' : t).replace(/\s+/g, ' ').trim(); return x.length > n ? x.slice(0, n - 1) + '…' : x; };
  function nameOf(c) {
    if (c.label) return clip(c.label, 80);
    if (c.title) return clip(c.title, 80);
    return c.type === 'button' && c.action === 'reset' ? 'Reset' : c.id;
  }
  function factsOf(comps, S0, lastMarks, stats) {
    const byId = Object.fromEntries(comps.map((c) => [c.id, c]));
    const isControl = (c) => ['slider', 'select', 'toggle', 'play'].includes(c.type) || (c.type === 'button' && c.action !== 'reset') ||
      (c.type === 'chart' && (c.onDrag || c.onClick));
    const controls = comps.filter(isControl).slice(0, 12).map((c) => {
      const kind = c.type === 'chart' ? (c.onDrag ? 'drag on chart' : 'click on chart') : c.type;
      const f = { id: c.id, kind, label: nameOf(c) };
      if (c.type === 'slider') { f.min = c.min; f.max = c.max; f.value = S0[c.key]; }
      if (c.type === 'select') f.options = c.options.slice(0, 8).map((o) => clip(o !== null && typeof o === 'object' ? (o.label ?? o.value) : o, 40));
      return f;
    });
    const labels = [];
    const add = (t) => { const x = clip(t, 80); if (x && !/^[\s\-+−0-9.,%e×]*$/i.test(x) && !labels.includes(x) && labels.length < 40) labels.push(x); };
    for (const c of comps) {
      if (c.type === 'chart' || c.type === 'readout') add(nameOf(c));
      if (c.type === 'chart') { add(c.xLabel); add(c.yLabel); }
      if (isControl(c) && c.type !== 'chart') add(nameOf(c));
    }
    for (const marks of Object.values(lastMarks)) {
      for (const m of marks || []) {
        if (m.type === 'text') add(m.text);
        if (m.label) add(m.label);
        if (Array.isArray(m.labels)) m.labels.slice(0, 12).forEach(add);
        // Labels inside structural marks, so the reviewer sees a diagram's terms too (before Oct 30 it saw none).
        if (m.type === 'graph' && Array.isArray(m.nodes)) m.nodes.slice(0, 12).forEach((nd) => nd && add(nd.label));
        if (m.type === 'sequence' && Array.isArray(m.tokens)) m.tokens.slice(0, 12).forEach(add);
        if (m.type === 'matrix') [m.rowLabels, m.colLabels].forEach((ls) => Array.isArray(ls) && ls.slice(0, 6).forEach(add));
        if (m.type === 'tree' && m.root) {
          const walk = (nd, depth) => { if (!nd || depth > 3) return; add(nd.label); add(nd.edge); (nd.children || []).slice(0, 4).forEach((ch) => walk(ch, depth + 1)); };
          walk(m.root, 0);
        }
      }
    }
    const changes = comps.filter(isControl).slice(0, 12).map((c) => ({
      control: nameOf(c), changes: [...stats[c.id].views].slice(0, 8).map((id) => nameOf(byId[id])), redrawn: stats[c.id].viewChanged,
    }));
    return { controls, labels, changes };
  }

  if (isNode) module.exports = { validate };
  else root.validateSpec = validate;
})(typeof window !== 'undefined' ? window : globalThis);
