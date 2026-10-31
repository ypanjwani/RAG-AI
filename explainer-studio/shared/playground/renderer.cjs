// Copied from the ai-playground experiment (Bloging-substack/ECommerce/ai-playground), adapted for Explainer Studio's Playground mode.
// Spec -> live playground. Compiles slots, creates the shared store, mounts
// components. Components only talk through the store.
(function (root) {
  const { createStore, sandbox, helpers, components } = root;
  const CONTROL_TYPES = ['slider', 'select', 'toggle', 'button', 'play'];
  const SLOT_NAMES = ['onChange', 'onClick', 'onTick', 'onDrag', 'draw', 'value'];
  const INIT_SEED = 42;

  function renderPlayground(container, spec, opts = {}) {
    const onError = opts.onError || ((where, msg) => console.warn(where, msg));
    const clone = (v) => JSON.parse(JSON.stringify(v));

    // Compile every slot up front so syntax errors surface before mounting.
    const slotsById = {};
    for (const c of spec.components) {
      const s = {};
      for (const name of SLOT_NAMES) if (typeof c[name] === 'string') s[name] = sandbox.compile(c[name], name, c.id);
      slotsById[c.id] = s;
    }
    const init = spec.init ? sandbox.compile(spec.init, 'init', 'init') : null;

    function initialState() {
      let st = clone(spec.state || {});
      if (init) {
        helpers.seed(INIT_SEED);
        const patch = init(sandbox.view(sandbox.deepFreeze(clone(st))));
        if (patch && typeof patch === 'object') st = Object.assign(st, clone(patch));
      }
      return sandbox.deepFreeze(st);
    }

    const store = createStore(initialState());
    const players = [];
    let lastErr = '';

    function report(where, err) {
      const msg = err && err.message ? err.message : String(err);
      if (msg !== lastErr) { lastErr = msg; onError(where, msg); }
    }

    const ctx = {
      store,
      runWith(fn, where, st, ...args) {
        try { return fn(st, ...args); } catch (e) { report(where, e); return undefined; }
      },
      run(fn, where, ...args) {
        return ctx.runWith(fn, where, sandbox.view(store.get()), ...args);
      },
      apply(patch, where) {
        if (patch === undefined || patch === null) return;
        if (typeof patch !== 'object' || Array.isArray(patch)) return report(where, new Error('must return an object patch'));
        const bad = sandbox.findBadValue(patch);
        if (bad) return report(where, new Error(bad));
        store.set(sandbox.deepFreeze(clone(patch)));
      },
      registerPlayer(p) { players.push(p); },
      reset() {
        players.forEach((p) => p.reset());
        store.set(initialState());
      },
    };

    // ---- layout
    container.innerHTML = '';
    const page = document.createElement('div');
    page.className = 'pg';
    const title = document.createElement('h2');
    title.className = 'pg-title';
    title.textContent = spec.title || '';
    const body = document.createElement('div');
    body.className = 'pg-body';
    const controls = document.createElement('aside');
    controls.className = 'pg-controls';
    const views = document.createElement('section');
    views.className = 'pg-views';
    const readouts = document.createElement('div');
    readouts.className = 'pg-readouts';
    const charts = document.createElement('div');
    charts.className = 'pg-charts';
    views.append(readouts, charts);
    body.append(controls, views);
    page.append(title, body);
    container.append(page);

    const buttons = document.createElement('div');
    buttons.className = 'pg-buttons';
    for (const c of spec.components) {
      if (c.type === 'button' && c.action === 'reset') continue; // the library adds its own below
      const mount = components[c.type];
      if (!mount) { report(c.id, new Error(`unknown component type "${c.type}"`)); continue; }
      let box;
      if (c.type === 'button' || c.type === 'play') box = buttons;
      else if (CONTROL_TYPES.includes(c.type)) box = controls;
      else if (c.type === 'readout') box = readouts;
      else box = charts;
      mount(box, c, slotsById[c.id], ctx);
    }
    // Every playground gets exactly one Reset, last in the button row.
    components.button(buttons, { id: 'reset', type: 'button', action: 'reset' }, {}, ctx);
    controls.append(buttons);
    if (!controls.children.length) controls.remove();
    if (!readouts.children.length) readouts.remove();
    charts.dataset.count = charts.children.length;

    return { store, destroy() { players.forEach((p) => p.stop()); container.innerHTML = ''; } };
  }

  root.renderPlayground = renderPlayground;
})(window);
